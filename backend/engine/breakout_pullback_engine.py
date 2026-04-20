"""
Breakout Pullback 라이브 엔진 (선물).

전략:
- 일봉 N일 high/low 돌파 감지 → 2-4% 풀백 후 진입 (가짜 돌파 필터)
- 양방향 (high 돌파→long 풀백 대기, low 돌파→short 풀백 대기)
- 선물 2x leverage

안전: 누적 -10%, 일일 -5% 자동 중지.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import select

from config import AppConfig
from core.event_bus import emit_event
from core.models import Order
from db.session import get_session_factory
from exchange.base import ExchangeAdapter
from services.market_data import MarketDataService

logger = structlog.get_logger(__name__)

MAX_TOTAL_LOSS_PCT = 0.10
MAX_DAILY_LOSS_PCT = 0.05
MIN_NOTIONAL = 10

DEFAULT_COINS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"]


@dataclass
class BreakoutSignal:
    """돌파 감지 후 풀백 대기 상태."""
    symbol: str
    side: str  # "long" or "short"
    breakout_price: float  # 돌파가
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BPPosition:
    symbol: str
    side: str  # "long" or "short"
    quantity: float
    entry_price: float
    sl_price: float
    tp_price: float
    trail_activated: bool = False
    highest_since_entry: float = 0.0
    lowest_since_entry: float = float("inf")


class BreakoutPullbackEngine:
    """일봉 N일 고가/저가 돌파 → 풀백 진입 엔진."""

    EXCHANGE_NAME = "binance_breakout_pb"
    STRATEGY_NAME = "breakout_pullback"

    def __init__(
        self,
        config: AppConfig,
        futures_exchange: ExchangeAdapter,
        market_data: MarketDataService,
        initial_capital_usdt: float = 150.0,
        leverage: int = 2,
        coins: list[str] | None = None,
        lookback: int = 20,
        pullback_pct: float = 4.0,
        sl_pct: float = 5.0,
        tp_pct: float = 8.0,
        trail_act: float = 5.0,
        trail_stop: float = 3.0,
    ):
        self._config = config
        self._exchange = futures_exchange
        self._market_data = market_data
        self._initial_capital = initial_capital_usdt
        self._leverage = leverage
        self._coins = coins or list(DEFAULT_COINS)
        self._lookback = lookback
        self._pullback_pct = pullback_pct
        self._sl_pct = sl_pct
        self._tp_pct = tp_pct
        self._trail_act = trail_act
        self._trail_stop = trail_stop

        self._is_running = False
        self._task: asyncio.Task | None = None
        self._positions: dict[str, BPPosition] = {}
        self._pending_signals: dict[str, BreakoutSignal] = {}
        self._cumulative_pnl = 0.0
        self._daily_pnl = 0.0
        self._last_eval_date: Optional[datetime] = None
        self._paused = False
        self._daily_paused = False
        self._consecutive_close_failures = 0
        self._coordinator = None

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def tracked_coins(self) -> list[str]:
        return list(self._coins)

    def set_engine_registry(self, r): pass
    def set_broadcast_callback(self, c): pass
    def set_agent_coordinator(self, c): pass
    def set_futures_rnd_coordinator(self, coord):
        self._coordinator = coord

    async def start(self):
        if self._is_running:
            return
        self._is_running = True
        await self._restore_state()
        self._task = asyncio.create_task(self._loop(), name="breakout_pullback_loop")
        logger.info("breakout_pullback_started", capital=self._initial_capital, coins=self._coins)
        await emit_event("info", "engine",
                         f"Breakout Pullback 시작 ({self._initial_capital} USDT, {self._leverage}x)")

    async def stop(self):
        if not self._is_running:
            return
        self._is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("breakout_pullback_stopped")

    async def _loop(self):
        """매일 UTC 00:45 평가 (일봉 close 후)."""
        while self._is_running:
            try:
                now = datetime.now(timezone.utc)
                # 다음 UTC 00:45 계산
                target = now.replace(hour=0, minute=45, second=0, microsecond=0)
                if now >= target:
                    from datetime import timedelta
                    target += timedelta(days=1)
                wait = (target - now).total_seconds()
                logger.info("breakout_pb_next_eval", at=target.isoformat(),
                            wait_hours=round(wait / 3600, 1))
                await asyncio.sleep(wait)
                if self._is_running:
                    await self._evaluate()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("breakout_pb_loop_error", error=str(e), exc_info=True)
                await asyncio.sleep(300)

    async def evaluate_now(self):
        await self._evaluate()

    async def _evaluate(self):
        now = datetime.now(timezone.utc)
        today = now.date()
        if self._last_eval_date != today:
            self._daily_pnl = 0.0
            self._daily_paused = False
            self._last_eval_date = today

        if self._paused or self._daily_paused:
            return

        for symbol in self._coins:
            try:
                await self._evaluate_symbol(symbol)
            except Exception as e:
                logger.error("breakout_pb_eval_error", symbol=symbol, error=str(e), exc_info=True)

        logger.info("breakout_pb_eval_complete", positions=len(self._positions),
                     pending=len(getattr(self, '_pending_signals', {})),
                     pnl=round(self._cumulative_pnl, 2))
        await self._check_loss_limits()

    async def _evaluate_symbol(self, symbol: str):
        df = await self._market_data.get_ohlcv_df(symbol, "1d", limit=self._lookback + 5)
        if df is None or len(df) < self._lookback + 1:
            return

        # 현재 포지션 SL/TP 체크
        if symbol in self._positions:
            await self._check_sl_tp(symbol, df)
            return

        current_close = float(df["close"].iloc[-1])
        lookback_data = df.iloc[-(self._lookback + 1):-1]  # 최근 N일 (오늘 제외)
        high_n = float(lookback_data["high"].max())
        low_n = float(lookback_data["low"].min())

        # 풀백 대기 중인 시그널 확인
        if symbol in self._pending_signals:
            signal = self._pending_signals[symbol]
            await self._check_pullback_entry(symbol, signal, current_close)
            return

        # 신규 돌파 감지
        if current_close > high_n:
            self._pending_signals[symbol] = BreakoutSignal(
                symbol=symbol, side="long", breakout_price=current_close,
            )
            logger.info("breakout_detected", symbol=symbol, side="long",
                        breakout_price=current_close, high_n=high_n)
        elif current_close < low_n:
            self._pending_signals[symbol] = BreakoutSignal(
                symbol=symbol, side="short", breakout_price=current_close,
            )
            logger.info("breakout_detected", symbol=symbol, side="short",
                        breakout_price=current_close, low_n=low_n)

    async def _check_pullback_entry(self, symbol: str, signal: BreakoutSignal, current_close: float):
        """풀백 진입 조건 확인."""
        # 풀백 대기 3일 초과 시 시그널 취소
        elapsed = (datetime.now(timezone.utc) - signal.detected_at).total_seconds()
        if elapsed > 3 * 86400:
            del self._pending_signals[symbol]
            logger.info("breakout_signal_expired", symbol=symbol, side=signal.side)
            return

        pullback_threshold = signal.breakout_price * (self._pullback_pct / 100.0)

        if signal.side == "long":
            # 가격이 돌파가 대비 pullback_pct% 이상 하락했다가 회복 시 진입
            if current_close <= signal.breakout_price - pullback_threshold:
                # 풀백 발생 — 진입
                await self._open_position(symbol, "long", current_close)
                del self._pending_signals[symbol]
        else:
            if current_close >= signal.breakout_price + pullback_threshold:
                await self._open_position(symbol, "short", current_close)
                del self._pending_signals[symbol]

    async def _check_sl_tp(self, symbol: str, df):
        """SL/TP/trailing 체크."""
        pos = self._positions[symbol]
        current_close = float(df["close"].iloc[-1])

        # Trailing 업데이트
        if pos.side == "long":
            if current_close > pos.highest_since_entry:
                pos.highest_since_entry = current_close
            # trailing 활성화
            gain_pct = (current_close - pos.entry_price) / pos.entry_price * 100
            if gain_pct >= self._trail_act:
                pos.trail_activated = True
        else:
            if current_close < pos.lowest_since_entry:
                pos.lowest_since_entry = current_close
            gain_pct = (pos.entry_price - current_close) / pos.entry_price * 100
            if gain_pct >= self._trail_act:
                pos.trail_activated = True

        # SL 체크
        if pos.side == "long" and current_close <= pos.sl_price:
            await self._close_position(symbol, current_close, "sl_hit")
            return
        if pos.side == "short" and current_close >= pos.sl_price:
            await self._close_position(symbol, current_close, "sl_hit")
            return

        # TP 체크
        if pos.side == "long" and current_close >= pos.tp_price:
            await self._close_position(symbol, current_close, "tp_hit")
            return
        if pos.side == "short" and current_close <= pos.tp_price:
            await self._close_position(symbol, current_close, "tp_hit")
            return

        # Trailing stop 체크
        if pos.trail_activated:
            if pos.side == "long":
                trail_stop_price = pos.highest_since_entry * (1 - self._trail_stop / 100)
                if current_close <= trail_stop_price:
                    await self._close_position(symbol, current_close, "trail_stop")
                    return
            else:
                trail_stop_price = pos.lowest_since_entry * (1 + self._trail_stop / 100)
                if current_close >= trail_stop_price:
                    await self._close_position(symbol, current_close, "trail_stop")
                    return

    async def _open_position(self, symbol: str, side: str, price: float):
        available = self._initial_capital + self._cumulative_pnl
        per_coin = available / len(self._coins)
        notional = per_coin * self._leverage * 0.9
        if notional < MIN_NOTIONAL:
            return
        qty = notional / price

        if side == "long":
            sl_price = price * (1 - self._sl_pct / 100)
            tp_price = price * (1 + self._tp_pct / 100)
        else:
            sl_price = price * (1 + self._sl_pct / 100)
            tp_price = price * (1 - self._tp_pct / 100)

        try:
            if side == "long":
                order = await self._exchange.create_market_buy(symbol, qty)
            else:
                order = await self._exchange.create_market_sell(symbol, qty)

            status = getattr(order, 'status', None)
            exec_qty = float(order.filled or 0)
            exec_price = float(order.price or 0)

            if status not in ('filled', 'closed') or exec_qty <= 0 or exec_price <= 0:
                logger.error("breakout_pb_open_not_filled", symbol=symbol, side=side, status=status)
                return

            self._positions[symbol] = BPPosition(
                symbol=symbol, side=side, quantity=exec_qty, entry_price=exec_price,
                sl_price=sl_price, tp_price=tp_price,
                highest_since_entry=exec_price, lowest_since_entry=exec_price,
            )
            await self._record_order(symbol, "buy" if side == "long" else "sell",
                                     exec_price, exec_qty,
                                     reason=f"breakout_pb_{side}_entry")
            detail = (
                f"SL {sl_price:.2f} (-{self._sl_pct}%) | "
                f"TP {tp_price:.2f} (+{self._tp_pct}%) | "
                f"풀백 {self._pullback_pct}% 후 진입"
            )
            await emit_event("info", "rnd_trade",
                             f"{'📈' if side == 'long' else '📉'} BreakoutPB {side}: "
                             f"{symbol} @ {exec_price:.2f}",
                             detail=detail,
                             metadata={"engine": "BreakoutPB", "symbol": symbol, "direction": side,
                                       "price": exec_price, "quantity": exec_qty, "leverage": self._leverage})
        except Exception as e:
            logger.error("breakout_pb_open_error", symbol=symbol, side=side, error=str(e))

    async def _close_position(self, symbol: str, price: float, reason: str = ""):
        pos = self._positions.get(symbol)
        if not pos:
            return
        try:
            if pos.side == "long":
                order = await self._exchange.create_market_sell(symbol, pos.quantity, reduce_only=True)
            else:
                order = await self._exchange.create_market_buy(symbol, pos.quantity, reduce_only=True)

            status = getattr(order, 'status', None)
            filled_qty = float(order.filled or 0)
            exec_price = float(order.price or 0)

            if status not in ('filled', 'closed') or filled_qty <= 0 or exec_price <= 0:
                self._consecutive_close_failures += 1
                logger.error("breakout_pb_close_not_filled", symbol=symbol, side=pos.side, status=status,
                             consecutive=self._consecutive_close_failures)
                if self._consecutive_close_failures >= 3:
                    self._paused = True
                    await emit_event("error", "engine",
                                     f"🚨 BreakoutPB 청산 {self._consecutive_close_failures}회 연속 실패 — 자동 중지",
                                     detail=f"포지션 {pos.side} {symbol} qty={pos.quantity} 수동 확인 필요")
                return

            self._consecutive_close_failures = 0

            if pos.side == "long":
                pnl = (exec_price - pos.entry_price) * filled_qty
            else:
                pnl = (pos.entry_price - exec_price) * filled_qty

            self._cumulative_pnl += pnl
            self._daily_pnl += pnl
            del self._positions[symbol]

            await self._record_order(symbol, "sell" if pos.side == "long" else "buy",
                                     exec_price, filled_qty,
                                     pnl=pnl, reason=f"breakout_pb_{pos.side}_exit_{reason}")
            emoji = "💰" if pnl > 0 else "💸"
            await emit_event("info", "rnd_trade",
                             f"{emoji} BreakoutPB exit {pos.side}: {symbol} PnL {pnl:+.2f} ({reason})",
                             metadata={"engine": "BreakoutPB", "symbol": symbol, "direction": pos.side,
                                       "price": exec_price, "entry_price": pos.entry_price,
                                       "realized_pnl": pnl, "reason": reason})
        except Exception as e:
            logger.error("breakout_pb_close_error", symbol=symbol, error=str(e))

    async def _check_loss_limits(self):
        if self._cumulative_pnl <= -self._initial_capital * MAX_TOTAL_LOSS_PCT:
            self._paused = True
            await emit_event("error", "engine",
                             f"🚨 BreakoutPB 누적 손실 한도 ({self._cumulative_pnl:.2f}) — 자동 중지")
        if self._daily_pnl <= -self._initial_capital * MAX_DAILY_LOSS_PCT:
            self._daily_paused = True

    async def _record_order(self, symbol, side, price, qty, pnl=0.0, reason=""):
        sf = get_session_factory()
        async with sf() as session:
            order = Order(
                exchange=self.EXCHANGE_NAME, symbol=symbol, side=side,
                order_type="market", status="filled",
                executed_price=price, executed_quantity=qty,
                fee=qty * price * 0.0004, fee_currency="USDT",
                is_paper=False, strategy_name=self.STRATEGY_NAME,
                signal_reason=reason, realized_pnl=pnl if "exit" in reason else 0.0,
                created_at=datetime.now(timezone.utc), filled_at=datetime.now(timezone.utc),
            )
            session.add(order)
            await session.commit()

    async def _restore_state(self):
        sf = get_session_factory()
        async with sf() as session:
            result = await session.execute(
                select(Order).where(Order.exchange == self.EXCHANGE_NAME)
                .where(Order.strategy_name == self.STRATEGY_NAME)
                .order_by(Order.created_at)
            )
            orders = result.scalars().all()
            net = {}
            cum_pnl = 0.0
            for o in orders:
                q = float(o.executed_quantity or 0)
                p = float(o.executed_price or 0)
                if "entry" in (o.signal_reason or ""):
                    side = "long" if o.side == "buy" else "short"
                    if side == "long":
                        sl = p * (1 - self._sl_pct / 100)
                        tp = p * (1 + self._tp_pct / 100)
                    else:
                        sl = p * (1 + self._sl_pct / 100)
                        tp = p * (1 - self._tp_pct / 100)
                    net[o.symbol] = {"side": side, "qty": q, "price": p, "sl": sl, "tp": tp}
                elif "exit" in (o.signal_reason or ""):
                    net.pop(o.symbol, None)
                    cum_pnl += float(o.realized_pnl or 0)
            self._cumulative_pnl = cum_pnl
            for sym, info in net.items():
                self._positions[sym] = BPPosition(
                    symbol=sym, side=info["side"], quantity=info["qty"],
                    entry_price=info["price"], sl_price=info["sl"], tp_price=info["tp"],
                    highest_since_entry=info["price"], lowest_since_entry=info["price"],
                )
            logger.info("breakout_pb_restored", positions=len(self._positions),
                        pnl=round(cum_pnl, 2))

    def get_status(self) -> dict:
        return {
            "exchange": self.EXCHANGE_NAME,
            "is_running": self._is_running,
            "leverage": self._leverage,
            "capital_usdt": self._initial_capital,
            "coins": self._coins,
            "lookback": self._lookback,
            "pullback_pct": self._pullback_pct,
            "positions": [
                {"symbol": p.symbol, "side": p.side, "qty": p.quantity,
                 "entry": p.entry_price, "sl": p.sl_price, "tp": p.tp_price,
                 "trail_activated": p.trail_activated}
                for p in self._positions.values()
            ],
            "pending_signals": [
                {"symbol": s.symbol, "side": s.side, "breakout_price": s.breakout_price}
                for s in self._pending_signals.values()
            ],
            "cumulative_pnl": round(self._cumulative_pnl, 2),
            "daily_pnl": round(self._daily_pnl, 2),
            "paused": self._paused,
        }
