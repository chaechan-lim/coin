import asyncio
import structlog
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from config import AppConfig
from core.enums import SignalType, MarketState
from core.models import Position
from exchange.base import ExchangeAdapter
from services.market_data import MarketDataService
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from strategies.combiner import SignalCombiner, CombinedDecision
from engine.order_manager import OrderManager
from engine.portfolio_manager import PortfolioManager
from db.session import get_session_factory

logger = structlog.get_logger(__name__)


# ── 시장 상태별 동적 손절 프로필 (하이브리드) ────────────────────────
# (atr_multiplier, floor_pct, cap_pct)
_DYNAMIC_SL_PROFILES = {
    "strong_uptrend": (2.5, 4.0, 12.0),
    "uptrend":        (2.0, 4.0, 10.0),
    "sideways":       (2.0, 4.0,  7.0),
    "downtrend":      (2.0, 4.0,  7.0),
}
_DEFAULT_SL_PROFILE = (2.0, 4.0, 7.0)


@dataclass
class PositionTracker:
    """In-memory state for SL/TP/trailing stop tracking."""
    entry_price: float
    highest_price: float
    stop_loss_pct: float = 5.0       # 동적 SL %
    take_profit_pct: float = 10.0
    trailing_activation_pct: float = 3.0
    trailing_stop_pct: float = 3.0
    trailing_active: bool = False     # 트레일링 활성 여부
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
    ):
        self._config = config
        self._exchange = exchange
        self._market_data = market_data
        self._order_manager = order_manager
        self._portfolio_manager = portfolio_manager
        self._combiner = combiner
        self._agent_coordinator = agent_coordinator

        self._strategies: dict[str, BaseStrategy] = {}
        self._is_running = False
        self._paused_coins: set[str] = set()
        self._suppressed_coins: set[str] = set()
        self._last_trade_time: dict[str, datetime] = {}
        self._daily_trade_count = 0
        self._daily_reset_date = datetime.now(timezone.utc).date()

        # SL/TP/trailing stop tracking
        self._position_trackers: dict[str, PositionTracker] = {}
        self._market_state: str = MarketState.SIDEWAYS.value
        self._market_state_updated: datetime | None = None

        # 거래량 급등 로테이션 상태
        self._last_rotation_time: datetime | None = None
        self._current_surge_symbol: str | None = None
        self._all_surge_scores: dict[str, float] = {}
        self._last_surge_scan_time: datetime | None = None

        # WebSocket broadcast callback
        self._broadcast_callback = None

    def set_broadcast_callback(self, callback) -> None:
        self._broadcast_callback = callback

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

        self._strategies = StrategyRegistry.create_all()

        # Grid/DCA는 combiner에서 제외 — 독립 전략이므로 라이브에서도 비활성
        for excluded in ("grid_trading", "dca_momentum"):
            self._strategies.pop(excluded, None)

        logger.info(
            "engine_initialized",
            strategies=list(self._strategies.keys()),
            mode=self._config.trading.mode,
        )

    async def start(self) -> None:
        """Start the trading engine main loop."""
        self._is_running = True
        logger.info("engine_started")
        while self._is_running:
            try:
                await self._evaluation_cycle()
            except Exception as e:
                logger.error("engine_cycle_error", error=str(e), exc_info=True)
            await asyncio.sleep(self._config.trading.evaluation_interval_sec)

    async def stop(self) -> None:
        """Stop the trading engine gracefully."""
        self._is_running = False
        logger.info("engine_stopping")

    def pause_buying(self, coins: list[str]) -> None:
        self._paused_coins.update(coins)
        logger.warning("buying_paused", coins=coins)

    def suppress_buys(self, coins: list[str]) -> None:
        self._suppressed_coins.update(coins)

    def resume_buying(self, coins: list[str] | None = None) -> None:
        if coins:
            self._paused_coins -= set(coins)
            self._suppressed_coins -= set(coins)
        else:
            self._paused_coins.clear()
            self._suppressed_coins.clear()

    def _reset_daily_counter(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._daily_reset_date:
            self._daily_trade_count = 0
            self._daily_reset_date = today

    def _can_trade(self, symbol: str) -> tuple[bool, str]:
        """Check anti-overtrading constraints."""
        self._reset_daily_counter()

        if self._daily_trade_count >= self._config.trading.daily_trade_limit:
            return False, f"Daily trade limit reached ({self._config.trading.daily_trade_limit})"

        last = self._last_trade_time.get(symbol)
        if last:
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            if elapsed < self._config.trading.min_trade_interval_sec:
                remaining = self._config.trading.min_trade_interval_sec - elapsed
                return False, f"Coin cooldown: {remaining:.0f}s remaining"

        if symbol in self._paused_coins:
            return False, "Buying paused by risk agent"

        return True, "OK"

    # ── 시장 상태 감지 ──────────────────────────────────────────────

    def _detect_market_state(self, df: pd.DataFrame) -> str:
        """SMA20/SMA60 + ADX + RSI로 시장 상태 감지."""
        if df is None or len(df) < 60:
            return MarketState.SIDEWAYS.value

        row = df.iloc[-1]
        sma20 = row.get("sma_20")
        sma60 = row.get("sma_60")
        adx = row.get("ADX_14")
        rsi = row.get("rsi_14")

        if any(v is None or (isinstance(v, float) and pd.isna(v))
               for v in [sma20, sma60, adx, rsi]):
            return MarketState.SIDEWAYS.value

        sma20, sma60, adx, rsi = float(sma20), float(sma60), float(adx), float(rsi)
        uptrend = sma20 > sma60
        strong_trend = adx > 25

        if uptrend and strong_trend and rsi > 55:
            return MarketState.STRONG_UPTREND.value
        elif uptrend:
            return MarketState.UPTREND.value
        elif not uptrend and (strong_trend or rsi < 45):
            return MarketState.DOWNTREND.value
        else:
            return MarketState.SIDEWAYS.value

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
            df = await self._market_data.get_candles("BTC/KRW", "4h", 200)
            new_state = self._detect_market_state(df)
            if new_state != self._market_state:
                logger.info(
                    "market_state_changed",
                    old=self._market_state,
                    new=new_state,
                )
                self._combiner.apply_market_state(new_state)

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
            # 트래커 없으면 생성 (재시작 후 복원)
            tracker = PositionTracker(
                entry_price=position.average_buy_price,
                highest_price=position.average_buy_price,
            )
            # 동적 SL 계산
            try:
                df = await self._market_data.get_candles(symbol, "4h", 200)
                tracker.stop_loss_pct = self._calc_dynamic_sl(
                    df, position.average_buy_price, self._market_state
                )
            except Exception:
                tracker.stop_loss_pct = 5.0  # fallback
            self._position_trackers[symbol] = tracker

        # 현재 가격
        try:
            price = await self._market_data.get_current_price(symbol)
        except Exception as e:
            logger.warning("price_fetch_failed_sl_check", symbol=symbol, error=str(e))
            return False

        # 최고가 업데이트
        if price > tracker.highest_price:
            tracker.highest_price = price

        entry = tracker.entry_price
        pnl_pct = (price - entry) / entry * 100

        sell_reason = None

        # 백테스트 동일 우선순위: 트레일링 활성화 → 트레일링 발동 → SL → TP(트레일링 미활성 시만)

        # 1. 트레일링 활성화 체크
        if (tracker.trailing_activation_pct > 0
                and not tracker.trailing_active
                and pnl_pct >= tracker.trailing_activation_pct):
            tracker.trailing_active = True

        # 2. 트레일링 스탑 발동
        if tracker.trailing_active and tracker.trailing_stop_pct > 0:
            drawdown_from_peak = (tracker.highest_price - price) / tracker.highest_price * 100
            if drawdown_from_peak >= tracker.trailing_stop_pct:
                sell_reason = (
                    f"Trailing Stop: 고점 대비 -{drawdown_from_peak:.2f}% "
                    f"(고점 {tracker.highest_price:.0f}, 현재 {price:.0f}, 수익 {pnl_pct:+.1f}%)"
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

        if sell_reason:
            logger.info(
                "stop_condition_triggered",
                symbol=symbol,
                reason=sell_reason,
                price=price,
                entry=entry,
                pnl_pct=round(pnl_pct, 2),
            )
            await self._execute_stop_sell(session, symbol, position, price, sell_reason)
            return True

        return False

    async def _execute_stop_sell(
        self,
        session: AsyncSession,
        symbol: str,
        position: Position,
        price: float,
        reason: str,
    ) -> None:
        """SL/TP/trailing에 의한 전량 매도."""
        # 시스템 생성 매도 시그널
        sell_signal = Signal(
            strategy_name="risk_management",
            signal_type=SignalType.SELL,
            confidence=1.0,
            reason=reason,
        )

        order = await self._order_manager.create_order(
            session, symbol, "sell", position.quantity, price, sell_signal
        )
        await self._portfolio_manager.update_position_on_sell(
            session, symbol, position.quantity, price,
            position.quantity * price, order.fee
        )

        # 트래커 제거
        self._position_trackers.pop(symbol, None)

        # 매매 추적
        self._last_trade_time[symbol] = datetime.now(timezone.utc)
        self._daily_trade_count += 1

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

    # ── 평가 사이클 ────────────────────────────────────────────────

    async def _evaluation_cycle(self) -> None:
        """Run one evaluation cycle for all tracked coins."""
        # 시장 상태 업데이트
        await self._maybe_update_market_state()

        session_factory = get_session_factory()
        async with session_factory() as session:
            try:
                coins = self._config.trading.tracked_coins

                for symbol in coins:
                    await self._evaluate_coin(session, symbol)

                # 거래량 급등 로테이션 모드
                if self._config.trading.rotation_enabled:
                    surges = await self._scan_volume_surges()
                    if surges:
                        await self._try_rotation(session, surges)
                    logger.info(
                        "surge_scan_complete",
                        surge_count=len(surges),
                        top_surges=[(s, round(sc, 1)) for s, sc in surges[:3]] if surges else [],
                    )

                await self._portfolio_manager.take_snapshot(session)
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

    async def _evaluate_coin(self, session: AsyncSession, symbol: str) -> None:
        """Evaluate a single coin: SL/TP first, then strategy signals."""
        # ── 1. 기존 포지션 SL/TP/trailing 체크 ──
        result = await session.execute(
            select(Position).where(Position.symbol == symbol, Position.quantity > 0)
        )
        position = result.scalar_one_or_none()

        if position and position.quantity > 0:
            stopped = await self._check_stop_conditions(session, symbol, position)
            if stopped:
                return  # 이미 매도했으므로 스킵

        # ── 2. 거래 가능 여부 체크 ──
        can_trade, reason = self._can_trade(symbol)

        # ── 3. 전략 시그널 수집 ──
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

        # ── 4. 결합 판단 + 실행 ──
        if signals and can_trade:
            decision = self._combiner.combine(signals)
            await self._process_decision(session, symbol, decision)

    # ── 거래량 급등 로테이션 ──────────────────────────────────────

    async def _scan_volume_surges(self) -> list[tuple[str, float]]:
        """20코인 거래량 서지 스캔. (symbol, surge_score) 리스트 반환."""
        surges: list[tuple[str, float]] = []
        all_scores: dict[str, float] = {}
        threshold = self._config.trading.surge_threshold
        for symbol in self._config.trading.rotation_coins:
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
        """서지 코인 중 최고 점수로 로테이션 시도."""
        now = datetime.now(timezone.utc)

        # 쿨다운 체크
        if self._last_rotation_time:
            elapsed = (now - self._last_rotation_time).total_seconds()
            if elapsed < self._config.trading.rotation_cooldown_sec:
                return

        for symbol, score in surges:
            # 현재 보유 코인이면 스킵
            if symbol == self._current_surge_symbol:
                continue

            # 서지 로테이션은 추세 필터 무시 (서지 자체가 강한 시그널)

            # 거래 가능 여부 체크
            can_trade, reason = self._can_trade(symbol)
            if not can_trade:
                continue

            # 전략 확인 (combiner)
            confirmed, confidence = await self._get_surge_confirmation(session, symbol)
            if not confirmed:
                continue

            # 기존 포지션 매도 (로테이션)
            await self._execute_rotation_sell(session)

            # 새 코인 매수
            await self._execute_rotation_buy(session, symbol, score, confidence)

            self._last_rotation_time = now
            self._current_surge_symbol = symbol
            break  # 최고 서지 1개만

    async def _get_surge_confirmation(self, session: AsyncSession, symbol: str) -> tuple[bool, float]:
        """서지 코인에 대해 기존 전략 파이프라인으로 BUY 확인."""
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

        decision = self._combiner.combine(signals)
        if (decision.action == SignalType.BUY
                and decision.combined_confidence >= self._config.trading.min_combined_confidence):
            return True, float(decision.combined_confidence)
        return False, 0.0

    async def _execute_rotation_sell(self, session: AsyncSession) -> None:
        """로테이션을 위한 기존 포지션 전량 매도."""
        result = await session.execute(
            select(Position).where(Position.quantity > 0)
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
            except Exception as e:
                logger.error("rotation_sell_error", symbol=position.symbol, error=str(e))

    async def _execute_rotation_buy(
        self, session: AsyncSession, symbol: str, surge_score: float, confidence: float,
    ) -> None:
        """서지 코인 매수. 기존 _process_decision BUY 로직 재사용."""
        try:
            ticker = await self._market_data.get_ticker(symbol)
            price = ticker.last

            cash = self._portfolio_manager.cash_balance
            amount_krw = cash * self._config.risk.max_trade_size_pct
            if amount_krw < 5000 and cash >= 5000:
                amount_krw = cash
            amount_krw = amount_krw / 1.003  # 수수료 마진

            min_order_krw = 500
            if amount_krw < min_order_krw:
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
            )
            await self._portfolio_manager.update_position_on_buy(
                session, symbol, amount, price, amount_krw, order.fee,
            )

            # 포지션 트래커 생성
            try:
                df = await self._market_data.get_candles(symbol, "4h", 200)
                sl_pct = self._calc_dynamic_sl(df, price, self._market_state)
            except Exception:
                sl_pct = 5.0
            self._position_trackers[symbol] = PositionTracker(
                entry_price=price,
                highest_price=price,
                stop_loss_pct=sl_pct,
            )

            self._last_trade_time[symbol] = datetime.now(timezone.utc)
            self._daily_trade_count += 1

            logger.info(
                "rotation_buy",
                symbol=symbol,
                price=price,
                surge_score=round(surge_score, 1),
                confidence=round(confidence, 3),
                sl_pct=round(sl_pct, 2),
            )

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
        """시장 상태별 매수 정책. 'reduce' / 'allow' 반환."""
        if self._market_state == "downtrend":
            return "reduce"  # 포지션 50% 축소 매수
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

        # 추세 필터: downtrend=50% 축소
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
            # 이미 포지션 있으면 추가 매수 안 함
            result = await session.execute(
                select(Position).where(Position.symbol == symbol, Position.quantity > 0)
            )
            if result.scalar_one_or_none():
                return

            # 포지션 사이징: max_trade_size_pct 사용 (소액 테스트 대응)
            cash = self._portfolio_manager.cash_balance
            size_pct = self._config.risk.max_trade_size_pct
            if trend_action == "reduce":
                size_pct *= 0.5  # downtrend: 50% 축소
                logger.info("buy_reduced_downtrend", symbol=symbol, size_pct=round(size_pct, 3))
            amount_krw = cash * size_pct

            # 최소 주문금액 미달 시 잔고 전체 시도
            if amount_krw < 5000 and cash >= 5000:
                amount_krw = cash

            # 수수료(0.25%) 감안 — 총비용이 잔고 초과하지 않도록
            amount_krw = amount_krw / 1.003  # 0.3% 마진 (수수료 + 안전마진)

            min_order_krw = 500  # 빗썸 최소 주문금액
            if amount_krw < min_order_krw:
                logger.debug("order_too_small", symbol=symbol, amount_krw=amount_krw)
                return

            amount = amount_krw / price

            order = await self._order_manager.create_order(
                session, symbol, "buy", amount, price, primary_signal, decision
            )
            await self._portfolio_manager.update_position_on_buy(
                session, symbol, amount, price, amount_krw, order.fee
            )

            # 포지션 트래커 생성 (SL/TP/trailing 추적 시작)
            try:
                df = await self._market_data.get_candles(symbol, "4h", 200)
                sl_pct = self._calc_dynamic_sl(df, price, self._market_state)
            except Exception:
                sl_pct = 5.0
            self._position_trackers[symbol] = PositionTracker(
                entry_price=price,
                highest_price=price,
                stop_loss_pct=sl_pct,
            )

            logger.info(
                "position_opened",
                symbol=symbol,
                price=price,
                sl_pct=round(sl_pct, 2),
                market_state=self._market_state,
            )

        elif decision.action == SignalType.SELL:
            result = await session.execute(
                select(Position).where(Position.symbol == symbol, Position.quantity > 0)
            )
            position = result.scalar_one_or_none()
            if not position or position.quantity <= 0:
                return

            order = await self._order_manager.create_order(
                session, symbol, "sell", position.quantity, price, primary_signal, decision
            )
            await self._portfolio_manager.update_position_on_sell(
                session, symbol, position.quantity, price,
                position.quantity * price, order.fee
            )

            # 트래커 제거
            self._position_trackers.pop(symbol, None)

        # 매매 추적
        self._last_trade_time[symbol] = datetime.now(timezone.utc)
        self._daily_trade_count += 1

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
            "surge_threshold": self._config.trading.surge_threshold,
            "current_surge_symbol": self._current_surge_symbol,
            "last_rotation_time": self._last_rotation_time,
            "last_scan_time": self._last_surge_scan_time,
            "rotation_enabled": self._config.trading.rotation_enabled,
            "rotation_cooldown_sec": self._config.trading.rotation_cooldown_sec,
            "market_state": self._market_state,
            "tracked_coins": self._config.trading.tracked_coins,
            "rotation_coins": self._config.trading.rotation_coins,
        }
