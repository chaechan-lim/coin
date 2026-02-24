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

logger = structlog.get_logger(__name__)

_scheduler = None
_engine_instance: TradingEngine | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    global _scheduler, _engine_instance
    config = get_config()

    logger.info("startup_begin", mode=config.trading.mode)

    # ── 1. DB 초기화 ─────────────────────────────────────────
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("db_tables_ready")

    # ── 2. 거래소 어댑터 ──────────────────────────────────────
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

    # ── 3. 핵심 서비스 ───────────────────────────────────────
    # 라이브 모드: 실제 잔고 조회 / 페이퍼 모드: 설정값 사용
    if is_paper:
        initial_krw = config.trading.initial_balance_krw
    else:
        try:
            balances = await exchange.fetch_balance()
            krw_bal = balances.get("KRW", None)
            initial_krw = krw_bal.free if krw_bal else config.trading.initial_balance_krw
            logger.info("live_balance_fetched", krw_balance=initial_krw)
        except Exception as e:
            initial_krw = config.trading.initial_balance_krw
            logger.warning("balance_fetch_failed_using_config", error=str(e), fallback=initial_krw)

    market_data = MarketDataService(exchange)
    notification = NotificationService(config.notification)
    combiner = SignalCombiner(min_confidence=config.trading.min_combined_confidence)
    order_mgr = OrderManager(exchange, is_paper=is_paper)
    portfolio_mgr = PortfolioManager(
        market_data=market_data,
        initial_balance_krw=initial_krw,
        is_paper=is_paper,
    )

    # ── 4. AI 에이전트 ───────────────────────────────────────
    from agents.trade_review import TradeReviewAgent
    market_agent = MarketAnalysisAgent(market_data)
    risk_agent = RiskManagementAgent(config.risk, market_data)
    trade_review_agent = TradeReviewAgent(review_window_hours=24)
    coordinator = AgentCoordinator(market_agent, risk_agent, combiner, trade_review_agent)

    # ── 5. 트레이딩 엔진 ─────────────────────────────────────
    trading_engine = TradingEngine(
        config=config,
        exchange=exchange,
        market_data=market_data,
        order_manager=order_mgr,
        portfolio_manager=portfolio_mgr,
        combiner=combiner,
        agent_coordinator=coordinator,
    )
    coordinator.set_engine(trading_engine)
    await trading_engine.initialize()
    _engine_instance = trading_engine

    # WebSocket 브로드캐스트 연결
    trading_engine.set_broadcast_callback(ws_manager.broadcast)

    # ── 6. API 의존성 주입 ────────────────────────────────────
    set_portfolio_manager(portfolio_mgr)
    set_engine_and_combiner(trading_engine, combiner)
    set_dashboard_deps(trading_engine, coordinator, config)

    # ── 7. 스케줄러 시작 ─────────────────────────────────────
    session_factory = get_session_factory()
    _scheduler = setup_scheduler(
        engine=trading_engine,
        coordinator=coordinator,
        portfolio_manager=portfolio_mgr,
        config=config,
        session_factory=session_factory,
    )
    _scheduler.start()

    # 최초 시장 분석 실행
    asyncio.create_task(_run_initial_analysis(coordinator, portfolio_mgr, config))

    await notification.send_engine_alert(
        f"🚀 트레이딩 봇 시작 ({config.trading.mode.upper()} 모드)\n"
        f"추적 코인: {', '.join(config.trading.tracked_coins)}"
    )
    logger.info("startup_complete")

    yield  # ─── 앱 실행 중 ───

    # ── Shutdown ─────────────────────────────────────────────
    logger.info("shutdown_begin")
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    if _engine_instance:
        await _engine_instance.stop()
    await exchange.close()
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
        description="빗썸 기반 24시간 자동 트레이딩 봇 API",
        version="0.1.0",
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
        }

    return app


app = create_app()
