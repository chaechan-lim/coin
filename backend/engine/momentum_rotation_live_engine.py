"""
Momentum Rotation 라이브 엔진 (선물 Long/Short Equity).

전략:
- 5코인 중 가장 강한 2개 long + 가장 약한 2개 short
- 매주 리밸런싱 (수요일 UTC 01:00)
- 달러 뉴트럴 (long notional ≈ short notional)
- 백테스트: 360d +67.69%, Sharpe 1.78, MDD 12.65%

안전: 누적 -10%, 일일 -5% 자동 중지.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import structlog
from sqlalchemy import select

from config import AppConfig
from core.event_bus import emit_event
from core.models import Order
from db.session import get_session_factory
from exchange.base import ExchangeAdapter
from services.market_data import MarketDataService

logger = structlog.get_logger(__name__)

COINS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"]
LOOKBACK_DAYS = 14
REBALANCE_INTERVAL_HOURS = 168  # 7일
TOP_N = 2
BOTTOM_N = 2

MAX_TOTAL_LOSS_PCT = 0.10
MAX_DAILY_LOSS_PCT = 0.05
MIN_NOTIONAL = 10


@dataclass
class MomentumPosition:
    symbol: str
    side: str  # "long" or "short"
    quantity: float
    entry_price: float


class MomentumRotationLiveEngine:
    """Long/Short Momentum Rotation — 매주 리밸런싱."""

    EXCHANGE_NAME = "binance_momentum"

    def __init__(
        self,
        config: AppConfig,
        futures_exchange: ExchangeAdapter,
        market_data: MarketDataService,
        initial_capital_usdt: float = 100.0,
        leverage: int = 2,
        coins: list[str] | None = None,
    ):
        self._config = config
        self._exchange = futures_exchange
        self._market_data = market_data
        self._initial_capital = initial_capital_usdt
        self._leverage = leverage
        self._coins = coins or COINS

        self._is_running = False
        self._task: asyncio.Task | None = None
        self._positions: dict[str, MomentumPosition] = {}
        self._cumulative_pnl = 0.0
        self._daily_pnl = 0.0
        self._last_eval_date: Optional[datetime] = None
        self._paused = False
        self._daily_paused = False
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
        self._task = asyncio.create_task(self._loop(), name="momentum_rotation_loop")
        logger.info("momentum_rotation_started", capital=self._initial_capital, coins=self._coins)
        await emit_event("info", "engine", f"Momentum Rotation 시작 ({self._initial_capital} USDT, {self._leverage}x)")

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
        logger.info("momentum_rotation_stopped")

    async def _loop(self):
        """매주 수요일 UTC 01:00 리밸런싱."""
        while self._is_running:
            try:
                now = datetime.now(timezone.utc)
                # 다음 수요일 01:00 계산 (weekday: 0=월, 2=수)
                days_until_wed = (2 - now.weekday()) % 7
                if days_until_wed == 0 and now.hour >= 1:
                    days_until_wed = 7
                target = (now + pd.Timedelta(days=days_until_wed)).replace(
                    hour=1, minute=0, second=0, microsecond=0
                )
                wait = (target - now).total_seconds()
                logger.info("momentum_next_rebalance", at=target.isoformat(), wait_hours=round(wait/3600, 1))
                await asyncio.sleep(wait)
                if self._is_running:
                    await self._rebalance()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("momentum_loop_error", error=str(e), exc_info=True)
                await asyncio.sleep(300)

    async def evaluate_now(self):
        await self._rebalance()

    async def _rebalance(self):
        now = datetime.now(timezone.utc)
        today = now.date()
        if self._last_eval_date != today:
            self._daily_pnl = 0.0
            self._daily_paused = False
            self._last_eval_date = today

        if self._paused or self._daily_paused:
            logger.warning("momentum_paused", total=self._paused, daily=self._daily_paused)
            return

        logger.info("momentum_rebalance_start")

        # 1. 코인별 14일 수익률 계산
        coin_returns = {}
        for symbol in self._coins:
            try:
                df = await self._market_data.get_ohlcv_df(symbol, "1d", limit=30)
                if df is None or len(df) < LOOKBACK_DAYS + 2:
                    continue
                current = float(df["close"].iloc[-1])
                past = float(df["close"].iloc[-(LOOKBACK_DAYS + 1)])
                coin_returns[symbol] = (current - past) / past
            except Exception as e:
                logger.warning("momentum_fetch_failed", symbol=symbol, error=str(e))

        if len(coin_returns) < TOP_N + BOTTOM_N:
            logger.warning("momentum_insufficient_data", available=len(coin_returns))
            return

        sorted_coins = sorted(coin_returns.items(), key=lambda x: x[1], reverse=True)
        target_longs = [c for c, _ in sorted_coins[:TOP_N]]
        target_shorts = [c for c, _ in sorted_coins[-BOTTOM_N:]]

        logger.info("momentum_ranking",
                     longs=[(c, f"{r*100:.1f}%") for c, r in sorted_coins[:TOP_N]],
                     shorts=[(c, f"{r*100:.1f}%") for c, r in sorted_coins[-BOTTOM_N:]])

        # 2. 기존 포지션 청산
        for symbol in list(self._positions.keys()):
            await self._close_position(symbol)

        # 3. 새 포지션 진입
        available = self._initial_capital + self._cumulative_pnl
        n_sides = TOP_N + BOTTOM_N
        per_side = (available * self._leverage) / n_sides if n_sides > 0 else 0

        for symbol in target_longs:
            await self._open_position(symbol, "long", per_side)
        for symbol in target_shorts:
            await self._open_position(symbol, "short", per_side)

        await self._check_loss_limits()
        logger.info("momentum_rebalance_complete",
                     positions=len(self._positions),
                     cumulative_pnl=round(self._cumulative_pnl, 2))

    async def _open_position(self, symbol: str, side: str, notional: float):
        try:
            df = await self._market_data.get_ohlcv_df(symbol, "1d", limit=5)
            if df is None:
                return
            price = float(df["close"].iloc[-1])
            qty = notional / price
            if notional < MIN_NOTIONAL:
                return

            if side == "long":
                order = await self._exchange.create_market_buy(symbol, qty)
            else:
                order = await self._exchange.create_market_sell(symbol, qty)

            exec_price = float(getattr(order, 'executed_price', None) or price)
            exec_qty = float(getattr(order, 'executed_quantity', None) or qty)

            self._positions[symbol] = MomentumPosition(
                symbol=symbol, side=side, quantity=exec_qty, entry_price=exec_price
            )
            await self._record_order(symbol, "buy" if side == "long" else "sell",
                                      exec_price, exec_qty,
                                      reason=f"momentum_{side}_entry")
            await emit_event("info", "engine",
                             f"{'📈' if side=='long' else '📉'} Momentum {side}: {symbol} @ {exec_price:.2f}")
        except Exception as e:
            logger.error("momentum_open_error", symbol=symbol, side=side, error=str(e))

    async def _close_position(self, symbol: str):
        pos = self._positions.get(symbol)
        if not pos:
            return
        try:
            df = await self._market_data.get_ohlcv_df(symbol, "1d", limit=5)
            price = float(df["close"].iloc[-1]) if df is not None else pos.entry_price

            if pos.side == "long":
                order = await self._exchange.create_market_sell(symbol, pos.quantity)
                pnl = (price - pos.entry_price) * pos.quantity
            else:
                order = await self._exchange.create_market_buy(symbol, pos.quantity)
                pnl = (pos.entry_price - price) * pos.quantity

            exec_price = float(getattr(order, 'executed_price', None) or price)
            self._cumulative_pnl += pnl
            self._daily_pnl += pnl
            del self._positions[symbol]

            await self._record_order(symbol, "sell" if pos.side == "long" else "buy",
                                      exec_price, pos.quantity,
                                      pnl=pnl, reason=f"momentum_{pos.side}_exit")
            emoji = "💰" if pnl > 0 else "💸"
            await emit_event("info", "engine",
                             f"{emoji} Momentum exit {pos.side}: {symbol} PnL {pnl:+.2f}")
        except Exception as e:
            logger.error("momentum_close_error", symbol=symbol, error=str(e))

    async def _check_loss_limits(self):
        if self._cumulative_pnl <= -self._initial_capital * MAX_TOTAL_LOSS_PCT:
            self._paused = True
            await emit_event("error", "engine",
                             f"🚨 Momentum 누적 손실 한도 ({self._cumulative_pnl:.2f}) — 자동 중지")
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
                is_paper=False, strategy_name="momentum_rotation",
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
                .where(Order.strategy_name == "momentum_rotation")
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
                self._positions[sym] = MomentumPosition(
                    symbol=sym, side=info["side"], quantity=info["qty"], entry_price=info["price"]
                )
            logger.info("momentum_restored", positions=len(self._positions), pnl=round(cum_pnl, 2))

    def get_status(self) -> dict:
        return {
            "exchange": self.EXCHANGE_NAME,
            "is_running": self._is_running,
            "leverage": self._leverage,
            "capital_usdt": self._initial_capital,
            "coins": self._coins,
            "positions": [{"symbol": p.symbol, "side": p.side, "qty": p.quantity, "entry": p.entry_price}
                          for p in self._positions.values()],
            "cumulative_pnl": round(self._cumulative_pnl, 2),
            "daily_pnl": round(self._daily_pnl, 2),
            "paused": self._paused,
        }
