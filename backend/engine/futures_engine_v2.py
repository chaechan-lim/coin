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
import time
import structlog
from datetime import datetime, timezone

from sqlalchemy import or_, select

from config import AppConfig
from core.event_bus import emit_event
from core.models import Order, Position
from db.session import get_session_factory
from core.enums import Regime
from engine.regime_detector import RegimeDetector
from engine.regime_evaluators import RegimeLongEvaluator, RegimeShortEvaluator
from engine.strategy_selector import StrategySelector
from engine.spot_evaluator import SpotEvaluator
from engine.tier1_manager import Tier1Manager
from engine.liquidation_guard import LiquidationGuard
from engine.tier2_scanner import Tier2Scanner
from engine.safe_order_pipeline import SafeOrderPipeline, OrderRequest
from engine.balance_guard import BalanceGuard
from engine.position_state_tracker import PositionState, PositionStateTracker
from engine.order_manager import OrderManager
from engine.portfolio_manager import PortfolioManager
from exchange.base import ExchangeAdapter
from services.market_data import MarketDataService
from strategies.combiner import SignalCombiner
from strategies.cis_momentum import CISMomentumStrategy
from strategies.bnf_deviation import BNFDeviationStrategy
from strategies.donchian_channel import DonchianChannelStrategy
from strategies.larry_williams import LarryWilliamsStrategy

logger = structlog.get_logger(__name__)


class FuturesEngineV2:
    """선물 엔진 v2 — 레짐 적응형, 상시 포지션."""

    EXCHANGE_NAME = "binance_futures"

    # WS 재연결 상수
    _WS_RECONNECT_MIN = 5  # 최소 재연결 대기 (초)
    _WS_RECONNECT_MAX = 300  # 최대 재연결 대기 (초)
    _WS_RECONNECT_FACTOR = 2  # 지수 백오프 배율
    _WS_MAX_ERRORS = 3  # WS 폴백 전환 기준 연속 에러
    _FAST_SL_INTERVAL = 30  # 폴백 폴링 주기 (초)

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

        # 레짐 변경 시 Tier1 즉시 재평가 트리거 이벤트 (COIN-50)
        self._regime_changed_event: asyncio.Event = asyncio.Event()

        # 핵심 컴포넌트
        self._regime = RegimeDetector(
            adx_enter=v2_cfg.regime_adx_enter,
            adx_exit=v2_cfg.regime_adx_exit,
            confirm_count=v2_cfg.regime_confirm_count,
            min_duration_h=v2_cfg.regime_min_duration_h,
            on_regime_change=self._on_regime_change,
        )
        self._strategies = StrategySelector()
        self._positions = PositionStateTracker()
        self._guard = BalanceGuard(
            exchange=exchange,
            exchange_name=self.EXCHANGE_NAME,
            warn_pct=v2_cfg.balance_divergence_warn_pct,
            pause_pct=v2_cfg.balance_divergence_pause_pct,
            resync_callback=self._resync_cash,
        )

        self._safe_order = SafeOrderPipeline(
            order_manager=order_manager,
            portfolio_manager=portfolio_manager,
            balance_guard=self._guard,
            exchange=exchange,
            leverage=v2_cfg.leverage,
        )

        # ML Signal Filter (선택적 — 모델 파일 존재 시 활성) (COIN-40)
        self._ml_filter = None
        try:
            from strategies.ml_filter import MLSignalFilter, MODEL_DIR

            model_path = MODEL_DIR / "signal_filter.pkl"
            if model_path.exists():
                self._ml_filter = MLSignalFilter(min_win_prob=0.52)
                self._ml_filter.load(str(model_path))
                logger.info("v2_ml_filter_loaded", model_path=str(model_path))
        except Exception as e:
            logger.warning("v2_ml_filter_load_failed", error=str(e))

        # ── Evaluator 생성: strategy_mode에 따라 분기 (COIN-46) ──
        self._strategy_mode = v2_cfg.strategy_mode
        if v2_cfg.strategy_mode == "regime":
            # 레짐 3전략: RegimeLongEvaluator + RegimeShortEvaluator
            # 백테스트 PF 2.17, MDD 5.42%, Sharpe 1.61 (ALL PASS)
            regime_eval_interval = v2_cfg.tier1_regime_eval_interval_sec
            self._long_evaluator = RegimeLongEvaluator(
                strategy_selector=self._strategies,
                regime_detector=self._regime,
                market_data=market_data,
                eval_interval=regime_eval_interval,
            )
            self._short_evaluator = RegimeShortEvaluator(
                strategy_selector=self._strategies,
                regime_detector=self._regime,
                market_data=market_data,
                eval_interval=regime_eval_interval,
            )
            logger.info(
                "v2_strategy_mode_regime",
                eval_interval=regime_eval_interval,
                cooldown_hours=v2_cfg.tier1_regime_cooldown_hours,
                min_confidence=v2_cfg.tier1_min_confidence,
            )
        else:
            # 현물 4전략 폴백 (strategy_mode=spot)
            spot_strategies = [
                CISMomentumStrategy(),
                BNFDeviationStrategy(),
                DonchianChannelStrategy(),
                LarryWilliamsStrategy(),
            ]
            spot_combiner = SignalCombiner(
                strategy_weights=SignalCombiner.SPOT_WEIGHTS.copy(),
                min_confidence=v2_cfg.tier1_long_min_confidence,
                directional_weights=False,
                exchange_name=self.EXCHANGE_NAME,
                min_sell_active_weight=v2_cfg.min_sell_active_weight,
            )
            spot_evaluator = SpotEvaluator(
                strategies=spot_strategies,
                combiner=spot_combiner,
                market_data=market_data,
                eval_interval=v2_cfg.tier1_long_eval_interval_sec,
                min_confidence=v2_cfg.tier1_long_min_confidence,
                cooldown_hours=v2_cfg.tier1_long_cooldown_hours,
                sl_atr_mult=v2_cfg.tier1_long_sl_atr_mult,
                tp_atr_mult=v2_cfg.tier1_long_tp_atr_mult,
                trail_activation_atr_mult=v2_cfg.tier1_long_trail_activation_atr_mult,
                trail_stop_atr_mult=v2_cfg.tier1_long_trail_stop_atr_mult,
            )
            self._long_evaluator = spot_evaluator
            self._short_evaluator = spot_evaluator
            logger.info("v2_strategy_mode_spot")

        # COIN-48: WS/eval 동시 청산 방지 뮤텍스 (Tier1Manager와 공유)
        self._close_lock = asyncio.Lock()

        # COIN-76: 청산 거리 검증 가드
        self._liquidation_guard = LiquidationGuard(exchange)

        self._tier1 = Tier1Manager(
            coins=list(v2_cfg.tier1_coins),
            safe_order=self._safe_order,
            position_tracker=self._positions,
            regime_detector=self._regime,
            portfolio_manager=portfolio_manager,
            market_data=market_data,
            long_evaluator=self._long_evaluator,
            short_evaluator=self._short_evaluator,
            leverage=v2_cfg.leverage,
            max_position_pct=v2_cfg.tier1_max_position_pct,
            min_confidence=v2_cfg.tier1_min_confidence,
            cooldown_seconds=v2_cfg.tier1_cooldown_seconds,
            long_cooldown_seconds=int(v2_cfg.tier1_sl_long_cooldown_hours * 3600),
            short_cooldown_seconds=int(v2_cfg.tier1_sl_short_cooldown_hours * 3600),
            exchange_name=self.EXCHANGE_NAME,
            on_close_callback=self._on_sell_completed,
            ml_filter=self._ml_filter,
            # Risk management (COIN-42)
            asymmetric_mode=v2_cfg.asymmetric_mode,
            dynamic_sl=v2_cfg.dynamic_sl,
            atr_leverage_scaling=v2_cfg.atr_leverage_scaling,
            daily_buy_limit=v2_cfg.tier1_daily_buy_limit,
            max_daily_coin_buys=v2_cfg.tier1_max_daily_coin_buys,
            max_eval_errors=v2_cfg.tier1_max_eval_errors,
            # COIN-43: 최대 보유 시간 + 교차 거래소 체크
            max_hold_hours=v2_cfg.tier1_max_hold_hours,
            cross_exchange_checker=self._check_cross_exchange_position,
            # COIN-48: WS/eval 동시 청산 방지 뮤텍스 공유
            close_lock=self._close_lock,
            # 전략 평가 쓰로틀: 백테스트 최적값과 일치 (COIN-50 보완)
            strategy_eval_interval_sec=v2_cfg.tier1_regime_eval_interval_sec,
            # COIN-76: 청산 거리 검증 가드
            liquidation_guard=self._liquidation_guard,
        )

        self._tier2 = Tier2Scanner(
            safe_order=self._safe_order,
            position_tracker=self._positions,
            exchange=exchange,
            portfolio_manager=portfolio_manager,
            regime_detector=self._regime,
            max_concurrent=v2_cfg.tier2_max_concurrent,
            max_position_pct=v2_cfg.tier2_max_position_pct,
            max_hold_minutes=v2_cfg.tier2_max_hold_minutes,
            vol_threshold=v2_cfg.tier2_vol_threshold,
            close_lock=self._close_lock,
            price_threshold=v2_cfg.tier2_price_threshold,
            sl_pct=v2_cfg.tier2_sl_pct,
            tp_pct=v2_cfg.tier2_tp_pct,
            trail_activation_pct=v2_cfg.tier2_trail_activation_pct,
            trail_stop_pct=v2_cfg.tier2_trail_stop_pct,
            daily_trade_limit=v2_cfg.tier2_daily_trade_limit,
            cooldown_per_symbol_sec=v2_cfg.tier2_cooldown_per_symbol_sec,
            leverage=v2_cfg.leverage,
            # COIN-23: 신규 필터 파라미터
            rsi_overbought=v2_cfg.tier2_rsi_overbought,
            rsi_oversold=v2_cfg.tier2_rsi_oversold,
            min_atr_pct=v2_cfg.tier2_min_atr_pct,
            exhaustion_pct=v2_cfg.tier2_exhaustion_pct,
            min_score=v2_cfg.tier2_min_score,
            consecutive_sl_cooldown_sec=v2_cfg.tier2_consecutive_sl_cooldown_sec,
        )

        self._pm = portfolio_manager
        self._om = order_manager
        self._is_running = False
        self._tasks: list[asyncio.Task] = []
        self._engine_registry = None
        self._recovery_manager = None
        self._broadcast_callback = None

        # WS 모니터링 상태 (_close_lock은 위에서 생성, Tier1Manager와 공유)
        self._ws_reconnect_lock = asyncio.Lock()  # 재연결 동시 호출 방지
        self._last_reconnect_at: float = 0.0  # 마지막 재연결 시각 (monotonic)
        self._ws_consecutive_successes: int = 0  # WS 연속 성공 카운터 (폴백 해제 기준)
        self._ws_unrealized_pnl: dict[
            str, float
        ] = {}  # 포지션별 미실현 PnL (잔고 감사용)
        self._ws_monitor_task: asyncio.Task | None = None
        self._ws_balance_task: asyncio.Task | None = None  # _ws_balance_loop
        self._ws_pos_task: asyncio.Task | None = None  # _ws_position_loop
        self._fast_sl_task: asyncio.Task | None = None
        self._ws_enabled = True  # WS 활성화 플래그

        # health_monitor 호환 속성: Tier1Manager의 실제 에러 카운터를 참조
        self._position_trackers: dict = {}

        # agent coordinator 호환 속성
        self._agent_coordinator = None
        self._paused_coins: set[str] = set()
        self._suppressed_coins: set[str] = set()
        self._sells_since_review: int = 0
        self._REVIEW_TRIGGER_SELLS: int = 5
        self._background_tasks: set[asyncio.Task] = set()

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

    @property
    def _eval_error_counts(self) -> dict[str, int]:
        """health_monitor 호환: Tier1Manager의 실제 에러 카운터 참조."""
        return self._tier1._eval_error_counts

    @_eval_error_counts.setter
    def _eval_error_counts(self, value: dict[str, int]) -> None:
        """health_monitor 호환: 외부에서 에러 카운터 설정 허용."""
        self._tier1._eval_error_counts = value

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

    def suppress_buys(self, coins: list[str]) -> None:
        """coordinator 호환: 리스크 WARNING 시 매수 억제 (v2는 no-op 로그)."""
        logger.info("v2_buys_suppressed", coins=coins)

    def set_agent_coordinator(self, coordinator) -> None:
        """에이전트 코디네이터 연결."""
        self._agent_coordinator = coordinator

    async def _on_sell_completed(self) -> None:
        """매도 완료 시 카운터 증가 -> N회마다 매매 회고 트리거."""
        self._sells_since_review += 1
        if (
            self._sells_since_review >= self._REVIEW_TRIGGER_SELLS
            and self._agent_coordinator
        ):
            self._sells_since_review = 0
            task = asyncio.create_task(
                self._agent_coordinator.run_trade_review(),
                name="v2_trade_review",
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            logger.info("v2_trade_review_triggered", trigger=self._REVIEW_TRIGGER_SELLS)

    async def _resync_cash(self, new_cash: float) -> None:
        """BalanceGuard가 호출하는 내부 장부 재동기화 콜백."""
        old_cash = self._pm.cash_balance
        async with self._pm.cash_lock:
            self._pm.cash_balance = new_cash
        logger.warning(
            "v2_cash_resynced",
            old_cash=round(old_cash, 4),
            new_cash=round(new_cash, 4),
            diff=round(new_cash - old_cash, 4),
        )

    # ── COIN-43: 교차 거래소 포지션 충돌 감지 ──────────────

    async def _check_cross_exchange_position(
        self,
        symbol: str,
        confidence: float,
    ) -> bool | None:
        """선물 숏 진입 전 현물 롱 확인 (COIN-43).

        Returns:
            None  = 교차 포지션 없음 (숏 진행)
            True  = 교차 포지션 청산 성공 (숏 진행)
            False = 교차 포지션 있으나 청산 불가/차단 (숏 차단)
        """
        if not self._engine_registry:
            return None

        # 기초 자산 추출 (e.g., "BTC/USDT" → "BTC")
        base = symbol.split("/")[0]

        # 모든 다른 거래소에서 같은 기초 자산 롱 포지션 검색
        sf = get_session_factory()
        async with sf() as session:
            result = await session.execute(
                select(Position).where(
                    Position.symbol.like(f"{base}/%"),
                    Position.quantity > 0,
                    Position.exchange != self.EXCHANGE_NAME,
                    or_(Position.direction != "short", Position.direction.is_(None)),
                )
            )
            cross_pos = result.scalars().first()

        if not cross_pos:
            return None  # 교차 포지션 없음

        # 높은 신뢰도면 현물 롱 청산 후 숏 진행
        if confidence >= Tier1Manager.CROSS_FLIP_MIN_CONFIDENCE:
            cross_engine = self._engine_registry.get_engine(cross_pos.exchange)
            if cross_engine and hasattr(
                cross_engine, "close_position_for_cross_exchange"
            ):
                cross_symbol = f"{base}/USDT"
                # 교차 엔진의 quote currency에 맞게 심볼 구성
                if hasattr(cross_engine, "_ec"):
                    cross_symbol = f"{base}/{cross_engine._ec.quote_currency}"
                flipped = await cross_engine.close_position_for_cross_exchange(
                    cross_symbol,
                    f"교차 전환: {self.EXCHANGE_NAME} SHORT(conf={confidence:.2f}) → 롱 청산",
                )
                if flipped:
                    await emit_event(
                        "info",
                        "risk",
                        f"교차 포지션 전환: {cross_pos.exchange} {base} 롱 청산 → {self.EXCHANGE_NAME} 숏 진행",
                        metadata={"symbol": symbol, "confidence": round(confidence, 2)},
                    )
                    return True

        # 청산 실패 또는 낮은 신뢰도 → 숏 차단
        await emit_event(
            "warning",
            "risk",
            f"교차 거래소 충돌: {symbol} 숏 차단 (현물 롱 보유 중)",
            metadata={
                "symbol": symbol,
                "cross_exchange": cross_pos.exchange,
                "cross_qty": cross_pos.quantity,
                "confidence": round(confidence, 2),
            },
        )
        return False

    # ── 시작/중지 ──────────────────────────────

    async def initialize(self) -> None:
        """초기화: 포지션 복원 + 쿨다운 복원 + 일일 매수 복원 + 레버리지 설정."""
        sf = get_session_factory()
        async with sf() as session:
            count = await self._positions.restore_from_db(session, self.EXCHANGE_NAME)
            logger.info("v2_positions_restored", count=count)

            # COIN-41: 쿨다운 DB 복원 (재시작 시 쿨다운 소실 방지)
            cooldown_count = await self._tier1.restore_cooldowns(session)
            if cooldown_count:
                logger.info("v2_cooldowns_restored", count=cooldown_count)

            # COIN-41: 일일 매수 카운터 복원
            await self._tier1.restore_daily_buy_count(session)

        for symbol in self.tracked_coins:
            try:
                await self._exchange.set_leverage(
                    symbol,
                    self._config.futures_v2.leverage,
                )
            except Exception:
                pass

    async def start(self) -> None:
        if self._is_running:
            return
        self._is_running = True
        await emit_event("info", "engine", "선물 엔진 v2 시작")

        # COIN-41: 다운타임 중 SL/TP 초과 포지션 즉시 체크
        await self._check_downtime_stops()

        # WS 초기화
        ws_started = False
        if self._ws_enabled:
            try:
                await self._exchange.create_ws_exchange()
                self._ws_monitor_task = asyncio.create_task(
                    self._ws_price_monitor_loop(), name="v2_ws_price"
                )
                self._ws_balance_task = asyncio.create_task(
                    self._ws_balance_loop(), name="v2_ws_balance"
                )
                self._ws_pos_task = asyncio.create_task(
                    self._ws_position_loop(), name="v2_ws_position"
                )
                ws_started = True
                logger.info("v2_ws_started")
            except Exception as e:
                logger.warning("v2_ws_init_failed", error=str(e))

        # WS 실패 시 폴백 시작
        if not ws_started:
            self._fast_sl_task = asyncio.create_task(
                self._fast_stop_check_loop(), name="v2_fast_sl"
            )
            logger.info("v2_fast_sl_fallback_started")

        self._tasks = [
            asyncio.create_task(self._regime_loop(), name="v2_regime"),
            asyncio.create_task(self._tier1_loop(), name="v2_tier1"),
            asyncio.create_task(self._tier2_loop(), name="v2_tier2"),
            asyncio.create_task(self._balance_guard_loop(), name="v2_guard"),
            asyncio.create_task(self._income_loop(), name="v2_income"),
            asyncio.create_task(self._persist_loop(), name="v2_persist"),
        ]
        # WS 태스크도 관리 목록에 추가
        for t in (
            self._ws_monitor_task,
            self._ws_balance_task,
            self._ws_pos_task,
            self._fast_sl_task,
        ):
            if t is not None:
                self._tasks.append(t)

    async def stop(self) -> None:
        self._is_running = False

        # 셧다운 포지션 경고: 보유 중인 포지션 PnL 로깅 + 이벤트 (COIN-43)
        await self._log_shutdown_positions()

        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._tasks = []
        self._ws_monitor_task = None
        self._ws_balance_task = None
        self._ws_pos_task = None
        self._fast_sl_task = None

        # WS 연결 해제
        try:
            await self._exchange.close_ws()
        except Exception:
            pass

        await emit_event("info", "engine", "선물 엔진 v2 중지")

    async def _log_shutdown_positions(self) -> None:
        """셧다운 시 보유 포지션 PnL 로깅 + 이벤트 발생 (COIN-43)."""
        try:
            sf = get_session_factory()
            async with sf() as session:
                result = await session.execute(
                    select(Position).where(
                        Position.quantity > 0,
                        Position.exchange == self.EXCHANGE_NAME,
                    )
                )
                positions = result.scalars().all()

                if not positions:
                    return

                for pos in positions:
                    direction = pos.direction or "long"
                    try:
                        price = await self._market_data.get_current_price(pos.symbol)
                        if pos.average_buy_price and pos.average_buy_price > 0:
                            if direction == "long":
                                pnl_pct = (
                                    (price - pos.average_buy_price)
                                    / pos.average_buy_price
                                    * 100
                                )
                            else:
                                pnl_pct = (
                                    (pos.average_buy_price - price)
                                    / pos.average_buy_price
                                    * 100
                                )
                        else:
                            pnl_pct = 0.0
                    except Exception:
                        price = 0
                        pnl_pct = 0

                    lev = pos.leverage or self._config.futures_v2.leverage
                    logger.warning(
                        "v2_stop_open_position",
                        symbol=pos.symbol,
                        direction=direction,
                        leverage=lev,
                        quantity=pos.quantity,
                        entry=pos.average_buy_price,
                        current_price=price,
                        pnl_pct=round(pnl_pct, 2),
                    )

                await emit_event(
                    "warning",
                    "engine",
                    f"선물 엔진 v2 중지: {len(positions)}개 포지션 보유 중 (레버리지 포지션 주의)",
                    metadata={
                        "positions": [
                            {
                                "symbol": p.symbol,
                                "direction": p.direction or "long",
                                "leverage": p.leverage
                                or self._config.futures_v2.leverage,
                            }
                            for p in positions
                        ]
                    },
                )
        except Exception as e:
            logger.warning("v2_shutdown_position_log_failed", error=str(e))

    # ── WS 실시간 모니터링 ────────────────────────

    async def _ws_reconnect(self, backoff: float) -> float:
        """WS 재연결 시도. 성공 시 backoff 리셋, 실패 시 증가된 backoff 반환.

        _ws_reconnect_lock으로 동시 호출을 직렬화하고, freshness check로
        최근 재연결된 경우 중복 재연결을 스킵하여 reconnect storm을 방지한다.
        """
        async with self._ws_reconnect_lock:
            # 최근 재연결됐으면 스킵 (다른 루프가 이미 재연결함)
            if (time.monotonic() - self._last_reconnect_at) < self._WS_RECONNECT_MIN:
                logger.debug("v2_ws_reconnect_skipped_fresh")
                return self._WS_RECONNECT_MIN

            wait = min(backoff, self._WS_RECONNECT_MAX)
            logger.info("v2_ws_reconnect_attempt", wait_sec=wait)
            await asyncio.sleep(wait)

            try:
                await self._exchange.close_ws()
            except Exception:
                pass

            try:
                await self._exchange.create_ws_exchange()
                self._last_reconnect_at = time.monotonic()
                logger.info("v2_ws_reconnected")
                return self._WS_RECONNECT_MIN
            except Exception as e:
                logger.warning("v2_ws_reconnect_failed", error=str(e))
                return min(wait * self._WS_RECONNECT_FACTOR, self._WS_RECONNECT_MAX)

    async def _ws_price_monitor_loop(self) -> None:
        """WS 실시간 가격 수신 → 보유 포지션 SL/TP/trailing 체크.

        3회 연속 에러 시 _fast_stop_check_loop 폴백 자동 시작.
        재연결 성공 시 폴백 해제.
        """
        backoff = self._WS_RECONNECT_MIN
        consecutive_errors = 0

        while self._is_running:
            try:
                symbols = self._positions.all_symbols()
                if not symbols:
                    await asyncio.sleep(5)
                    continue

                tickers = await self._exchange.watch_tickers(symbols)
                consecutive_errors = 0
                backoff = self._WS_RECONNECT_MIN

                # 폴백 해제: 3회 연속 성공 후 (대칭 히스테리시스)
                self._ws_consecutive_successes += 1
                if (
                    self._fast_sl_task
                    and not self._fast_sl_task.done()
                    and self._ws_consecutive_successes >= self._WS_MAX_ERRORS
                ):
                    self._fast_sl_task.cancel()
                    try:
                        await self._fast_sl_task
                    except asyncio.CancelledError:
                        pass
                    self._fast_sl_task = None
                    logger.info(
                        "v2_fast_sl_fallback_cancelled",
                        after_successes=self._ws_consecutive_successes,
                    )

                for symbol in symbols:
                    if symbol in tickers:
                        price = float(tickers[symbol].get("last", 0))
                        if price > 0:
                            await self._realtime_stop_check(symbol, price)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                self._ws_consecutive_successes = 0
                logger.warning(
                    "v2_ws_price_error",
                    error=str(e),
                    consecutive=consecutive_errors,
                )

                if consecutive_errors >= self._WS_MAX_ERRORS:
                    # 폴백 시작
                    if not self._fast_sl_task or self._fast_sl_task.done():
                        self._fast_sl_task = asyncio.create_task(
                            self._fast_stop_check_loop(),
                            name="v2_fast_sl",
                        )
                        self._tasks.append(self._fast_sl_task)
                        logger.warning("v2_ws_fallback_activated")

                    backoff = await self._ws_reconnect(backoff)
                    if backoff == self._WS_RECONNECT_MIN:
                        consecutive_errors = 0
                else:
                    await asyncio.sleep(5)

    async def _realtime_stop_check(self, symbol: str, price: float) -> None:
        """WS 가격으로 SL/TP/trailing 즉시 체크 — 경량 2단계 필터링.

        Phase 1: 인메모리 PositionState로 빠른 필터 (DB 미접근).
        Phase 2: 히트 시 close_lock 획득 → DB 포지션 조회 → 청산 실행.
        """
        state = self._positions.get(symbol)
        if not state:
            return

        # extreme 가격 업데이트 (트레일링용)
        state.update_extreme(price)

        # --- Phase 1: 빠른 필터 (99%는 여기서 리턴) ---
        entry = state.entry_price
        if entry <= 0:
            return

        # Tier1: ATR 기반 SL/TP
        if state.tier == "tier1":
            atr = self._positions.get_atr(symbol)
            if atr <= 0:
                return  # ATR 미캐시 시 SL/TP 체크 스킵

            sl_hit = state.check_stop_loss(price, atr)
            tp_hit = state.check_take_profit(price, atr)
            trail_hit = state.check_trailing_stop(price, atr)

            if not (sl_hit or tp_hit or trail_hit):
                return

            # --- Phase 2: close_lock 하에 DB 검증 + 청산 ---
            if sl_hit:
                reason = f"[WS] SL hit: price={price:.2f}"
            elif trail_hit:
                reason = f"[WS] Trailing stop: price={price:.2f}"
            else:
                reason = f"[WS] TP hit: price={price:.2f}"

            # COIN-43: Tier1 WS SL/TP 이벤트도 Tier1Manager 쿨다운 사용
            self._tier1._emit_stop_event_throttled(symbol, state, price, reason)

            async with self._close_lock:
                await self._execute_ws_close(symbol, state, price, reason)

        # Tier2: 퍼센트 기반 SL/TP
        elif state.tier == "tier2":
            leverage = self._config.futures_v2.leverage
            if state.is_long:
                pnl_pct = (price - entry) / entry * 100 * leverage
            else:
                pnl_pct = (entry - price) / entry * 100 * leverage

            sl_pct = self._config.futures_v2.tier2_sl_pct
            tp_pct = self._config.futures_v2.tier2_tp_pct

            if pnl_pct <= -sl_pct:
                reason = f"[WS] Tier2 SL: {pnl_pct:.1f}%"
            elif pnl_pct >= tp_pct:
                reason = f"[WS] Tier2 TP: +{pnl_pct:.1f}%"
            else:
                return

            async with self._close_lock:
                await self._execute_ws_close(symbol, state, price, reason)

    async def _execute_ws_close(
        self,
        symbol: str,
        state: PositionState,
        price: float,
        reason: str,
    ) -> None:
        """close_lock 하에서 DB 검증 + 청산 실행 (WS 모니터 전용)."""
        # DB에서 포지션 재확인
        sf = get_session_factory()
        async with sf() as session:
            result = await session.execute(
                select(Position).where(
                    Position.symbol == symbol,
                    Position.quantity > 0,
                    Position.exchange == self.EXCHANGE_NAME,
                )
            )
            db_pos = result.scalar_one_or_none()
            if not db_pos:
                return  # 이미 청산됨

            request = OrderRequest(
                symbol=symbol,
                direction=state.direction,
                action="close",
                quantity=state.quantity,
                price=price,
                margin=state.margin,
                leverage=self._config.futures_v2.leverage,
                strategy_name=state.strategy_name or "ws_stop",
                confidence=0.0,
                tier=state.tier,
                entry_price=state.entry_price,
            )
            resp = await self._safe_order.execute_order(session, request)
            if resp.success:
                self._positions.close_position(symbol)
                self._ws_unrealized_pnl.pop(symbol, None)
                logger.warning(
                    "v2_ws_position_closed",
                    symbol=symbol,
                    reason=reason,
                    price=price,
                    direction=state.direction.value,
                )
                await session.commit()

                # Tier1: 방향별 쿨다운 설정 + 알림 쿨다운 해제 (COIN-43)
                if state.tier == "tier1":
                    self._tier1._set_exit_cooldown(symbol, state.direction)
                    self._tier1._last_stop_event_time.pop(symbol, None)

                # 매도 콜백 (매매 회고 트리거)
                if self._on_sell_completed:
                    try:
                        await self._on_sell_completed()
                    except Exception as e:
                        logger.debug("v2_on_sell_callback_error", error=str(e))

    async def _handle_external_close(
        self,
        session,
        symbol: str,
        db_pos: Position,
    ) -> bool:
        """WS 포지션 루프에서 외부 청산 감지 시 처리 (COIN-48).

        close_lock으로 eval/WS 동시 청산 방지.
        마진+PnL을 내부 cash에 반환하고 거래 기록을 생성한다.

        Returns True if position was closed (cash returned).
        """
        async with self._close_lock:
            # 락 획득 후 인메모리 상태 재확인 — eval 루프가 먼저 청산했을 수 있음.
            # _positions는 락 보유 중 유일한 신뢰 소스: eval이 닫으면 즉시 제거됨.
            # db_pos는 별도 세션에서 읽었으므로 오래된(stale) 데이터일 수 있음.
            if not self._positions.get(symbol):
                return False

            invested = db_pos.total_invested or db_pos.margin_used or 0
            entry = db_pos.average_buy_price or 0
            direction = getattr(db_pos, "direction", "long")
            leverage = getattr(db_pos, "leverage", 1) or 1
            old_qty = db_pos.quantity

            # 현재가 추정 (PnL 계산용)
            try:
                current_price = await self._market_data.get_current_price(symbol)
            except Exception:
                current_price = entry  # 가격 조회 실패 시 entry로 추정
                logger.warning(
                    "v2_external_close_price_fallback",
                    symbol=symbol,
                    fallback_price=round(entry, 4),
                    reason="get_current_price failed; PnL will be estimated as 0",
                )

            # PnL 계산
            if entry > 0 and current_price > 0:
                if direction == "short":
                    pnl_pct = (entry - current_price) / entry * leverage * 100
                else:
                    pnl_pct = (current_price - entry) / entry * leverage * 100
            else:
                pnl_pct = 0.0
            pnl_amount = invested * pnl_pct / 100 if invested else 0.0

            # 수수료 추정 (거래소 SL/TP도 수수료 발생, CLAUDE.md: 0.04%)
            fee = (
                round(current_price * old_qty * 0.0004, 4) if current_price > 0 else 0.0
            )

            # DB 포지션 업데이트
            db_pos.quantity = 0
            db_pos.current_value = 0
            db_pos.last_sell_at = datetime.now(timezone.utc)

            # 인메모리 포지션 제거 — 락 범위 내에서 즉시 수행, eval 루프 재시도 방지
            self._positions.close_position(symbol)
            self._ws_unrealized_pnl.pop(symbol, None)

            # 거래 기록 생성
            close_side = "sell" if direction != "short" else "buy"
            order = Order(
                exchange=self.EXCHANGE_NAME,
                symbol=symbol,
                side=close_side,
                order_type="market",
                status="filled",
                requested_price=current_price,
                executed_price=current_price,
                requested_quantity=old_qty,
                executed_quantity=old_qty,
                fee=fee,
                fee_currency="USDT",
                is_paper=False,
                direction=direction,
                leverage=leverage,
                margin_used=invested,
                entry_price=entry,
                realized_pnl=round(pnl_amount, 4),
                realized_pnl_pct=round(pnl_pct, 2),
                strategy_name="external_close",
                signal_confidence=0.0,
                signal_reason="WS 외부 청산 감지 (거래소 SL/TP/수동 등)",
                filled_at=datetime.now(timezone.utc),
            )
            session.add(order)

            # 내부 cash에 마진+PnL 반환 — COIN-70: PM 공유 cash_lock 하에 원자적으로 반환
            if invested > 0:
                cash_returned = max(invested + pnl_amount, 0.0)
                async with self._pm.cash_lock:
                    self._pm.cash_balance += cash_returned
                self._pm._realized_pnl += pnl_amount
                logger.info(
                    "v2_external_close_cash_returned",
                    symbol=symbol,
                    invested=round(invested, 2),
                    pnl_amount=round(pnl_amount, 2),
                    cash_returned=round(cash_returned, 2),
                    cash_balance=round(self._pm.cash_balance, 2),
                )

            logger.warning(
                "v2_external_close_detected",
                symbol=symbol,
                db_quantity=old_qty,
                entry=round(entry, 4),
                current_price=round(current_price, 4),
                pnl_pct=round(pnl_pct, 2),
                cash_returned=round(max(invested + pnl_amount, 0.0), 2)
                if invested > 0
                else 0,
            )
            return True

    async def _fast_stop_check_loop(self) -> None:
        """WS 실패 시 30초 폴링 SL/TP 폴백.

        _realtime_stop_check는 shield로 보호하여, 폴백 태스크 취소 시
        진행 중인 청산 주문이 중단되지 않도록 한다.
        """
        while self._is_running:
            await asyncio.sleep(self._FAST_SL_INTERVAL)
            try:
                positions = dict(self._positions.positions)
                if not positions:
                    continue

                for symbol, state in positions.items():
                    try:
                        ticker = await self._exchange.fetch_ticker(symbol)
                        price = ticker.last
                        if price > 0:
                            await asyncio.shield(
                                self._realtime_stop_check(symbol, price)
                            )
                    except asyncio.CancelledError:
                        raise  # 태스크 취소는 전파
                    except Exception as e:
                        logger.debug(
                            "v2_fast_sl_check_error", symbol=symbol, error=str(e)
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("v2_fast_sl_loop_error", error=str(e))

    async def _ws_balance_loop(self) -> None:
        """WS 잔고 실시간 감사 — 내부 장부 vs 거래소 잔고 차이 모니터링.

        cash 갱신 안 함 (감사만). >2% 괴리 시 경고.
        """
        backoff = self._WS_RECONNECT_MIN
        consecutive_errors = 0

        while self._is_running:
            try:
                balance = await self._exchange.watch_balance()
                consecutive_errors = 0
                backoff = self._WS_RECONNECT_MIN

                usdt = balance.get("USDT", {})
                if isinstance(usdt, dict):
                    wallet_total = float(usdt.get("total", 0) or 0)
                    margin_used = float(usdt.get("used", 0) or 0)
                else:
                    wallet_total = float(getattr(usdt, "total", 0) or 0)
                    margin_used = float(getattr(usdt, "used", 0) or 0)

                # CLAUDE.md 규약: walletBalance에서 unrealizedPnl + totalMargin 차감
                total_unrealized = sum(self._ws_unrealized_pnl.values())
                exchange_cash = wallet_total - total_unrealized - margin_used
                internal_cash = self._pm.cash_balance

                if exchange_cash > 0:
                    diff = abs(exchange_cash - internal_cash)
                    if diff > 5.0 or (
                        internal_cash > 0 and diff / internal_cash > 0.02
                    ):
                        logger.warning(
                            "v2_ws_balance_discrepancy",
                            internal=round(internal_cash, 2),
                            exchange=round(exchange_cash, 2),
                            diff=round(diff, 2),
                        )

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                logger.warning(
                    "v2_ws_balance_error",
                    error=str(e),
                    consecutive=consecutive_errors,
                )
                if consecutive_errors >= self._WS_MAX_ERRORS:
                    backoff = await self._ws_reconnect(backoff)
                    if backoff == self._WS_RECONNECT_MIN:
                        consecutive_errors = 0
                else:
                    await asyncio.sleep(5)

    async def _ws_position_loop(self) -> None:
        """WS 포지션 실시간 수신 → DB 포지션 즉시 갱신 (margin, unrealizedPnl, 수량).

        인메모리 PositionState.extreme_price도 함께 업데이트.
        """
        backoff = self._WS_RECONNECT_MIN
        consecutive_errors = 0

        while self._is_running:
            try:
                positions = await self._exchange.watch_positions()
                if not positions:
                    continue

                consecutive_errors = 0
                backoff = self._WS_RECONNECT_MIN

                sf = get_session_factory()
                async with sf() as session:
                    changed = False
                    post_commit_closes: list[str] = []

                    for fp in positions:
                        contracts = float(fp.get("contracts", 0) or 0)
                        sym_raw = fp.get("symbol", "")
                        # "BTC/USDT:USDT" → "BTC/USDT"
                        sym = sym_raw.replace(":USDT", "") if sym_raw else ""
                        if not sym:
                            continue

                        result = await session.execute(
                            select(Position).where(
                                Position.symbol == sym,
                                Position.exchange == self.EXCHANGE_NAME,
                                Position.quantity > 0,
                            )
                        )
                        db_pos = result.scalar_one_or_none()
                        if not db_pos:
                            continue

                        # 외부 청산 감지 (청산/수동 종료 등) — COIN-48: lock + cash 반환
                        if contracts == 0 and db_pos.quantity > 0:
                            closed = await self._handle_external_close(
                                session,
                                sym,
                                db_pos,
                            )
                            if closed:
                                changed = True
                                post_commit_closes.append(sym)
                            continue

                        margin = float(fp.get("initialMargin", 0) or 0)
                        entry = float(fp.get("entryPrice", 0) or 0)
                        liq = float(fp.get("liquidationPrice", 0) or 0) or None
                        unrealized = float(fp.get("unrealizedPnl", 0) or 0)
                        mark = float(fp.get("markPrice", 0) or 0)
                        updated = False

                        # 수량 변동 (1% 기준)
                        if (
                            contracts > 0
                            and db_pos.quantity > 0
                            and abs(db_pos.quantity - contracts)
                            / max(db_pos.quantity, 0.0001)
                            > 0.01
                        ):
                            db_pos.quantity = contracts
                            updated = True

                        # 마진 변동 (>0.1 USDT)
                        if margin > 0 and abs((db_pos.margin_used or 0) - margin) > 0.1:
                            db_pos.margin_used = margin
                            updated = True

                        # 진입가 변동
                        if entry > 0 and abs(db_pos.average_buy_price - entry) > 0.0001:
                            db_pos.average_buy_price = entry
                            updated = True

                        # 청산가
                        if liq and db_pos.liquidation_price != liq:
                            db_pos.liquidation_price = liq
                            updated = True

                        # 미실현 PnL → current_value + 잔고 감사용 캐시
                        if contracts > 0 and entry > 0:
                            new_value = margin + unrealized
                            if abs((db_pos.current_value or 0) - new_value) > 0.1:
                                db_pos.current_value = new_value
                                updated = True
                            self._ws_unrealized_pnl[sym] = unrealized

                        # 인메모리 extreme 업데이트
                        state = self._positions.get(sym)
                        if state and mark > 0:
                            state.update_extreme(mark)

                        if updated:
                            changed = True
                            logger.debug(
                                "v2_ws_position_updated",
                                symbol=sym,
                                margin=round(margin, 2),
                                contracts=contracts,
                            )

                    if changed:
                        await session.commit()

                    # commit 성공 후 인메모리 포지션 제거
                    for sym_close in post_commit_closes:
                        self._positions.close_position(sym_close)
                        self._ws_unrealized_pnl.pop(sym_close, None)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                logger.warning(
                    "v2_ws_position_error",
                    error=str(e),
                    consecutive=consecutive_errors,
                )
                if consecutive_errors >= self._WS_MAX_ERRORS:
                    backoff = await self._ws_reconnect(backoff)
                    if backoff == self._WS_RECONNECT_MIN:
                        consecutive_errors = 0
                else:
                    await asyncio.sleep(5)

    # ── COIN-41: 다운타임 SL/TP 체크 ──────────────

    async def _check_downtime_stops(self) -> None:
        """서버 시작 직후 다운타임 중 SL/TP 초과 포지션 즉시 체크 및 처리."""
        try:
            positions = self._positions.positions
            if not positions:
                logger.info("v2_downtime_stops_no_positions")
                return

            triggered = 0
            sf = get_session_factory()
            async with sf() as session:
                for symbol, state in list(positions.items()):
                    try:
                        price = await self._market_data.get_current_price(symbol)
                        if price <= 0:
                            continue

                        # extreme price 업데이트
                        state.update_extreme(price)

                        # ATR 필요 — 5m 캔들에서 조회
                        df = await self._market_data.get_ohlcv_df(symbol, "5m", 200)
                        if df is None or len(df) < 20:
                            continue
                        atr_col = "atr_14"
                        if atr_col not in df.columns or df[atr_col].isna().all():
                            logger.debug("v2_downtime_stop_no_atr", symbol=symbol)
                            continue
                        atr = (
                            float(df[atr_col].iloc[-1])
                            if not df[atr_col].isna().iloc[-1]
                            else 0.0
                        )
                        if atr <= 0:
                            continue

                        # SL/TP/trailing 체크
                        if await self._tier1.check_position_stop(
                            session, symbol, state, price, atr
                        ):
                            triggered += 1
                            logger.warning(
                                "v2_downtime_stop_triggered",
                                symbol=symbol,
                                direction=state.direction.value,
                                price=price,
                            )
                    except Exception as e:
                        logger.warning(
                            "v2_downtime_stop_check_error",
                            symbol=symbol,
                            error=str(e),
                        )

                if triggered:
                    await session.commit()
                    logger.warning(
                        "v2_downtime_stops_executed",
                        count=triggered,
                    )
                    await emit_event(
                        "warning",
                        "engine",
                        f"V2 다운타임 SL/TP 도달 포지션 {triggered}건 처리",
                        metadata={
                            "exchange": self.EXCHANGE_NAME,
                            "count": triggered,
                        },
                    )
                else:
                    logger.info(
                        "v2_downtime_stops_all_clear",
                        positions=len(positions),
                    )
        except Exception as e:
            logger.warning("v2_downtime_stop_check_failed", error=str(e))

    # ── 콜백들 ──────────────────────────────────

    def _on_regime_change(self, prev: Regime | None, new: Regime) -> None:
        """RegimeDetector 레짐 전환 확정 시 Tier1 즉시 재평가 트리거 (COIN-50)."""
        logger.info("v2_regime_change_trigger_eval", prev=str(prev), new=str(new))
        self._regime_changed_event.set()
        # 레짐 변경 시 전략 평가 쓰로틀 리셋 → 즉시 재평가
        self._tier1.reset_eval_throttle()

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
                            coin_df = await self._market_data.get_ohlcv_df(
                                coin, "1h", 200
                            )
                            if coin_df is not None and len(coin_df) >= 50:
                                self._regime.detect(coin_df)
                                self._regime._per_coin[coin] = self._regime.detect(
                                    coin_df
                                )
                        except Exception:
                            pass
            except Exception as e:
                logger.error("v2_regime_error", error=str(e))
            await asyncio.sleep(3600)

    async def _tier1_loop(self) -> None:
        """Tier 1 코인 평가 루프.

        tier1_eval_interval_sec(60s) 주기로 평가하되,
        레짐 변경 감지 시 즉시 재평가 (COIN-50).
        """
        # 첫 실행 전 레짐 초기화 대기
        await asyncio.sleep(5)
        while self._is_running:
            # 평가 시작 전에 클리어 — 평가 중 레짐 변경이 오면 이벤트가 set된 채로 남아
            # 다음 wait_for가 즉시 반환됨 (COIN-50 race condition fix)
            self._regime_changed_event.clear()
            try:
                sf = get_session_factory()
                async with sf() as session:
                    await self._tier1.evaluation_cycle(session)
                    await session.commit()
            except Exception as e:
                logger.error("v2_tier1_error", error=str(e))

            # 레짐 변경 또는 일반 인터벌 — 먼저 도달한 쪽에서 깨어남
            interval = self._config.futures_v2.tier1_eval_interval_sec
            try:
                await asyncio.wait_for(
                    self._regime_changed_event.wait(),
                    timeout=float(interval),
                )
                logger.info("v2_tier1_regime_triggered")
            except asyncio.TimeoutError:
                pass  # 정상 인터벌 만료

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
                    logger.critical(
                        "v2_balance_critical", divergence=result.divergence_pct
                    )
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
        """5분마다 포지션 상태 DB 영속화 + 포트폴리오 스냅샷."""
        await asyncio.sleep(120)
        while self._is_running:
            try:
                sf = get_session_factory()
                async with sf() as session:
                    await self._positions.persist_to_db(session, self.EXCHANGE_NAME)
                    # COIN-41: 쿨다운 DB 영속화
                    await self._tier1.persist_cooldowns(session)
                    await session.commit()

                    # 포트폴리오 스냅샷 저장 (daily_pnl 계산용)
                    try:
                        snap = await self._pm.take_snapshot(session)
                        if snap is not None:
                            await session.commit()
                            logger.debug(
                                "v2_snapshot_taken",
                                total=round(snap.total_value_krw, 2),
                                cash=round(snap.cash_balance_krw, 2),
                            )

                            # 포트폴리오 업데이트 브로드캐스트
                            if self._broadcast_callback:
                                summary = await self._pm.get_portfolio_summary(
                                    session,
                                )
                                await self._broadcast_callback(
                                    {
                                        "event": "portfolio_update",
                                        "exchange": self.EXCHANGE_NAME,
                                        "data": summary,
                                    }
                                )
                    except Exception as snap_err:
                        logger.warning("v2_snapshot_error", error=str(snap_err))
            except Exception as e:
                logger.warning("v2_persist_error", error=str(e))
            await asyncio.sleep(300)

    # ── API 호환 메서드 ──────────────────────────

    @property
    def strategies(self) -> dict:
        """v2 활성 전략 이름 → 전략 객체 매핑 (전략 성과/비교 탭용).

        strategy_mode에 따라 반환하는 전략이 다름:
        - regime: 레짐 3전략 (trend_follower, mean_reversion, vol_breakout)
        - spot: 현물 4전략 (cis_momentum, bnf_deviation, donchian_channel, larry_williams)
        """
        seen: dict[str, object] = {}
        if self._strategy_mode == "regime":
            # 레짐 전략: StrategySelector에서 모든 전략 객체를 가져옴 (중복 제거)
            for strategy in self._strategies.all_strategies.values():
                name = getattr(strategy, "name", None)
                if name and name not in seen:
                    seen[name] = strategy
        else:
            # SpotEvaluator의 현물 전략들 (실제 주문에 사용되는 전략명)
            evaluator = self._long_evaluator
            if hasattr(evaluator, "_strategies"):
                for strategy in evaluator._strategies:
                    name = getattr(strategy, "name", None)
                    if name and name not in seen:
                        seen[name] = strategy
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

    def get_tier1_status(self) -> dict:
        """Tier1 운영 상태 반환 (관측용 API)."""
        return self._tier1.get_status()

    def resume_balance_guard(self) -> dict:
        """BalanceGuard 수동 재개 (관리자 API용).

        Returns:
            재개 후 상태 정보.
        """
        was_paused = self._guard.is_paused
        self._guard.resume(reason="manual_api")
        return {
            "was_paused": was_paused,
            "is_paused": self._guard.is_paused,
            "guard": self._guard.get_status(),
        }

    def get_balance_guard_status(self) -> dict:
        """BalanceGuard 상태 반환 (API용)."""
        return self._guard.get_status()

    def get_status(self) -> dict:
        """엔진 상태 정보 반환 (API용)."""
        regime = self._regime.current
        ws_price_ok = (
            self._ws_monitor_task is not None and not self._ws_monitor_task.done()
        )
        ws_balance_ok = (
            self._ws_balance_task is not None and not self._ws_balance_task.done()
        )
        ws_position_ok = self._ws_pos_task is not None and not self._ws_pos_task.done()
        fast_sl_active = (
            self._fast_sl_task is not None and not self._fast_sl_task.done()
        )
        return {
            "engine": "futures_v2",
            "is_running": self._is_running,
            "strategy_mode": self._strategy_mode,
            "regime": regime.regime.value if regime else "unknown",
            "regime_confidence": regime.confidence if regime else 0.0,
            "tier1_positions": self._positions.active_count("tier1"),
            "tier2_positions": self._positions.active_count("tier2"),
            "total_positions": self._positions.active_count(),
            "balance_guard_paused": self._guard.is_paused,
            "balance_guard": self._guard.get_status(),
            "tracked_coins": self.tracked_coins,
            "ws_price_monitor": ws_price_ok,
            "ws_balance_audit": ws_balance_ok,
            "ws_position_sync": ws_position_ok,
            "fast_sl_fallback": fast_sl_active,
            "daily_buy_count": self._tier1._daily_buy_count,
            "eval_error_counts": dict(self._tier1._eval_error_counts),
        }
