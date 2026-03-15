"""
FuturesEngineV2 — 레짐 적응형 선물 엔진.

3-Layer 아키텍처:
  Layer 1: RegimeDetector (1h, 시장 레짐 감지)
  Layer 2: StrategySelector (레짐→전략 매핑)
  Layer 3: Tier1Manager + Tier2Scanner (5m, 실행)

TradingEngine을 상속하지 않음 (완전 독립).
SurgeEngine을 대체 (Tier 2로 통합).
"""
import asyncio
import structlog
from datetime import datetime, timezone

from config import AppConfig
from core.event_bus import emit_event
from db.session import get_session_factory
from engine.regime_detector import RegimeDetector
from engine.strategy_selector import StrategySelector
from engine.tier1_manager import Tier1Manager
from engine.tier2_scanner import Tier2Scanner
from engine.safe_order_pipeline import SafeOrderPipeline
from engine.balance_guard import BalanceGuard
from engine.position_state_tracker import PositionStateTracker
from engine.order_manager import OrderManager
from engine.portfolio_manager import PortfolioManager
from exchange.base import ExchangeAdapter
from services.market_data import MarketDataService

logger = structlog.get_logger(__name__)


class FuturesEngineV2:
    """선물 엔진 v2 — 레짐 적응형, 상시 포지션."""

    EXCHANGE_NAME = "binance_futures"

    def __init__(
        self,
        config: AppConfig,
        exchange: ExchangeAdapter,
        market_data: MarketDataService,
        order_manager: OrderManager,
        portfolio_manager: PortfolioManager,
    ):
        self._config = config
        self._exchange = exchange
        self._market_data = market_data

        v2_cfg = config.futures_v2

        # 핵심 컴포넌트
        self._regime = RegimeDetector(
            adx_enter=v2_cfg.regime_adx_enter,
            adx_exit=v2_cfg.regime_adx_exit,
            confirm_count=v2_cfg.regime_confirm_count,
            min_duration_h=v2_cfg.regime_min_duration_h,
        )
        self._strategies = StrategySelector()
        self._positions = PositionStateTracker()
        self._guard = BalanceGuard(
            exchange=exchange,
            exchange_name=self.EXCHANGE_NAME,
            warn_pct=v2_cfg.balance_divergence_warn_pct,
            pause_pct=v2_cfg.balance_divergence_pause_pct,
        )

        self._safe_order = SafeOrderPipeline(
            order_manager=order_manager,
            portfolio_manager=portfolio_manager,
            balance_guard=self._guard,
            exchange=exchange,
            leverage=v2_cfg.leverage,
        )

        self._tier1 = Tier1Manager(
            coins=list(v2_cfg.tier1_coins),
            safe_order=self._safe_order,
            position_tracker=self._positions,
            regime_detector=self._regime,
            strategy_selector=self._strategies,
            portfolio_manager=portfolio_manager,
            market_data=market_data,
            leverage=v2_cfg.leverage,
            max_position_pct=v2_cfg.tier1_max_position_pct,
            min_confidence=v2_cfg.tier1_min_confidence,
            cooldown_seconds=v2_cfg.tier1_cooldown_seconds,
        )

        self._tier2 = Tier2Scanner(
            safe_order=self._safe_order,
            position_tracker=self._positions,
            exchange=exchange,
            portfolio_manager=portfolio_manager,
            max_concurrent=v2_cfg.tier2_max_concurrent,
            max_position_pct=v2_cfg.tier2_max_position_pct,
            max_hold_minutes=v2_cfg.tier2_max_hold_minutes,
            vol_threshold=v2_cfg.tier2_vol_threshold,
            price_threshold=v2_cfg.tier2_price_threshold,
            sl_pct=v2_cfg.tier2_sl_pct,
            tp_pct=v2_cfg.tier2_tp_pct,
            daily_trade_limit=v2_cfg.tier2_daily_trade_limit,
            leverage=v2_cfg.leverage,
        )

        self._pm = portfolio_manager
        self._om = order_manager
        self._is_running = False
        self._tasks: list[asyncio.Task] = []
        self._engine_registry = None
        self._recovery_manager = None
        self._broadcast_callback = None

        # health_monitor 호환 속성
        self._eval_error_counts: dict[str, int] = {}
        self._position_trackers: dict = {}

    # ── EngineRegistry 호환 인터페이스 ──────────

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def tracked_coins(self) -> list[str]:
        return list(self._config.futures_v2.tier1_coins)

    @property
    def exchange_name(self) -> str:
        return self.EXCHANGE_NAME

    def set_engine_registry(self, registry) -> None:
        self._engine_registry = registry

    def set_recovery_manager(self, recovery) -> None:
        self._recovery_manager = recovery

    def set_broadcast_callback(self, callback) -> None:
        self._broadcast_callback = callback

    def pause_buying(self, coins: list[str] | None = None) -> None:
        """health_monitor 호환: API 장애 시 매수 일시중지 (v2는 no-op 로그)."""
        logger.warning("v2_buying_paused", coins=coins)

    def resume_buying(self, coins: list[str] | None = None) -> None:
        """health_monitor 호환: API 복구 시 매수 재개 (v2는 no-op 로그)."""
        logger.info("v2_buying_resumed", coins=coins)

    # ── 시작/중지 ──────────────────────────────

    async def initialize(self) -> None:
        """초기화: 포지션 복원 + 레버리지 설정."""
        sf = get_session_factory()
        async with sf() as session:
            count = await self._positions.restore_from_db(session, self.EXCHANGE_NAME)
            logger.info("v2_positions_restored", count=count)

        for symbol in self.tracked_coins:
            try:
                await self._exchange.set_leverage(
                    symbol, self._config.futures_v2.leverage,
                )
            except Exception:
                pass

    async def start(self) -> None:
        if self._is_running:
            return
        self._is_running = True
        await emit_event("info", "engine", "선물 엔진 v2 시작")

        self._tasks = [
            asyncio.create_task(self._regime_loop(), name="v2_regime"),
            asyncio.create_task(self._tier1_loop(), name="v2_tier1"),
            asyncio.create_task(self._tier2_loop(), name="v2_tier2"),
            asyncio.create_task(self._balance_guard_loop(), name="v2_guard"),
            asyncio.create_task(self._income_loop(), name="v2_income"),
            asyncio.create_task(self._persist_loop(), name="v2_persist"),
        ]

    async def stop(self) -> None:
        self._is_running = False
        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._tasks = []
        await emit_event("info", "engine", "선물 엔진 v2 중지")

    # ── 루프들 ──────────────────────────────────

    async def _regime_loop(self) -> None:
        """1시간마다 레짐 업데이트."""
        while self._is_running:
            try:
                df = await self._market_data.get_ohlcv_df("BTC/USDT", "1h", 200)
                if df is not None and len(df) >= 50:
                    await self._regime.update(df, "BTC/USDT")

                    # 개별 코인 레짐도 업데이트
                    for coin in self.tracked_coins:
                        if coin == "BTC/USDT":
                            continue
                        try:
                            coin_df = await self._market_data.get_ohlcv_df(coin, "1h", 200)
                            if coin_df is not None and len(coin_df) >= 50:
                                self._regime.detect(coin_df)
                                self._regime._per_coin[coin] = self._regime.detect(coin_df)
                        except Exception:
                            pass
            except Exception as e:
                logger.error("v2_regime_error", error=str(e))
            await asyncio.sleep(3600)

    async def _tier1_loop(self) -> None:
        """60초마다 Tier 1 코인 평가."""
        # 첫 실행 전 레짐 초기화 대기
        await asyncio.sleep(5)
        while self._is_running:
            try:
                sf = get_session_factory()
                async with sf() as session:
                    await self._tier1.evaluation_cycle(session)
                    await session.commit()
            except Exception as e:
                logger.error("v2_tier1_error", error=str(e))
            await asyncio.sleep(self._config.futures_v2.tier1_eval_interval_sec)

    async def _tier2_loop(self) -> None:
        """60초마다 Tier 2 스캔."""
        await asyncio.sleep(10)
        v2_cfg = self._config.futures_v2
        if not v2_cfg.tier2_enabled:
            return
        while self._is_running:
            try:
                sf = get_session_factory()
                async with sf() as session:
                    await self._tier2.scan_cycle(session)
                    await session.commit()
            except Exception as e:
                logger.error("v2_tier2_error", error=str(e))
            await asyncio.sleep(v2_cfg.tier2_scan_interval_sec)

    async def _balance_guard_loop(self) -> None:
        """5분마다 잔고 교차 검증."""
        await asyncio.sleep(60)
        while self._is_running:
            try:
                result = await self._guard.periodic_reconcile(self._pm.cash_balance)
                if result.is_critical:
                    logger.critical("v2_balance_critical", divergence=result.divergence_pct)
            except Exception as e:
                logger.warning("v2_guard_error", error=str(e))
            await asyncio.sleep(self._config.futures_v2.balance_check_interval_sec)

    async def _income_loop(self) -> None:
        """8시간마다 펀딩비 반영."""
        await asyncio.sleep(30)
        while self._is_running:
            try:
                await self._pm.apply_income(self._exchange)
            except Exception:
                pass
            await asyncio.sleep(8 * 3600)

    async def _persist_loop(self) -> None:
        """5분마다 포지션 상태 DB 영속화."""
        await asyncio.sleep(120)
        while self._is_running:
            try:
                sf = get_session_factory()
                async with sf() as session:
                    await self._positions.persist_to_db(session, self.EXCHANGE_NAME)
                    await session.commit()
            except Exception as e:
                logger.warning("v2_persist_error", error=str(e))
            await asyncio.sleep(300)

    # ── API 호환 메서드 ──────────────────────────

    @property
    def strategies(self) -> dict:
        """v2 레짐 전략 이름 → 전략 객체 매핑 (전략 성과 탭용).

        중복 객체 제거: 같은 전략 인스턴스가 여러 레짐에 매핑될 수 있으므로
        name 기준으로 deduplicate.
        """
        seen: dict[str, object] = {}
        for strategy in self._strategies._strategies.values():
            if strategy.name not in seen:
                seen[strategy.name] = strategy
        return seen

    @property
    def rotation_status(self) -> dict:
        """종목/로테이션 탭용 상태 (v2 레짐 적응형 엔진)."""
        regime = self._regime.current
        market_state = regime.regime.value if regime else "sideways"
        return {
            "rotation_enabled": False,
            "surge_threshold": 0.0,
            "market_state": market_state,
            "current_surge_symbol": None,
            "last_rotation_time": None,
            "last_scan_time": None,
            "rotation_cooldown_sec": 0,
            "tracked_coins": self.tracked_coins,
            "rotation_coins": [],
            "all_surge_scores": {},
        }

    def get_status(self) -> dict:
        """엔진 상태 정보 반환 (API용)."""
        regime = self._regime.current
        return {
            "engine": "futures_v2",
            "is_running": self._is_running,
            "regime": regime.regime.value if regime else "unknown",
            "regime_confidence": regime.confidence if regime else 0.0,
            "tier1_positions": self._positions.active_count("tier1"),
            "tier2_positions": self._positions.active_count("tier2"),
            "total_positions": self._positions.active_count(),
            "balance_guard_paused": self._guard.is_paused,
            "tracked_coins": self.tracked_coins,
        }
