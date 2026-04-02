import asyncio
import structlog
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from config import AppConfig
from core.enums import SignalType, MarketState
from core.models import Position, Order
from exchange.base import ExchangeAdapter
from services.market_data import MarketDataService
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from strategies.combiner import SignalCombiner, CombinedDecision
from engine.order_manager import OrderManager
from engine.portfolio_manager import PortfolioManager
from db.session import get_session_factory
from core.event_bus import emit_event
from core.error_classifier import classify_error, ClassifiedError, ErrorCategory

logger = structlog.get_logger(__name__)


def _effective_direction(direction) -> str:
    """Return 'short' or 'long', treating None and any non-short value as 'long'.

    Accepts Direction enum instances (str-based), plain strings, or None.
    """
    return "short" if direction == "short" else "long"


# ── 시장 상태별 동적 손절 프로필 (하이브리드) ────────────────────────
# (atr_multiplier, floor_pct, cap_pct)
_DYNAMIC_SL_PROFILES = {
    "strong_uptrend": (2.5, 3.0, 12.0),
    "uptrend":        (2.0, 3.0, 10.0),
    "sideways":       (2.0, 3.0,  7.0),
    "downtrend":      (2.0, 3.0,  7.0),
}
_DEFAULT_SL_PROFILE = (2.0, 3.0, 7.0)


@dataclass
class EngineConfig:
    """Exchange-agnostic engine configuration.

    main.py에서 거래소별 설정을 주입 → 엔진 내부에서 거래소 분기 제거.
    """
    exchange_name: str = "bithumb"
    mode: str = "paper"
    quote_currency: str = "KRW"        # KRW or USDT

    # 추적 코인
    tracked_coins: list[str] = field(default_factory=list)

    # 평가 주기
    evaluation_interval_sec: int = 300

    # 매매 제한
    min_combined_confidence: float = 0.50
    daily_buy_limit: int = 20
    max_daily_coin_buys: int = 3
    min_trade_interval_sec: int = 3600
    cooldown_after_sell_sec: int = 14400
    min_profit_vs_fee_ratio: float = 2.0

    # 주문 사이징
    min_order_amount: float = 5000     # KRW(5000) or USDT(5)
    fee_margin: float = 1.003          # 1 + fee (빗썸 0.3%, 바이낸스 0.2%)
    min_fallback_amount: float = 5000  # 잔고 전체 시도 기준
    max_trade_size_pct: float = 0.30   # 1회 매매 최대 비중

    # 전략
    asymmetric_mode: bool = True
    paired_exit: bool = True         # 페어링 매도: 진입 전략의 SELL만 허용
    max_single_coin_pct: float = 0.40
    rebalancing_enabled: bool = True
    rebalancing_target_pct: float = 0.35

    # 로테이션 (서지 매수)
    rotation_enabled: bool = True
    rotation_coins: list[str] = field(default_factory=list)
    surge_threshold: float = 3.0
    rotation_cooldown_sec: int = 7200
    stablecoins: set[str] = field(default_factory=lambda: {"USDT/KRW", "USDC/KRW", "DAI/KRW", "TUSD/KRW"})
    min_quote_volume: float = 1e9      # 최소 24h 거래대금

    @property
    def quote_suffix(self) -> str:
        return f"/{self.quote_currency}"

    @property
    def btc_symbol(self) -> str:
        return f"BTC/{self.quote_currency}"

    @classmethod
    def from_app_config(cls, app_config, exchange_name: str) -> "EngineConfig":
        """AppConfig에서 거래소별 EngineConfig 자동 생성."""
        if exchange_name == "binance_spot":
            bst = app_config.binance_spot_trading
            bc = app_config.binance
            return cls(
                exchange_name=exchange_name,
                mode=bst.mode,
                quote_currency="USDT",
                tracked_coins=list(bc.tracked_coins),
                evaluation_interval_sec=bst.evaluation_interval_sec,
                min_combined_confidence=bst.min_combined_confidence,
                daily_buy_limit=bst.daily_buy_limit,
                max_daily_coin_buys=bst.max_daily_coin_buys,
                min_trade_interval_sec=bst.cooldown_after_buy_sec,
                cooldown_after_sell_sec=bst.cooldown_after_sell_sec,
                min_order_amount=5.0,
                fee_margin=1.002,
                min_fallback_amount=10.0,
                max_trade_size_pct=bst.max_trade_size_pct,
                asymmetric_mode=app_config.trading.asymmetric_mode,
                paired_exit=app_config.trading.paired_exit,
                max_single_coin_pct=app_config.risk.max_single_coin_pct,
                rebalancing_enabled=app_config.risk.rebalancing_enabled,
                rebalancing_target_pct=app_config.risk.rebalancing_target_pct,
                rotation_enabled=bst.rotation_enabled,
                rotation_coins=[],
                surge_threshold=app_config.trading.surge_threshold,
                rotation_cooldown_sec=app_config.trading.rotation_cooldown_sec,
                stablecoins={"USDC/USDT", "DAI/USDT", "TUSD/USDT", "BUSD/USDT", "FDUSD/USDT"},
                min_quote_volume=5e6,
            )
        elif exchange_name == "bithumb":
            tc = app_config.trading
            return cls(
                exchange_name=exchange_name,
                mode=tc.mode,
                quote_currency="KRW",
                tracked_coins=list(tc.tracked_coins),
                evaluation_interval_sec=tc.evaluation_interval_sec,
                min_combined_confidence=tc.min_combined_confidence,
                daily_buy_limit=tc.daily_buy_limit,
                max_daily_coin_buys=tc.max_daily_coin_buys,
                min_trade_interval_sec=tc.min_trade_interval_sec,
                cooldown_after_sell_sec=tc.cooldown_after_sell_sec,
                min_order_amount=5000,
                fee_margin=1.003,
                min_fallback_amount=5000,
                max_trade_size_pct=app_config.risk.max_trade_size_pct,
                asymmetric_mode=tc.asymmetric_mode,
                paired_exit=tc.paired_exit,
                max_single_coin_pct=app_config.risk.max_single_coin_pct,
                rebalancing_enabled=app_config.risk.rebalancing_enabled,
                rebalancing_target_pct=app_config.risk.rebalancing_target_pct,
                rotation_enabled=tc.rotation_enabled,
                rotation_coins=list(tc.rotation_coins),
                surge_threshold=tc.surge_threshold,
                rotation_cooldown_sec=tc.rotation_cooldown_sec,
            )
        elif "binance" in exchange_name:
            # binance_futures 등 — 선물 엔진은 자체 설정 사용, 기본값만 세팅
            bt = app_config.binance_trading
            bc = app_config.binance
            return cls(
                exchange_name=exchange_name,
                mode=bt.mode,
                quote_currency="USDT",
                tracked_coins=list(bc.tracked_coins),
                evaluation_interval_sec=bt.evaluation_interval_sec,
                min_combined_confidence=bt.min_combined_confidence,
                daily_buy_limit=bt.daily_buy_limit,
                max_daily_coin_buys=bt.max_daily_coin_buys,
                min_trade_interval_sec=bt.min_trade_interval_sec,
                cooldown_after_sell_sec=bt.min_trade_interval_sec,
                min_order_amount=5.0,
                fee_margin=1.002,
                min_fallback_amount=10.0,
                max_trade_size_pct=bt.max_trade_size_pct,
                asymmetric_mode=False,
                max_single_coin_pct=app_config.risk.max_single_coin_pct,
                rebalancing_enabled=False,
                rotation_enabled=False,
                stablecoins={"USDC/USDT", "DAI/USDT", "TUSD/USDT", "BUSD/USDT", "FDUSD/USDT"},
                min_quote_volume=5e6,
            )
        else:
            raise ValueError(f"Unknown exchange: {exchange_name}")


@dataclass
class PositionTracker:
    """In-memory state for SL/TP/trailing stop tracking.

    extreme_price: 롱=최고가(peak), 숏=최저가(trough). 트레일링 스탑 기준점.
    DB 컬럼 Position.highest_price와 매핑 (하위 호환).
    """
    entry_price: float
    extreme_price: float
    stop_loss_pct: float = 5.0       # 동적 SL % (Optuna binance 최적화 2026-03-13)
    take_profit_pct: float = 14.0
    trailing_activation_pct: float = 3.0
    trailing_stop_pct: float = 1.5
    trailing_active: bool = False     # 트레일링 활성 여부
    is_surge: bool = False            # 서지 코인 여부
    max_hold_hours: float = 0        # 최대 보유 시간 (0=무제한)
    entered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class TradingEngine:
    """Main trading engine orchestrator with SL/TP/trailing/dynamic-SL."""

    def __init__(
        self,
        config: AppConfig,
        exchange: ExchangeAdapter,
        market_data: MarketDataService,
        order_manager: OrderManager,
        portfolio_manager: PortfolioManager,
        combiner: SignalCombiner,
        agent_coordinator=None,
        exchange_name: str = "bithumb",
        tracked_coins: list[str] | None = None,
        evaluation_interval_sec: int | None = None,
        engine_config: EngineConfig | None = None,
    ):
        self._config = config
        self._exchange = exchange
        self._market_data = market_data
        self._order_manager = order_manager
        self._portfolio_manager = portfolio_manager
        self._combiner = combiner
        self._agent_coordinator = agent_coordinator
        self._exchange_name = exchange_name

        # EngineConfig: 명시적으로 전달되면 사용, 아니면 자동 생성
        if engine_config:
            self._ec = engine_config
        else:
            self._ec = EngineConfig.from_app_config(config, exchange_name)

        # tracked_coins/eval_interval 오버라이드 (하위 호환)
        if tracked_coins:
            self._ec.tracked_coins = list(tracked_coins)
        if evaluation_interval_sec:
            self._ec.evaluation_interval_sec = evaluation_interval_sec

        self._tracked_coins = self._ec.tracked_coins or None
        self._eval_interval = self._ec.evaluation_interval_sec

        self._strategies: dict[str, BaseStrategy] = {}
        self._is_running = False
        self._paused_coins: set[str] = set()
        self._suppressed_coins: set[str] = set()
        self._last_trade_time: dict[str, datetime] = {}
        self._last_sell_time: dict[str, datetime] = {}  # 매도 시각 (재매수 대기용)
        self._daily_buy_count = 0                       # 일일 총 매수 횟수
        self._daily_coin_buy_count: dict[str, int] = {} # 코인별 일일 매수 횟수
        self._daily_trade_count = 0                     # 레거시 (호환)
        self._daily_reset_date = datetime.now(timezone.utc).date()

        # SL/TP/trailing stop tracking
        self._position_trackers: dict[str, PositionTracker] = {}
        self._eval_error_counts: dict[str, int] = {}  # 연속 평가 오류 카운터
        self._MAX_EVAL_ERRORS = 3  # 3회 연속 실패 → 강제 청산
        self._last_stop_event_time: dict[str, datetime] = {}  # SL/TP/trailing 이벤트 스팸 방지
        self._market_state: str = MarketState.SIDEWAYS.value
        self._market_confidence: float = 0.5
        self._market_state_updated: datetime | None = None
        self._bearish_clear_time: datetime | None = None  # 히스테리시스: bearish 해제 시점

        # 거래량 급등 로테이션 상태
        self._last_rotation_time: datetime | None = None
        self._current_surge_symbol: str | None = None
        self._all_surge_scores: dict[str, float] = {}
        self._last_surge_scan_time: datetime | None = None

        # 동적 로테이션 코인 (거래대금 상위 자동 선정)
        self._dynamic_rotation_coins: list[str] = []
        self._rotation_coins_updated: datetime | None = None

        # 리밸런싱 쿨다운 (코인별 마지막 리밸런싱 시각)
        self._last_rebalance: dict[str, datetime] = {}

        # 매도 카운터 → N회마다 매매 회고 트리거
        self._sells_since_review: int = 0
        self._REVIEW_TRIGGER_SELLS: int = 5

        # WebSocket broadcast callback
        self._broadcast_callback = None

        # Self-healing recovery manager (main.py에서 set_recovery_manager()로 주입)
        self._recovery_manager = None

        # 교차 거래소 포지션 전환용 (main.py에서 set_engine_registry()로 주입)
        self._engine_registry = None

    @property
    def tracked_coins(self) -> list[str]:
        """추적 코인 목록 (외부 접근용)."""
        return list(self._ec.tracked_coins)

    @property
    def _min_order_amount(self) -> float:
        return self._ec.min_order_amount

    @property
    def _fee_margin(self) -> float:
        return self._ec.fee_margin

    @property
    def _min_fallback_amount(self) -> float:
        return self._ec.min_fallback_amount

    def set_broadcast_callback(self, callback) -> None:
        self._broadcast_callback = callback

    def set_engine_registry(self, registry) -> None:
        """교차 거래소 포지션 전환을 위한 엔진 레지스트리 주입."""
        self._engine_registry = registry

    # 교차 거래소 포지션 전환 최소 신뢰도
    CROSS_FLIP_MIN_CONFIDENCE = 0.65

    async def close_position_for_cross_exchange(self, symbol: str, reason: str) -> bool:
        """다른 엔진의 요청으로 포지션 청산. 자체 세션 사용. 성공 시 True."""
        sf = get_session_factory()
        try:
            async with sf() as session:
                result = await session.execute(
                    select(Position).where(
                        Position.symbol == symbol,
                        Position.quantity > 0,
                        Position.exchange == self._exchange_name,
                    )
                )
                position = result.scalar_one_or_none()
                if not position:
                    return False

                price = await self._market_data.get_current_price(symbol)
                if price <= 0:
                    logger.warning("cross_close_no_price", symbol=symbol)
                    return False

                await self._execute_stop_sell(session, symbol, position, price, reason)
                await session.commit()
                logger.info(
                    "cross_exchange_position_closed",
                    symbol=symbol, exchange=self._exchange_name,
                    price=price, reason=reason,
                )
                return True
        except Exception as e:
            logger.error("cross_exchange_close_failed", symbol=symbol, error=str(e))
            return False

    async def _on_sell_completed(self) -> None:
        """매도 완료 시 카운터 증가 → N회마다 매매 회고 트리거."""
        self._sells_since_review += 1
        if (self._sells_since_review >= self._REVIEW_TRIGGER_SELLS
                and self._agent_coordinator):
            self._sells_since_review = 0
            try:
                asyncio.create_task(self._agent_coordinator.run_trade_review())
                logger.info("trade_review_triggered_by_sells",
                            exchange=self._exchange_name,
                            trigger=self._REVIEW_TRIGGER_SELLS)
            except Exception as e:
                logger.warning("trade_review_trigger_failed", error=str(e))

    async def _save_tracker_to_db(self, session: AsyncSession, symbol: str, tracker: PositionTracker) -> None:
        """PositionTracker 상태를 DB Position 레코드에 저장."""
        result = await session.execute(
            select(Position).where(
                Position.symbol == symbol,
                Position.exchange == self._exchange_name,
            )
        )
        pos = result.scalar_one_or_none()
        if pos:
            pos.stop_loss_pct = tracker.stop_loss_pct
            pos.take_profit_pct = tracker.take_profit_pct
            pos.trailing_activation_pct = tracker.trailing_activation_pct
            pos.trailing_stop_pct = tracker.trailing_stop_pct
            pos.trailing_active = tracker.trailing_active
            # 방향별 extreme_price 저장: 롱 → highest_price, 숏 → lowest_price
            # 반대 컬럼을 None으로 클리어해 롱→숏(또는 숏→롱) 전환 시 스테일 값 방지
            if _effective_direction(pos.direction) == "short":
                pos.lowest_price = tracker.extreme_price
                pos.highest_price = None
            else:
                pos.highest_price = tracker.extreme_price
                pos.lowest_price = None
            pos.max_hold_hours = tracker.max_hold_hours
            await session.flush()

    async def initialize(self) -> None:
        """Initialize strategies and load configurations."""
        import strategies.volatility_breakout
        import strategies.ma_crossover
        import strategies.rsi_strategy
        import strategies.macd_crossover
        import strategies.bollinger_rsi
        import strategies.stochastic_rsi
        import strategies.obv_divergence
        import strategies.supertrend
        import strategies.bnf_deviation
        import strategies.cis_momentum
        import strategies.larry_williams
        import strategies.donchian_channel
        import strategies.bb_squeeze

        self._strategies = StrategyRegistry.create_all()

        # 거래소 유형별 전략 분기:
        # - 선물(binance_futures): 기존 6전략 유지 (검증 완료)
        # - 현물(bithumb/binance_spot): 신규 3전략 (추세추종, 540일 PF 1.63)
        is_futures = "futures" in self._exchange_name

        # 항상 제거: Grid/DCA/volatility_breakout/supertrend
        always_excluded = {"grid_trading", "dca_momentum", "volatility_breakout",
                           "supertrend"}

        if is_futures:
            # 선물: 기존 6전략 유지, 신규 전략 제거
            futures_excluded = always_excluded | {
                "bnf_deviation", "cis_momentum", "larry_williams", "donchian_channel",
            }
            for excluded in futures_excluded:
                self._strategies.pop(excluded, None)
        else:
            # 현물: 신규 4전략 사용, 기존 6전략 + 선물전용 제거
            spot_excluded = always_excluded | {
                "ma_crossover", "rsi", "macd_crossover",
                "bollinger_rsi", "stochastic_rsi", "obv_divergence",
                "bb_squeeze",
            }
            for excluded in spot_excluded:
                self._strategies.pop(excluded, None)
            # 현물 combiner 가중치를 신규 3전략으로 교체
            from strategies.combiner import SignalCombiner
            self._combiner.weights = SignalCombiner.SPOT_WEIGHTS.copy()

        logger.info(
            "engine_initialized",
            strategies=list(self._strategies.keys()),
            mode=self._ec.mode,
        )

    async def _restore_trade_timestamps(self) -> None:
        """DB에서 매매 타임스탬프 + 일일 매수 카운터 복원 (재시작 시 쿨다운/제한 유지)."""
        from db.session import get_session_factory
        session_factory = get_session_factory()
        async with session_factory() as session:
            result = await session.execute(
                select(Position).where(Position.exchange == self._exchange_name)
            )
            for pos in result.scalars().all():
                if pos.quantity and pos.quantity > 0:
                    if pos.last_trade_at:
                        self._last_trade_time[pos.symbol] = pos.last_trade_at
                    if pos.last_sell_at:
                        self._last_sell_time[pos.symbol] = pos.last_sell_at
                elif pos.last_sell_at:
                    # 청산된 포지션도 쿨다운 내이면 복원 (재시작 시 쿨다운 유지)
                    elapsed = (datetime.now(timezone.utc) - pos.last_sell_at).total_seconds()
                    cooldown = getattr(self._ec, 'cooldown_after_sell_sec', 518400)
                    if elapsed < cooldown:
                        self._last_sell_time[pos.symbol] = pos.last_sell_at
                        logger.debug("restored_closed_cooldown",
                                     symbol=pos.symbol, remaining_h=round((cooldown - elapsed) / 3600, 1))
                    else:
                        logger.debug("skip_expired_cooldown",
                                     symbol=pos.symbol, last_sell_at=pos.last_sell_at)

            # 일일 매수 카운터 복원: 오늘 buy 주문 수 (UTC 기준)
            today = datetime.now(timezone.utc).date()
            today_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
            buy_orders = await session.execute(
                select(Order.symbol, func.count()).where(
                    Order.exchange == self._exchange_name,
                    Order.side == "buy",
                    Order.created_at >= today_start,
                ).group_by(Order.symbol)
            )
            total_buys = 0
            for symbol, count in buy_orders.all():
                self._daily_coin_buy_count[symbol] = count
                total_buys += count
            self._daily_buy_count = total_buys
            self._daily_reset_date = today
            if total_buys:
                logger.info("daily_buy_count_restored",
                            total=total_buys, coins=dict(self._daily_coin_buy_count))

        restored = len(self._last_trade_time)
        if restored:
            logger.info("trade_timestamps_restored", count=restored)

    _FAST_SL_INTERVAL = 30  # 현물 빠른 SL/TP 체크 간격 (초)

    async def _check_downtime_stops(self) -> None:
        """서버 시작 직후 다운타임 중 SL/TP 초과 포지션 즉시 체크 및 처리."""
        from db.session import get_session_factory
        try:
            sf = get_session_factory()
            async with sf() as session:
                result = await session.execute(
                    select(Position).where(
                        Position.quantity > 0,
                        Position.exchange == self._exchange_name,
                    )
                )
                positions = list(result.scalars().all())
                if not positions:
                    return

                triggered = 0
                for pos in positions:
                    try:
                        sold = await self._check_stop_conditions(session, pos.symbol, pos)
                        if sold:
                            triggered += 1
                            logger.warning("downtime_stop_triggered",
                                         symbol=pos.symbol, exchange=self._exchange_name)
                        await session.commit()
                    except Exception as e:
                        logger.debug("downtime_stop_check_error",
                                   symbol=pos.symbol, error=str(e))

                if triggered:
                    logger.warning("downtime_stops_executed",
                                 exchange=self._exchange_name, count=triggered)
                    await emit_event(
                        "warning", "engine",
                        f"다운타임 SL/TP 도달 포지션 {triggered}건 처리",
                        metadata={"exchange": self._exchange_name, "count": triggered},
                    )
                else:
                    logger.info("downtime_stops_all_clear",
                              exchange=self._exchange_name, positions=len(positions))
        except Exception as e:
            logger.warning("downtime_stop_check_failed", error=str(e))

    async def start(self) -> None:
        """Start the trading engine main loop."""
        self._is_running = True
        self._tasks: list[asyncio.Task] = []
        await self._restore_trade_timestamps()
        logger.info("engine_started")
        await emit_event("info", "engine", f"{self._exchange_name} 엔진 시작", metadata={"mode": self._ec.mode, "exchange": self._exchange_name})

        # 다운타임 중 SL/TP 초과 포지션 즉시 체크
        await self._check_downtime_stops()

        # 전략 평가 루프 + 빠른 SL/TP 체크 루프 병렬 실행
        self._tasks = [
            asyncio.create_task(self._strategy_loop(), name=f"{self._exchange_name}_strategy"),
            asyncio.create_task(self._fast_stop_check_loop(), name=f"{self._exchange_name}_fast_sl"),
        ]
        await asyncio.gather(*self._tasks, return_exceptions=True)

    _MAX_CONSECUTIVE_ERRORS = 5
    _ERROR_PAUSE_SEC = 60

    async def _strategy_loop(self) -> None:
        """기존 전략 평가 루프 (5분 주기). 연속 에러 시 일시 중지."""
        consecutive_errors = 0
        while self._is_running:
            try:
                await self._evaluation_cycle()
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                logger.error("engine_cycle_error", error=str(e), consecutive=consecutive_errors, exc_info=True)
                if consecutive_errors >= self._MAX_CONSECUTIVE_ERRORS:
                    logger.warning("engine_pausing_after_errors", count=consecutive_errors, pause_sec=self._ERROR_PAUSE_SEC)
                    await emit_event("warning", "engine", f"연속 {consecutive_errors}회 에러 — {self._ERROR_PAUSE_SEC}초 대기", metadata={"exchange": self._exchange_name})
                    await asyncio.sleep(self._ERROR_PAUSE_SEC)
                    consecutive_errors = 0
            await asyncio.sleep(self._ec.evaluation_interval_sec)

    async def _fast_stop_check_loop(self) -> None:
        """보유 포지션 SL/TP/trailing 빠른 체크 (30초 주기, 가격만 조회)."""
        from db.session import get_session_factory
        from sqlalchemy.orm import selectinload
        while self._is_running:
            await asyncio.sleep(self._FAST_SL_INTERVAL)
            try:
                trackers = dict(self._position_trackers)
                if not trackers:
                    continue
                symbols = list(trackers.keys())
                session_factory = get_session_factory()
                async with session_factory() as session:
                    # Batch fetch all positions at once (N+1 → 1 query)
                    result = await session.execute(
                        select(Position).where(
                            Position.symbol.in_(symbols),
                            Position.quantity > 0,
                            Position.exchange == self._exchange_name,
                        )
                    )
                    positions = {p.symbol: p for p in result.scalars().all()}

                    for symbol in symbols:
                        try:
                            position = positions.get(symbol)
                            if not position:
                                continue
                            await self._check_stop_conditions(session, symbol, position)
                            await session.commit()
                        except Exception as e:
                            logger.debug("fast_stop_check_error", symbol=symbol, error=str(e))
            except Exception as e:
                logger.warning("fast_stop_loop_error", error=str(e))

    async def stop(self) -> None:
        """Stop the trading engine gracefully — cancel all running tasks."""
        self._is_running = False
        for task in getattr(self, '_tasks', []):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._tasks = []
        # stale 인메모리 dict 정리 (tracked_coins에 없는 항목)
        live_coins = set(self._ec.tracked_coins)
        for d in (self._eval_error_counts, self._last_sell_time, self._last_trade_time):
            stale = [k for k in d if k not in live_coins]
            for k in stale:
                d.pop(k, None)
        logger.info("engine_stopping")
        await emit_event("info", "engine", f"{self._exchange_name} 엔진 중지", metadata={"exchange": self._exchange_name})

    def pause_buying(self, coins: list[str]) -> None:
        self._paused_coins.update(coins)
        logger.warning("buying_paused", coins=coins)
        asyncio.ensure_future(emit_event("warning", "risk", "매수 일시중지", metadata={"coins": coins}))

    def suppress_buys(self, coins: list[str]) -> None:
        self._suppressed_coins.update(coins)

    def resume_buying(self, coins: list[str] | None = None) -> None:
        if coins:
            self._paused_coins -= set(coins)
            self._suppressed_coins -= set(coins)
        else:
            self._paused_coins.clear()
            self._suppressed_coins.clear()

    def set_recovery_manager(self, recovery_manager) -> None:
        """RecoveryManager 주입 (main.py lifespan에서 호출)."""
        self._recovery_manager = recovery_manager

    async def _execute_with_retry(self, operation, context: str, symbol: str):
        """분류 기반 재시도: transient→백오프, resource→잔고복구, permanent→억제.

        Returns
        -------
        결과 또는 None (최종 실패 시).
        """
        last_error = None
        for attempt in range(3):
            try:
                return await operation()
            except Exception as e:
                last_error = e
                classified = classify_error(e, context, symbol)

                if not classified.retryable or attempt >= classified.max_retries:
                    await emit_event(
                        "error", "engine",
                        f"주문 실패 (최종): {symbol}",
                        detail=f"{context}: {e}",
                        metadata={
                            "symbol": symbol, "context": context,
                            "category": classified.category.value,
                            "attempts": attempt + 1,
                            "exchange": self._exchange_name,
                        },
                    )
                    return None

                # 복구 시도
                if self._recovery_manager:
                    recovery = await self._recovery_manager.attempt_recovery(classified)
                    logger.warning(
                        f"{context}_retry",
                        symbol=symbol,
                        attempt=attempt + 1,
                        category=classified.category.value,
                        recovery=recovery.action_taken,
                        exchange=self._exchange_name,
                    )
                else:
                    logger.warning(
                        f"{context}_retry_no_recovery",
                        symbol=symbol,
                        attempt=attempt + 1,
                        exchange=self._exchange_name,
                    )

                # 백오프 대기
                if classified.backoff_base > 0:
                    wait = classified.backoff_base * (2 ** attempt)
                    await asyncio.sleep(wait)

        return None

    def _reset_daily_counter(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._daily_reset_date:
            self._daily_buy_count = 0
            self._daily_coin_buy_count.clear()
            self._daily_trade_count = 0
            self._daily_reset_date = today
            # 복구 카운터도 리셋
            if self._recovery_manager:
                self._recovery_manager.reset_daily()

    def _can_trade(self, symbol: str, side: str = "buy") -> tuple[bool, str]:
        """Check anti-overtrading constraints.

        매도(sell)는 일일 제한/코인별 제한을 받지 않음 (손절·익절은 무조건 실행).
        매수(buy)만 일일 총 매수 상한 + 코인별 매수 상한 적용.
        코인당 최소 거래 간격 및 리스크 에이전트 일시중지는 매수에만 적용.
        """
        self._reset_daily_counter()

        if side == "buy":
            # 일일 총 매수 상한
            if self._daily_buy_count >= self._ec.daily_buy_limit:
                return False, f"Daily buy limit reached ({self._ec.daily_buy_limit})"

            # 코인별 일일 매수 상한
            coin_buys = self._daily_coin_buy_count.get(symbol, 0)
            if coin_buys >= self._ec.max_daily_coin_buys:
                return False, f"Coin daily buy limit reached ({symbol}: {coin_buys}/{self._ec.max_daily_coin_buys})"

            # 코인당 최소 거래 간격
            last = self._last_trade_time.get(symbol)
            if last:
                elapsed = (datetime.now(timezone.utc) - last).total_seconds()
                if elapsed < self._ec.min_trade_interval_sec:
                    remaining = self._ec.min_trade_interval_sec - elapsed
                    return False, f"Coin cooldown: {remaining:.0f}s remaining"

            # 매도 후 재매수 대기 (당일 왕복 방지)
            last_sell = self._last_sell_time.get(symbol)
            if last_sell:
                sell_elapsed = (datetime.now(timezone.utc) - last_sell).total_seconds()
                washout = self._ec.cooldown_after_sell_sec
                if sell_elapsed < washout:
                    remaining = washout - sell_elapsed
                    return False, f"Post-sell washout: {remaining:.0f}s remaining"

            # 리스크 에이전트에 의한 매수 중지
            if symbol in self._paused_coins:
                return False, "Buying paused by risk agent"

        return True, "OK"

    # ── 시장 상태 감지 (5요소 스코어링 — 에이전트 방식) ────────────────

    def _detect_market_state(self, df: pd.DataFrame) -> tuple[str, float]:
        """5요소 스코어링 시장 상태 감지.

        Factors: Price vs SMA20, SMA20/SMA50 정렬, RSI, 7일 가격변동, 거래량/SMA20.
        Returns: (state_str, confidence)
        """
        if df is None or len(df) < 60:
            return MarketState.SIDEWAYS.value, 0.3

        scores = {
            MarketState.STRONG_UPTREND: 0.0,
            MarketState.UPTREND: 0.0,
            MarketState.SIDEWAYS: 0.0,
            MarketState.DOWNTREND: 0.0,
        }

        row = df.iloc[-1]
        current_price = float(row["close"])

        # 1. Price vs SMA20 거리
        sma20 = row.get("sma_20")
        if sma20 is not None and not (isinstance(sma20, float) and pd.isna(sma20)):
            sma20 = float(sma20)
            if sma20 > 0:
                dist_pct = (current_price - sma20) / sma20
                if dist_pct > 0.05:
                    scores[MarketState.STRONG_UPTREND] += 2
                elif dist_pct > 0.01:
                    scores[MarketState.UPTREND] += 1.5
                elif dist_pct < -0.05:
                    scores[MarketState.DOWNTREND] += 2
                elif dist_pct < -0.01:
                    scores[MarketState.DOWNTREND] += 1.5
                else:
                    scores[MarketState.SIDEWAYS] += 1

        # 2. SMA20 vs SMA50 정렬
        sma50 = row.get("sma_50")
        if (sma20 is not None and sma50 is not None
                and not (isinstance(sma20, float) and pd.isna(sma20))
                and not (isinstance(sma50, float) and pd.isna(sma50))):
            sma50_f = float(sma50)
            sma20_f = float(sma20) if not isinstance(sma20, float) else sma20
            if sma20_f > sma50_f:
                scores[MarketState.UPTREND] += 1
                scores[MarketState.STRONG_UPTREND] += 0.5
            else:
                scores[MarketState.DOWNTREND] += 1

        # 3. RSI
        rsi = row.get("rsi_14")
        if rsi is not None and not (isinstance(rsi, float) and pd.isna(rsi)):
            rsi = float(rsi)
            if rsi > 70:
                scores[MarketState.STRONG_UPTREND] += 1
            elif rsi > 55:
                scores[MarketState.UPTREND] += 1
            elif rsi < 30:
                scores[MarketState.DOWNTREND] += 1.5
            elif rsi < 45:
                scores[MarketState.DOWNTREND] += 1
            else:
                scores[MarketState.SIDEWAYS] += 1.5

        # 4. 7일 가격변동 (4h=42캔들)
        if len(df) > 1:
            td = (df.index[1] - df.index[0]).total_seconds() / 3600
            candles_per_7d = int(7 * 24 / td) if td > 0 else 42
            lookback_idx = max(0, len(df) - 1 - candles_per_7d)
            week_ago_price = float(df.iloc[lookback_idx]["close"])
            if week_ago_price > 0:
                week_change_pct = (current_price - week_ago_price) / week_ago_price * 100
                if week_change_pct > 10:
                    scores[MarketState.STRONG_UPTREND] += 2
                elif week_change_pct > 3:
                    scores[MarketState.UPTREND] += 1.5
                elif week_change_pct < -10:
                    scores[MarketState.DOWNTREND] += 2
                elif week_change_pct < -3:
                    scores[MarketState.DOWNTREND] += 1.5
                else:
                    scores[MarketState.SIDEWAYS] += 2

        # 5. 거래량 / volume_sma_20 (캔들 방향 반영)
        vol_sma = row.get("volume_sma_20")
        cur_vol = row.get("volume")
        if (vol_sma is not None and cur_vol is not None
                and not (isinstance(vol_sma, float) and pd.isna(vol_sma))
                and not (isinstance(cur_vol, float) and pd.isna(cur_vol))):
            vol_sma_f = float(vol_sma)
            if vol_sma_f > 0:
                vol_ratio = float(cur_vol) / vol_sma_f
                if vol_ratio > 2.0:
                    candle_open = row.get("open")
                    if candle_open is not None and float(candle_open) > 0:
                        if current_price >= float(candle_open):
                            scores[MarketState.STRONG_UPTREND] += 0.5
                        else:
                            scores[MarketState.DOWNTREND] += 0.5
                    else:
                        scores[MarketState.STRONG_UPTREND] += 0.25
                        scores[MarketState.DOWNTREND] += 0.25

        # 최고 스코어 상태 결정
        best_state = max(scores, key=scores.get)
        total = sum(scores.values())
        confidence = scores[best_state] / total if total > 0 else 0.3

        # CRASH 매핑: downtrend + 높은 신뢰도 + 높은 raw score
        if best_state == MarketState.DOWNTREND and confidence >= 0.55 and scores[MarketState.DOWNTREND] >= 5.0:
            return MarketState.CRASH.value, round(confidence, 2)

        return best_state.value, round(confidence, 2)

    def _calc_dynamic_sl(self, df: pd.DataFrame, price: float, market_state: str) -> float:
        """ATR + 시장 상태 기반 동적 손절 % 계산."""
        atr_mult, floor_pct, cap_pct = _DYNAMIC_SL_PROFILES.get(
            market_state, _DEFAULT_SL_PROFILE,
        )
        if df is None or len(df) < 14:
            return cap_pct

        atr_val = df.iloc[-1].get("atr_14")
        if atr_val is None or (isinstance(atr_val, float) and pd.isna(atr_val)) or price <= 0:
            return cap_pct

        atr_pct = float(atr_val) / price * 100
        raw_sl = atr_pct * atr_mult
        return max(floor_pct, min(raw_sl, cap_pct))

    # ── 주기적 시장 상태 업데이트 ──────────────────────────────────

    async def _maybe_update_market_state(self) -> None:
        """30분마다 시장 상태 재평가 (BTC 기준). 보유 포지션 SL도 재조정."""
        now = datetime.now(timezone.utc)
        if (self._market_state_updated
                and (now - self._market_state_updated).total_seconds() < 1800):
            return

        try:
            btc_symbol = self._ec.btc_symbol
            df = await self._market_data.get_candles(btc_symbol, "4h", 200)
            new_state, new_confidence = self._detect_market_state(df)
            self._market_confidence = new_confidence

            if new_state != self._market_state:
                old_state = self._market_state

                # 에이전트의 마지막 분석도 참고 로깅
                agent_state = None
                if self._agent_coordinator and self._agent_coordinator.last_market_analysis:
                    agent_state = self._agent_coordinator.last_market_analysis.state.value

                logger.info(
                    "market_state_changed",
                    old=old_state,
                    new=new_state,
                    confidence=new_confidence,
                    agent_state=agent_state,
                )
                await emit_event(
                    "info", "strategy",
                    f"시장 상태: {old_state}→{new_state} (신뢰도 {new_confidence:.0%})",
                    metadata={
                        "old": old_state, "new": new_state,
                        "confidence": new_confidence, "agent_state": agent_state,
                    },
                )
                self._combiner.apply_market_state(new_state)

                # 히스테리시스: bearish → non-bearish 전환 시 타이머 시작
                if (old_state in ("crash", "downtrend")
                        and new_state not in ("crash", "downtrend")):
                    self._bearish_clear_time = now
                    logger.info("bearish_clear_hysteresis_start",
                                old=old_state, new=new_state)

                # 에이전트 분석 결과도 즉시 동기화 (프론트엔드 불일치 방지)
                if (self._agent_coordinator
                        and self._agent_coordinator.last_market_analysis):
                    try:
                        self._agent_coordinator.last_market_analysis.state = MarketState(new_state)
                    except ValueError:
                        pass

                # 보유 중 포지션 동적 SL 재조정 (백테스트 동일)
                for symbol, tracker in self._position_trackers.items():
                    try:
                        sym_df = await self._market_data.get_candles(symbol, "4h", 200)
                        price = await self._market_data.get_current_price(symbol)
                        old_sl = tracker.stop_loss_pct
                        tracker.stop_loss_pct = self._calc_dynamic_sl(sym_df, price, new_state)
                        if old_sl != tracker.stop_loss_pct:
                            logger.info(
                                "dynamic_sl_recalculated",
                                symbol=symbol,
                                old_sl=round(old_sl, 2),
                                new_sl=round(tracker.stop_loss_pct, 2),
                                market_state=new_state,
                            )
                    except Exception as e:
                        logger.debug("sl_recalc_failed", symbol=symbol, error=str(e))

            self._market_state = new_state
            self._market_state_updated = now
        except Exception as e:
            logger.warning("market_state_update_failed", error=str(e))

    # ── SL/TP/Trailing Stop 체크 ──────────────────────────────────

    async def _check_stop_conditions(
        self, session: AsyncSession, symbol: str, position: Position
    ) -> bool:
        """포지션의 SL/TP/trailing stop 조건 체크. 매도 시 True 반환."""
        tracker = self._position_trackers.get(symbol)
        if not tracker:
            # 트래커 없으면 DB에서 복원 (재시작 후)
            if position.stop_loss_pct is not None:
                # DB에 저장된 트래커 값으로 복원
                tracker = PositionTracker(
                    entry_price=position.average_buy_price,
                    extreme_price=position.highest_price or position.average_buy_price,
                    stop_loss_pct=position.stop_loss_pct,
                    take_profit_pct=position.take_profit_pct or 10.0,
                    trailing_activation_pct=position.trailing_activation_pct or 5.0,
                    trailing_stop_pct=position.trailing_stop_pct or 4.0,
                    trailing_active=position.trailing_active or False,
                    is_surge=position.is_surge or False,
                    max_hold_hours=position.max_hold_hours or 0,
                )
                if position.entered_at:
                    ea = position.entered_at
                    if ea.tzinfo is None:
                        ea = ea.replace(tzinfo=timezone.utc)
                    tracker.entered_at = ea
                logger.info("tracker_restored_from_db", symbol=symbol,
                            sl=round(tracker.stop_loss_pct, 2),
                            trailing_active=tracker.trailing_active,
                            highest_price=round(tracker.extreme_price, 2))
            elif getattr(position, 'is_surge', False):
                # 서지 코인 → 서지 프로필 복원 (마이그레이션 전 포지션)
                tracker = PositionTracker(
                    entry_price=position.average_buy_price,
                    extreme_price=position.average_buy_price,
                    stop_loss_pct=4.0,
                    take_profit_pct=8.0,
                    trailing_activation_pct=1.5,
                    trailing_stop_pct=2.0,
                    is_surge=True,
                    max_hold_hours=48,
                )
                if position.entered_at:
                    ea = position.entered_at
                    if ea.tzinfo is None:
                        ea = ea.replace(tzinfo=timezone.utc)
                    tracker.entered_at = ea
                logger.info("tracker_restored_surge", symbol=symbol)
            else:
                # 일반 코인 → 동적 SL 계산 (마이그레이션 전 포지션)
                tracker = PositionTracker(
                    entry_price=position.average_buy_price,
                    extreme_price=position.average_buy_price,
                )
                if position.entered_at:
                    ea = position.entered_at
                    if ea.tzinfo is None:
                        ea = ea.replace(tzinfo=timezone.utc)
                    tracker.entered_at = ea
                try:
                    df = await self._market_data.get_candles(symbol, "4h", 200)
                    tracker.stop_loss_pct = self._calc_dynamic_sl(
                        df, position.average_buy_price, self._market_state
                    )
                except Exception:
                    tracker.stop_loss_pct = 3.0
                logger.info("tracker_restored_normal", symbol=symbol, sl=round(tracker.stop_loss_pct, 2))
            self._position_trackers[symbol] = tracker

        # 현재 가격
        try:
            price = await self._market_data.get_current_price(symbol)
        except Exception as e:
            logger.warning("price_fetch_failed_sl_check", symbol=symbol, error=str(e))
            return False

        # 최고가 업데이트 + DB 반영
        tracker_changed = False
        if price > tracker.extreme_price:
            tracker.extreme_price = price
            tracker_changed = True

        entry = tracker.entry_price
        if entry <= 0:
            # fallback: avg_buy_price > 0 이면 사용, 아니면 SL/TP 체크 스킵
            if position.average_buy_price and position.average_buy_price > 0:
                entry = position.average_buy_price
                tracker.entry_price = entry
                logger.warning("tracker_entry_price_recovered", symbol=symbol,
                               source="avg_buy_price", entry=entry)
            else:
                logger.error("tracker_entry_price_unrecoverable", symbol=symbol,
                             entry=entry, price=price, avg_buy=position.average_buy_price)
                return False  # SL/TP 체크 불가 — 잘못된 기준가로 청산 방지
        pnl_pct = (price - entry) / entry * 100 if entry > 0 else 0.0

        sell_reason = None

        # 백테스트 동일 우선순위: 트레일링 활성화 → 트레일링 발동 → SL → TP(트레일링 미활성 시만)

        # 1. 트레일링 활성화 체크
        if (tracker.trailing_activation_pct > 0
                and not tracker.trailing_active
                and pnl_pct >= tracker.trailing_activation_pct):
            tracker.trailing_active = True
            tracker_changed = True

        # 2. 트레일링 스탑 발동
        if tracker.trailing_active and tracker.trailing_stop_pct > 0:
            drawdown_from_peak = (tracker.extreme_price - price) / tracker.extreme_price * 100
            if drawdown_from_peak >= tracker.trailing_stop_pct:
                sell_reason = (
                    f"Trailing Stop: 고점 대비 -{drawdown_from_peak:.2f}% "
                    f"(고점 {tracker.extreme_price:.0f}, 현재 {price:.0f}, 수익 {pnl_pct:+.1f}%)"
                )

        # 3. 손절 (Stop Loss)
        if not sell_reason and pnl_pct <= -tracker.stop_loss_pct:
            sell_reason = f"SL 발동: {pnl_pct:.2f}% (한도 -{tracker.stop_loss_pct:.1f}%)"

        # 4. 익절 — 트레일링 미활성 시에만 (활성 후에는 트레일링이 관리)
        if (not sell_reason
                and not tracker.trailing_active
                and tracker.take_profit_pct > 0
                and pnl_pct >= tracker.take_profit_pct):
            sell_reason = f"TP 발동: +{pnl_pct:.2f}% (목표 +{tracker.take_profit_pct:.1f}%)"

        # 5. 시간 기반 강제 청산 (서지 코인 등)
        if not sell_reason and tracker.max_hold_hours > 0:
            held_hours = (datetime.now(timezone.utc) - tracker.entered_at).total_seconds() / 3600
            if held_hours >= tracker.max_hold_hours:
                sell_reason = f"보유 시간 초과: {held_hours:.1f}h (한도 {tracker.max_hold_hours:.0f}h, 수익 {pnl_pct:+.1f}%)"

        # trailing_active/extreme_price 변경 시 DB 반영
        if tracker_changed and not sell_reason:
            await self._save_tracker_to_db(session, symbol, tracker)

        if sell_reason:
            # 코인 이름 포함 + 이벤트 스팸 방지 (5분 쿨다운)
            coin_label = symbol.split("/")[0]
            now = datetime.now(timezone.utc)
            last_event = self._last_stop_event_time.get(symbol)
            if not last_event or (now - last_event).total_seconds() >= 300:
                logger.info(
                    "stop_condition_triggered",
                    symbol=symbol,
                    reason=sell_reason,
                    price=price,
                    entry=entry,
                    pnl_pct=round(pnl_pct, 2),
                )
                await emit_event(
                    "warning", "trade", f"[{coin_label}] {sell_reason}",
                    metadata={"symbol": symbol, "pnl_pct": round(pnl_pct, 2), "price": price, "entry_price": entry},
                )
                self._last_stop_event_time[symbol] = now
            try:
                await self._execute_stop_sell(session, symbol, position, price, sell_reason)
                self._last_stop_event_time.pop(symbol, None)
                return True
            except Exception as e:
                logger.warning("stop_sell_failed", symbol=symbol, reason=sell_reason, error=str(e))
                return False

        return False

    async def _force_close_stuck_position(
        self, session: AsyncSession, symbol: str, last_error: str,
    ) -> None:
        """연속 평가 실패 포지션 강제 매도. 가격 조회 불가 시 DB에서 직접 제거."""
        result = await session.execute(
            select(Position).where(
                Position.symbol == symbol,
                Position.quantity > 0,
                Position.exchange == self._exchange_name,
            )
        )
        position = result.scalar_one_or_none()
        if not position:
            self._eval_error_counts.pop(symbol, None)
            return

        count = self._eval_error_counts.get(symbol, 0)
        logger.warning(
            "force_close_stuck_position",
            symbol=symbol,
            quantity=float(position.quantity),
            consecutive_errors=count,
            last_error=last_error,
        )

        # 1차 시도: 시장가 매도
        try:
            price = await self._market_data.get_current_price(symbol)
            await self._execute_stop_sell(
                session, symbol, position, price,
                f"강제 매도: 연속 {count}회 평가 실패 ({last_error})",
            )
            self._eval_error_counts.pop(symbol, None)
            # 강제 청산은 에러 기반이므로 매도 후 재매수 쿨다운 면제
            self._last_sell_time.pop(symbol, None)
            return
        except Exception as close_err:
            logger.warning("force_close_market_failed", symbol=symbol, error=str(close_err))

        # 2차: 가격 조회 불가 → DB 포지션 0으로 리셋
        entry = position.average_buy_price or 0
        position.quantity = 0
        position.current_price = entry
        position.current_value = 0
        await session.commit()
        self._position_trackers.pop(symbol, None)
        self._eval_error_counts.pop(symbol, None)
        # 강제 청산은 에러 기반이므로 쿨다운 면제
        self._last_sell_time.pop(symbol, None)

        logger.error(
            "force_close_db_cleanup",
            symbol=symbol,
            detail="거래소 매도 실패 → DB 포지션 강제 리셋",
        )
        await emit_event(
            "critical", "engine",
            f"강제 매도 (DB 리셋): {symbol}",
            detail=f"연속 {count}회 평가 실패, 거래소 매도 불가 → DB 포지션 0으로 리셋. "
                   f"수동으로 거래소에서 {symbol} 포지션을 확인하세요.",
            metadata={"symbol": symbol, "consecutive_errors": count},
        )

    async def _execute_stop_sell(
        self,
        session: AsyncSession,
        symbol: str,
        position: Position,
        price: float,
        reason: str,
    ) -> None:
        """SL/TP/trailing에 의한 전량 매도."""
        # 방어: DB 수량이 거래소 실잔고보다 클 수 있음 (매수 시 stepSize/수수료 차이)
        # 실잔고 기준으로 매도 수량 클램핑
        sell_qty = position.quantity
        sell_qty = await self._clamp_sell_qty_to_balance(symbol, sell_qty)

        if sell_qty <= 0:
            logger.warning("stop_sell_zero_balance", symbol=symbol,
                           db_qty=position.quantity, reason=reason)
            return

        # 시스템 생성 매도 시그널
        sell_signal = Signal(
            strategy_name="risk_management",
            signal_type=SignalType.SELL,
            confidence=1.0,
            reason=reason,
        )

        order = await self._order_manager.create_order(
            session, symbol, "sell", sell_qty, price, sell_signal,
            order_type="market",
            entry_price=position.average_buy_price if position.average_buy_price and position.average_buy_price > 0 else None,
        )

        # 미체결 주문은 거래소 취소 + 포트폴리오 건드리지 않음
        if order.status != "filled":
            logger.warning("sell_order_not_filled", symbol=symbol, status=order.status,
                           order_id=order.id)
            if order.exchange_order_id:
                try:
                    await self._order_manager.cancel_order_by_id(session, order.id)
                except Exception:
                    pass
            raise RuntimeError(f"sell_order_not_filled: {order.status}")

        await self._portfolio_manager.update_position_on_sell(
            session, symbol, sell_qty, price,
            sell_qty * price, order.fee
        )

        # 트래커 제거
        self._position_trackers.pop(symbol, None)

        # 매매 추적 (매도는 buy 카운터에 포함하지 않음)
        now = datetime.now(timezone.utc)
        self._last_trade_time[symbol] = now
        self._last_sell_time[symbol] = now  # 매도 후 재매수 대기용
        self._daily_trade_count += 1
        await self._on_sell_completed()

        # 브로드캐스트
        if self._broadcast_callback:
            await self._broadcast_callback({
                "event": "trade_executed",
                "data": {
                    "symbol": symbol,
                    "side": "sell",
                    "price": price,
                    "strategy": "risk_management",
                    "confidence": 1.0,
                    "reason": reason,
                },
            })

    # ── 매도 수량 방어 (거래소 실잔고 클램핑) ────────────────────────

    async def _clamp_sell_qty_to_balance(self, symbol: str, qty: float) -> float:
        """DB 수량이 거래소 실잔고보다 클 경우 실잔고로 클램핑.

        매수 시 stepSize 내림, 수수료 차감 등으로 DB qty > 실잔고가 될 수 있다.
        매도 시 DB qty 그대로 전달하면 insufficient balance 에러 발생.
        """
        try:
            balances = await self._exchange.fetch_balance()
            # symbol: "BTC/USDT" → base: "BTC"
            base = symbol.split("/")[0]
            bal = balances.get(base)
            if bal is None:
                return qty

            actual_free = bal.free
            if actual_free < qty:
                logger.warning(
                    "sell_qty_clamped_to_balance",
                    symbol=symbol,
                    db_qty=qty,
                    exchange_free=actual_free,
                    diff=round(qty - actual_free, 10),
                )
                return actual_free
        except Exception as e:
            # fetch_balance 실패 시 원래 수량 유지 (거래소에서 reject하면 에러 핸들링)
            logger.warning("clamp_sell_balance_fetch_failed", symbol=symbol, error=str(e))
        return qty

    # ── 포트폴리오 리밸런싱 ─────────────────────────────────────────

    _REBALANCE_COOLDOWN_SEC = 3600  # 동일 코인 1시간 쿨다운

    async def _check_and_rebalance(self, session: AsyncSession) -> None:
        """비중 초과 코인 자동 일부 매도 (max_single_coin_pct → target_pct)."""
        if not self._ec.rebalancing_enabled:
            return

        summary = await self._portfolio_manager.get_portfolio_summary(session)
        total_value = summary.get("total_value_krw", 0)
        if total_value <= 0:
            return

        now = datetime.now(timezone.utc)
        positions = summary.get("positions", [])

        for pos_info in positions:
            symbol = pos_info["symbol"]
            current_value = pos_info["current_value"]
            weight = current_value / total_value

            if weight <= self._ec.max_single_coin_pct:
                continue

            # 서지 포지션 스킵
            tracker = self._position_trackers.get(symbol)
            if tracker and tracker.is_surge:
                continue

            # 쿨다운 체크
            last = self._last_rebalance.get(symbol)
            if last and (now - last).total_seconds() < self._REBALANCE_COOLDOWN_SEC:
                continue

            # 매도 수량 계산: (weight - target) / weight 비율만큼
            target = self._ec.rebalancing_target_pct
            sell_ratio = (weight - target) / weight
            qty = pos_info["quantity"] * sell_ratio
            price = pos_info["current_price"]

            if qty <= 0 or price <= 0:
                continue

            weight_pct = round(weight * 100, 1)
            target_pct = round(target * 100, 1)
            logger.info(
                "rebalancing_triggered",
                symbol=symbol, weight_pct=weight_pct,
                target_pct=target_pct, sell_qty=qty,
            )

            await self._execute_rebalancing_sell(session, symbol, qty, price)
            self._last_rebalance[symbol] = now

            await emit_event(
                "warning", "risk",
                f"리밸런싱: {symbol} 비중 {weight_pct}%→{target_pct}%",
                metadata={"symbol": symbol, "weight": weight_pct, "target": target_pct},
            )

    async def _execute_rebalancing_sell(
        self, session: AsyncSession, symbol: str, qty: float, price: float
    ) -> None:
        """리밸런싱 부분 매도 (현물)."""
        signal = Signal(
            strategy_name="rebalancing",
            signal_type=SignalType.SELL,
            confidence=1.0,
            reason=f"포트폴리오 리밸런싱: 비중 초과 부분 매도",
        )

        # 진입가 조회
        pos_result = await session.execute(
            select(Position).where(Position.symbol == symbol, Position.exchange == self._exchange_name)
        )
        pos = pos_result.scalar_one_or_none()
        ep = pos.average_buy_price if pos and pos.average_buy_price and pos.average_buy_price > 0 else None

        order = await self._order_manager.create_order(
            session, symbol, "sell", qty, price, signal,
            order_type="market",
            entry_price=ep,
        )

        if order.status != "filled":
            logger.warning("rebalancing_sell_not_filled", symbol=symbol, status=order.status)
            if order.exchange_order_id:
                try:
                    await self._order_manager.cancel_order_by_id(session, order.id)
                except Exception:
                    pass
            return

        await self._portfolio_manager.update_position_on_sell(
            session, symbol, qty, price,
            qty * price, order.fee,
        )

        logger.info(
            "rebalancing_sell_executed",
            symbol=symbol, qty=round(qty, 8), price=price,
        )

    # ── 평가 사이클 ────────────────────────────────────────────────

    async def _evaluation_cycle(self) -> None:
        """Run one evaluation cycle for all tracked coins."""
        import time as _time
        _cycle_start = _time.monotonic()
        # 시장 상태 업데이트
        await self._maybe_update_market_state()

        async with self._portfolio_manager._sync_lock:
            session_factory = get_session_factory()
            async with session_factory() as session:
                try:
                    # 매 사이클 시작 시 현금 잔고 정합성 확인
                    await self._portfolio_manager.reconcile_cash_from_db(session)

                    # 포트폴리오 리밸런싱 (비중 초과 코인 자동 일부 매도)
                    await self._check_and_rebalance(session)

                    coins = set(self._ec.tracked_coins)

                    # 보유 중인 포지션도 평가 대상에 포함 (서지 코인 SL/TP/SELL)
                    result = await session.execute(
                        select(Position.symbol).where(Position.quantity > 0, Position.exchange == self._exchange_name)
                    )
                    held = {r[0] for r in result.all()}
                    all_coins = list(coins | held)

                    for symbol in all_coins:
                        try:
                            await self._evaluate_coin(session, symbol)
                            self._eval_error_counts.pop(symbol, None)  # 성공 시 카운터 리셋
                        except Exception as coin_err:
                            count = self._eval_error_counts.get(symbol, 0) + 1
                            self._eval_error_counts[symbol] = count
                            logger.error("evaluate_coin_error", symbol=symbol,
                                         error=str(coin_err), consecutive_errors=count,
                                         exc_info=True)
                            # 프론트엔드 시스템 로그에 에러 표시
                            level = "critical" if count >= self._MAX_EVAL_ERRORS else "warning"
                            await emit_event(
                                level, "engine",
                                f"코인 평가 실패: {symbol} ({count}회 연속)",
                                detail=str(coin_err),
                                metadata={"symbol": symbol, "consecutive_errors": count,
                                          "exchange": self._exchange_name},
                            )
                            # 연속 N회 실패 + 보유 포지션 → 강제 매도
                            if count >= self._MAX_EVAL_ERRORS and symbol in held:
                                await self._force_close_stuck_position(session, symbol, str(coin_err))
                            continue

                    # 거래량 급등 로테이션 모드
                    if self._ec.rotation_enabled:
                        surges = await self._scan_volume_surges()
                        if surges:
                            await self._try_rotation(session, surges)
                        logger.info(
                            "surge_scan_complete",
                            surge_count=len(surges),
                            top_surges=[(s, round(sc, 1)) for s, sc in surges[:3]] if surges else [],
                        )

                    # 매매 기록 먼저 커밋 (스냅샷 스킵과 무관하게 주문/포지션 영속화)
                    await session.commit()

                    # 스냅샷 직전 현금 잔고 재보정 (eval 중 sync 인터리빙 방지)
                    await self._portfolio_manager.reconcile_cash_from_db(session)
                    snap = await self._portfolio_manager.take_snapshot(session)
                    if snap is not None:
                        await session.commit()

                    if self._broadcast_callback:
                        summary = await self._portfolio_manager.get_portfolio_summary(session)
                        await self._broadcast_callback({
                            "event": "portfolio_update",
                            "data": summary,
                        })

                except Exception as e:
                    await session.rollback()
                    logger.error("evaluation_cycle_error", error=str(e), exc_info=True)
                    await emit_event("error", "engine", "평가 사이클 오류", detail=str(e))
                finally:
                    elapsed_ms = (_time.monotonic() - _cycle_start) * 1000
                    logger.info("evaluation_cycle_complete", elapsed_ms=round(elapsed_ms, 1), exchange=self._exchange_name, coins=len(all_coins))

    async def _evaluate_coin(self, session: AsyncSession, symbol: str) -> None:
        """Evaluate a single coin: SL/TP first, then strategy signals."""
        # ── 1. 기존 포지션 SL/TP/trailing 체크 ──
        result = await session.execute(
            select(Position).where(Position.symbol == symbol, Position.quantity > 0, Position.exchange == self._exchange_name)
        )
        position = result.scalar_one_or_none()

        if position and position.quantity > 0:
            stopped = await self._check_stop_conditions(session, symbol, position)
            if stopped:
                return  # 이미 매도했으므로 스킵

            # 서지 포지션은 전용 SL/TP/trailing/max_hold로만 종료
            # 일반 전략 SELL 시그널은 무시 (서지 모멘텀 패턴이 다름)
            tracker = self._position_trackers.get(symbol)
            if tracker and tracker.is_surge:
                return

        # ── 2. 페어링 매도: 포지션이 있으면 진입 전략만 체크 ──
        has_position = position and position.quantity > 0
        if has_position and self._ec.paired_exit and position.strategy_name:
            entry_strat = self._strategies.get(position.strategy_name)
            if entry_strat:
                try:
                    df = await self._market_data.get_candles(
                        symbol, entry_strat.required_timeframe,
                        max(entry_strat.min_candles_required + 50, 200),
                    )
                    ticker = await self._market_data.get_ticker(symbol)
                    signal = await entry_strat.analyze(df, ticker)
                    await self._order_manager.log_signal_only(session, signal, symbol)

                    if signal.signal_type == SignalType.SELL:
                        decision = CombinedDecision(
                            action=SignalType.SELL,
                            combined_confidence=signal.confidence,
                            contributing_signals=[signal],
                            final_reason=f"페어링 매도: {signal.reason}",
                        )
                        await emit_event(
                            "info", "signal",
                            f"시그널: {symbol} SELL (페어링: {position.strategy_name})",
                            detail=decision.final_reason,
                            metadata={
                                "symbol": symbol, "action": "SELL",
                                "confidence": round(signal.confidence, 2),
                                "strategies": [f"{position.strategy_name}({signal.confidence:.0%})"],
                                "market_state": self._market_state,
                                "paired_exit": True,
                            },
                        )
                        await self._process_decision(session, symbol, decision)
                except Exception as e:
                    logger.warning("paired_exit_error", symbol=symbol, strategy=position.strategy_name, error=str(e))
            return  # 포지션 보유 중 — 추가 매수 불가, 페어링 체크 완료

        # ── 3. 매수 가능 여부 체크 (매도는 항상 허용) ──
        can_buy, buy_block_reason = self._can_trade(symbol, side="buy")

        # ── 4. 전략 시그널 수집 ──
        signals: list[Signal] = []

        for name, strategy in self._strategies.items():
            # tracked_coins에 있으면 모든 전략 실행 (default_coins 무시)
            # rotation 코인은 default_coins/applicable_market_types 체크

            try:
                df = await self._market_data.get_candles(
                    symbol, strategy.required_timeframe, max(strategy.min_candles_required + 50, 200)
                )
                ticker = await self._market_data.get_ticker(symbol)
                signal = await strategy.analyze(df, ticker)
                signals.append(signal)

                await self._order_manager.log_signal_only(session, signal, symbol)

            except Exception as e:
                logger.warning(
                    "strategy_error",
                    strategy=name,
                    symbol=symbol,
                    error=str(e),
                )

        # ── 5. 결합 판단 + 실행 ──
        if signals:
            decision = self._combiner.combine(signals, market_state=self._market_state, symbol=symbol)

            # 통합 시그널 이벤트 (BUY/SELL만, HOLD 제외)
            if decision.action != SignalType.HOLD:
                action_str = decision.action.name
                contribs = [
                    f"{s.strategy_name}({s.confidence:.0%})"
                    for s in decision.contributing_signals
                    if s.signal_type == decision.action
                ]
                await emit_event(
                    "info", "signal",
                    f"시그널: {symbol} {action_str}",
                    detail=decision.final_reason,
                    metadata={
                        "symbol": symbol,
                        "action": action_str,
                        "confidence": round(decision.combined_confidence, 2),
                        "strategies": contribs,
                        "market_state": self._market_state,
                    },
                )

            # 페어링 미적용 포지션: 투표 SELL 허용 (strategy_name 없는 레거시 포지션)
            if has_position and self._ec.paired_exit:
                # paired_exit ON인데 strategy_name 없는 경우 — 투표 SELL 폴백
                if decision.action == SignalType.SELL:
                    await self._process_decision(session, symbol, decision)
                return

            # can_buy=False → 매수만 차단, 매도는 항상 허용
            if can_buy or decision.action == SignalType.SELL:
                await self._process_decision(session, symbol, decision)
            elif decision.action == SignalType.BUY and not can_buy:
                logger.warning(
                    "buy_blocked_by_trade_limit",
                    symbol=symbol,
                    reason=buy_block_reason,
                    confidence=round(decision.combined_confidence, 2),
                )
                await emit_event(
                    "warning", "engine",
                    f"매수 차단: {symbol} (conf={decision.combined_confidence:.2f})",
                    detail=f"차단 사유: {buy_block_reason}",
                    metadata={"symbol": symbol, "reason": buy_block_reason,
                              "confidence": round(decision.combined_confidence, 2),
                              "exchange": self._exchange_name},
                )

    # ── 거래량 급등 로테이션 ──────────────────────────────────────

    _ROTATION_REFRESH_SEC = 6 * 3600  # 6시간마다 갱신
    _MAX_ROTATION_COINS = 40  # 로테이션 코인 상한

    async def _refresh_rotation_coins(self) -> None:
        """전체 마켓에서 24h 거래대금 상위 코인을 로테이션 대상으로 선정."""
        now = datetime.now(timezone.utc)
        if (self._rotation_coins_updated
                and (now - self._rotation_coins_updated).total_seconds() < self._ROTATION_REFRESH_SEC):
            return

        try:
            tickers = await self._exchange.fetch_tickers()
            tracked = set(self._ec.tracked_coins)
            suffix = self._ec.quote_suffix
            stables = self._ec.stablecoins
            min_vol = self._ec.min_quote_volume

            ranked = []
            # 이전 스캔에서 "마켓 없음" 에러 난 심볼 제외
            failed_syms = {k for k, v in self._eval_error_counts.items() if v >= 3}
            for sym, t in tickers.items():
                if not sym.endswith(suffix):
                    continue
                if sym in tracked or sym in stables or sym in failed_syms:
                    continue
                vol = t.get("quoteVolume") or 0
                if vol >= min_vol:
                    ranked.append((sym, vol))

            ranked.sort(key=lambda x: x[1], reverse=True)
            self._dynamic_rotation_coins = [sym for sym, _ in ranked[:self._MAX_ROTATION_COINS]]
            self._rotation_coins_updated = now

            logger.info(
                "rotation_coins_refreshed",
                exchange=self._exchange_name,
                count=len(self._dynamic_rotation_coins),
                top5=[s for s, _ in ranked[:5]],
            )
        except Exception as e:
            logger.warning("rotation_coins_refresh_failed", error=str(e))
            if not self._dynamic_rotation_coins and self._ec.rotation_coins:
                self._dynamic_rotation_coins = list(self._ec.rotation_coins)

    def _get_rotation_coins(self) -> list[str]:
        """동적 코인이 있으면 사용, 없으면 config 폴백."""
        if self._dynamic_rotation_coins:
            return self._dynamic_rotation_coins
        return list(self._ec.rotation_coins)

    async def _scan_volume_surges(self) -> list[tuple[str, float]]:
        """거래대금 상위 코인 거래량 서지 스캔. (symbol, surge_score) 리스트 반환."""
        await self._refresh_rotation_coins()
        surges: list[tuple[str, float]] = []
        all_scores: dict[str, float] = {}
        threshold = self._ec.surge_threshold
        for symbol in self._get_rotation_coins():
            try:
                df = await self._market_data.get_candles(symbol, "1h", 30)
                if df is None or len(df) < 21:
                    continue
                current_vol = df.iloc[-1]["volume"]
                avg_vol = df.iloc[-1].get("volume_sma_20", 0)
                if avg_vol is None or avg_vol <= 0:
                    continue
                score = current_vol / avg_vol
                all_scores[symbol] = round(score, 2)
                if score >= threshold:
                    surges.append((symbol, score))
            except Exception as e:
                logger.debug("surge_scan_error", symbol=symbol, error=str(e))
        self._all_surge_scores = all_scores
        self._last_surge_scan_time = datetime.now(timezone.utc)
        surges.sort(key=lambda x: x[1], reverse=True)
        return surges

    async def _try_rotation(self, session: AsyncSession, surges: list[tuple[str, float]]) -> None:
        """서지 코인을 현금으로 매수 (기존 포지션 유지)."""
        now = datetime.now(timezone.utc)

        # 쿨다운 체크
        if self._last_rotation_time:
            elapsed = (now - self._last_rotation_time).total_seconds()
            if elapsed < self._ec.rotation_cooldown_sec:
                return

        # 현금 부족 시 스킵
        cash = self._portfolio_manager.cash_balance
        if cash < 5000:
            return

        # 이미 보유 중인 심볼 조회
        result = await session.execute(
            select(Position.symbol).where(Position.quantity > 0, Position.exchange == self._exchange_name)
        )
        held_symbols = {r[0] for r in result.all()}

        for symbol, score in surges:
            # 이미 보유 중이면 스킵
            if symbol in held_symbols:
                logger.debug("rotation_skip_held", symbol=symbol)
                continue

            # 매수 가능 여부 체크
            can_buy, reason = self._can_trade(symbol, side="buy")
            if not can_buy:
                logger.info("rotation_skip_cant_trade", symbol=symbol, reason=reason)
                continue

            # 전략 확인 (combiner) — 서지는 임계값 완화
            confirmed, confidence = await self._get_surge_confirmation(
                session, symbol,
            )
            if not confirmed:
                logger.info("rotation_skip_not_confirmed", symbol=symbol, score=round(score, 1))
                continue

            # 현금으로 서지 코인 매수 (기존 포지션 유지)
            await self._execute_rotation_buy(session, symbol, score, confidence)

            self._last_rotation_time = now
            self._current_surge_symbol = symbol
            break  # 최고 서지 1개만

    async def _get_surge_confirmation(
        self, session: AsyncSession, symbol: str,
        force_on_strong_surge: bool = False,
    ) -> tuple[bool, float]:
        """서지 코인에 대해 기존 전략 파이프라인으로 BUY 확인.

        서지는 그 자체로 강한 시그널이므로 임계값을 일반 매수보다 낮춤 (0.20).
        BUY 시그널이 1개라도 있으면 확인 통과.
        force_on_strong_surge=True이면 HOLD도 허용 (SELL만 거부).
        """
        signals: list[Signal] = []
        for name, strategy in self._strategies.items():
            try:
                df = await self._market_data.get_candles(
                    symbol, strategy.required_timeframe,
                    max(strategy.min_candles_required + 50, 200),
                )
                ticker = await self._market_data.get_ticker(symbol)
                signal = await strategy.analyze(df, ticker)
                signals.append(signal)
            except Exception:
                pass

        if not signals:
            return False, 0.0

        decision = self._combiner.combine(signals, market_state=self._market_state, symbol=symbol)

        # BUY 시그널이 있으면 즉시 확인
        if (decision.action == SignalType.BUY
                and decision.combined_confidence >= 0.20):
            logger.info(
                "surge_confirmed", symbol=symbol,
                confidence=round(decision.combined_confidence, 3),
                method="strategy_buy",
            )
            return True, float(decision.combined_confidence)

        # 강한 서지 (임계값 2배): SELL이 아니면 허용 (HOLD 통과)
        if force_on_strong_surge and decision.action != SignalType.SELL:
            logger.info(
                "surge_confirmed_strong", symbol=symbol,
                confidence=round(decision.combined_confidence, 3),
                strategy_action=decision.action.value,
                method="strong_surge_override",
            )
            return True, max(float(decision.combined_confidence), 0.25)

        # 일반 서지: BUY만 허용 (엄격 모드)
        logger.info(
            "surge_rejected_no_buy", symbol=symbol,
            strategy_action=decision.action.value,
            confidence=round(decision.combined_confidence, 3),
        )
        return False, 0.0

    async def _execute_rotation_sell(self, session: AsyncSession) -> None:
        """로테이션을 위한 기존 포지션 전량 매도."""
        result = await session.execute(
            select(Position).where(Position.quantity > 0, Position.exchange == self._exchange_name)
        )
        positions = result.scalars().all()

        for position in positions:
            try:
                price = await self._market_data.get_current_price(position.symbol)
                await self._execute_stop_sell(
                    session, position.symbol, position, price,
                    f"로테이션 매도 (새 서지 코인 발견)"
                )
                logger.info(
                    "rotation_sell",
                    symbol=position.symbol,
                    price=price,
                    quantity=position.quantity,
                )
                await emit_event("info", "rotation", f"로테이션 매도: {position.symbol}", metadata={"price": price})
            except Exception as e:
                logger.error("rotation_sell_error", symbol=position.symbol, error=str(e))

    async def _execute_rotation_buy(
        self, session: AsyncSession, symbol: str, surge_score: float, confidence: float,
    ) -> None:
        """서지 코인을 현금의 15%로 매수 (기존 포지션 유지)."""
        try:
            ticker = await self._market_data.get_ticker(symbol)
            price = ticker.last

            cash = self._portfolio_manager.cash_balance
            surge_size_pct = 0.15  # 현금의 15%
            amount_krw = cash * surge_size_pct
            amount_krw = amount_krw / self._fee_margin

            if amount_krw < self._min_order_amount:
                logger.debug("rotation_buy_too_small", symbol=symbol, amount_krw=amount_krw)
                return

            amount = amount_krw / price

            buy_signal = Signal(
                strategy_name="rotation_surge",
                signal_type=SignalType.BUY,
                confidence=confidence,
                reason=f"거래량 서지 x{surge_score:.1f} + 전략 확인",
            )

            order = await self._order_manager.create_order(
                session, symbol, "buy", amount, price, buy_signal,
                order_type="market",
            )

            # 미체결 주문은 거래소 취소 + 포트폴리오 건드리지 않음
            if order.status != "filled":
                logger.warning("rotation_buy_not_filled", symbol=symbol, status=order.status,
                               order_id=order.id)
                if order.exchange_order_id:
                    try:
                        await self._order_manager.cancel_order_by_id(session, order.id)
                    except Exception:
                        pass
                return

            # 거래소 실제 체결 수량/가격 사용
            executed_qty = float(order.executed_quantity) if order.executed_quantity else amount
            executed_price = float(order.executed_price) if order.executed_price else price
            actual_cost = executed_qty * executed_price

            await self._portfolio_manager.update_position_on_buy(
                session, symbol, executed_qty, executed_price, actual_cost, order.fee,
                is_surge=True,
            )

            # 서지 전용 포지션 트래커 (백테스트 C 프로필)
            self._position_trackers[symbol] = PositionTracker(
                entry_price=executed_price,
                extreme_price=executed_price,
                stop_loss_pct=4.0,
                take_profit_pct=8.0,
                trailing_activation_pct=1.5,
                trailing_stop_pct=2.0,
                is_surge=True,
                max_hold_hours=48,
            )
            await self._save_tracker_to_db(session, symbol, self._position_trackers[symbol])

            self._last_trade_time[symbol] = datetime.now(timezone.utc)
            self._daily_trade_count += 1
            self._daily_buy_count += 1
            self._daily_coin_buy_count[symbol] = self._daily_coin_buy_count.get(symbol, 0) + 1

            logger.info(
                "rotation_buy",
                symbol=symbol,
                price=executed_price,
                executed_qty=executed_qty,
                surge_score=round(surge_score, 1),
                confidence=round(confidence, 3),
                sl_pct=4.0,
            )
            await emit_event("info", "rotation", f"로테이션 매수: {symbol}", metadata={"surge_score": round(surge_score, 1), "price": executed_price})

            if self._broadcast_callback:
                await self._broadcast_callback({
                    "event": "trade_executed",
                    "data": {
                        "symbol": symbol,
                        "side": "buy",
                        "price": price,
                        "strategy": "rotation_surge",
                        "confidence": confidence,
                        "reason": f"Volume surge x{surge_score:.1f}",
                    },
                })

        except Exception as e:
            logger.error("rotation_buy_error", symbol=symbol, error=str(e), exc_info=True)

    # ── 추세 필터 ──────────────────────────────────────────────────

    def _trend_filter_action(self) -> str:
        """시장 상태별 매수 정책. 'heavy_reduce' / 'reduce' / 'allow' 반환."""
        if self._market_state == "crash":
            return "heavy_reduce"  # crash: 25% 축소 매수
        if self._market_state == "downtrend":
            return "reduce"        # downtrend: 50% 축소 매수
        return "allow"

    async def _process_decision(
        self, session: AsyncSession, symbol: str, decision: CombinedDecision
    ) -> None:
        """Process a combined decision and execute if warranted."""
        if decision.action == SignalType.HOLD:
            return

        if decision.action == SignalType.BUY and symbol in self._suppressed_coins:
            logger.info("buy_suppressed", symbol=symbol)
            return

        # 추세 필터: crash=차단, downtrend=50% 축소
        trend_action = self._trend_filter_action()

        primary_signal = max(
            [s for s in decision.contributing_signals if s.signal_type == decision.action],
            key=lambda s: s.confidence,
            default=None,
        )
        if not primary_signal:
            return

        ticker = await self._market_data.get_ticker(symbol)
        price = ticker.last

        if decision.action == SignalType.BUY:
            # ── 비대칭 전략: 시장 상태별 차등 매수 기준 ──
            if self._ec.asymmetric_mode:
                # 하락장 매수 완전 차단
                if self._market_state in ("crash", "downtrend"):
                    logger.info("asymmetric_buy_blocked",
                                symbol=symbol, market_state=self._market_state)
                    return
                # 히스테리시스: bearish 해제 후 36h 매수 금지
                if self._bearish_clear_time:
                    elapsed = (datetime.now(timezone.utc) - self._bearish_clear_time).total_seconds()
                    if elapsed < 36 * 3600:
                        logger.info("hysteresis_buy_blocked", symbol=symbol,
                                    hours_since_bearish=round(elapsed / 3600, 1),
                                    market_state=self._market_state)
                        return
                # 시장 상태별 신뢰도 임계값
                base_conf = self._ec.min_combined_confidence
                _asym_conf = {
                    "strong_uptrend": max(base_conf - 0.15, 0.35),
                    "uptrend":        max(base_conf - 0.10, 0.40),
                    "sideways":       base_conf + 0.05,
                }
                min_conf = _asym_conf.get(self._market_state, base_conf)
            else:
                # 기존 로직
                min_conf = self._ec.min_combined_confidence
                if self._market_confidence < 0.35:
                    min_conf += 0.10

            if decision.combined_confidence < min_conf:
                logger.debug(
                    "buy_confidence_too_low", symbol=symbol,
                    combined=round(decision.combined_confidence, 3),
                    threshold=round(min_conf, 3),
                    market_state=self._market_state,
                )
                return

            # 이미 포지션 있으면 추가 매수 안 함
            result = await session.execute(
                select(Position).where(Position.symbol == symbol, Position.quantity > 0, Position.exchange == self._exchange_name)
            )
            if result.scalar_one_or_none():
                return

            # 교차 거래소 포지션 충돌 체크 (현물 매수 vs 선물 숏)
            base = symbol.split("/")[0]
            cross_result = await session.execute(
                select(Position).where(
                    Position.symbol.like(f"{base}/%"),
                    Position.quantity > 0,
                    Position.exchange != self._exchange_name,
                    Position.direction == "short",
                )
            )
            cross_pos = cross_result.scalars().first()
            if cross_pos:
                # 높은 신뢰도면 반대 포지션 청산 후 진행 (포지션 방향 전환)
                flipped = False
                if (decision.combined_confidence >= self.CROSS_FLIP_MIN_CONFIDENCE
                        and self._engine_registry):
                    cross_engine = self._engine_registry.get_engine(cross_pos.exchange)
                    if cross_engine:
                        cross_symbol = f"{base}/{cross_engine._ec.quote_currency}"
                        flipped = await cross_engine.close_position_for_cross_exchange(
                            cross_symbol,
                            f"교차 전환: {self._exchange_name} BUY(conf={decision.combined_confidence:.2f}) → 숏 청산",
                        )
                        if flipped:
                            await emit_event(
                                "info", "risk",
                                f"교차 포지션 전환: {cross_pos.exchange} {base} 숏 청산 → {self._exchange_name} 매수 진행",
                                metadata={"symbol": symbol, "confidence": round(decision.combined_confidence, 2)},
                            )
                if not flipped:
                    logger.warning(
                        "cross_exchange_conflict_blocked",
                        symbol=symbol,
                        cross_exchange=cross_pos.exchange,
                        cross_direction="short",
                        cross_qty=cross_pos.quantity,
                    )
                    await emit_event(
                        "warning", "risk",
                        f"교차 거래소 충돌: {symbol} 매수 차단 (선물 숏 보유 중)",
                        metadata={"symbol": symbol, "cross_qty": cross_pos.quantity},
                    )
                    return

            # 포지션 사이징 (잔고 부족 시 복구 시도)
            cash = self._portfolio_manager.cash_balance
            if cash < self._min_order_amount:
                if self._recovery_manager:
                    from core.exceptions import InsufficientBalanceError
                    recovery = await self._recovery_manager.attempt_recovery(
                        ClassifiedError(
                            category=ErrorCategory.RESOURCE,
                            original=InsufficientBalanceError(f"cash={cash}"),
                            symbol=symbol,
                            context="buy_sizing",
                            retryable=True,
                            max_retries=1,
                            backoff_base=1.0,
                            recovery_action="reconcile_cash",
                        ))
                    if recovery.resolved:
                        cash = self._portfolio_manager.cash_balance
                if cash < self._min_order_amount:
                    logger.warning("buy_skip_no_cash_after_recovery", symbol=symbol,
                                   cash=round(cash, 2), min_order=self._min_order_amount)
                    return

            if self._ec.asymmetric_mode:
                # 비대칭 사이징: 상승장 공격적, 횡보장 보수적
                _asym_size = {
                    "strong_uptrend": self._ec.max_trade_size_pct,       # 풀 사이즈
                    "uptrend":        self._ec.max_trade_size_pct * 0.8,  # 80%
                    "sideways":       self._ec.max_trade_size_pct * 0.5,  # 50%
                }
                size_pct = _asym_size.get(self._market_state, self._ec.max_trade_size_pct * 0.5)
            else:
                size_pct = self._ec.max_trade_size_pct
                if trend_action == "heavy_reduce":
                    size_pct *= 0.25
                    logger.info("buy_reduced_crash", symbol=symbol, size_pct=round(size_pct, 3))
                elif trend_action == "reduce":
                    size_pct *= 0.5
                    logger.info("buy_reduced_downtrend", symbol=symbol, size_pct=round(size_pct, 3))
            amount_krw = cash * size_pct

            # 최소 주문금액 미달 시 잔고 전체 시도
            if amount_krw < self._min_fallback_amount and cash >= self._min_fallback_amount:
                amount_krw = cash

            # 수수료 감안 — 총비용이 잔고 초과하지 않도록
            amount_krw = amount_krw / self._fee_margin

            if amount_krw < self._min_order_amount:
                logger.info("order_too_small", symbol=symbol,
                            amount_krw=round(amount_krw, 2), cash=round(cash, 2),
                            min_order=self._min_order_amount)
                return

            amount = amount_krw / price

            if self._recovery_manager:
                order = await self._execute_with_retry(
                    lambda: self._order_manager.create_order(
                        session, symbol, "buy", amount, price, primary_signal, decision,
                        order_type="market",
                    ),
                    context="buy_order", symbol=symbol,
                )
                if order is None:
                    return
            else:
                try:
                    order = await self._order_manager.create_order(
                        session, symbol, "buy", amount, price, primary_signal, decision,
                        order_type="market",
                    )
                except Exception as e:
                    logger.error("buy_order_failed", symbol=symbol, error=str(e),
                                 amount=round(amount, 8), amount_krw=round(amount_krw, 2))
                    await emit_event("error", "trade",
                                     f"매수 주문 실패: {symbol}",
                                     detail=str(e),
                                     metadata={"symbol": symbol, "amount_krw": round(amount_krw, 2)})
                    return

            # 미체결 주문은 거래소 취소 + 포트폴리오 건드리지 않음
            if order.status != "filled":
                logger.warning("buy_order_not_filled", symbol=symbol, status=order.status,
                               order_id=order.id)
                if order.exchange_order_id:
                    try:
                        await self._order_manager.cancel_order_by_id(session, order.id)
                    except Exception:
                        pass
                return

            # 거래소 실제 체결 수량/가격 사용 (요청값 ≠ 체결값: stepSize 내림, 수수료 차감 등)
            executed_qty = float(order.executed_quantity) if order.executed_quantity else amount
            executed_price = float(order.executed_price) if order.executed_price else price
            actual_cost = executed_qty * executed_price

            await self._portfolio_manager.update_position_on_buy(
                session, symbol, executed_qty, executed_price, actual_cost, order.fee,
                strategy_name=primary_signal.strategy_name,
            )

            # 포지션 트래커 생성 (SL/TP/trailing 추적 시작)
            try:
                df = await self._market_data.get_candles(symbol, "4h", 200)
                sl_pct = self._calc_dynamic_sl(df, executed_price, self._market_state)
            except Exception:
                sl_pct = 3.0
            self._position_trackers[symbol] = PositionTracker(
                entry_price=executed_price,
                extreme_price=executed_price,
                stop_loss_pct=sl_pct,
            )
            await self._save_tracker_to_db(session, symbol, self._position_trackers[symbol])

            logger.info(
                "position_opened",
                symbol=symbol,
                price=executed_price,
                executed_qty=executed_qty,
                sl_pct=round(sl_pct, 2),
                market_state=self._market_state,
            )
            tracker = self._position_trackers[symbol]
            sl_price = round(executed_price * (1 - tracker.stop_loss_pct / 100))
            tp_price = round(executed_price * (1 + tracker.take_profit_pct / 100))
            await emit_event("info", "trade", f"매수: {symbol}", metadata={
                "price": executed_price, "sl_pct": round(sl_pct, 2),
                "sl_price": sl_price, "tp_price": tp_price,
                "strategy": primary_signal.strategy_name,
                "confidence": round(decision.combined_confidence, 2),
                "amount_krw": round(actual_cost, 0),
                "market_state": self._market_state,
            })

        elif decision.action == SignalType.SELL:
            result = await session.execute(
                select(Position).where(Position.symbol == symbol, Position.quantity > 0, Position.exchange == self._exchange_name)
            )
            position = result.scalar_one_or_none()
            if not position or position.quantity <= 0:
                return

            # 방어: 실잔고 기준으로 매도 수량 클램핑
            sell_qty = await self._clamp_sell_qty_to_balance(symbol, position.quantity)
            if sell_qty <= 0:
                logger.warning("sell_zero_balance", symbol=symbol, db_qty=position.quantity)
                return

            ep = position.average_buy_price if position.average_buy_price and position.average_buy_price > 0 else None

            if self._recovery_manager:
                order = await self._execute_with_retry(
                    lambda: self._order_manager.create_order(
                        session, symbol, "sell", sell_qty, price, primary_signal, decision,
                        order_type="market", entry_price=ep,
                    ),
                    context="sell_order", symbol=symbol,
                )
                if order is None:
                    return
            else:
                try:
                    order = await self._order_manager.create_order(
                        session, symbol, "sell", sell_qty, price, primary_signal, decision,
                        order_type="market", entry_price=ep,
                    )
                except Exception as e:
                    logger.error("sell_order_failed", symbol=symbol, error=str(e))
                    await emit_event("error", "trade", f"매도 주문 실패: {symbol}", detail=str(e))
                    return

            # 미체결 주문은 거래소 취소 + 포트폴리오 건드리지 않음
            if order.status != "filled":
                logger.warning("sell_order_not_filled", symbol=symbol, status=order.status,
                               order_id=order.id)
                if order.exchange_order_id:
                    try:
                        await self._order_manager.cancel_order_by_id(session, order.id)
                    except Exception:
                        pass
                return

            # P&L 계산 (update_position_on_sell 이전에 — 전량 매도 시 avg_price 리셋됨)
            entry_price = position.average_buy_price or price
            pnl_pct = (price - entry_price) / entry_price * 100 if entry_price > 0 else 0

            await self._portfolio_manager.update_position_on_sell(
                session, symbol, sell_qty, price,
                sell_qty * price, order.fee
            )
            await emit_event("info", "trade", f"매도: {symbol}", metadata={
                "price": price,
                "strategy": primary_signal.strategy_name,
                "confidence": round(decision.combined_confidence, 2),
                "pnl_pct": round(pnl_pct, 2),
                "entry_price": entry_price,
            })

            # 트래커 제거
            self._position_trackers.pop(symbol, None)

        # 매매 추적
        now = datetime.now(timezone.utc)
        self._last_trade_time[symbol] = now
        self._daily_trade_count += 1
        if decision.action == SignalType.BUY:
            self._daily_buy_count += 1
            self._daily_coin_buy_count[symbol] = self._daily_coin_buy_count.get(symbol, 0) + 1
        else:
            self._last_sell_time[symbol] = now  # 매도 후 재매수 대기용
            await self._on_sell_completed()

        # 브로드캐스트
        if self._broadcast_callback:
            await self._broadcast_callback({
                "event": "trade_executed",
                "data": {
                    "symbol": symbol,
                    "side": decision.action.value.lower(),
                    "price": price,
                    "strategy": primary_signal.strategy_name,
                    "confidence": decision.combined_confidence,
                    "reason": decision.final_reason,
                },
            })

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def strategies(self) -> dict[str, BaseStrategy]:
        return self._strategies

    @property
    def rotation_status(self) -> dict:
        return {
            "all_surge_scores": self._all_surge_scores,
            "surge_threshold": self._ec.surge_threshold,
            "current_surge_symbol": self._current_surge_symbol,
            "last_rotation_time": self._last_rotation_time,
            "last_scan_time": self._last_surge_scan_time,
            "rotation_enabled": self._ec.rotation_enabled,
            "rotation_cooldown_sec": self._ec.rotation_cooldown_sec,
            "market_state": self._market_state,
            "market_confidence": self._market_confidence,
            "tracked_coins": self.tracked_coins,
            "rotation_coins": self._get_rotation_coins(),
            "rotation_coins_count": len(self._get_rotation_coins()),
            "rotation_coins_updated": self._rotation_coins_updated,
        }
