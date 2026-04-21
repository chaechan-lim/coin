"""
코인 자동 매매 시스템 — FastAPI 진입점
"""
from dotenv import load_dotenv
load_dotenv()

# numba/llvmlite mock — pandas_ta가 자동 import하지만 우리 함수(sma,rsi,bbands 등)는
# numba 불필요. llvmlite.so 로드를 방지하여 ~50MB RSS 절감.
import types as _types
import sys
_numba_mock = _types.ModuleType('numba')
def _fake_njit(*args, **kwargs):
    if args and callable(args[0]):
        return args[0]
    def decorator(func):
        return func
    return decorator
_numba_mock.njit = _fake_njit
_numba_mock.prange = range
sys.modules['numba'] = _numba_mock
sys.modules['llvmlite'] = _types.ModuleType('llvmlite')
sys.modules['llvmlite.binding'] = _types.ModuleType('llvmlite.binding')
del _types, _numba_mock, _fake_njit

import asyncio
import gc
from datetime import datetime, timezone
import structlog
from contextlib import asynccontextmanager

# Windows에서 asyncpg / websockets 호환성을 위해 SelectorEventLoop 사용
# (ProactorEventLoop은 일부 네트워킹 라이브러리와 충돌 가능)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sqlalchemy import select, func

from config import get_config
from db.session import get_engine, get_session_factory
from core.models import Base

from exchange.bithumb_v2_adapter import BithumbV2Adapter
from exchange.paper_adapter import PaperAdapter
from services.market_data import MarketDataService
from services.notification import NotificationDispatcher

from strategies.combiner import SignalCombiner
from engine.order_manager import OrderManager
from engine.portfolio_manager import PortfolioManager
from engine.trading_engine import TradingEngine
from engine.scheduler import setup_scheduler

from agents.market_analysis import MarketAnalysisAgent
from agents.risk_management import RiskManagementAgent
from agents.coordinator import AgentCoordinator
from agents.trade_review import TradeReviewAgent
from agents.performance_analytics import PerformanceAnalyticsAgent
from agents.strategy_advisor import StrategyAdvisorAgent
from engine.recovery import RecoveryManager
from engine.health_monitor import HealthMonitor
from agents.diagnostic_agent import DiagnosticAgent

from api.router import create_api_router, get_ws_router
from api.websocket import ws_manager
from api.dependencies import engine_registry
from core.event_bus import set_broadcast as set_event_broadcast, set_notification as set_event_notification, emit_event
from services.notification.discord import DiscordAdapter
from services.notification.telegram import TelegramAdapter
from services.notification.daily_summary import send_daily_summary

logger = structlog.get_logger(__name__)

_scheduler = None
_engine_instance: TradingEngine | None = None
_binance_engine = None
_binance_spot_engine = None
_surge_engine = None
_donchian_engine = None  # Donchian Daily Ensemble (R&D 라이브)
_donchian_futures_engine = None  # Donchian Futures Bi-Directional (R&D 라이브)
_pairs_live_engine = None  # Pairs Trading (R&D 라이브)
_futures_rnd_coordinator = None
_research_auto_review_service = None
_research_stage_gate_service = None
_notification_dispatcher: NotificationDispatcher | None = None
_discord_bot_task: asyncio.Task | None = None
_discord_bot_instance = None


def _create_agent_stack(
    market_data: MarketDataService,
    config,
    exchange_name: str,
    market_symbol: str = "BTC/KRW",
) -> AgentCoordinator:
    """AI 에이전트 5종 + Coordinator 생성."""
    # R&D 엔진 exchange 목록 (에이전트가 R&D 주문만 분석하도록)
    # 메인 엔진 비활성 시 메인 exchange를 제외하여 잔재 데이터 오염 방지
    rnd_extras: list[str] = []
    if exchange_name == "binance_futures":
        rnd_only = [
            "binance_donchian_futures", "binance_pairs", "binance_momentum", "binance_hmm",
            "binance_breakout_pb", "binance_vol_mom", "binance_btc_neutral", "binance_surge",
        ]
        # 메인 선물 엔진이 비활성이면 R&D만 분석 (메인 잔재 제외)
        if not config.binance_trading.enabled and not config.futures_v2.enabled:
            rnd_extras = rnd_only  # exchange_name은 여전히 binance_futures (DB 기록용)
        else:
            rnd_extras = rnd_only
    elif exchange_name == "binance_spot":
        rnd_extras = ["binance_donchian", "binance_fgdca"]

    market_agent = MarketAnalysisAgent(market_data, market_symbol=market_symbol, exchange_name=exchange_name)
    risk_agent = RiskManagementAgent(config.risk, market_data, exchange_name=exchange_name)
    trade_review = TradeReviewAgent(review_window_hours=24, exchange_name=exchange_name,
                                    extra_exchanges=rnd_extras)
    perf_agent = PerformanceAnalyticsAgent(exchange_name=exchange_name, extra_exchanges=rnd_extras)
    strategy_advisor = StrategyAdvisorAgent(exchange_name=exchange_name, extra_exchanges=rnd_extras)
    min_sell_wt = 0.0
    if exchange_name == "binance_futures":
        min_sell_wt = config.binance_trading.min_sell_active_weight
    combiner = SignalCombiner(
        min_confidence=_get_min_confidence(config, exchange_name),
        exchange_name=exchange_name,
        min_sell_active_weight=min_sell_wt,
    )
    coordinator = AgentCoordinator(
        market_agent, risk_agent, combiner, trade_review,
        exchange_name=exchange_name,
        performance_agent=perf_agent,
        strategy_advisor=strategy_advisor,
    )
    return combiner, coordinator


def _get_min_confidence(config, exchange_name: str) -> float:
    if exchange_name == "bithumb":
        return config.trading.min_combined_confidence
    elif exchange_name == "binance_futures":
        return config.binance_trading.min_combined_confidence
    else:
        return config.binance_spot_trading.min_combined_confidence


async def _sync_live_state(
    portfolio_mgr: PortfolioManager,
    adapter,
    tracked_coins: list[str],
    exchange_name: str,
    currency: str,
    initial_balance: float,
    *,
    is_futures: bool = False,
) -> None:
    """라이브 모드 초기화: 포지션 동기화 + DB 복원 + 시드 입금."""
    from core.models import CapitalTransaction, Position as PositionModel

    sf = get_session_factory()
    async with sf() as sess:
        await portfolio_mgr.sync_exchange_positions(sess, adapter, tracked_coins)

        if is_futures:
            await portfolio_mgr.initialize_cash_from_exchange(adapter)

        await portfolio_mgr.restore_state_from_db(sess)
        spike_fixed = await PortfolioManager.cleanup_spike_snapshots(sess, exchange_name)
        if spike_fixed:
            logger.info("spike_cleanup", exchange=exchange_name, fixed=spike_fixed)

        # 시드 입금 자동 생성 (CapitalTransaction 0건이면)
        cnt_result = await sess.execute(
            select(func.count()).select_from(CapitalTransaction)
            .where(CapitalTransaction.exchange == exchange_name)
        )
        if cnt_result.scalar() == 0:
            pos_result = await sess.execute(
                select(func.coalesce(func.sum(PositionModel.total_invested), 0))
                .where(PositionModel.exchange == exchange_name, PositionModel.quantity > 0)
            )
            actual_invested = float(pos_result.scalar())
            actual_total = portfolio_mgr.cash_balance + actual_invested
            seed_amount = actual_total if actual_total > 0 else initial_balance
            rounding = 0 if currency == "KRW" else 2

            seed = CapitalTransaction(
                exchange=exchange_name,
                tx_type="deposit",
                amount=round(seed_amount, rounding),
                currency=currency,
                note="초기 원금 (자동 생성, 실제 자산 기준)",
                source="seed",
                confirmed=True,
            )
            sess.add(seed)
            await sess.flush()
            logger.info("seed_deposit_created",
                        exchange=exchange_name,
                        amount=round(seed_amount, rounding),
                        cash=round(portfolio_mgr.cash_balance, rounding),
                        invested=round(actual_invested, rounding))

        await portfolio_mgr.load_initial_balance_from_db(sess)
        await sess.commit()


def _create_self_healing(
    engine,
    portfolio_mgr: PortfolioManager,
    adapter,
    market_data: MarketDataService,
    exchange_name: str,
    tracked_coins: list[str],
) -> HealthMonitor:
    """RecoveryManager + DiagnosticAgent + HealthMonitor 생성 및 연결."""
    recovery = RecoveryManager(
        engine=engine,
        portfolio_manager=portfolio_mgr,
        exchange_adapter=adapter,
        exchange_name=exchange_name,
        tracked_coins=tracked_coins,
    )
    diagnostic = DiagnosticAgent(
        engine=engine,
        portfolio_manager=portfolio_mgr,
        exchange_adapter=adapter,
        exchange_name=exchange_name,
        tracked_coins=tracked_coins,
    )
    recovery.set_diagnostic_agent(diagnostic)
    engine.set_recovery_manager(recovery)

    return HealthMonitor(
        engine=engine,
        portfolio_manager=portfolio_mgr,
        exchange_adapter=adapter,
        market_data=market_data,
        exchange_name=exchange_name,
        tracked_coins=tracked_coins,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    global _scheduler, _engine_instance, _binance_engine, _binance_spot_engine, _surge_engine, _donchian_engine, _donchian_futures_engine, _pairs_live_engine, _futures_rnd_coordinator, _research_auto_review_service, _research_stage_gate_service, _notification_dispatcher
    config = get_config()

    logger.info("startup_begin", mode=config.trading.mode)

    # ── 1. DB 초기화 ─────────────────────────────────────────
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 신규 컬럼 마이그레이션 (이미 존재하면 무시)
        from sqlalchemy import text, inspect as sa_inspect
        def _add_columns(sync_conn):
            insp = sa_inspect(sync_conn)
            position_cols = {c["name"] for c in insp.get_columns("positions")}
            if "strategy_name" not in position_cols:
                sync_conn.execute(text("ALTER TABLE positions ADD COLUMN strategy_name VARCHAR(50)"))
            order_cols = {c["name"] for c in insp.get_columns("orders")}
            if "trade_group_id" not in order_cols:
                sync_conn.execute(text("ALTER TABLE orders ADD COLUMN trade_group_id VARCHAR(50)"))
            if "trade_group_type" not in order_cols:
                sync_conn.execute(text("ALTER TABLE orders ADD COLUMN trade_group_type VARCHAR(30)"))
            order_indexes = {idx["name"] for idx in insp.get_indexes("orders")}
            if "ix_orders_trade_group" not in order_indexes:
                sync_conn.execute(
                    text("CREATE INDEX IF NOT EXISTS ix_orders_trade_group ON orders (exchange, trade_group_id, created_at)")
                )
        await conn.run_sync(_add_columns)
    logger.info("db_tables_ready")

    from research.stage_gate import ResearchStageGateService

    _research_stage_gate_service = ResearchStageGateService(get_session_factory(), engine_registry=engine_registry)
    engine_registry.set_shared("research_stage_gate_service", _research_stage_gate_service)

    # ── 알림 어댑터 등록 (엔진 독립) ─────────────────────────
    notification = NotificationDispatcher()
    _notification_dispatcher = notification
    if config.notification.enabled:
        discord_webhook = config.notification.discord_webhook_url
        if discord_webhook:
            notification.add_adapter(DiscordAdapter(discord_webhook))
        tg_token = config.notification.telegram_bot_token
        tg_chat = config.notification.telegram_chat_id
        if tg_token and tg_chat:
            notification.add_adapter(TelegramAdapter(tg_token, tg_chat))
    if notification.adapters:
        set_event_notification(notification.handle_event)
        logger.info("notification_dispatchers_registered",
                     adapters=[type(a).__name__ for a in notification.adapters])

    # WebSocket 브로드캐스트 연결 (엔진 독립)
    set_event_broadcast(ws_manager.broadcast)

    # ── 2. 빗썸 거래소 (조건부) ──────────────────────────────
    exchange = None  # for shutdown
    bithumb_health = None

    if config.exchange.enabled:
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

        # ── 3. 빗썸 핵심 서비스 ───────────────────────────────
        initial_krw = config.trading.initial_balance_krw
        market_data = MarketDataService(exchange)
        combiner, coordinator = _create_agent_stack(market_data, config, "bithumb")
        order_mgr = OrderManager(exchange, is_paper=is_paper, exchange_name="bithumb")
        portfolio_mgr = PortfolioManager(
            market_data=market_data,
            initial_balance_krw=initial_krw,
            is_paper=is_paper,
            exchange_name="bithumb",
        )

        if not is_paper:
            await _sync_live_state(
                portfolio_mgr, exchange, config.trading.tracked_coins,
                "bithumb", "KRW", initial_krw,
            )

        # ── 4. 빗썸 트레이딩 엔진 ─────────────────────────────
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
        trading_engine.set_broadcast_callback(ws_manager.broadcast)

        bithumb_health = _create_self_healing(
            trading_engine, portfolio_mgr, exchange, market_data,
            "bithumb", config.trading.tracked_coins,
        )

        engine_registry.register("bithumb", trading_engine, portfolio_mgr, combiner, coordinator)
    else:
        logger.info("bithumb_disabled")

    # ── 7. 바이낸스 선물 (조건부) ──────────────────────────────
    futures_health = None
    spot_health = None
    binance_exchange = None
    if config.binance.enabled:
        try:
            from exchange.binance_usdm_adapter import BinanceUSDMAdapter

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
            initial_usdt = bt.initial_balance_usdt
            logger.info("binance_mode", mode=bt.mode, is_paper=binance_is_paper)

            binance_order_mgr = OrderManager(
                binance_adapter, is_paper=binance_is_paper,
                exchange_name="binance_futures", fee_currency="USDT",
            )
            binance_portfolio_mgr = PortfolioManager(
                market_data=binance_market_data,
                initial_balance_krw=initial_usdt,
                is_paper=binance_is_paper,
                exchange_name="binance_futures",
            )

            if not config.binance_trading.enabled:
                logger.info("binance_futures_main_engine_disabled")
            elif config.futures_v2.enabled:
                # ── v2 레짐 적응형 엔진 ──
                from engine.futures_engine_v2 import FuturesEngineV2
                from services.derivatives_data import DerivativesDataService

                v2_mode = config.futures_v2.mode
                logger.info("futures_v2_mode", mode=v2_mode)

                binance_derivatives = DerivativesDataService()

                binance_engine = FuturesEngineV2(
                    config=config,
                    exchange=binance_adapter,
                    market_data=binance_market_data,
                    order_manager=binance_order_mgr,
                    portfolio_manager=binance_portfolio_mgr,
                    derivatives_data=binance_derivatives,
                )
                await binance_engine.initialize()
                binance_engine.set_broadcast_callback(ws_manager.broadcast)
                _binance_engine = binance_engine

                if not (v2_mode == "paper"):
                    await _sync_live_state(
                        binance_portfolio_mgr, binance_adapter, list(config.futures_v2.tier1_coins),
                        "binance_futures", "USDT", initial_usdt, is_futures=True,
                    )

                # V2 에이전트 스택: 회고/성과분석/전략어드바이저 (COIN-38)
                binance_combiner, binance_coordinator = _create_agent_stack(
                    binance_market_data, config, "binance_futures", market_symbol="BTC/USDT",
                )
                binance_coordinator.set_engine(binance_engine)
                binance_engine.set_agent_coordinator(binance_coordinator)

                engine_registry.register(
                    "binance_futures", binance_engine, binance_portfolio_mgr,
                    binance_combiner, binance_coordinator,
                )

                futures_health = _create_self_healing(
                    binance_engine, binance_portfolio_mgr, binance_adapter,
                    binance_market_data, "binance_futures", list(config.futures_v2.tier1_coins),
                )

                logger.info("binance_futures_v2_ready",
                             mode=v2_mode,
                             initial_usdt=round(initial_usdt, 2),
                             tier1_coins=list(config.futures_v2.tier1_coins),
                             leverage=config.futures_v2.leverage,
                             tier2_enabled=config.futures_v2.tier2_enabled)
            elif config.binance_trading.enabled:
                # ── v1 기존 7전략+ML 엔진 ──
                from engine.futures_engine import BinanceFuturesEngine

                binance_combiner, binance_coordinator = _create_agent_stack(
                    binance_market_data, config, "binance_futures", market_symbol="BTC/USDT",
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

                if not binance_is_paper:
                    await _sync_live_state(
                        binance_portfolio_mgr, binance_adapter, config.binance.tracked_coins,
                        "binance_futures", "USDT", initial_usdt, is_futures=True,
                    )

                engine_registry.register(
                    "binance_futures", binance_engine, binance_portfolio_mgr,
                    binance_combiner, binance_coordinator,
                )

                futures_health = _create_self_healing(
                    binance_engine, binance_portfolio_mgr, binance_adapter,
                    binance_market_data, "binance_futures", config.binance.tracked_coins,
                )

                logger.info("binance_futures_v1_ready",
                             mode=bt.mode,
                             is_paper=binance_is_paper,
                             initial_usdt=round(initial_usdt, 2),
                             tracked_coins=config.binance.tracked_coins,
                             leverage=config.binance.default_leverage)
        except Exception as e:
            logger.error("binance_futures_init_failed", error=str(e), exc_info=True)

    # 메인 선물 엔진 없이도 R&D 에이전트용 coordinator 보장
    if config.binance.enabled and not _binance_engine:
        existing_coord = engine_registry.get_coordinator("binance_futures")
        if not existing_coord and binance_market_data:
            _rnd_combiner, _rnd_coordinator = _create_agent_stack(
                binance_market_data, config, "binance_futures", market_symbol="BTC/USDT",
            )
            engine_registry.register("binance_futures", None, None, _rnd_combiner, _rnd_coordinator)
            logger.info("rnd_agent_coordinator_created", exchange="binance_futures")

    # ── 7b. 바이낸스 현물 (조건부) ─────────────────────────────
    binance_spot_exchange = None
    spot_adapter = None  # 7b-2 (Donchian) 등 외부에서 사용
    spot_market_data = None
    if config.binance.spot_enabled:
        try:
            from exchange.binance_spot_adapter import BinanceSpotAdapter

            spot_adapter = BinanceSpotAdapter(
                api_key=config.binance.api_key,
                api_secret=config.binance.api_secret,
                testnet=config.binance.testnet,
            )
            await spot_adapter.initialize()
            binance_spot_exchange = spot_adapter

            bst = config.binance_spot_trading
            spot_is_paper = bst.mode == "paper"
            logger.info("binance_spot_mode", mode=bst.mode, is_paper=spot_is_paper)

            initial_spot_usdt = bst.initial_balance_usdt

            if spot_is_paper:
                spot_exchange = PaperAdapter(
                    real_adapter=spot_adapter,
                    initial_balance_krw=initial_spot_usdt,
                    taker_fee_pct=0.001,
                    base_currency="USDT",
                )
                await spot_exchange.initialize()
            else:
                spot_exchange = spot_adapter

            spot_market_data = MarketDataService(spot_exchange)
            spot_combiner, spot_coordinator = _create_agent_stack(
                spot_market_data, config, "binance_spot", market_symbol="BTC/USDT",
            )
            spot_order_mgr = OrderManager(
                spot_exchange, is_paper=spot_is_paper,
                exchange_name="binance_spot", fee_currency="USDT",
            )
            spot_portfolio_mgr = PortfolioManager(
                market_data=spot_market_data,
                initial_balance_krw=initial_spot_usdt,
                is_paper=spot_is_paper,
                exchange_name="binance_spot",
            )

            spot_engine = TradingEngine(
                config=config,
                exchange=spot_exchange,
                market_data=spot_market_data,
                order_manager=spot_order_mgr,
                portfolio_manager=spot_portfolio_mgr,
                combiner=spot_combiner,
                agent_coordinator=spot_coordinator,
                exchange_name="binance_spot",
                tracked_coins=config.binance.tracked_coins,
                evaluation_interval_sec=bst.evaluation_interval_sec,
            )
            spot_coordinator.set_engine(spot_engine)
            await spot_engine.initialize()
            spot_engine.set_broadcast_callback(ws_manager.broadcast)
            _binance_spot_engine = spot_engine

            if not spot_is_paper:
                await _sync_live_state(
                    spot_portfolio_mgr, spot_adapter, config.binance.tracked_coins,
                    "binance_spot", "USDT", initial_spot_usdt,
                )

            engine_registry.register(
                "binance_spot", spot_engine, spot_portfolio_mgr,
                spot_combiner, spot_coordinator,
            )

            spot_health = _create_self_healing(
                spot_engine, spot_portfolio_mgr, spot_exchange,
                spot_market_data, "binance_spot", config.binance.tracked_coins,
            )

            logger.info("binance_spot_engine_ready",
                         mode=bst.mode,
                         is_paper=spot_is_paper,
                         initial_usdt=round(initial_spot_usdt, 2),
                         tracked_coins=config.binance.tracked_coins)
        except Exception as e:
            logger.error("binance_spot_init_failed", error=str(e), exc_info=True)

    # ── 7b-2. Donchian Daily Ensemble (R&D 라이브) ───────────
    # 학술 검증 (SSRN 2025): 약세장 방어 alpha +44.71% / 180d, MDD 3.64%
    # 라이브 운영 — 자금 200 USDT, 손실 한도 -10% / -5% 자동 중지
    if (config.binance.enabled and binance_spot_exchange
            and getattr(config, 'donchian_daily_enabled', False)):
        try:
            from engine.donchian_daily_engine import DonchianDailyEngine
            donchian_capital = float(getattr(config, 'donchian_daily_capital_usdt', 200.0))
            _donchian_engine = DonchianDailyEngine(
                config=config,
                spot_exchange=spot_adapter,  # 실제 어댑터 (paper 우회 — 라이브)
                market_data=spot_market_data,
                initial_capital_usdt=donchian_capital,
            )
            engine_registry.register(
                "binance_donchian", _donchian_engine, None, None, None,
            )
            logger.info("donchian_daily_engine_ready",
                        capital_usdt=donchian_capital,
                        coins=_donchian_engine.coins)
        except Exception as e:
            logger.error("donchian_daily_init_failed", error=str(e), exc_info=True)

    # ── 7b-2b. Fear & Greed DCA (현물, 매주 월요일) ───────────
    _fgdca_engine = None
    if (getattr(config, 'fear_greed_dca_enabled', False)
            and config.binance.enabled and binance_spot_exchange):
        try:
            from engine.fear_greed_dca_engine import FearGreedDCAEngine
            fgdca_capital = float(getattr(config, 'fear_greed_dca_capital_usdt', 200.0))
            _fgdca_engine = FearGreedDCAEngine(
                config=config,
                spot_exchange=spot_adapter,
                market_data=spot_market_data,
                initial_capital_usdt=fgdca_capital,
            )
            engine_registry.register("binance_fgdca", _fgdca_engine, None, None, None)
            logger.info("fgdca_engine_ready", capital=fgdca_capital)
        except Exception as e:
            logger.error("fgdca_init_failed", error=str(e), exc_info=True)

    rd_futures_enabled = bool(
        getattr(config, "donchian_futures_bi_enabled", False)
        or getattr(config, "pairs_trading_live_enabled", False)
        or getattr(config, "breakout_pullback_enabled", False)
        or getattr(config, "volume_momentum_enabled", False)
        or getattr(config, "btc_neutral_mr_enabled", False)
    )
    rd_futures_allowed = not config.binance_trading.enabled
    if rd_futures_enabled and not rd_futures_allowed:
        logger.error(
            "rd_futures_requires_main_engine_disabled",
            binance_trading_enabled=config.binance_trading.enabled,
            donchian_futures_enabled=getattr(config, "donchian_futures_bi_enabled", False),
            pairs_live_enabled=getattr(config, "pairs_trading_live_enabled", False),
        )
    elif rd_futures_enabled:
        from engine.futures_rnd_coordinator import FuturesRndCoordinator

        _futures_rnd_coordinator = FuturesRndCoordinator(
            global_capital_usdt=float(getattr(config, "futures_rnd_global_capital_usdt", 150.0)),
            daily_loss_limit_pct=float(getattr(config, "futures_rnd_daily_loss_limit_pct", 0.05)),
            total_loss_limit_pct=float(getattr(config, "futures_rnd_total_loss_limit_pct", 0.10)),
        )
        engine_registry.set_shared("futures_rnd_coordinator", _futures_rnd_coordinator)

    # ── 7b-3. Donchian Futures Bi-Directional (R&D 라이브) ─────────
    if config.binance.enabled and binance_exchange and rd_futures_allowed and getattr(config, "donchian_futures_bi_enabled", False):
        try:
            from engine.donchian_futures_bi_engine import DonchianFuturesBiEngine
            donchian_futures_capital = float(getattr(config, "donchian_futures_bi_capital_usdt", 100.0))
            donchian_futures_leverage = int(getattr(config, "donchian_futures_bi_leverage", 2))
            _donchian_futures_engine = DonchianFuturesBiEngine(
                config=config,
                futures_exchange=binance_exchange,
                market_data=binance_market_data,
                initial_capital_usdt=donchian_futures_capital,
                leverage=donchian_futures_leverage,
            )
            if _futures_rnd_coordinator is not None:
                _donchian_futures_engine.set_futures_rnd_coordinator(_futures_rnd_coordinator)
            engine_registry.register("binance_donchian_futures", _donchian_futures_engine, None, None, None)
            logger.info(
                "donchian_futures_bi_engine_ready",
                capital_usdt=donchian_futures_capital,
                leverage=donchian_futures_leverage,
                coins=_donchian_futures_engine.coins,
            )
        except Exception as e:
            logger.error("donchian_futures_bi_init_failed", error=str(e), exc_info=True)

    # ── 7b-4. Pairs Trading Live R&D ─────────────────────────────
    if config.binance.enabled and binance_exchange and rd_futures_allowed and getattr(config, "pairs_trading_live_enabled", False):
        try:
            from engine.pairs_trading_live_engine import PairsTradingLiveEngine

            _pairs_live_engine = PairsTradingLiveEngine(
                config=config,
                futures_exchange=binance_exchange,
                market_data=binance_market_data,
                initial_capital_usdt=float(getattr(config, "pairs_trading_live_capital_usdt", 75.0)),
                leverage=int(getattr(config, "pairs_trading_live_leverage", 2)),
                coin_a=str(getattr(config, "pairs_trading_live_coin_a", "BTC/USDT")),
                coin_b=str(getattr(config, "pairs_trading_live_coin_b", "ETH/USDT")),
                lookback_hours=int(getattr(config, "pairs_trading_live_lookback_hours", 336)),
                z_entry=float(getattr(config, "pairs_trading_live_z_entry", 2.0)),
                z_exit=float(getattr(config, "pairs_trading_live_z_exit", 0.5)),
                z_stop=float(getattr(config, "pairs_trading_live_z_stop", 5.0)),
            )
            if _futures_rnd_coordinator is not None:
                _pairs_live_engine.set_futures_rnd_coordinator(_futures_rnd_coordinator)
            engine_registry.register("binance_pairs", _pairs_live_engine, None, None, None)
            logger.info(
                "pairs_live_engine_ready",
                capital_usdt=getattr(config, "pairs_trading_live_capital_usdt", 75.0),
                leverage=getattr(config, "pairs_trading_live_leverage", 2),
                coin_a=getattr(config, "pairs_trading_live_coin_a", "BTC/USDT"),
                coin_b=getattr(config, "pairs_trading_live_coin_b", "ETH/USDT"),
            )
        except Exception as e:
            logger.error("pairs_live_init_failed", error=str(e), exc_info=True)

    # ── 7b-5. Momentum Rotation (선물 Long/Short, 매주 리밸런싱) ──
    _momentum_engine = None
    if (config.momentum_rotation_live_enabled and config.binance.enabled
            and binance_exchange and rd_futures_allowed):
        try:
            from engine.momentum_rotation_live_engine import MomentumRotationLiveEngine
            _momentum_engine = MomentumRotationLiveEngine(
                config=config,
                futures_exchange=binance_exchange,
                market_data=binance_market_data,
                initial_capital_usdt=config.momentum_rotation_live_capital_usdt,
                leverage=config.momentum_rotation_live_leverage,
            )
            if _futures_rnd_coordinator:
                _momentum_engine.set_futures_rnd_coordinator(_futures_rnd_coordinator)
            engine_registry.register("binance_momentum", _momentum_engine, None, None, None)
            logger.info("momentum_rotation_engine_ready",
                        capital=config.momentum_rotation_live_capital_usdt,
                        leverage=config.momentum_rotation_live_leverage)
        except Exception as e:
            logger.error("momentum_rotation_init_failed", error=str(e), exc_info=True)

    # ── 7b-6. HMM Regime Detection (선물, 매시간 평가) ──
    _hmm_engine = None
    if (config.hmm_regime_live_enabled and config.binance.enabled
            and binance_exchange and rd_futures_allowed):
        try:
            from engine.hmm_regime_live_engine import HMMRegimeLiveEngine
            _hmm_engine = HMMRegimeLiveEngine(
                config=config,
                futures_exchange=binance_exchange,
                market_data=binance_market_data,
                initial_capital_usdt=config.hmm_regime_live_capital_usdt,
                leverage=config.hmm_regime_live_leverage,
                symbols=["ETH/USDT"],  # BTC는 Pairs와 충돌, 백테스트에서도 ETH가 우월
            )
            if _futures_rnd_coordinator:
                _hmm_engine.set_futures_rnd_coordinator(_futures_rnd_coordinator)
            engine_registry.register("binance_hmm", _hmm_engine, None, None, None)
            logger.info("hmm_regime_engine_ready",
                        capital=config.hmm_regime_live_capital_usdt,
                        leverage=config.hmm_regime_live_leverage)
        except Exception as e:
            logger.error("hmm_regime_init_failed", error=str(e), exc_info=True)

    # ── 7b-7. Breakout Pullback (선물 R&D, 매일 00:45 UTC) ──
    _breakout_pb_engine = None
    if (getattr(config, "breakout_pullback_enabled", False) and config.binance.enabled
            and binance_exchange and rd_futures_allowed):
        try:
            from engine.breakout_pullback_engine import BreakoutPullbackEngine
            _breakout_pb_engine = BreakoutPullbackEngine(
                config=config,
                futures_exchange=binance_exchange,
                market_data=binance_market_data,
                initial_capital_usdt=config.breakout_pullback_capital_usdt,
                leverage=config.breakout_pullback_leverage,
            )
            if _futures_rnd_coordinator:
                _breakout_pb_engine.set_futures_rnd_coordinator(_futures_rnd_coordinator)
            engine_registry.register("binance_breakout_pb", _breakout_pb_engine, None, None, None)
            logger.info("breakout_pullback_engine_ready",
                        capital=config.breakout_pullback_capital_usdt,
                        leverage=config.breakout_pullback_leverage)
        except Exception as e:
            logger.error("breakout_pullback_init_failed", error=str(e), exc_info=True)

    # ── 7b-8. Volume Momentum (선물 R&D, 매시간 xx:05) ──
    _vol_mom_engine = None
    if (getattr(config, "volume_momentum_enabled", False) and config.binance.enabled
            and binance_exchange and rd_futures_allowed):
        try:
            from engine.volume_momentum_engine import VolumeMomentumEngine
            _vol_mom_engine = VolumeMomentumEngine(
                config=config,
                futures_exchange=binance_exchange,
                market_data=binance_market_data,
                initial_capital_usdt=config.volume_momentum_capital_usdt,
                leverage=config.volume_momentum_leverage,
            )
            if _futures_rnd_coordinator:
                _vol_mom_engine.set_futures_rnd_coordinator(_futures_rnd_coordinator)
            engine_registry.register("binance_vol_mom", _vol_mom_engine, None, None, None)
            logger.info("volume_momentum_engine_ready",
                        capital=config.volume_momentum_capital_usdt,
                        leverage=config.volume_momentum_leverage)
        except Exception as e:
            logger.error("volume_momentum_init_failed", error=str(e), exc_info=True)

    # ── 7b-9. BTC Neutral Alt MR (선물 R&D, 매일 01:00 UTC) ──
    _btc_neutral_engine = None
    if (getattr(config, "btc_neutral_mr_enabled", False) and config.binance.enabled
            and binance_exchange and rd_futures_allowed):
        try:
            from engine.btc_neutral_alt_mr_engine import BTCNeutralAltMREngine
            _btc_neutral_engine = BTCNeutralAltMREngine(
                config=config,
                futures_exchange=binance_exchange,
                market_data=binance_market_data,
                initial_capital_usdt=config.btc_neutral_mr_capital_usdt,
                leverage=config.btc_neutral_mr_leverage,
            )
            if _futures_rnd_coordinator:
                _btc_neutral_engine.set_futures_rnd_coordinator(_futures_rnd_coordinator)
            engine_registry.register("binance_btc_neutral", _btc_neutral_engine, None, None, None)
            logger.info("btc_neutral_mr_engine_ready",
                        capital=config.btc_neutral_mr_capital_usdt,
                        leverage=config.btc_neutral_mr_leverage)
        except Exception as e:
            logger.error("btc_neutral_mr_init_failed", error=str(e), exc_info=True)

    # ── 7c. 서지 엔진 (조건부) — 선물 PM 잔고 통합 ─────────────
    if config.surge_trading.enabled and config.binance.enabled and binance_exchange:
        try:
            from engine.surge_engine import SurgeEngine

            surge_is_paper = config.surge_trading.mode == "paper"

            # 선물 PM 공유 (별도 PM 생성 안 함)
            binance_pm = engine_registry.get_portfolio_manager("binance_futures")

            surge_order_mgr = OrderManager(
                binance_exchange, is_paper=surge_is_paper,
                exchange_name="binance_surge", fee_currency="USDT",
            )

            surge_eng = SurgeEngine(
                config=config,
                exchange=binance_exchange,
                futures_pm=binance_pm,
                order_manager=surge_order_mgr,
                engine_registry=engine_registry,
            )
            await surge_eng.initialize()
            _surge_engine = surge_eng

            # PM=None — 서지 잔고는 선물 PM과 통합, 프론트에서도 선물 탭에 통합 표시
            engine_registry.register(
                "binance_surge", surge_eng, None,
                None, None,
            )

            logger.info("surge_engine_ready",
                         mode=config.surge_trading.mode,
                         leverage=config.surge_trading.leverage)
        except Exception as e:
            logger.error("surge_engine_init_failed", error=str(e), exc_info=True)

    # ── 7.5 교차 거래소 포지션 전환을 위한 엔진 레지스트리 주입 ────────
    for ex_name in engine_registry.available_exchanges:
        eng = engine_registry.get_engine(ex_name)
        if eng:
            eng.set_engine_registry(engine_registry)

    from research.registry import get_candidate_by_venue

    bootstrap_candidate_keys = {
        candidate.key
        for exchange_name in engine_registry.available_exchanges
        if (candidate := get_candidate_by_venue(exchange_name)) is not None
    }
    await _research_stage_gate_service.ensure_bootstrap_states(bootstrap_candidate_keys)

    from research.auto_review_service import ResearchAutoReviewService

    _research_auto_review_service = ResearchAutoReviewService(engine_registry)
    engine_registry.set_shared("research_auto_review_service", _research_auto_review_service)

    # ── 8. 스케줄러 시작 ─────────────────────────────────────
    session_factory = get_session_factory()
    bithumb_coord = engine_registry.get_coordinator("bithumb")
    bithumb_pm = engine_registry.get_portfolio_manager("bithumb")
    _scheduler = setup_scheduler(
        config=config,
        session_factory=session_factory,
        coordinator=bithumb_coord,
        portfolio_manager=bithumb_pm,
    )

    # 바이낸스 스케줄 잡 추가
    from engine.scheduler import _wrap
    from agents.coordinator import MARKET_ANALYSIS_ENABLED
    if config.binance.enabled:
        binance_coord = engine_registry.get_coordinator("binance_futures")
        binance_pm = engine_registry.get_portfolio_manager("binance_futures")
        if binance_coord:
            # COIN-53: MarketAnalysisAgent 비활성화 — 에이전트 판정이 매매에 미사용
            if MARKET_ANALYSIS_ENABLED:
                _scheduler.add_job(
                    _wrap(binance_coord.run_market_analysis),
                    name="binance_market_analysis",
                    seconds=900,
                )

            if binance_pm:
                async def binance_risk_check():
                    await binance_coord.run_risk_evaluation(binance_pm.cash_balance)
                _scheduler.add_job(
                    _wrap(binance_risk_check),
                    name="binance_risk_check",
                    seconds=300,
                )
            # 매매 회고 (6시간마다)
            _scheduler.add_job(
                _wrap(binance_coord.run_trade_review),
                name="binance_futures_trade_review",
                seconds=21600,  # 6h
            )
            # 일일 성과 분석 (21:30 KST = 12:30 UTC)
            _scheduler.add_cron_job(
                _wrap(binance_coord.run_performance_analysis),
                name="binance_futures_performance_analytics",
                hour=12, minute=30,
            )
            # 주간 전략 어드바이저 (일요일 22:00 KST = 13:00 UTC)
            _scheduler.add_weekly_cron_job(
                _wrap(binance_coord.run_strategy_advice),
                name="binance_futures_strategy_advice",
                day_of_week="sun", hour=13, minute=0,
            )

    # 바이낸스 현물 스케줄 잡 추가
    if config.binance.spot_enabled and _binance_spot_engine:
        spot_coord = engine_registry.get_coordinator("binance_spot")
        spot_pm = engine_registry.get_portfolio_manager("binance_spot")
        if spot_coord and spot_pm:
            if MARKET_ANALYSIS_ENABLED:
                _scheduler.add_job(
                    _wrap(spot_coord.run_market_analysis),
                    name="binance_spot_market_analysis",
                    seconds=900,
                )

            # 메인 현물 엔진 리스크 체크 — 메인 비활성이면 스킵
            if config.binance_spot_trading.enabled:
                async def binance_spot_risk_check():
                    await spot_coord.run_risk_evaluation(spot_pm.cash_balance)
                _scheduler.add_job(
                    _wrap(binance_spot_risk_check),
                    name="binance_spot_risk_check",
                    seconds=300,
                )

    # ── R&D 성과 분석 스케줄러 — 매일 22:00 KST (13:00 UTC) ──
    from services.rnd_performance_review import run_rnd_performance_review
    _scheduler.add_cron_job(
        _wrap(run_rnd_performance_review),
        name="rnd_performance_review",
        hour=13, minute=0,  # 22:00 KST
    )
    logger.info("rnd_performance_review_scheduled", time="22:00 KST daily")

    # ── 입출금 자동 감지 스케줄러 ───────────────────────────────
    from engine.capital_sync import sync_binance_deposits, sync_binance_internal_transfers, detect_bithumb_balance_change

    if config.binance.enabled and _binance_engine and not (config.binance_trading.mode == "paper"):
        binance_adapter_for_sync = engine_registry.get_engine("binance_futures")
        if binance_adapter_for_sync:
            async def capital_sync_binance():
                sf = get_session_factory()
                async with sf() as sess:
                    new_txs = await sync_binance_deposits(sess, binance_adapter_for_sync._exchange, exchange_name="binance_futures")
                    if new_txs:
                        b_pm = engine_registry.get_portfolio_manager("binance_futures")
                        if b_pm:
                            await b_pm.load_initial_balance_from_db(sess)
                    await sess.commit()
            _scheduler.add_job(
                _wrap(capital_sync_binance),
                name="capital_sync_binance",
                seconds=1800,
            )

            _last_internal_transfer_sync: datetime | None = None

            async def capital_sync_internal_transfers():
                """spot↔futures 내부 이체 자동 감지 (5분 주기).

                PM cash 조정은 sess.commit() 성공 후에 수행하여
                커밋 실패 → 롤백 → 재시도 시 이중 조정을 방지.
                ORM 속성은 세션 닫히기 전에 추출 (DetachedInstanceError 방지).
                """
                nonlocal _last_internal_transfer_sync
                sf = get_session_factory()
                sync_from = _last_internal_transfer_sync
                # ORM 속성을 세션 안에서 plain dict로 추출
                tx_snapshots: list[dict] = []
                async with sf() as sess:
                    new_txs, all_ok = await sync_binance_internal_transfers(
                        sess,
                        binance_adapter_for_sync._exchange,
                        exchange_name="binance_futures",
                        last_sync_time=sync_from,
                    )
                    # 세션 닫히기 전에 필요한 값 추출
                    for tx in new_txs:
                        tx_snapshots.append({
                            "tx_type": tx.tx_type,
                            "amount": tx.amount,
                            "exchange_tx_id": tx.exchange_tx_id,
                        })
                    await sess.commit()
                # 방향 중 하나라도 API 오류 시 타임스탬프를 전진시키지 않음
                # → 다음 틱에서 동일 윈도우를 재조회해 누락 방지
                if all_ok:
                    _last_internal_transfer_sync = datetime.now(timezone.utc)
                if tx_snapshots:
                    b_pm = engine_registry.get_portfolio_manager("binance_futures")
                    if b_pm is None:
                        logger.warning(
                            "pm_cash_adjustment_skipped_pm_unavailable",
                            tx_ids=[s["exchange_tx_id"] for s in tx_snapshots],
                        )
                    else:
                        for s in tx_snapshots:
                            if s["tx_type"] == "deposit":
                                b_pm.cash_balance += s["amount"]
                            else:
                                b_pm.cash_balance = max(0.0, b_pm.cash_balance - s["amount"])
                            logger.info(
                                "futures_pm_cash_adjusted_for_transfer",
                                tx_type=s["tx_type"],
                                amount=s["amount"],
                                new_cash=b_pm.cash_balance,
                            )
            _scheduler.add_job(
                _wrap(capital_sync_internal_transfers),
                name="capital_sync_internal_transfers",
                seconds=300,
            )

    if config.exchange.enabled and config.trading.mode != "paper":
        async def capital_detect_bithumb():
            sf = get_session_factory()
            async with sf() as sess:
                b_pm = engine_registry.get_portfolio_manager("bithumb")
                b_eng = engine_registry.get_engine("bithumb")
                if b_pm and b_eng:
                    await detect_bithumb_balance_change(sess, b_pm, b_eng._exchange, exchange_name="bithumb")
                await sess.commit()
        _scheduler.add_job(
            _wrap(capital_detect_bithumb),
            name="capital_detect_bithumb",
            seconds=300,
        )

    # ── 일일 손익 기록 스케줄러 ─────────────────────────────────
    async def daily_pnl_job():
        sf = get_session_factory()
        for ex_name in engine_registry.available_exchanges:
            try:
                async with sf() as sess:
                    await PortfolioManager.record_daily_pnl(sess, ex_name)
                    await sess.commit()
            except Exception as e:
                logger.warning("daily_pnl_record_failed", exchange=ex_name, error=str(e))
    _scheduler.add_job(
        _wrap(daily_pnl_job),
        name="daily_pnl_record",
        seconds=86400,
    )

    # 시작 시 누락된 일일 PnL 보충 (최근 7일)
    async def daily_pnl_catchup():
        from datetime import timedelta
        sf = get_session_factory()
        from datetime import datetime, timezone
        today_utc = datetime.now(timezone.utc).date()
        for ex_name in engine_registry.available_exchanges:
            for days_ago in range(1, 8):
                target = today_utc - timedelta(days=days_ago)
                try:
                    async with sf() as sess:
                        await PortfolioManager.record_daily_pnl(sess, ex_name, target_date=target)
                        await sess.commit()
                except Exception:
                    pass
    asyncio.create_task(daily_pnl_catchup(), name="daily_pnl_catchup")

    # ── 일일 요약 스케줄러 — 메인 엔진 비활성이므로 스킵 ────
    # R&D 성과 리포트 (rnd_performance_review, 22:00 KST)가 대체
    # if _notification_dispatcher and _notification_dispatcher.adapters:
    #     async def daily_summary_job():
    #         await send_daily_summary(engine_registry)
    #     _scheduler.add_cron_job(
    #         _wrap(daily_summary_job),
    #         name="daily_summary",
    #         hour=12, minute=5,
    #     )

    # ── 포지션 동기화 스케줄러 (1분) — 수동 매매 반영, 잔고 실시간성 개선 ──
    async def position_sync_job():
        sf = get_session_factory()
        for ex_name in engine_registry.available_exchanges:
            eng = engine_registry.get_engine(ex_name)
            pm = engine_registry.get_portfolio_manager(ex_name)
            if not eng or not pm:
                continue
            coins = eng.tracked_coins if hasattr(eng, 'tracked_coins') else []
            async with sf() as sess:
                await pm.sync_exchange_positions(sess, eng._exchange, coins)
                await sess.commit()
    _scheduler.add_job(
        _wrap(position_sync_job),
        name="position_sync",
        seconds=120,
    )

    # ── 헬스체크 스케줄러 (120초) ─────────────────────────────
    if bithumb_health:
        _scheduler.add_job(
            _wrap(bithumb_health.run_health_checks),
            name="bithumb_health_check",
            seconds=120,
        )
    if _binance_engine and futures_health:
        _scheduler.add_job(
            _wrap(futures_health.run_health_checks),
            name="futures_health_check",
            seconds=120,
        )
    if _binance_spot_engine and spot_health and config.binance_spot_trading.enabled:
        _scheduler.add_job(
            _wrap(spot_health.run_health_checks),
            name="spot_health_check",
            seconds=120,
        )

    if _research_auto_review_service is not None:
        _scheduler.add_job(
            _wrap(_research_auto_review_service.refresh_all),
            name="research_auto_review_refresh",
            seconds=900,
        )

    _scheduler.start()
    if _research_auto_review_service is not None:
        asyncio.create_task(
            _research_auto_review_service.refresh_all(),
            name="research_auto_review_initial_refresh",
        )

    # 최초 시장 분석 — 메인 엔진 비활성이므로 스킵
    # R&D 엔진은 자체 평가 루프가 있어서 별도 initial analysis 불필요
    # if bithumb_coord and bithumb_pm:
    #     asyncio.create_task(_run_initial_analysis(bithumb_coord, bithumb_pm, config))
    # if config.futures_v2.enabled and _binance_engine:
    #     ...
    # if config.binance_spot_trading.enabled and _binance_spot_engine:
    #     ...

    # 실제 추적 코인 리스트 (엔진 인스턴스의 동적 코인 포함)
    spot_coins = _engine_instance.tracked_coins if _engine_instance else config.trading.tracked_coins
    futures_coins = _binance_engine.tracked_coins if _binance_engine else config.binance.tracked_coins

    # 현재 포지션 요약
    positions_summary = await _build_positions_summary(engine_registry)

    # ── 다운타임 포지션 감사 ────────────────────────────────
    cleared_all = []
    for ex_name in engine_registry.available_exchanges:
        pm = engine_registry.get_portfolio_manager(ex_name)
        if pm and pm._cleared_positions:
            for cp in pm._cleared_positions:
                cp["exchange"] = ex_name
            cleared_all.extend(pm._cleared_positions)
            pm._cleared_positions.clear()
    if cleared_all:
        lines = []
        for cp in cleared_all:
            lines.append(
                f"  {cp['exchange']} {cp['symbol']} "
                f"({cp['direction']}, {cp['leverage']}x, 투자={cp['invested']:.2f}): "
                f"{cp['reason']}"
            )
        detail = "\n".join(lines)
        logger.warning("downtime_positions_cleared", count=len(cleared_all), positions=cleared_all)
        await emit_event(
            "warning", "system",
            f"다운타임 중 {len(cleared_all)}개 포지션 종료 감지",
            detail=detail,
            metadata={"cleared_positions": cleared_all},
        )

    logger.info("startup_complete")

    # R&D 엔진 현황 수집
    rnd_engines_active = []
    rnd_total_capital = 0.0
    for name, label in [
        ("binance_donchian", "Donchian Spot"),
        ("binance_donchian_futures", "DonchianF"),
        ("binance_pairs", "Pairs"),
        ("binance_momentum", "Momentum"),
        ("binance_hmm", "HMM"),
        ("binance_fgdca", "DCA"),
        ("binance_breakout_pb", "BreakoutPB"),
        ("binance_vol_mom", "VolMom"),
        ("binance_btc_neutral", "BTCNeutral"),
    ]:
        eng = engine_registry.get_engine(name)
        if eng and eng.is_running:
            cap = 0
            if hasattr(eng, "get_status"):
                s = eng.get_status()
                cap = s.get("capital_usdt") or s.get("initial_capital", 0)
            rnd_engines_active.append(f"{label}({cap:.0f})")
            rnd_total_capital += cap

    startup_parts = []
    if rnd_engines_active:
        startup_parts.append(f"R&D {len(rnd_engines_active)}개 엔진: {', '.join(rnd_engines_active)} (총 {rnd_total_capital:.0f} USDT)")
    else:
        startup_parts.append("R&D 엔진 없음")
    if not config.futures_v2.enabled:
        startup_parts.append("메인 선물 V2 비활성")
    if not config.binance_spot_trading.enabled:
        startup_parts.append("메인 현물 비활성")

    startup_detail = " | ".join(startup_parts)
    metadata = {
        "rnd_engines": rnd_engines_active,
        "rnd_total_capital": rnd_total_capital,
        "positions_summary": positions_summary or None,
    }
    await emit_event("info", "system", "서버 시작", detail=startup_detail, metadata=metadata)

    # 메모리 최적화: 초기화 완료 후 가비지 컬렉션
    collected = gc.collect()
    logger.info("gc_after_startup", collected=collected)

    # ── Discord 봇 (조건부) ──────────────────────────────────
    global _discord_bot_task, _discord_bot_instance
    if config.discord_bot.enabled and config.discord_bot.bot_token:
        try:
            from services.discord_bot import TradingBot
            _discord_bot_instance = TradingBot(
                config=config,
                engine_registry=engine_registry,
                session_factory=session_factory,
            )
            _discord_bot_task = asyncio.create_task(_discord_bot_instance.start(), name="discord_bot")
            # 선제 알림: 이벤트 버스에 봇 알림 콜백 추가
            from core.event_bus import set_bot_alert
            set_bot_alert(_discord_bot_instance.send_alert)
            logger.info("discord_bot_started")
        except Exception as e:
            logger.warning("discord_bot_init_failed", error=str(e))

    # ── 엔진 자동 시작 ────────────────────────────────────────
    auto_start_engines = []
    # 빗썸: paper 모드면 자동 시작 안 함 (자산 이전 완료)
    if _engine_instance and not _engine_instance.is_running and config.trading.mode == "live":
        auto_start_engines.append(("bithumb", _engine_instance))
    if _binance_engine and not _binance_engine.is_running:
        auto_start_engines.append(("binance_futures", _binance_engine))
    if _binance_spot_engine and not _binance_spot_engine.is_running and config.binance_spot_trading.enabled:
        auto_start_engines.append(("binance_spot", _binance_spot_engine))
    if _donchian_engine and not _donchian_engine.is_running:
        auto_start_engines.append(("binance_donchian", _donchian_engine))
    if _fgdca_engine and not _fgdca_engine.is_running:
        auto_start_engines.append(("binance_fgdca", _fgdca_engine))
    if _donchian_futures_engine and not _donchian_futures_engine.is_running:
        auto_start_engines.append(("binance_donchian_futures", _donchian_futures_engine))
    if _pairs_live_engine and not _pairs_live_engine.is_running:
        auto_start_engines.append(("binance_pairs", _pairs_live_engine))
    if _momentum_engine and not _momentum_engine.is_running:
        auto_start_engines.append(("binance_momentum", _momentum_engine))
    if _hmm_engine and not _hmm_engine.is_running:
        auto_start_engines.append(("binance_hmm", _hmm_engine))
    if _breakout_pb_engine and not _breakout_pb_engine.is_running:
        auto_start_engines.append(("binance_breakout_pb", _breakout_pb_engine))
    if _vol_mom_engine and not _vol_mom_engine.is_running:
        auto_start_engines.append(("binance_vol_mom", _vol_mom_engine))
    if _btc_neutral_engine and not _btc_neutral_engine.is_running:
        auto_start_engines.append(("binance_btc_neutral", _btc_neutral_engine))
    if _surge_engine and not _surge_engine.is_running:
        auto_start_engines.append(("binance_surge", _surge_engine))
    auto_start_allowed: dict[str, bool] = {}
    if _research_stage_gate_service is not None:
        for name, _ in auto_start_engines:
            auto_start_allowed[name] = await _research_stage_gate_service.is_execution_allowed_for_venue(name)
    for name, eng in auto_start_engines:
        if _research_stage_gate_service is not None and not auto_start_allowed.get(name, True):
            logger.info("engine_auto_start_blocked_by_stage_gate", exchange=name)
            continue
        asyncio.create_task(eng.start(), name=f"engine_{name}")
        logger.info("engine_auto_started", exchange=name)

    yield  # ─── 앱 실행 중 ───

    # ── Shutdown ─────────────────────────────────────────────
    logger.info("shutdown_begin")
    if _discord_bot_instance:
        await _discord_bot_instance.close()
    if _discord_bot_task:
        _discord_bot_task.cancel()
    shutdown_positions = await _build_positions_summary(engine_registry)
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    if _engine_instance:
        await _engine_instance.stop()
    if _binance_engine:
        await _binance_engine.stop()
    if _binance_spot_engine:
        await _binance_spot_engine.stop()
    if _donchian_engine:
        await _donchian_engine.stop()
    if _fgdca_engine:
        await _fgdca_engine.stop()
    if _donchian_futures_engine:
        await _donchian_futures_engine.stop()
    if _pairs_live_engine:
        await _pairs_live_engine.stop()
    if _momentum_engine:
        await _momentum_engine.stop()
    if _hmm_engine:
        await _hmm_engine.stop()
    if _breakout_pb_engine:
        await _breakout_pb_engine.stop()
    if _vol_mom_engine:
        await _vol_mom_engine.stop()
    if _btc_neutral_engine:
        await _btc_neutral_engine.stop()
    if _surge_engine:
        await _surge_engine.stop()
    if exchange:
        await exchange.close()
    if binance_exchange:
        await binance_exchange.close()
    if binance_spot_exchange:
        await binance_spot_exchange.close()
    shutdown_detail = "모든 엔진 중지 완료"
    if shutdown_positions:
        shutdown_detail += f" | {shutdown_positions}"
    await emit_event("info", "system", "서버 종료", detail=shutdown_detail,
                     metadata={"positions_summary": shutdown_positions or None})
    if _notification_dispatcher:
        await _notification_dispatcher.close()
    logger.info("shutdown_complete")


async def _build_positions_summary(registry) -> str:
    """현재 보유 포지션 요약 문자열 생성 (DB 조회)."""
    from db.session import get_session_factory
    from core.models import Position

    parts = []
    try:
        session_factory = get_session_factory()
        config = get_config()
        async with session_factory() as session:
            for ex_name in registry.available_exchanges:
                # 빗썸: paper 모드면 알림 제외
                if ex_name == "bithumb" and config.trading.mode != "live":
                    continue
                pm = registry.get_portfolio_manager(ex_name)
                if not pm:
                    continue
                result = await session.execute(
                    select(Position).where(
                        Position.exchange == ex_name,
                        Position.quantity > 0,
                    )
                )
                positions = result.scalars().all()
                pos_list = []
                for pos in positions:
                    direction = getattr(pos, "direction", "long") or "long"
                    arrow = "↑" if direction == "long" else "↓"
                    pos_list.append(f"{pos.symbol.split('/')[0]}{arrow}")
                currency = "USDT" if "binance" in ex_name else "KRW"
                if "bithumb" in ex_name:
                    label = "빗썸 현물"
                elif "binance_spot" in ex_name:
                    label = "바이낸스 현물"
                else:
                    label = "선물"
                cash = pm.cash_balance
                if pos_list:
                    parts.append(f"[{label}] {', '.join(pos_list)} | 현금 {cash:,.0f} {currency}")
                else:
                    parts.append(f"[{label}] 포지션 없음 | 현금 {cash:,.0f} {currency}")
    except Exception as e:
        logger.warning("build_positions_summary_failed", error=str(e))
    return "\n".join(parts)


async def _run_initial_analysis(coordinator, portfolio_mgr, config):
    """Run initial risk check and trade review on startup.

    COIN-53: Market analysis startup call removed — MarketAnalysisAgent 비활성화.
    """
    await asyncio.sleep(5)  # Let everything settle
    try:
        # COIN-53: run_market_analysis() 제거 — MARKET_ANALYSIS_ENABLED=False
        await coordinator.run_risk_evaluation(portfolio_mgr.cash_balance)
        await coordinator.run_trade_review()
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
        import os
        import psutil
        process = psutil.Process(os.getpid())
        mem = process.memory_info()

        engines = {}
        for name in engine_registry.available_exchanges:
            eng = engine_registry.get_engine(name)
            if not eng:
                engines[name] = {"running": False}
                continue
            ws_status = "n/a"
            if hasattr(eng, '_monitor_task') and eng._monitor_task:
                ws_status = "fallback" if (getattr(eng, '_fast_sl_task', None)
                                           and not eng._fast_sl_task.done()) else "connected"
            engines[name] = {
                "running": eng.is_running,
                "mode": eng._ec.mode if hasattr(eng, '_ec') else "unknown",
                "tracked_coins": len(eng.tracked_coins) if hasattr(eng, 'tracked_coins') else 0,
                "positions": len(getattr(eng, '_position_trackers', {})),
                "ws_status": ws_status,
            }

        db_ok = True
        try:
            sf = get_session_factory()
            async with sf() as session:
                await session.execute(select(1))
        except Exception:
            db_ok = False

        return {
            "status": "ok" if db_ok else "degraded",
            "engines": engines,
            "exchanges": engine_registry.available_exchanges,
            "memory_rss_mb": round(mem.rss / 1024 / 1024, 1),
            "uptime_hours": round((datetime.now(timezone.utc).timestamp() - process.create_time()) / 3600, 1),
            "db_connected": db_ok,
            "scheduler_running": _scheduler.running if _scheduler else False,
        }

    return app


app = create_app()
