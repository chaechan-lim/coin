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

COINS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT",
    "UNI/USDT", "ATOM/USDT", "FIL/USDT", "APT/USDT", "ARB/USDT",
    "OP/USDT", "NEAR/USDT", "SUI/USDT", "TIA/USDT", "SEI/USDT",
    "INJ/USDT", "AAVE/USDT", "LTC/USDT", "ETC/USDT",
]
LOOKBACK_DAYS = 14
REBALANCE_INTERVAL_HOURS = 168  # 7일
TOP_N = 3
BOTTOM_N = 3

# SL/Trailing (백테스트: SL8+trail4/2 → 360d +173.8%)
SL_PCT = 8.0          # 손절 8% (레버리지 반영)
TRAIL_ACT_PCT = 4.0   # 수익 4% 이상 시 trailing 활성
TRAIL_STOP_PCT = 2.0  # 고점 대비 2% 하락 시 청산
DAILY_SL_CHECK = True  # 매일 SL/trailing 체크

MAX_TOTAL_LOSS_PCT = 0.10
MAX_DAILY_LOSS_PCT = 0.05
MIN_NOTIONAL = 10


@dataclass
class MomentumPosition:
    symbol: str
    side: str  # "long" or "short"
    quantity: float
    entry_price: float
    peak: float = 0.0  # trailing용 고점(long)/저점(short), 0이면 entry_price 사용


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
        self._consecutive_close_failures = 0
        self._coordinator = None
        self._last_rebalance_date = None

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
        """매일 SL/trailing 체크 + 매주 수요일 리밸런싱."""
        while self._is_running:
            try:
                now = datetime.now(timezone.utc)

                # 매일 SL/trailing 체크
                if DAILY_SL_CHECK and self._positions:
                    await self._check_sl_trailing()

                # 수요일이면 리밸런싱
                if now.weekday() == 2 and now.hour >= 1:
                    # 오늘 이미 리밸런싱했으면 스킵
                    if not self._last_rebalance_date or self._last_rebalance_date != now.date():
                        await self._rebalance()
                        self._last_rebalance_date = now.date()

                # 다음 날 01:05까지 대기
                tomorrow = (now + pd.Timedelta(days=1)).replace(hour=1, minute=5, second=0, microsecond=0)
                wait = max(60, (tomorrow - datetime.now(timezone.utc)).total_seconds())
                logger.info("momentum_next_check", at=tomorrow.isoformat(),
                            positions=len(self._positions), wait_hours=round(wait/3600, 1))
                await asyncio.sleep(wait)
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

        # 청산 실패한 포지션이 남아있으면 신규 진입 중단
        if self._positions:
            logger.warning("momentum_rebalance_aborted", remaining=list(self._positions.keys()))
            return

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

            # 체결 확인
            status = getattr(order, 'status', None)
            exec_qty = float(order.filled or 0)
            exec_price = float(order.price or 0)

            if status not in ('filled', 'closed') or exec_qty <= 0 or exec_price <= 0:
                logger.error("momentum_open_not_filled",
                             symbol=symbol, side=side, status=status, filled=exec_qty, price=exec_price)
                return  # 체결 안 됨 → 포지션 미등록

            self._positions[symbol] = MomentumPosition(
                symbol=symbol, side=side, quantity=exec_qty, entry_price=exec_price,
                peak=exec_price,
            )
            await self._record_order(symbol, "buy" if side == "long" else "sell",
                                      exec_price, exec_qty,
                                      reason=f"momentum_{side}_entry")
            notional = exec_qty * exec_price
            await emit_event("info", "rnd_trade",
                             f"{'📈' if side=='long' else '📉'} Momentum {side}: {symbol} @ {exec_price:.2f}",
                             detail=f"수량 {exec_qty:.6f} | 명목 {notional:.1f} USDT | 청산: 다음 주 리밸런싱",
                             metadata={"engine": "Momentum", "symbol": symbol, "direction": side,
                                       "price": exec_price, "quantity": exec_qty, "leverage": self._leverage})
        except Exception as e:
            logger.error("momentum_open_error", symbol=symbol, side=side, error=str(e))

    async def _check_sl_trailing(self):
        """매일 SL/trailing 체크 — 보유 포지션 high/low 기반."""
        for symbol in list(self._positions.keys()):
            pos = self._positions[symbol]
            try:
                df = await self._market_data.get_ohlcv_df(symbol, "1d", limit=3)
                if df is None or len(df) < 1:
                    continue
                last = df.iloc[-1]
                price = float(last["close"])
                high = float(last["high"])
                low = float(last["low"])

                if pos.side == "long":
                    # peak 초기화 방어 (0이면 entry_price)
                    if pos.peak <= 0: pos.peak = pos.entry_price
                    if high > pos.peak: pos.peak = high
                    # SL (low 기준)
                    low_pnl = (low - pos.entry_price) / pos.entry_price * 100 * self._leverage
                    if SL_PCT > 0 and low_pnl <= -SL_PCT:
                        sl_price = pos.entry_price * (1 - SL_PCT / 100 / self._leverage)
                        await self._close_position_at(symbol, sl_price, f"sl_hit ({low_pnl:.1f}%)")
                        continue
                    # Trailing
                    peak_pnl = (pos.peak - pos.entry_price) / pos.entry_price * 100 * self._leverage
                    if TRAIL_ACT_PCT > 0 and peak_pnl >= TRAIL_ACT_PCT:
                        trail_price = pos.peak * (1 - TRAIL_STOP_PCT / 100 / self._leverage)
                        if low <= trail_price:
                            await self._close_position_at(symbol, trail_price, f"trailing ({peak_pnl:.1f}%→{TRAIL_STOP_PCT}% drop)")
                            continue
                else:  # short
                    # peak 초기화 방어 (0이면 entry_price)
                    if pos.peak <= 0: pos.peak = pos.entry_price
                    if low < pos.peak: pos.peak = low
                    high_pnl = (pos.entry_price - high) / pos.entry_price * 100 * self._leverage
                    if SL_PCT > 0 and high_pnl <= -SL_PCT:
                        sl_price = pos.entry_price * (1 + SL_PCT / 100 / self._leverage)
                        await self._close_position_at(symbol, sl_price, f"sl_hit ({high_pnl:.1f}%)")
                        continue
                    peak_pnl = (pos.entry_price - pos.peak) / pos.entry_price * 100 * self._leverage
                    if TRAIL_ACT_PCT > 0 and peak_pnl >= TRAIL_ACT_PCT:
                        trail_price = pos.peak * (1 + TRAIL_STOP_PCT / 100 / self._leverage)
                        if high >= trail_price:
                            await self._close_position_at(symbol, trail_price, f"trailing ({peak_pnl:.1f}%→{TRAIL_STOP_PCT}% rise)")
                            continue
            except Exception as e:
                logger.error("momentum_sl_check_error", symbol=symbol, error=str(e))

    async def _close_position_at(self, symbol: str, trigger_price: float, reason: str):
        """특정 가격에 포지션 청산 (SL/trailing). 체결 확인 후에만 포지션 삭제."""
        pos = self._positions.get(symbol)
        if not pos:
            return
        try:
            if pos.side == "long":
                order = await self._exchange.create_market_sell(symbol, pos.quantity, reduce_only=True)
            else:
                order = await self._exchange.create_market_buy(symbol, pos.quantity, reduce_only=True)

            # 체결 확인 — 체결 안 되면 포지션 유지
            status = getattr(order, 'status', None)
            filled_qty = float(order.filled or 0)
            exec_price = float(order.price or 0)

            if status not in ('filled', 'closed') or filled_qty <= 0 or exec_price <= 0:
                self._consecutive_close_failures += 1
                logger.error("momentum_sl_close_not_filled",
                             symbol=symbol, status=status, filled=filled_qty, price=exec_price,
                             consecutive=self._consecutive_close_failures)
                if self._consecutive_close_failures >= 3:
                    self._paused = True
                    await emit_event("error", "engine",
                                     f"🚨 Momentum 청산 {self._consecutive_close_failures}회 연속 실패 — 자동 중지",
                                     detail=f"포지션 {pos.side} {symbol} qty={pos.quantity} 수동 확인 필요")
                return  # 체결 안 됨 → 포지션 유지, 다음 체크에서 재시도

            self._consecutive_close_failures = 0

            # 실제 체결가 기준 PnL
            if pos.side == "long":
                pnl = (exec_price - pos.entry_price) * filled_qty
            else:
                pnl = (pos.entry_price - exec_price) * filled_qty

            self._cumulative_pnl += pnl
            self._daily_pnl += pnl
            del self._positions[symbol]

            await self._record_order(symbol, "sell" if pos.side == "long" else "buy",
                                      exec_price, filled_qty,
                                      pnl=pnl, reason=f"momentum_{pos.side}_{reason}")
            emoji = "🛑" if pnl < 0 else "💰"
            await emit_event("info", "rnd_trade",
                             f"{emoji} Momentum {reason}: {symbol} PnL {pnl:+.2f}",
                             detail=f"진입 {pos.entry_price:.2f} → 청산 {exec_price:.2f} | {pos.side}",
                             metadata={"engine": "Momentum", "symbol": symbol, "direction": pos.side,
                                       "price": exec_price, "entry_price": pos.entry_price,
                                       "realized_pnl": pnl, "reason": reason})
            logger.info("momentum_sl_trailing_exit", symbol=symbol, reason=reason,
                        pnl=round(pnl, 2), exec_price=exec_price, filled=filled_qty)
            await self._check_loss_limits()
        except Exception as e:
            # 주문 실패 → 포지션 유지 (다음 체크에서 재시도)
            logger.error("momentum_sl_close_error", symbol=symbol, error=str(e), exc_info=True)

    async def _close_position(self, symbol: str):
        """리밸런싱 청산. 체결 확인 후에만 포지션 삭제."""
        pos = self._positions.get(symbol)
        if not pos:
            return
        try:
            if pos.side == "long":
                order = await self._exchange.create_market_sell(symbol, pos.quantity, reduce_only=True)
            else:
                order = await self._exchange.create_market_buy(symbol, pos.quantity, reduce_only=True)

            # 체결 확인
            status = getattr(order, 'status', None)
            filled_qty = float(order.filled or 0)
            exec_price = float(order.price or 0)

            if status not in ('filled', 'closed') or filled_qty <= 0 or exec_price <= 0:
                self._consecutive_close_failures += 1
                logger.error("momentum_close_not_filled",
                             symbol=symbol, status=status, filled=filled_qty, price=exec_price,
                             consecutive=self._consecutive_close_failures)
                if self._consecutive_close_failures >= 3:
                    self._paused = True
                    await emit_event("error", "engine",
                                     f"🚨 Momentum 청산 {self._consecutive_close_failures}회 연속 실패 — 자동 중지",
                                     detail=f"포지션 {pos.side} {symbol} qty={pos.quantity} 수동 확인 필요")
                return  # 체결 안 됨 → 포지션 유지

            self._consecutive_close_failures = 0

            if pos.side == "long":
                pnl = (exec_price - pos.entry_price) * filled_qty
            else:
                pnl = (pos.entry_price - exec_price) * filled_qty

            self._cumulative_pnl += pnl
            self._daily_pnl += pnl
            del self._positions[symbol]

            await self._record_order(symbol, "sell" if pos.side == "long" else "buy",
                                      exec_price, pos.quantity,
                                      pnl=pnl, reason=f"momentum_{pos.side}_exit")
            emoji = "💰" if pnl > 0 else "💸"
            await emit_event("info", "rnd_trade",
                             f"{emoji} Momentum exit {pos.side}: {symbol} PnL {pnl:+.2f}",
                             metadata={"engine": "Momentum", "symbol": symbol, "direction": pos.side,
                                       "price": exec_price, "entry_price": pos.entry_price,
                                       "realized_pnl": pnl, "reason": "rebalance"})
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
                signal_reason=reason,
                realized_pnl=pnl if ("exit" in reason or "sl_hit" in reason or "trailing" in reason) else 0.0,
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
                reason = o.signal_reason or ""
                if "entry" in reason:
                    side = "long" if o.side == "buy" else "short"
                    net[o.symbol] = {"side": side, "qty": q, "price": p}
                elif "exit" in reason or "sl_hit" in reason or "trailing" in reason:
                    net.pop(o.symbol, None)
                    cum_pnl += float(o.realized_pnl or 0)
            self._cumulative_pnl = cum_pnl
            for sym, info in net.items():
                self._positions[sym] = MomentumPosition(
                    symbol=sym, side=info["side"], quantity=info["qty"],
                    entry_price=info["price"], peak=info["price"],
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
