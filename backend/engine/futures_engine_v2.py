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

from sqlalchemy import select

from config import AppConfig
from core.event_bus import emit_event
from core.models import Position
from db.session import get_session_factory
from engine.regime_detector import RegimeDetector
from engine.strategy_selector import StrategySelector
from engine.spot_evaluator import SpotEvaluator
from engine.tier1_manager import Tier1Manager
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
    _WS_RECONNECT_MIN = 5       # 최소 재연결 대기 (초)
    _WS_RECONNECT_MAX = 300     # 최대 재연결 대기 (초)
    _WS_RECONNECT_FACTOR = 2    # 지수 백오프 배율
    _WS_MAX_ERRORS = 3          # WS 폴백 전환 기준 연속 에러
    _FAST_SL_INTERVAL = 30      # 폴백 폴링 주기 (초)

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
            resync_callback=self._resync_cash,
        )

        self._safe_order = SafeOrderPipeline(
            order_manager=order_manager,
            portfolio_manager=portfolio_manager,
            balance_guard=self._guard,
            exchange=exchange,
            leverage=v2_cfg.leverage,
        )

        # 양방향 SpotEvaluator: 현물 4전략 기반 롱+숏 (COIN-28)
        # 하나의 SpotEvaluator 인스턴스가 long_evaluator + short_evaluator 모두 담당.
        # BUY → 롱 진입 / 숏 청산, SELL → 숏 진입 / 롱 청산.
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

        # WS 모니터링 상태
        self._close_lock = asyncio.Lock()  # WS/eval 동시 청산 방지
        self._ws_reconnect_lock = asyncio.Lock()  # 재연결 동시 호출 방지
        self._last_reconnect_at: float = 0.0  # 마지막 재연결 시각 (monotonic)
        self._ws_consecutive_successes: int = 0  # WS 연속 성공 카운터 (폴백 해제 기준)
        self._ws_unrealized_pnl: dict[str, float] = {}  # 포지션별 미실현 PnL (잔고 감사용)
        self._ws_monitor_task: asyncio.Task | None = None
        self._ws_bp_task: asyncio.Task | None = None   # WS balance audit loop
        self._ws_pos_task: asyncio.Task | None = None   # WS position sync loop
        self._fast_sl_task: asyncio.Task | None = None
        self._ws_enabled = True  # WS 활성화 플래그

        # health_monitor 호환 속성
        self._eval_error_counts: dict[str, int] = {}
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
        if (self._sells_since_review >= self._REVIEW_TRIGGER_SELLS
                and self._agent_coordinator):
            self._sells_since_review = 0
            task = asyncio.create_task(
                self._agent_coordinator.run_trade_review(),
                name="v2_trade_review",
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            logger.info("v2_trade_review_triggered",
                        trigger=self._REVIEW_TRIGGER_SELLS)

    async def _resync_cash(self, new_cash: float) -> None:
        """BalanceGuard가 호출하는 내부 장부 재동기화 콜백."""
        old_cash = self._pm.cash_balance
        self._pm._cash_balance = new_cash
        logger.warning(
            "v2_cash_resynced",
            old_cash=round(old_cash, 4),
            new_cash=round(new_cash, 4),
            diff=round(new_cash - old_cash, 4),
        )

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

        # WS 초기화
        ws_started = False
        if self._ws_enabled:
            try:
                await self._exchange.create_ws_exchange()
                self._ws_monitor_task = asyncio.create_task(
                    self._ws_price_monitor_loop(), name="v2_ws_price"
                )
                self._ws_bp_task = asyncio.create_task(
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
        for t in (self._ws_monitor_task, self._ws_bp_task, self._ws_pos_task, self._fast_sl_task):
            if t is not None:
                self._tasks.append(t)

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
        self._ws_monitor_task = None
        self._ws_bp_task = None
        self._ws_pos_task = None
        self._fast_sl_task = None

        # WS 연결 해제
        try:
            await self._exchange.close_ws()
        except Exception:
            pass

        await emit_event("info", "engine", "선물 엔진 v2 중지")

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
                if (self._fast_sl_task and not self._fast_sl_task.done()
                        and self._ws_consecutive_successes >= self._WS_MAX_ERRORS):
                    self._fast_sl_task.cancel()
                    try:
                        await self._fast_sl_task
                    except asyncio.CancelledError:
                        pass
                    self._fast_sl_task = None
                    logger.info("v2_fast_sl_fallback_cancelled",
                                after_successes=self._ws_consecutive_successes)

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
        self, symbol: str, state: PositionState, price: float, reason: str,
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
                logger.warning(
                    "v2_ws_position_closed",
                    symbol=symbol,
                    reason=reason,
                    price=price,
                    direction=state.direction.value,
                )
                await session.commit()

                # Tier1: 방향별 쿨다운 설정
                if state.tier == "tier1":
                    self._tier1._set_exit_cooldown(symbol, state.direction)

                # 매도 콜백 (매매 회고 트리거)
                if self._on_sell_completed:
                    try:
                        await self._on_sell_completed()
                    except Exception:
                        pass

    async def _fast_stop_check_loop(self) -> None:
        """WS 실패 시 30초 폴링 SL/TP 폴백."""
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
                            await self._realtime_stop_check(symbol, price)
                    except Exception as e:
                        logger.debug("v2_fast_sl_check_error", symbol=symbol, error=str(e))
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
                    if diff > 5.0 or (internal_cash > 0 and diff / internal_cash > 0.02):
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
                            )
                        )
                        db_pos = result.scalar_one_or_none()
                        if not db_pos:
                            continue

                        # 외부 청산 감지 (청산/수동 종료 등)
                        if contracts == 0 and db_pos.quantity > 0:
                            logger.warning(
                                "v2_external_close_detected",
                                symbol=sym,
                                db_quantity=db_pos.quantity,
                            )
                            db_pos.quantity = 0
                            db_pos.current_value = 0
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
                        if (contracts > 0 and db_pos.quantity > 0
                                and abs(db_pos.quantity - contracts) / max(db_pos.quantity, 0.0001) > 0.01):
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
                            db_pos.current_value = margin + unrealized
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

        SpotEvaluator의 현물 4전략만 반환 — 실제 주문 생성에 사용되는 전략.
        주문의 strategy_name이 이 전략 이름으로 기록되므로 /strategies/comparison이
        올바른 성과 데이터를 조회할 수 있다.
        V1 7전략(bollinger_rsi 등)과 V2 레짐 전략(trend_follower 등)은 비활성이므로 제외.
        """
        seen: dict[str, object] = {}
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
            self._ws_monitor_task is not None
            and not self._ws_monitor_task.done()
        )
        ws_balance_ok = (
            self._ws_bp_task is not None
            and not self._ws_bp_task.done()
        )
        ws_position_ok = (
            self._ws_pos_task is not None
            and not self._ws_pos_task.done()
        )
        fast_sl_active = (
            self._fast_sl_task is not None
            and not self._fast_sl_task.done()
        )
        return {
            "engine": "futures_v2",
            "is_running": self._is_running,
            "regime": regime.regime.value if regime else "unknown",
            "regime_confidence": regime.confidence if regime else 0.0,
            "tier1_positions": self._positions.active_count("tier1"),
            "tier2_positions": self._positions.active_count("tier2"),
            "total_positions": self._positions.active_count(),
            "balance_guard_paused": self._guard.is_paused,
            "balance_guard": self._guard.get_status(),
            "tracked_coins": self.tracked_coins,
            "ws_price_monitor": ws_price_ok,
            "ws_balance_position": ws_balance_ok,
            "ws_position_sync": ws_position_ok,
            "fast_sl_fallback": fast_sl_active,
        }
