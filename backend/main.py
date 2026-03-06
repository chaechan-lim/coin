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

from sqlalchemy import select, func

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
from api.dependencies import engine_registry
from core.event_bus import set_broadcast as set_event_broadcast, set_notification as set_event_notification, emit_event
from services.discord_event_handler import DiscordEventHandler, send_daily_summary

logger = structlog.get_logger(__name__)

_scheduler = None
_engine_instance: TradingEngine | None = None
_binance_engine = None
_binance_spot_engine = None
_discord_handler: DiscordEventHandler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    global _scheduler, _engine_instance, _binance_engine, _binance_spot_engine, _discord_handler
    config = get_config()

    logger.info("startup_begin", mode=config.trading.mode)

    from core.models import CapitalTransaction, Position as PositionModel

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
    # 원금은 항상 config에서 고정 (서버 재시작마다 재계산 방지)
    initial_krw = config.trading.initial_balance_krw

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

    # 라이브 모드: 거래소 실제 잔고 동기화 + DB 스냅샷 복원 + 원금 관리
    if not is_paper:
        session_factory = get_session_factory()
        async with session_factory() as sess:
            await portfolio_mgr.sync_exchange_positions(
                sess, exchange, config.trading.tracked_coins,
            )
            await portfolio_mgr.restore_state_from_db(sess)
            spike_fixed = await PortfolioManager.cleanup_spike_snapshots(sess, "bithumb")
            if spike_fixed:
                logger.info("bithumb_spike_cleanup", fixed=spike_fixed)

            # 시드 입금 자동 생성 (CapitalTransaction 0건이면)
            # 신규 DB: 실제 총자산(현금+포지션) 기준으로 시드 생성
            count_result = await sess.execute(
                select(func.count()).select_from(CapitalTransaction)
                .where(CapitalTransaction.exchange == "bithumb")
            )
            if count_result.scalar() == 0:
                # 실제 총자산 계산: sync 후 cash + 포지션 invested 합계
                pos_result = await sess.execute(
                    select(func.coalesce(func.sum(PositionModel.total_invested), 0))
                    .where(PositionModel.exchange == "bithumb", PositionModel.quantity > 0)
                )
                actual_invested = float(pos_result.scalar())
                actual_total = portfolio_mgr.cash_balance + actual_invested
                seed_amount = actual_total if actual_total > 0 else initial_krw

                seed = CapitalTransaction(
                    exchange="bithumb",
                    tx_type="deposit",
                    amount=round(seed_amount, 0),
                    currency="KRW",
                    note=f"초기 원금 (자동 생성, 실제 자산 기준)",
                    source="seed",
                    confirmed=True,
                )
                sess.add(seed)
                await sess.flush()
                logger.info("bithumb_seed_deposit_created",
                            amount=round(seed_amount, 0),
                            cash=round(portfolio_mgr.cash_balance, 0),
                            invested=round(actual_invested, 0))

            await portfolio_mgr.load_initial_balance_from_db(sess)
            await sess.commit()

    # ── 4. 빗썸 AI 에이전트 ───────────────────────────────────
    from agents.trade_review import TradeReviewAgent
    market_agent = MarketAnalysisAgent(market_data, exchange_name="bithumb")
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

    # Discord 이벤트 핸들러 연결
    discord_webhook = config.notification.discord_webhook_url
    if config.notification.enabled and discord_webhook:
        _discord_handler = DiscordEventHandler(discord_webhook)
        set_event_notification(_discord_handler.handle_event)
        logger.info("discord_event_handler_registered")

    # Self-healing: RecoveryManager + DiagnosticAgent 주입 (빗썸)
    from engine.recovery import RecoveryManager
    from engine.health_monitor import HealthMonitor
    from agents.diagnostic_agent import DiagnosticAgent

    bithumb_recovery = RecoveryManager(
        engine=trading_engine,
        portfolio_manager=portfolio_mgr,
        exchange_adapter=exchange,
        exchange_name="bithumb",
        tracked_coins=config.trading.tracked_coins,
    )
    bithumb_diagnostic = DiagnosticAgent(
        engine=trading_engine,
        portfolio_manager=portfolio_mgr,
        exchange_adapter=exchange,
        exchange_name="bithumb",
        tracked_coins=config.trading.tracked_coins,
    )
    bithumb_recovery.set_diagnostic_agent(bithumb_diagnostic)
    trading_engine.set_recovery_manager(bithumb_recovery)

    bithumb_health = HealthMonitor(
        engine=trading_engine,
        portfolio_manager=portfolio_mgr,
        exchange_adapter=exchange,
        market_data=market_data,
        exchange_name="bithumb",
        tracked_coins=config.trading.tracked_coins,
    )

    # EngineRegistry 등록 (빗썸)
    engine_registry.register("bithumb", trading_engine, portfolio_mgr, combiner, coordinator)

    # ── 7. 바이낸스 선물 (조건부) ──────────────────────────────
    futures_health = None
    spot_health = None
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

            # 원금은 항상 config에서 고정 (서버 재시작마다 재계산 방지)
            initial_usdt = bt.initial_balance_usdt

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

            binance_market_agent = MarketAnalysisAgent(binance_market_data, market_symbol="BTC/USDT", exchange_name="binance_futures")
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

            # 라이브 모드: 거래소 실제 잔고 동기화 + DB 스냅샷 복원 + 원금 관리
            if not binance_is_paper:
                sf = get_session_factory()
                async with sf() as sess:
                    await binance_portfolio_mgr.sync_exchange_positions(
                        sess, binance_adapter, config.binance.tracked_coins,
                    )
                    await binance_portfolio_mgr.restore_state_from_db(sess)
                    spike_fixed = await PortfolioManager.cleanup_spike_snapshots(sess, "binance_futures")
                    if spike_fixed:
                        logger.info("futures_spike_cleanup", fixed=spike_fixed)

                    # 시드 입금 자동 생성 (CapitalTransaction 0건이면)
                    cnt_result = await sess.execute(
                        select(func.count()).select_from(CapitalTransaction)
                        .where(CapitalTransaction.exchange == "binance_futures")
                    )
                    if cnt_result.scalar() == 0:
                        pos_result = await sess.execute(
                            select(func.coalesce(func.sum(PositionModel.total_invested), 0))
                            .where(PositionModel.exchange == "binance_futures", PositionModel.quantity > 0)
                        )
                        actual_invested = float(pos_result.scalar())
                        actual_total = binance_portfolio_mgr.cash_balance + actual_invested
                        seed_amount = actual_total if actual_total > 0 else initial_usdt

                        seed = CapitalTransaction(
                            exchange="binance_futures",
                            tx_type="deposit",
                            amount=round(seed_amount, 2),
                            currency="USDT",
                            note=f"초기 원금 (자동 생성, 실제 자산 기준)",
                            source="seed",
                            confirmed=True,
                        )
                        sess.add(seed)
                        await sess.flush()
                        logger.info("binance_seed_deposit_created",
                                    amount=round(seed_amount, 2),
                                    cash=round(binance_portfolio_mgr.cash_balance, 2),
                                    invested=round(actual_invested, 2))

                    await binance_portfolio_mgr.load_initial_balance_from_db(sess)
                    await sess.commit()

            # EngineRegistry 등록 (바이낸스)
            engine_registry.register(
                "binance_futures", binance_engine, binance_portfolio_mgr,
                binance_combiner, binance_coordinator,
            )

            # Self-healing: RecoveryManager + DiagnosticAgent + HealthMonitor (선물)
            futures_recovery = RecoveryManager(
                engine=binance_engine,
                portfolio_manager=binance_portfolio_mgr,
                exchange_adapter=binance_adapter,
                exchange_name="binance_futures",
                tracked_coins=config.binance.tracked_coins,
            )
            futures_diagnostic = DiagnosticAgent(
                engine=binance_engine,
                portfolio_manager=binance_portfolio_mgr,
                exchange_adapter=binance_adapter,
                exchange_name="binance_futures",
                tracked_coins=config.binance.tracked_coins,
            )
            futures_recovery.set_diagnostic_agent(futures_diagnostic)
            binance_engine.set_recovery_manager(futures_recovery)

            futures_health = HealthMonitor(
                engine=binance_engine,
                portfolio_manager=binance_portfolio_mgr,
                exchange_adapter=binance_adapter,
                market_data=binance_market_data,
                exchange_name="binance_futures",
                tracked_coins=config.binance.tracked_coins,
            )

            logger.info("binance_futures_engine_ready",
                         mode=bt.mode,
                         is_paper=binance_is_paper,
                         initial_usdt=round(initial_usdt, 2),
                         tracked_coins=config.binance.tracked_coins,
                         leverage=config.binance.default_leverage)
        except Exception as e:
            logger.error("binance_futures_init_failed", error=str(e), exc_info=True)

    # ── 7b. 바이낸스 현물 (조건부) ─────────────────────────────
    binance_spot_exchange = None
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
            spot_combiner = SignalCombiner(min_confidence=bst.min_combined_confidence)
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

            spot_market_agent = MarketAnalysisAgent(spot_market_data, market_symbol="BTC/USDT", exchange_name="binance_spot")
            spot_risk_agent = RiskManagementAgent(config.risk, spot_market_data, exchange_name="binance_spot")
            spot_trade_review = TradeReviewAgent(review_window_hours=24, exchange_name="binance_spot")
            spot_coordinator = AgentCoordinator(
                spot_market_agent, spot_risk_agent, spot_combiner, spot_trade_review,
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

            # 라이브 모드: 거래소 실제 잔고 동기화 + DB 스냅샷 복원 + 원금 관리
            if not spot_is_paper:
                sf = get_session_factory()
                async with sf() as sess:
                    await spot_portfolio_mgr.sync_exchange_positions(
                        sess, spot_adapter, config.binance.tracked_coins,
                    )
                    await spot_portfolio_mgr.restore_state_from_db(sess)
                    spike_fixed = await PortfolioManager.cleanup_spike_snapshots(sess, "binance_spot")
                    if spike_fixed:
                        logger.info("spot_spike_cleanup", fixed=spike_fixed)

                    # 시드 입금 자동 생성
                    cnt_result = await sess.execute(
                        select(func.count()).select_from(CapitalTransaction)
                        .where(CapitalTransaction.exchange == "binance_spot")
                    )
                    if cnt_result.scalar() == 0:
                        pos_result = await sess.execute(
                            select(func.coalesce(func.sum(PositionModel.total_invested), 0))
                            .where(PositionModel.exchange == "binance_spot", PositionModel.quantity > 0)
                        )
                        actual_invested = float(pos_result.scalar())
                        actual_total = spot_portfolio_mgr.cash_balance + actual_invested
                        seed_amount = actual_total if actual_total > 0 else initial_spot_usdt

                        seed = CapitalTransaction(
                            exchange="binance_spot",
                            tx_type="deposit",
                            amount=round(seed_amount, 2),
                            currency="USDT",
                            note="초기 원금 (자동 생성, 실제 자산 기준)",
                            source="seed",
                            confirmed=True,
                        )
                        sess.add(seed)
                        await sess.flush()
                        logger.info("binance_spot_seed_deposit_created",
                                    amount=round(seed_amount, 2),
                                    cash=round(spot_portfolio_mgr.cash_balance, 2),
                                    invested=round(actual_invested, 2))

                    await spot_portfolio_mgr.load_initial_balance_from_db(sess)
                    await sess.commit()

            engine_registry.register(
                "binance_spot", spot_engine, spot_portfolio_mgr,
                spot_combiner, spot_coordinator,
            )

            # Self-healing: RecoveryManager + DiagnosticAgent + HealthMonitor (현물)
            spot_recovery = RecoveryManager(
                engine=spot_engine,
                portfolio_manager=spot_portfolio_mgr,
                exchange_adapter=spot_exchange,
                exchange_name="binance_spot",
                tracked_coins=config.binance.tracked_coins,
            )
            spot_diagnostic = DiagnosticAgent(
                engine=spot_engine,
                portfolio_manager=spot_portfolio_mgr,
                exchange_adapter=spot_exchange,
                exchange_name="binance_spot",
                tracked_coins=config.binance.tracked_coins,
            )
            spot_recovery.set_diagnostic_agent(spot_diagnostic)
            spot_engine.set_recovery_manager(spot_recovery)

            spot_health = HealthMonitor(
                engine=spot_engine,
                portfolio_manager=spot_portfolio_mgr,
                exchange_adapter=spot_exchange,
                market_data=spot_market_data,
                exchange_name="binance_spot",
                tracked_coins=config.binance.tracked_coins,
            )

            logger.info("binance_spot_engine_ready",
                         mode=bst.mode,
                         is_paper=spot_is_paper,
                         initial_usdt=round(initial_spot_usdt, 2),
                         tracked_coins=config.binance.tracked_coins)
        except Exception as e:
            logger.error("binance_spot_init_failed", error=str(e), exc_info=True)

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

    # 바이낸스 현물 스케줄 잡 추가
    if config.binance.spot_enabled and _binance_spot_engine:
        spot_coord = engine_registry.get_coordinator("binance_spot")
        spot_pm = engine_registry.get_portfolio_manager("binance_spot")
        if spot_coord and spot_pm:
            _scheduler.add_job(
                _wrap(spot_coord.run_market_analysis),
                name="binance_spot_market_analysis",
                seconds=900,
            )

            async def binance_spot_risk_check():
                await spot_coord.run_risk_evaluation(spot_pm.cash_balance)
            _scheduler.add_job(
                _wrap(binance_spot_risk_check),
                name="binance_spot_risk_check",
                seconds=300,
            )
            _scheduler.add_job(
                _wrap(spot_coord.run_trade_review),
                name="binance_spot_trade_review",
                seconds=3600,
            )

    # ── 입출금 자동 감지 스케줄러 ───────────────────────────────
    from engine.capital_sync import sync_binance_deposits, detect_bithumb_balance_change
    from engine.scheduler import _wrap

    if config.binance.enabled and _binance_engine and not (config.binance_trading.mode == "paper"):
        binance_adapter_for_sync = engine_registry.get_engine("binance_futures")
        if binance_adapter_for_sync:
            async def capital_sync_binance():
                sf = get_session_factory()
                async with sf() as sess:
                    new_txs = await sync_binance_deposits(sess, binance_adapter_for_sync._exchange)
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

    if not is_paper:
        async def capital_detect_bithumb():
            sf = get_session_factory()
            async with sf() as sess:
                b_pm = engine_registry.get_portfolio_manager("bithumb")
                b_eng = engine_registry.get_engine("bithumb")
                if b_pm and b_eng:
                    await detect_bithumb_balance_change(sess, b_pm, b_eng._exchange)
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
    asyncio.create_task(daily_pnl_catchup())

    # ── 일일 요약 스케줄러 (Discord) ───────────────────────────
    if _discord_handler and discord_webhook:
        async def daily_summary_job():
            await send_daily_summary(discord_webhook, engine_registry)
        _scheduler.add_job(
            _wrap(daily_summary_job),
            name="daily_summary_discord",
            seconds=86400,
        )

    # ── 포지션 동기화 스케줄러 (1분) — 수동 매매 반영, 잔고 실시간성 개선 ──
    async def position_sync_job():
        sf = get_session_factory()
        for ex_name in engine_registry.available_exchanges:
            eng = engine_registry.get_engine(ex_name)
            pm = engine_registry.get_portfolio_manager(ex_name)
            if not eng or not pm:
                continue
            if hasattr(eng, '_tracked_coins') and eng._tracked_coins:
                coins = eng._tracked_coins
            elif "binance" in ex_name and hasattr(eng, 'tracked_coins'):
                coins = eng.tracked_coins
            else:
                coins = eng._config.trading.tracked_coins if hasattr(eng._config, 'trading') else []
            async with sf() as sess:
                await pm.sync_exchange_positions(sess, eng._exchange, coins)
                await sess.commit()
    _scheduler.add_job(
        _wrap(position_sync_job),
        name="position_sync",
        seconds=60,
    )

    # ── 헬스체크 스케줄러 (120초) ─────────────────────────────
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
    if _binance_spot_engine and spot_health:
        _scheduler.add_job(
            _wrap(spot_health.run_health_checks),
            name="spot_health_check",
            seconds=120,
        )

    _scheduler.start()

    # 최초 시장 분석 실행
    asyncio.create_task(_run_initial_analysis(coordinator, portfolio_mgr, config))

    # 바이낸스 최초 시장 분석
    if config.binance.enabled and _binance_engine:
        binance_coord = engine_registry.get_coordinator("binance_futures")
        binance_pm_init = engine_registry.get_portfolio_manager("binance_futures")
        if binance_coord and binance_pm_init:
            asyncio.create_task(_run_initial_analysis(binance_coord, binance_pm_init, config))

    # 바이낸스 현물 최초 시장 분석
    if config.binance.spot_enabled and _binance_spot_engine:
        spot_coord = engine_registry.get_coordinator("binance_spot")
        spot_pm_init = engine_registry.get_portfolio_manager("binance_spot")
        if spot_coord and spot_pm_init:
            asyncio.create_task(_run_initial_analysis(spot_coord, spot_pm_init, config))

    # 실제 추적 코인 리스트 (엔진 인스턴스의 동적 코인 포함)
    spot_coins = _engine_instance._config.trading.tracked_coins if _engine_instance else config.trading.tracked_coins
    futures_coins = _binance_engine.tracked_coins if _binance_engine else config.binance.tracked_coins

    # 현재 포지션 요약
    positions_summary = await _build_positions_summary(engine_registry)

    startup_msg = f"🚀 트레이딩 봇 시작 ({config.trading.mode.upper()} 모드)"
    # 빗썸: live 모드 + tracked_coins 있을 때만 표시
    bithumb_active = config.trading.mode == "live" and spot_coins
    if bithumb_active:
        startup_msg += f"\n빗썸 현물: {', '.join(spot_coins)}"
    if config.binance.enabled:
        startup_msg += f"\n선물: {', '.join(futures_coins)}"
    if config.binance.spot_enabled:
        startup_msg += f"\n바이낸스 현물: {', '.join(config.binance.tracked_coins)}"
    if positions_summary:
        startup_msg += f"\n{positions_summary}"
    await notification.send_engine_alert(startup_msg)

    logger.info("startup_complete")
    startup_parts = [f"{config.trading.mode} 모드"]
    if bithumb_active:
        startup_parts.append(f"빗썸 현물: {', '.join(spot_coins)}")
    if config.binance.enabled:
        startup_parts.append(f"선물: {', '.join(futures_coins)}")
    if config.binance.spot_enabled:
        startup_parts.append(f"바이낸스 현물: {', '.join(config.binance.tracked_coins)}")
    startup_detail = " | ".join(startup_parts)
    metadata = {"spot_coins": spot_coins, "futures_coins": futures_coins if config.binance.enabled else []}
    await emit_event("info", "system", "서버 시작", detail=startup_detail, metadata=metadata)

    # ── 엔진 자동 시작 ────────────────────────────────────────
    auto_start_engines = []
    # 빗썸: paper 모드면 자동 시작 안 함 (자산 이전 완료)
    if _engine_instance and not _engine_instance.is_running and config.trading.mode == "live":
        auto_start_engines.append(("bithumb", _engine_instance))
    if _binance_engine and not _binance_engine.is_running:
        auto_start_engines.append(("binance_futures", _binance_engine))
    if _binance_spot_engine and not _binance_spot_engine.is_running:
        auto_start_engines.append(("binance_spot", _binance_spot_engine))
    for name, eng in auto_start_engines:
        asyncio.create_task(eng.start())
        logger.info("engine_auto_started", exchange=name)

    yield  # ─── 앱 실행 중 ───

    # ── Shutdown ─────────────────────────────────────────────
    shutdown_positions = await _build_positions_summary(engine_registry)
    shutdown_detail = "서버 종료"
    if shutdown_positions:
        shutdown_detail += f" | {shutdown_positions}"
    await emit_event("info", "system", "서버 종료", detail=shutdown_detail)
    logger.info("shutdown_begin")
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    if _engine_instance:
        await _engine_instance.stop()
    if _binance_engine:
        await _binance_engine.stop()
    if _binance_spot_engine:
        await _binance_spot_engine.stop()
    await exchange.close()
    if binance_exchange:
        await binance_exchange.close()
    if binance_spot_exchange:
        await binance_spot_exchange.close()
    shutdown_msg = "🛑 트레이딩 봇 종료"
    if shutdown_positions:
        shutdown_msg += f"\n{shutdown_positions}"
    await notification.send_engine_alert(shutdown_msg)
    if _discord_handler:
        await _discord_handler.close()
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
    """Run initial market analysis, risk check, and trade review on startup."""
    await asyncio.sleep(5)  # Let everything settle
    try:
        await coordinator.run_market_analysis()
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
        return {
            "status": "ok",
            "mode": config.trading.mode,
            "engine_running": _engine_instance.is_running if _engine_instance else False,
            "binance_enabled": config.binance.enabled,
            "binance_running": _binance_engine.is_running if _binance_engine else False,
            "binance_spot_enabled": config.binance.spot_enabled,
            "binance_spot_running": _binance_spot_engine.is_running if _binance_spot_engine else False,
            "exchanges": engine_registry.available_exchanges,
        }

    return app


app = create_app()
