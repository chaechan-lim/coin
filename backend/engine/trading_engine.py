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
from core.event_bus import emit_event

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
    ):
        self._config = config
        self._exchange = exchange
        self._market_data = market_data
        self._order_manager = order_manager
        self._portfolio_manager = portfolio_manager
        self._combiner = combiner
        self._agent_coordinator = agent_coordinator
        self._exchange_name = exchange_name

        self._strategies: dict[str, BaseStrategy] = {}
        self._is_running = False
        self._paused_coins: set[str] = set()
        self._suppressed_coins: set[str] = set()
        self._last_trade_time: dict[str, datetime] = {}
        self._daily_buy_count = 0                       # 일일 총 매수 횟수
        self._daily_coin_buy_count: dict[str, int] = {} # 코인별 일일 매수 횟수
        self._daily_trade_count = 0                     # 레거시 (호환)
        self._daily_reset_date = datetime.now(timezone.utc).date()

        # SL/TP/trailing stop tracking
        self._position_trackers: dict[str, PositionTracker] = {}
        self._market_state: str = MarketState.SIDEWAYS.value
        self._market_confidence: float = 0.5
        self._market_state_updated: datetime | None = None

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

        # 비활성 전략 제거:
        # - Grid/DCA: 독립 관리형 (combiner 부적합)
        # - volatility_breakout/supertrend: 백테스트 0% 승률 → 잡음만 유발
        for excluded in ("grid_trading", "dca_momentum", "volatility_breakout", "supertrend"):
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
        await emit_event("info", "engine", "엔진 시작", metadata={"mode": self._config.trading.mode})
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
        await emit_event("info", "engine", "엔진 중지")

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

    def _reset_daily_counter(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._daily_reset_date:
            self._daily_buy_count = 0
            self._daily_coin_buy_count.clear()
            self._daily_trade_count = 0
            self._daily_reset_date = today

    def _can_trade(self, symbol: str, side: str = "buy") -> tuple[bool, str]:
        """Check anti-overtrading constraints.

        매도(sell)는 일일 제한/코인별 제한을 받지 않음 (손절·익절은 무조건 실행).
        매수(buy)만 일일 총 매수 상한 + 코인별 매수 상한 적용.
        코인당 최소 거래 간격 및 리스크 에이전트 일시중지는 매수에만 적용.
        """
        self._reset_daily_counter()

        if side == "buy":
            # 일일 총 매수 상한
            if self._daily_buy_count >= self._config.trading.daily_buy_limit:
                return False, f"Daily buy limit reached ({self._config.trading.daily_buy_limit})"

            # 코인별 일일 매수 상한
            coin_buys = self._daily_coin_buy_count.get(symbol, 0)
            if coin_buys >= self._config.trading.max_daily_coin_buys:
                return False, f"Coin daily buy limit reached ({symbol}: {coin_buys}/{self._config.trading.max_daily_coin_buys})"

            # 코인당 최소 거래 간격
            last = self._last_trade_time.get(symbol)
            if last:
                elapsed = (datetime.now(timezone.utc) - last).total_seconds()
                if elapsed < self._config.trading.min_trade_interval_sec:
                    remaining = self._config.trading.min_trade_interval_sec - elapsed
                    return False, f"Coin cooldown: {remaining:.0f}s remaining"

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
                if current_price > sma20 * 1.05:
                    scores[MarketState.STRONG_UPTREND] += 2
                elif current_price > sma20:
                    scores[MarketState.UPTREND] += 1.5
                elif current_price < sma20 * 0.95:
                    scores[MarketState.DOWNTREND] += 1.5
                elif current_price < sma20:
                    scores[MarketState.DOWNTREND] += 1.5

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

        # 5. 거래량 / volume_sma_20
        vol_sma = row.get("volume_sma_20")
        cur_vol = row.get("volume")
        if (vol_sma is not None and cur_vol is not None
                and not (isinstance(vol_sma, float) and pd.isna(vol_sma))
                and not (isinstance(cur_vol, float) and pd.isna(cur_vol))):
            vol_sma_f = float(vol_sma)
            if vol_sma_f > 0:
                vol_ratio = float(cur_vol) / vol_sma_f
                if vol_ratio > 2.0:
                    scores[MarketState.STRONG_UPTREND] += 0.5
                    scores[MarketState.DOWNTREND] += 0.5

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
            df = await self._market_data.get_candles("BTC/KRW", "4h", 200)
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
            if getattr(position, 'is_surge', False):
                # 서지 코인 → 서지 프로필 복원
                tracker = PositionTracker(
                    entry_price=position.average_buy_price,
                    highest_price=position.average_buy_price,
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
                # 일반 코인 → 동적 SL 계산
                tracker = PositionTracker(
                    entry_price=position.average_buy_price,
                    highest_price=position.average_buy_price,
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
                    tracker.stop_loss_pct = 5.0
                logger.info("tracker_restored_normal", symbol=symbol, sl=round(tracker.stop_loss_pct, 2))
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

        # 5. 시간 기반 강제 청산 (서지 코인 등)
        if not sell_reason and tracker.max_hold_hours > 0:
            held_hours = (datetime.now(timezone.utc) - tracker.entered_at).total_seconds() / 3600
            if held_hours >= tracker.max_hold_hours:
                sell_reason = f"보유 시간 초과: {held_hours:.1f}h (한도 {tracker.max_hold_hours:.0f}h, 수익 {pnl_pct:+.1f}%)"

        if sell_reason:
            logger.info(
                "stop_condition_triggered",
                symbol=symbol,
                reason=sell_reason,
                price=price,
                entry=entry,
                pnl_pct=round(pnl_pct, 2),
            )
            await emit_event(
                "warning", "trade", sell_reason,
                metadata={"symbol": symbol, "pnl_pct": round(pnl_pct, 2), "price": price, "entry_price": entry},
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

        await self._portfolio_manager.update_position_on_sell(
            session, symbol, position.quantity, price,
            position.quantity * price, order.fee
        )

        # 트래커 제거
        self._position_trackers.pop(symbol, None)

        # 매매 추적 (매도는 buy 카운터에 포함하지 않음)
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

    # ── 포트폴리오 리밸런싱 ─────────────────────────────────────────

    _REBALANCE_COOLDOWN_SEC = 3600  # 동일 코인 1시간 쿨다운

    async def _check_and_rebalance(self, session: AsyncSession) -> None:
        """비중 초과 코인 자동 일부 매도 (max_single_coin_pct → target_pct)."""
        risk = self._config.risk
        if not risk.rebalancing_enabled:
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

            if weight <= risk.max_single_coin_pct:
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
            target = risk.rebalancing_target_pct
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

        order = await self._order_manager.create_order(
            session, symbol, "sell", qty, price, signal,
            order_type="market",
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
        # 시장 상태 업데이트
        await self._maybe_update_market_state()

        session_factory = get_session_factory()
        async with session_factory() as session:
            try:
                # 매 사이클 시작 시 현금 잔고 정합성 확인
                await self._portfolio_manager.reconcile_cash_from_db(session)

                # 포트폴리오 리밸런싱 (비중 초과 코인 자동 일부 매도)
                await self._check_and_rebalance(session)

                coins = set(self._config.trading.tracked_coins)

                # 보유 중인 포지션도 평가 대상에 포함 (서지 코인 SL/TP/SELL)
                result = await session.execute(
                    select(Position.symbol).where(Position.quantity > 0, Position.exchange == self._exchange_name)
                )
                held = {r[0] for r in result.all()}
                all_coins = list(coins | held)

                for symbol in all_coins:
                    try:
                        await self._evaluate_coin(session, symbol)
                    except Exception as coin_err:
                        logger.error("evaluate_coin_error", symbol=symbol, error=str(coin_err))
                        continue

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
                await emit_event("error", "engine", "평가 사이클 오류", detail=str(e))

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

        # ── 2. 매수 가능 여부 체크 (매도는 항상 허용) ──
        can_buy, buy_block_reason = self._can_trade(symbol, side="buy")

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
        if signals:
            decision = self._combiner.combine(signals)
            # can_buy=False → 매수만 차단, 매도는 항상 허용
            if can_buy or decision.action == SignalType.SELL:
                await self._process_decision(session, symbol, decision)

    # ── 거래량 급등 로테이션 ──────────────────────────────────────

    _STABLECOINS = {"USDT/KRW", "USDC/KRW", "DAI/KRW", "TUSD/KRW"}
    _ROTATION_REFRESH_SEC = 6 * 3600  # 6시간마다 갱신
    _MIN_QUOTE_VOLUME = 1e9  # 최소 24h 거래대금 10억원
    _MAX_ROTATION_COINS = 40  # 로테이션 코인 상한

    async def _refresh_rotation_coins(self) -> None:
        """빗썸 전체 마켓에서 24h 거래대금 상위 코인을 로테이션 대상으로 선정."""
        now = datetime.now(timezone.utc)
        if (self._rotation_coins_updated
                and (now - self._rotation_coins_updated).total_seconds() < self._ROTATION_REFRESH_SEC):
            return

        try:
            tickers = await self._exchange.fetch_tickers()
            tracked = set(self._config.trading.tracked_coins)

            ranked = []
            for sym, t in tickers.items():
                if not sym.endswith("/KRW"):
                    continue
                if sym in tracked or sym in self._STABLECOINS:
                    continue
                vol = t.get("quoteVolume") or 0
                if vol >= self._MIN_QUOTE_VOLUME:
                    ranked.append((sym, vol))

            ranked.sort(key=lambda x: x[1], reverse=True)
            self._dynamic_rotation_coins = [sym for sym, _ in ranked[:self._MAX_ROTATION_COINS]]
            self._rotation_coins_updated = now

            logger.info(
                "rotation_coins_refreshed",
                count=len(self._dynamic_rotation_coins),
                top5=[s for s, _ in ranked[:5]],
            )
        except Exception as e:
            logger.warning("rotation_coins_refresh_failed", error=str(e))
            # 실패 시 config 폴백
            if not self._dynamic_rotation_coins:
                self._dynamic_rotation_coins = list(self._config.trading.rotation_coins)

    def _get_rotation_coins(self) -> list[str]:
        """동적 코인이 있으면 사용, 없으면 config 폴백."""
        if self._dynamic_rotation_coins:
            return self._dynamic_rotation_coins
        return list(self._config.trading.rotation_coins)

    async def _scan_volume_surges(self) -> list[tuple[str, float]]:
        """거래대금 상위 코인 거래량 서지 스캔. (symbol, surge_score) 리스트 반환."""
        await self._refresh_rotation_coins()
        surges: list[tuple[str, float]] = []
        all_scores: dict[str, float] = {}
        threshold = self._config.trading.surge_threshold
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
            if elapsed < self._config.trading.rotation_cooldown_sec:
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

        decision = self._combiner.combine(signals)

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

            await self._portfolio_manager.update_position_on_buy(
                session, symbol, amount, price, amount_krw, order.fee,
                is_surge=True,
            )

            # 서지 전용 포지션 트래커 (백테스트 C 프로필)
            self._position_trackers[symbol] = PositionTracker(
                entry_price=price,
                highest_price=price,
                stop_loss_pct=4.0,
                take_profit_pct=8.0,
                trailing_activation_pct=1.5,
                trailing_stop_pct=2.0,
                is_surge=True,
                max_hold_hours=48,
            )

            self._last_trade_time[symbol] = datetime.now(timezone.utc)
            self._daily_trade_count += 1
            self._daily_buy_count += 1
            self._daily_coin_buy_count[symbol] = self._daily_coin_buy_count.get(symbol, 0) + 1

            logger.info(
                "rotation_buy",
                symbol=symbol,
                price=price,
                surge_score=round(surge_score, 1),
                confidence=round(confidence, 3),
                sl_pct=4.0,
            )
            await emit_event("info", "rotation", f"로테이션 매수: {symbol}", metadata={"surge_score": round(surge_score, 1), "price": price})

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
            if self._config.trading.asymmetric_mode:
                # 하락장 매수 완전 차단
                if self._market_state in ("crash", "downtrend"):
                    logger.info("asymmetric_buy_blocked",
                                symbol=symbol, market_state=self._market_state)
                    return
                # 시장 상태별 신뢰도 임계값
                base_conf = self._config.trading.min_combined_confidence
                _asym_conf = {
                    "strong_uptrend": max(base_conf - 0.15, 0.35),
                    "uptrend":        max(base_conf - 0.10, 0.40),
                    "sideways":       base_conf + 0.05,
                }
                min_conf = _asym_conf.get(self._market_state, base_conf)
            else:
                # 기존 로직
                min_conf = self._config.trading.min_combined_confidence
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

            # 포지션 사이징
            cash = self._portfolio_manager.cash_balance
            if self._config.trading.asymmetric_mode:
                # 비대칭 사이징: 상승장 공격적, 횡보장 보수적
                _asym_size = {
                    "strong_uptrend": self._config.risk.max_trade_size_pct,       # 풀 사이즈
                    "uptrend":        self._config.risk.max_trade_size_pct * 0.8,  # 80%
                    "sideways":       self._config.risk.max_trade_size_pct * 0.5,  # 50%
                }
                size_pct = _asym_size.get(self._market_state, self._config.risk.max_trade_size_pct * 0.5)
            else:
                size_pct = self._config.risk.max_trade_size_pct
                if trend_action == "heavy_reduce":
                    size_pct *= 0.25
                    logger.info("buy_reduced_crash", symbol=symbol, size_pct=round(size_pct, 3))
                elif trend_action == "reduce":
                    size_pct *= 0.5
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
            await emit_event("info", "trade", f"매수: {symbol}", metadata={
                "price": price, "sl_pct": round(sl_pct, 2),
                "strategy": primary_signal.strategy_name,
                "confidence": round(decision.combined_confidence, 2),
                "amount_krw": round(amount_krw, 0),
                "market_state": self._market_state,
            })

        elif decision.action == SignalType.SELL:
            result = await session.execute(
                select(Position).where(Position.symbol == symbol, Position.quantity > 0, Position.exchange == self._exchange_name)
            )
            position = result.scalar_one_or_none()
            if not position or position.quantity <= 0:
                return

            order = await self._order_manager.create_order(
                session, symbol, "sell", position.quantity, price, primary_signal, decision
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
                return

            await self._portfolio_manager.update_position_on_sell(
                session, symbol, position.quantity, price,
                position.quantity * price, order.fee
            )
            # P&L 계산
            entry_price = position.average_buy_price or price
            pnl_pct = (price - entry_price) / entry_price * 100 if entry_price > 0 else 0
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
        self._last_trade_time[symbol] = datetime.now(timezone.utc)
        self._daily_trade_count += 1
        if decision.action == SignalType.BUY:
            self._daily_buy_count += 1
            self._daily_coin_buy_count[symbol] = self._daily_coin_buy_count.get(symbol, 0) + 1

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
            "market_confidence": self._market_confidence,
            "tracked_coins": self._config.trading.tracked_coins,
            "rotation_coins": self._get_rotation_coins(),
            "rotation_coins_count": len(self._get_rotation_coins()),
            "rotation_coins_updated": self._rotation_coins_updated,
        }
