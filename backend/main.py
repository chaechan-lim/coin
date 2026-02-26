"""
코인 자동 매매 시스템 — FastAPI 진입점
"""
from dotenv import load_dotenv
load_dotenv()

import asyncio
import sys
import structlog
from contextlib import asynccontextmanager

# Windows에서 asyncpg / websockets 호환성을 위해 SelectorEventLoop 사용
# (ProactorEventLoop은 일부 네트워킹 라이브러리와 충돌 가능)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import get_config
from db.session import get_engine, get_session_factory
from core.models import Base

from exchange.bithumb_v2_adapter import BithumbV2Adapter
from exchange.paper_adapter import PaperAdapter
from services.market_data import MarketDataService
from services.notification import NotificationService

from strategies.combiner import SignalCombiner
from engine.order_manager import OrderManager
from engine.portfolio_manager import PortfolioManager
from engine.trading_engine import TradingEngine
from engine.scheduler import setup_scheduler

from agents.market_analysis import MarketAnalysisAgent
from agents.risk_management import RiskManagementAgent
from agents.coordinator import AgentCoordinator

from api.router import create_api_router, get_ws_router
from api.websocket import ws_manager
from api.portfolio import set_portfolio_manager
from api.strategies import set_engine_and_combiner
from api.dashboard import set_dashboard_deps
from api.dependencies import engine_registry
from core.event_bus import set_broadcast as set_event_broadcast, emit_event

logger = structlog.get_logger(__name__)

_scheduler = None
_engine_instance: TradingEngine | None = None
_binance_engine = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    global _scheduler, _engine_instance, _binance_engine
    config = get_config()

    logger.info("startup_begin", mode=config.trading.mode)

    # ── 1. DB 초기화 ─────────────────────────────────────────
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("db_tables_ready")

    # ── 2. 빗썸 거래소 어댑터 ──────────────────────────────────
    bithumb = BithumbV2Adapter(
        api_key=config.exchange.api_key,
        api_secret=config.exchange.api_secret,
        rate_limit=config.exchange.rate_limit_per_sec,
    )
    await bithumb.initialize()

    if config.trading.mode == "paper":
        exchange = PaperAdapter(
            real_adapter=bithumb,
            initial_balance_krw=config.trading.initial_balance_krw,
        )
        await exchange.initialize()
    else:
        exchange = bithumb

    is_paper = config.trading.mode == "paper"
    logger.info("exchange_ready", mode=config.trading.mode, is_paper=is_paper)

    # ── 3. 빗썸 핵심 서비스 ───────────────────────────────────
    # 라이브 모드: 실제 잔고 + DB 포지션 기반 총자산 계산 / 페이퍼 모드: 설정값 사용
    if is_paper:
        initial_krw = config.trading.initial_balance_krw
    else:
        try:
            balances = await exchange.fetch_balance()
            krw_bal = balances.get("KRW", None)
            actual_krw = krw_bal.free if krw_bal else 0

            from sqlalchemy import select, func
            from core.models import Position
            session_factory = get_session_factory()
            async with session_factory() as session:
                result = await session.execute(
                    select(func.coalesce(func.sum(Position.total_invested), 0))
                    .where(Position.quantity > 0, Position.exchange == "bithumb")
                )
                total_invested = float(result.scalar())

            initial_krw = actual_krw + total_invested
            logger.info("live_balance_fetched",
                        krw_balance=actual_krw,
                        existing_invested=round(total_invested, 0),
                        initial_total=round(initial_krw, 0))
        except Exception as e:
            initial_krw = config.trading.initial_balance_krw
            logger.warning("balance_fetch_failed_using_config", error=str(e), fallback=initial_krw)

    market_data = MarketDataService(exchange)
    notification = NotificationService(config.notification)
    combiner = SignalCombiner(min_confidence=config.trading.min_combined_confidence)
    order_mgr = OrderManager(exchange, is_paper=is_paper, exchange_name="bithumb")
    portfolio_mgr = PortfolioManager(
        market_data=market_data,
        initial_balance_krw=initial_krw,
        is_paper=is_paper,
        exchange_name="bithumb",
    )

    # 라이브 모드: 시작 즉시 현금 잔고 보정
    if not is_paper:
        session_factory = get_session_factory()
        async with session_factory() as sess:
            await portfolio_mgr.reconcile_cash_from_db(sess)
            await sess.commit()

    # ── 4. 빗썸 AI 에이전트 ───────────────────────────────────
    from agents.trade_review import TradeReviewAgent
    market_agent = MarketAnalysisAgent(market_data)
    risk_agent = RiskManagementAgent(config.risk, market_data, exchange_name="bithumb")
    trade_review_agent = TradeReviewAgent(review_window_hours=24, exchange_name="bithumb")
    coordinator = AgentCoordinator(
        market_agent, risk_agent, combiner, trade_review_agent,
        exchange_name="bithumb",
    )

    # ── 5. 빗썸 트레이딩 엔진 ─────────────────────────────────
    trading_engine = TradingEngine(
        config=config,
        exchange=exchange,
        market_data=market_data,
        order_manager=order_mgr,
        portfolio_manager=portfolio_mgr,
        combiner=combiner,
        agent_coordinator=coordinator,
        exchange_name="bithumb",
    )
    coordinator.set_engine(trading_engine)
    await trading_engine.initialize()
    _engine_instance = trading_engine

    # WebSocket 브로드캐스트 연결
    trading_engine.set_broadcast_callback(ws_manager.broadcast)
    set_event_broadcast(ws_manager.broadcast)

    # ── 6. API 의존성 주입 (레거시 + 레지스트리) ────────────────
    set_portfolio_manager(portfolio_mgr)
    set_engine_and_combiner(trading_engine, combiner)
    set_dashboard_deps(trading_engine, coordinator, config)

    # EngineRegistry 등록 (빗썸)
    engine_registry.register("bithumb", trading_engine, portfolio_mgr, combiner, coordinator)

    # ── 7. 바이낸스 선물 (조건부) ──────────────────────────────
    binance_exchange = None
    if config.binance.enabled:
        try:
            from exchange.binance_usdm_adapter import BinanceUSDMAdapter
            from engine.futures_engine import BinanceFuturesEngine

            binance_adapter = BinanceUSDMAdapter(
                api_key=config.binance.api_key,
                api_secret=config.binance.api_secret,
                testnet=config.binance.testnet,
            )
            await binance_adapter.initialize()
            binance_exchange = binance_adapter

            binance_market_data = MarketDataService(binance_adapter)
            bt = config.binance_trading
            binance_is_paper = bt.mode == "paper"
            logger.info("binance_mode", mode=bt.mode, is_paper=binance_is_paper)

            # 라이브: 실제 USDT 잔고 + DB 기존 포지션 / 페이퍼: 설정값
            if binance_is_paper:
                initial_usdt = bt.initial_balance_usdt
            else:
                try:
                    balances = await binance_adapter.fetch_balance()
                    usdt_bal = balances.get("USDT", None)
                    actual_usdt = usdt_bal.free if usdt_bal else 0

                    from sqlalchemy import select as sa_select, func as sa_func
                    from core.models import Position as PosModel
                    sf = get_session_factory()
                    async with sf() as sess:
                        res = await sess.execute(
                            sa_select(sa_func.coalesce(sa_func.sum(PosModel.total_invested), 0))
                            .where(PosModel.quantity > 0, PosModel.exchange == "binance_futures")
                        )
                        total_invested_usdt = float(res.scalar())
                    initial_usdt = actual_usdt + total_invested_usdt
                    logger.info("binance_live_balance",
                                usdt_free=round(actual_usdt, 2),
                                invested=round(total_invested_usdt, 2),
                                initial_total=round(initial_usdt, 2))
                except Exception as e:
                    initial_usdt = bt.initial_balance_usdt
                    logger.warning("binance_balance_fetch_failed", error=str(e), fallback=initial_usdt)

            binance_combiner = SignalCombiner(min_confidence=bt.min_combined_confidence)
            binance_order_mgr = OrderManager(
                binance_adapter, is_paper=binance_is_paper,
                exchange_name="binance_futures", fee_currency="USDT",
            )
            binance_portfolio_mgr = PortfolioManager(
                market_data=binance_market_data,
                initial_balance_krw=initial_usdt,  # USDT 기준
                is_paper=binance_is_paper,
                exchange_name="binance_futures",
            )

            binance_market_agent = MarketAnalysisAgent(binance_market_data)
            binance_risk_agent = RiskManagementAgent(config.risk, binance_market_data, exchange_name="binance_futures")
            binance_trade_review = TradeReviewAgent(review_window_hours=24, exchange_name="binance_futures")
            binance_coordinator = AgentCoordinator(
                binance_market_agent, binance_risk_agent, binance_combiner, binance_trade_review,
                exchange_name="binance_futures",
            )

            binance_engine = BinanceFuturesEngine(
                config=config,
                exchange=binance_adapter,
                market_data=binance_market_data,
                order_manager=binance_order_mgr,
                portfolio_manager=binance_portfolio_mgr,
                combiner=binance_combiner,
                agent_coordinator=binance_coordinator,
            )
            binance_coordinator.set_engine(binance_engine)
            await binance_engine.initialize()
            binance_engine.set_broadcast_callback(ws_manager.broadcast)
            _binance_engine = binance_engine

            # 라이브 모드: 시작 즉시 현금 잔고 보정
            if not binance_is_paper:
                sf = get_session_factory()
                async with sf() as sess:
                    await binance_portfolio_mgr.reconcile_cash_from_db(sess)
                    await sess.commit()

            # EngineRegistry 등록 (바이낸스)
            engine_registry.register(
                "binance_futures", binance_engine, binance_portfolio_mgr,
                binance_combiner, binance_coordinator,
            )

            logger.info("binance_futures_engine_ready",
                         mode=bt.mode,
                         is_paper=binance_is_paper,
                         initial_usdt=round(initial_usdt, 2),
                         tracked_coins=config.binance.tracked_coins,
                         leverage=config.binance.default_leverage)
        except Exception as e:
            logger.error("binance_futures_init_failed", error=str(e), exc_info=True)

    # ── 8. 스케줄러 시작 ─────────────────────────────────────
    session_factory = get_session_factory()
    _scheduler = setup_scheduler(
        engine=trading_engine,
        coordinator=coordinator,
        portfolio_manager=portfolio_mgr,
        config=config,
        session_factory=session_factory,
    )

    # 바이낸스 스케줄 잡 추가
    if config.binance.enabled and _binance_engine:
        binance_coord = engine_registry.get_coordinator("binance_futures")
        binance_pm = engine_registry.get_portfolio_manager("binance_futures")
        if binance_coord and binance_pm:
            from engine.scheduler import _wrap
            _scheduler.add_job(
                _wrap(binance_coord.run_market_analysis),
                name="binance_market_analysis",
                seconds=900,
            )

            async def binance_risk_check():
                await binance_coord.run_risk_evaluation(binance_pm.cash_balance)
            _scheduler.add_job(
                _wrap(binance_risk_check),
                name="binance_risk_check",
                seconds=300,
            )
            _scheduler.add_job(
                _wrap(binance_coord.run_trade_review),
                name="binance_trade_review",
                seconds=3600,
            )

    _scheduler.start()

    # 최초 시장 분석 실행
    asyncio.create_task(_run_initial_analysis(coordinator, portfolio_mgr, config))

    await notification.send_engine_alert(
        f"🚀 트레이딩 봇 시작 ({config.trading.mode.upper()} 모드)\n"
        f"추적 코인: {', '.join(config.trading.tracked_coins)}"
        + (f"\n바이낸스 선물: {', '.join(config.binance.tracked_coins)}" if config.binance.enabled else "")
    )
    logger.info("startup_complete")
    await emit_event("info", "system", "서버 시작", detail=f"{config.trading.mode} 모드")

    yield  # ─── 앱 실행 중 ───

    # ── Shutdown ─────────────────────────────────────────────
    await emit_event("info", "system", "서버 종료")
    logger.info("shutdown_begin")
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    if _engine_instance:
        await _engine_instance.stop()
    if _binance_engine:
        await _binance_engine.stop()
    await exchange.close()
    if binance_exchange:
        await binance_exchange.close()
    await notification.send_engine_alert("🛑 트레이딩 봇 종료")
    logger.info("shutdown_complete")


async def _run_initial_analysis(coordinator, portfolio_mgr, config):
    """Run initial market analysis and risk check on startup."""
    await asyncio.sleep(5)  # Let everything settle
    try:
        await coordinator.run_market_analysis()
        await coordinator.run_risk_evaluation(portfolio_mgr.cash_balance)
    except Exception as e:
        logger.warning("initial_analysis_failed", error=str(e))


# ── FastAPI 앱 생성 ───────────────────────────────────────────
def create_app() -> FastAPI:
    config = get_config()

    app = FastAPI(
        title="코인 자동 매매 시스템",
        description="빗썸 + 바이낸스 선물 듀얼 트레이딩 봇 API",
        version="0.2.0",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://frontend:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    app.include_router(create_api_router())
    app.include_router(get_ws_router())

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "mode": config.trading.mode,
            "engine_running": _engine_instance.is_running if _engine_instance else False,
            "binance_enabled": config.binance.enabled,
            "binance_running": _binance_engine.is_running if _binance_engine else False,
            "exchanges": engine_registry.available_exchanges,
        }

    return app


app = create_app()
