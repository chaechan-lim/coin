"""
Volume Momentum 라이브 엔진 (선물).

전략:
- 1h 거래량 2x+ 급증 + 6h 모멘텀 방향 + RSI 필터 → 추세 따라가기
- 양방향 (상승+거래량→long, 하락+거래량→short)
- SL/TP: ATR 배수 기반
- 선물 2x leverage

안전: 누적 -10%, 일일 -5% 자동 중지.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np
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

DEFAULT_COINS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]


@dataclass
class VMPosition:
    symbol: str
    side: str  # "long" or "short"
    quantity: float
    entry_price: float
    sl_price: float
    tp_price: float


class VolumeMomentumEngine:
    """1h 거래량 급증 + 모멘텀 방향 → 추세 추종 엔진."""

    EXCHANGE_NAME = "binance_vol_mom"
    STRATEGY_NAME = "volume_momentum"

    def __init__(
        self,
        config: AppConfig,
        futures_exchange: ExchangeAdapter,
        market_data: MarketDataService,
        initial_capital_usdt: float = 100.0,
        leverage: int = 2,
        coins: list[str] | None = None,
        vol_mult: float = 2.0,
        rsi_long_max: float = 60.0,
        rsi_short_min: float = 40.0,
        sl_atr_mult: float = 2.5,
        tp_atr_mult: float = 5.0,
    ):
        self._config = config
        self._exchange = futures_exchange
        self._market_data = market_data
        self._initial_capital = initial_capital_usdt
        self._leverage = leverage
        self._coins = coins or list(DEFAULT_COINS)
        self._vol_mult = vol_mult
        self._rsi_long_max = rsi_long_max
        self._rsi_short_min = rsi_short_min
        self._sl_atr_mult = sl_atr_mult
        self._tp_atr_mult = tp_atr_mult

        self._is_running = False
        self._task: asyncio.Task | None = None
        self._positions: dict[str, VMPosition] = {}
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
        self._task = asyncio.create_task(self._loop(), name="volume_momentum_loop")
        logger.info("volume_momentum_started", capital=self._initial_capital, coins=self._coins)
        await emit_event("info", "engine",
                         f"Volume Momentum 시작 ({self._initial_capital} USDT, {self._leverage}x)")

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
        logger.info("volume_momentum_stopped")

    async def _loop(self):
        """매시간 xx:05 평가 (1h 캔들 close 후)."""
        while self._is_running:
            try:
                now = datetime.now(timezone.utc)
                # 다음 xx:05 계산
                target = now.replace(minute=5, second=0, microsecond=0)
                if now >= target:
                    from datetime import timedelta
                    target += timedelta(hours=1)
                wait = (target - now).total_seconds()
                await asyncio.sleep(wait)
                if self._is_running:
                    await self._evaluate()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("vol_mom_loop_error", error=str(e), exc_info=True)
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
                logger.error("vol_mom_eval_error", symbol=symbol, error=str(e), exc_info=True)

        logger.info("vol_mom_eval_complete", positions=len(self._positions),
                     pnl=round(self._cumulative_pnl, 2))
        await self._check_loss_limits()

    async def _evaluate_symbol(self, symbol: str):
        df = await self._market_data.get_ohlcv_df(symbol, "1h", limit=50)
        if df is None or len(df) < 20:
            return

        # 현재 포지션 SL/TP 체크 (intra-candle: high/low)
        if symbol in self._positions:
            await self._check_sl_tp(symbol, df)
            return

        # 거래량 급증 감지
        vol_ratio = self._compute_vol_ratio(df)
        if vol_ratio < self._vol_mult:
            return

        # 6h 모멘텀 방향
        close_now = float(df["close"].iloc[-1])
        close_6h_ago = float(df["close"].iloc[-7]) if len(df) >= 7 else float(df["close"].iloc[0])
        momentum = close_now - close_6h_ago

        # RSI 계산
        rsi = self._compute_rsi(df)
        if rsi is None:
            return

        # ATR 계산
        atr = self._compute_atr(df)
        if atr is None or atr <= 0:
            return

        # 방향 결정
        side = None
        if momentum > 0 and rsi < self._rsi_long_max:
            side = "long"
        elif momentum < 0 and rsi > self._rsi_short_min:
            side = "short"

        if side is None:
            return

        # SL/TP 계산 (ATR 기반)
        if side == "long":
            sl_price = close_now - atr * self._sl_atr_mult
            tp_price = close_now + atr * self._tp_atr_mult
        else:
            sl_price = close_now + atr * self._sl_atr_mult
            tp_price = close_now - atr * self._tp_atr_mult

        detail = (
            f"거래량 {vol_ratio:.1f}x | RSI {rsi:.0f} | "
            f"SL {sl_price:.2f} | TP {tp_price:.2f}"
        )
        logger.info("vol_mom_signal", symbol=symbol, side=side,
                     vol_ratio=round(vol_ratio, 1), rsi=round(rsi, 1),
                     sl=round(sl_price, 2), tp=round(tp_price, 2))

        await self._open_position(symbol, side, close_now, sl_price, tp_price, detail)

    @staticmethod
    def _compute_vol_ratio(df) -> float:
        """최근 1h 거래량 / 20시간 평균 거래량."""
        volumes = df["volume"].values
        if len(volumes) < 21:
            return 0.0
        current_vol = float(volumes[-1])
        avg_vol = float(np.mean(volumes[-21:-1]))
        if avg_vol <= 0:
            return 0.0
        return current_vol / avg_vol

    @staticmethod
    def _compute_rsi(df, period: int = 14) -> Optional[float]:
        """RSI 계산."""
        closes = df["close"].values
        if len(closes) < period + 1:
            return None
        deltas = np.diff(closes[-(period + 1):])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = float(np.mean(gains))
        avg_loss = float(np.mean(losses))
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1 + rs))

    @staticmethod
    def _compute_atr(df, period: int = 14) -> Optional[float]:
        """ATR 계산."""
        if len(df) < period + 1:
            return None
        high = df["high"].values[-(period + 1):]
        low = df["low"].values[-(period + 1):]
        close = df["close"].values[-(period + 1):]
        tr_list = []
        for i in range(1, len(high)):
            tr = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
            tr_list.append(tr)
        return float(np.mean(tr_list[-period:])) if tr_list else None

    async def _check_sl_tp(self, symbol: str, df):
        """SL/TP intra-candle 체크 (high/low 사용)."""
        pos = self._positions[symbol]
        current_high = float(df["high"].iloc[-1])
        current_low = float(df["low"].iloc[-1])
        current_close = float(df["close"].iloc[-1])

        if pos.side == "long":
            if current_low <= pos.sl_price:
                await self._close_position(symbol, pos.sl_price, "sl_hit")
                return
            if current_high >= pos.tp_price:
                await self._close_position(symbol, pos.tp_price, "tp_hit")
                return
        else:
            if current_high >= pos.sl_price:
                await self._close_position(symbol, pos.sl_price, "sl_hit")
                return
            if current_low <= pos.tp_price:
                await self._close_position(symbol, pos.tp_price, "tp_hit")
                return

    async def _open_position(self, symbol: str, side: str, price: float,
                             sl_price: float, tp_price: float, detail: str):
        available = self._initial_capital + self._cumulative_pnl
        per_coin = available / len(self._coins)
        notional = per_coin * self._leverage * 0.9
        if notional < MIN_NOTIONAL:
            return
        qty = notional / price

        try:
            if side == "long":
                order = await self._exchange.create_market_buy(symbol, qty)
            else:
                order = await self._exchange.create_market_sell(symbol, qty)

            status = getattr(order, 'status', None)
            exec_qty = float(getattr(order, 'executed_quantity', None) or getattr(order, 'filled', 0) or 0)
            exec_price = float(getattr(order, 'executed_price', None) or getattr(order, 'average', 0) or 0)

            if status not in ('filled', 'closed') or exec_qty <= 0 or exec_price <= 0:
                logger.error("vol_mom_open_not_filled", symbol=symbol, side=side, status=status)
                return

            self._positions[symbol] = VMPosition(
                symbol=symbol, side=side, quantity=exec_qty,
                entry_price=exec_price, sl_price=sl_price, tp_price=tp_price,
            )
            await self._record_order(symbol, "buy" if side == "long" else "sell",
                                     exec_price, exec_qty,
                                     reason=f"vol_mom_{side}_entry")
            await emit_event("info", "rnd_trade",
                             f"{'📈' if side == 'long' else '📉'} VolMom {side}: "
                             f"{symbol} @ {exec_price:.2f}",
                             detail=detail,
                             metadata={"engine": "VolMom", "symbol": symbol, "direction": side,
                                       "price": exec_price, "quantity": exec_qty, "leverage": self._leverage})
        except Exception as e:
            logger.error("vol_mom_open_error", symbol=symbol, side=side, error=str(e))

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
            filled_qty = float(getattr(order, 'executed_quantity', None) or getattr(order, 'filled', 0) or 0)
            exec_price = float(getattr(order, 'executed_price', None) or getattr(order, 'average', 0) or 0)

            if status not in ('filled', 'closed') or filled_qty <= 0 or exec_price <= 0:
                self._consecutive_close_failures += 1
                logger.error("vol_mom_close_not_filled", symbol=symbol, side=pos.side, status=status,
                             consecutive=self._consecutive_close_failures)
                if self._consecutive_close_failures >= 3:
                    self._paused = True
                    await emit_event("error", "engine",
                                     f"🚨 VolMom 청산 {self._consecutive_close_failures}회 연속 실패 — 자동 중지",
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
                                     pnl=pnl, reason=f"vol_mom_{pos.side}_exit_{reason}")
            emoji = "💰" if pnl > 0 else "💸"
            await emit_event("info", "rnd_trade",
                             f"{emoji} VolMom exit {pos.side}: {symbol} PnL {pnl:+.2f} ({reason})",
                             metadata={"engine": "VolMom", "symbol": symbol, "direction": pos.side,
                                       "price": exec_price, "entry_price": pos.entry_price,
                                       "realized_pnl": pnl, "reason": reason})
        except Exception as e:
            logger.error("vol_mom_close_error", symbol=symbol, error=str(e))

    async def _check_loss_limits(self):
        if self._cumulative_pnl <= -self._initial_capital * MAX_TOTAL_LOSS_PCT:
            self._paused = True
            await emit_event("error", "engine",
                             f"🚨 VolMom 누적 손실 한도 ({self._cumulative_pnl:.2f}) — 자동 중지")
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
                    net[o.symbol] = {"side": side, "qty": q, "price": p}
                elif "exit" in (o.signal_reason or ""):
                    net.pop(o.symbol, None)
                    cum_pnl += float(o.realized_pnl or 0)
            self._cumulative_pnl = cum_pnl
            for sym, info in net.items():
                # SL/TP를 복원 시 재계산 (간략화: ATR 없이 기본값)
                self._positions[sym] = VMPosition(
                    symbol=sym, side=info["side"], quantity=info["qty"],
                    entry_price=info["price"], sl_price=0.0, tp_price=0.0,
                )
            logger.info("vol_mom_restored", positions=len(self._positions),
                        pnl=round(cum_pnl, 2))

    def get_status(self) -> dict:
        return {
            "exchange": self.EXCHANGE_NAME,
            "is_running": self._is_running,
            "leverage": self._leverage,
            "capital_usdt": self._initial_capital,
            "coins": self._coins,
            "vol_mult": self._vol_mult,
            "positions": [
                {"symbol": p.symbol, "side": p.side, "qty": p.quantity,
                 "entry": p.entry_price, "sl": p.sl_price, "tp": p.tp_price}
                for p in self._positions.values()
            ],
            "cumulative_pnl": round(self._cumulative_pnl, 2),
            "daily_pnl": round(self._daily_pnl, 2),
            "paused": self._paused,
        }
