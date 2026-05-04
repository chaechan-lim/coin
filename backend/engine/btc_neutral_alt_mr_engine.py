"""
BTC Neutral Alt Mean-Reversion 라이브 엔진 (선물).

전략:
- 알트코인이 BTC 대비 z-score 극단 → 평균 회귀 베팅 (delta neutral)
- z < -z_entry → alt long + BTC short
- z > z_entry  → alt short + BTC long
- 청산: z가 z_exit 이내 또는 max_hold_days 초과
- 선물 2x leverage, 코인당 15% 자본

안전: 누적 -10%, 일일 -5% 자동 중지.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
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

DEFAULT_COINS = ["ETH/USDT", "SOL/USDT", "LINK/USDT", "BNB/USDT"]


@dataclass
class NeutralPosition:
    """Alt + BTC 양방향 포지션."""
    alt_symbol: str
    alt_side: str        # "long" or "short"
    alt_qty: float
    alt_entry: float
    btc_side: str        # 반대 방향
    btc_qty: float
    btc_entry: float
    entered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    entry_z: float = 0.0


class BTCNeutralAltMREngine:
    """BTC-Neutral 알트코인 평균회귀 엔진."""

    EXCHANGE_NAME = "binance_btc_neutral"
    STRATEGY_NAME = "btc_neutral_mr"
    BTC_SYMBOL = "BTC/USDT"

    def __init__(
        self,
        config: AppConfig,
        futures_exchange: ExchangeAdapter,
        market_data: MarketDataService,
        initial_capital_usdt: float = 100.0,
        leverage: int = 2,
        coins: list[str] | None = None,
        lookback_days: int = 7,
        z_entry: float = 2.0,
        z_exit: float = 0.3,
        max_hold_days: int = 21,
        max_concurrent: int = 3,
        position_pct: float = 0.15,
    ):
        self._config = config
        self._exchange = futures_exchange
        self._market_data = market_data
        self._initial_capital = initial_capital_usdt
        self._leverage = leverage
        self._coins = coins or list(DEFAULT_COINS)
        self._lookback_days = lookback_days
        self._z_entry = z_entry
        self._z_exit = z_exit
        self._max_hold_days = max_hold_days
        self._max_concurrent = max_concurrent
        self._position_pct = position_pct

        self._is_running = False
        self._task: asyncio.Task | None = None
        self._positions: dict[str, NeutralPosition] = {}
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
        return list(self._coins) + [self.BTC_SYMBOL]

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
        self._task = asyncio.create_task(self._loop(), name="btc_neutral_mr_loop")
        logger.info("btc_neutral_mr_started", capital=self._initial_capital, coins=self._coins)
        await emit_event("info", "engine",
                         f"BTC-Neutral MR 시작 ({self._initial_capital} USDT, {self._leverage}x)")

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
        logger.info("btc_neutral_mr_stopped")

    async def _loop(self):
        """매일 UTC 01:00 평가."""
        while self._is_running:
            try:
                now = datetime.now(timezone.utc)
                target = now.replace(hour=1, minute=0, second=0, microsecond=0)
                if now >= target:
                    target += timedelta(days=1)
                wait = (target - now).total_seconds()
                logger.info("btc_neutral_next_eval", at=target.isoformat(),
                            wait_hours=round(wait / 3600, 1))
                await asyncio.sleep(wait)
                if self._is_running:
                    await self._evaluate()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("btc_neutral_loop_error", error=str(e), exc_info=True)
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

        # 1. 기존 포지션 청산 체크
        for symbol in list(self._positions.keys()):
            await self._check_exit(symbol, now)

        # 2. 신규 진입 탐색
        if len(self._positions) < self._max_concurrent:
            await self._scan_entries()

        logger.info("btc_neutral_eval_complete", positions=len(self._positions),
                     pnl=round(self._cumulative_pnl, 2))
        await self._check_loss_limits()

    async def _scan_entries(self):
        """각 알트코인에 대해 z-score 계산 후 진입 조건 확인."""
        btc_df = await self._market_data.get_ohlcv_df(self.BTC_SYMBOL, "1h",
                                                       limit=self._lookback_days * 24 + 10)
        if btc_df is None or len(btc_df) < self._lookback_days * 24:
            return

        btc_closes = btc_df["close"].values

        for symbol in self._coins:
            if symbol in self._positions:
                continue
            if len(self._positions) >= self._max_concurrent:
                break

            try:
                z = await self._compute_z_score(symbol, btc_closes)
                if z is None:
                    continue

                if z < -self._z_entry:
                    # ALT가 BTC 대비 저평가 → alt long + BTC short
                    await self._open_pair(symbol, "long", z)
                elif z > self._z_entry:
                    # ALT가 BTC 대비 고평가 → alt short + BTC long
                    await self._open_pair(symbol, "short", z)
            except Exception as e:
                logger.error("btc_neutral_scan_error", symbol=symbol, error=str(e))

    async def _compute_z_score(self, alt_symbol: str, btc_closes=None) -> Optional[float]:
        """ALT/BTC 비율의 z-score 계산."""
        alt_df = await self._market_data.get_ohlcv_df(alt_symbol, "1h",
                                                       limit=self._lookback_days * 24 + 10)
        if alt_df is None or len(alt_df) < self._lookback_days * 24:
            return None

        if btc_closes is None:
            btc_df = await self._market_data.get_ohlcv_df(self.BTC_SYMBOL, "1h",
                                                           limit=self._lookback_days * 24 + 10)
            if btc_df is None or len(btc_df) < self._lookback_days * 24:
                return None
            btc_closes = btc_df["close"].values

        alt_closes = alt_df["close"].values
        n = self._lookback_days * 24
        btc_window = btc_closes[-n:]
        alt_window = alt_closes[-n:]

        if len(btc_window) != len(alt_window):
            min_len = min(len(btc_window), len(alt_window))
            btc_window = btc_window[-min_len:]
            alt_window = alt_window[-min_len:]

        # BTC 가격이 0인 경우 방어
        if np.any(btc_window <= 0):
            return None

        ratio = alt_window / btc_window
        mean = float(np.mean(ratio))
        std = float(np.std(ratio))
        if std < 1e-15:
            return None

        current_ratio = float(ratio[-1])
        return (current_ratio - mean) / std

    async def _check_exit(self, alt_symbol: str, now: datetime):
        """청산 조건: z가 exit 근처 또는 max_hold 초과."""
        pos = self._positions.get(alt_symbol)
        if not pos:
            return

        # max_hold 체크
        hold_hours = (now - pos.entered_at).total_seconds() / 3600
        if hold_hours >= self._max_hold_days * 24:
            await self._close_pair(alt_symbol, "max_hold_exceeded")
            return

        # z-score 재계산
        try:
            z = await self._compute_z_score(alt_symbol)
            if z is None:
                return

            if abs(z) <= self._z_exit:
                await self._close_pair(alt_symbol, f"z_reverted({z:.2f})")
        except Exception as e:
            logger.error("btc_neutral_exit_check_error", symbol=alt_symbol, error=str(e))

    async def _open_pair(self, alt_symbol: str, alt_side: str, z: float):
        """Alt + BTC 동시 진입."""
        available = self._initial_capital + self._cumulative_pnl
        notional = available * self._position_pct * self._leverage
        if notional < MIN_NOTIONAL * 2:
            return

        half_notional = notional / 2  # half for alt, half for BTC

        # Alt 가격 (사전 체크 — try 밖에서)
        alt_df = await self._market_data.get_ohlcv_df(alt_symbol, "1h", limit=5)
        btc_df = await self._market_data.get_ohlcv_df(self.BTC_SYMBOL, "1h", limit=5)
        if alt_df is None or btc_df is None:
            return

        alt_price = float(alt_df["close"].iloc[-1])
        btc_price = float(btc_df["close"].iloc[-1])
        alt_qty = half_notional / alt_price
        btc_qty = half_notional / btc_price
        btc_side = "short" if alt_side == "long" else "long"

        # 사전 가드: BTC 최소 정밀도 (0.001) 검사 — alt 진입 전에 차단
        BTC_MIN_QTY = 0.001
        if btc_qty < BTC_MIN_QTY:
            logger.warning(
                "btc_neutral_btc_qty_too_small",
                symbol=alt_symbol, btc_qty=btc_qty, min=BTC_MIN_QTY,
                half_notional=round(half_notional, 2),
                hint="자본 부족 — max_concurrent 축소 또는 자본 증액 필요",
            )
            await emit_event(
                "warning", "engine",
                f"BTCNeutral 자본 부족 — {alt_symbol} 진입 스킵 (BTC qty {btc_qty:.5f} < {BTC_MIN_QTY})",
                metadata={"engine": "BTCNeutral", "symbol": alt_symbol,
                          "btc_qty": btc_qty, "min": BTC_MIN_QTY,
                          "half_notional": round(half_notional, 2)},
            )
            return

        # alt 주문 체결 여부 (롤백 시 참조)
        alt_filled_qty = 0.0

        try:
            # Alt 주문
            if alt_side == "long":
                alt_order = await self._exchange.create_market_buy(alt_symbol, alt_qty)
            else:
                alt_order = await self._exchange.create_market_sell(alt_symbol, alt_qty)

            alt_status = getattr(alt_order, 'status', None)
            alt_exec_qty = float(alt_order.filled or 0)
            alt_exec_price = float(alt_order.price or 0)

            if alt_status not in ('filled', 'closed') or alt_exec_qty <= 0 or alt_exec_price <= 0:
                logger.error("btc_neutral_alt_order_not_filled", symbol=alt_symbol, status=alt_status)
                return

            alt_filled_qty = alt_exec_qty

            # BTC 주문
            if btc_side == "long":
                btc_order = await self._exchange.create_market_buy(self.BTC_SYMBOL, btc_qty)
            else:
                btc_order = await self._exchange.create_market_sell(self.BTC_SYMBOL, btc_qty)

            btc_status = getattr(btc_order, 'status', None)
            btc_exec_qty = float(btc_order.filled or 0)
            btc_exec_price = float(btc_order.price or 0)

            if btc_status not in ('filled', 'closed') or btc_exec_qty <= 0 or btc_exec_price <= 0:
                logger.error("btc_neutral_btc_order_not_filled", symbol=self.BTC_SYMBOL, status=btc_status)
                # 외부 finally 에서 처리되도록 raise
                raise RuntimeError("BTC leg not filled, rolling back alt")

            self._positions[alt_symbol] = NeutralPosition(
                alt_symbol=alt_symbol, alt_side=alt_side,
                alt_qty=alt_exec_qty, alt_entry=alt_exec_price,
                btc_side=btc_side, btc_qty=btc_exec_qty, btc_entry=btc_exec_price,
                entry_z=z,
            )

            # DB 기록 — Alt
            await self._record_order(alt_symbol,
                                     "buy" if alt_side == "long" else "sell",
                                     alt_exec_price, alt_exec_qty,
                                     reason=f"btcneutral_{alt_side}_alt_entry")
            # DB 기록 — BTC
            await self._record_order(self.BTC_SYMBOL,
                                     "buy" if btc_side == "long" else "sell",
                                     btc_exec_price, btc_exec_qty,
                                     reason=f"btcneutral_{btc_side}_btc_entry")

            coin_short = alt_symbol.split("/")[0]
            opposite_side = btc_side
            detail = f"{coin_short} z={z:.2f} | {coin_short} {alt_side} + BTC {opposite_side}"
            await emit_event("info", "rnd_trade",
                             f"🔄 BTCNeutral: {alt_symbol} {alt_side} @ {alt_exec_price:.2f}",
                             detail=detail,
                             metadata={"engine": "BTCNeutral", "symbol": alt_symbol, "direction": alt_side,
                                       "price": alt_exec_price, "leverage": self._leverage})
        except Exception as e:
            logger.error("btc_neutral_open_error", symbol=alt_symbol, error=str(e))
            # alt 체결됐으면 무조건 롤백 (BTC leg 누락 방지)
            if alt_filled_qty > 0:
                try:
                    if alt_side == "long":
                        await self._exchange.create_market_sell(
                            alt_symbol, alt_filled_qty, reduce_only=True
                        )
                    else:
                        await self._exchange.create_market_buy(
                            alt_symbol, alt_filled_qty, reduce_only=True
                        )
                    logger.info("btc_neutral_alt_rollback_ok",
                                symbol=alt_symbol, qty=alt_filled_qty)
                    await emit_event(
                        "warning", "engine",
                        f"BTCNeutral {alt_symbol} 진입 실패 — alt {alt_filled_qty} 롤백",
                        metadata={"engine": "BTCNeutral", "symbol": alt_symbol,
                                  "qty": alt_filled_qty, "error": str(e)[:100]},
                    )
                except Exception as rb_err:
                    logger.critical("btc_neutral_alt_rollback_failed",
                                    symbol=alt_symbol, qty=alt_filled_qty, error=str(rb_err))
                    await emit_event(
                        "critical", "engine",
                        f"🚨 BTCNeutral {alt_symbol} 롤백 실패 — 고아 포지션 발생: {alt_filled_qty}",
                        detail=f"수동 청산 필요. error={str(rb_err)[:200]}",
                        metadata={"engine": "BTCNeutral", "symbol": alt_symbol,
                                  "orphan_qty": alt_filled_qty, "side": alt_side},
                    )

    async def _close_pair(self, alt_symbol: str, reason: str = ""):
        """Alt + BTC 동시 청산."""
        pos = self._positions.get(alt_symbol)
        if not pos:
            return

        try:
            # Alt 청산
            if pos.alt_side == "long":
                alt_order = await self._exchange.create_market_sell(alt_symbol, pos.alt_qty, reduce_only=True)
            else:
                alt_order = await self._exchange.create_market_buy(alt_symbol, pos.alt_qty, reduce_only=True)

            alt_status = getattr(alt_order, 'status', None)
            alt_filled = float(alt_order.filled or 0)
            alt_exec_price = float(alt_order.price or 0)

            if alt_status not in ('filled', 'closed') or alt_filled <= 0 or alt_exec_price <= 0:
                self._consecutive_close_failures += 1
                logger.error("btc_neutral_alt_close_not_filled", symbol=alt_symbol, status=alt_status,
                             consecutive=self._consecutive_close_failures)
                if self._consecutive_close_failures >= 3:
                    self._paused = True
                    await emit_event("error", "engine",
                                     f"🚨 BTCNeutral 청산 {self._consecutive_close_failures}회 연속 실패 — 자동 중지",
                                     detail=f"포지션 {pos.alt_side} {alt_symbol} qty={pos.alt_qty} 수동 확인 필요")
                return

            # BTC 청산
            if pos.btc_side == "long":
                btc_order = await self._exchange.create_market_sell(self.BTC_SYMBOL, pos.btc_qty, reduce_only=True)
            else:
                btc_order = await self._exchange.create_market_buy(self.BTC_SYMBOL, pos.btc_qty, reduce_only=True)

            btc_status = getattr(btc_order, 'status', None)
            btc_filled = float(btc_order.filled or 0)
            btc_exec_price = float(btc_order.price or 0)

            if btc_status not in ('filled', 'closed') or btc_filled <= 0 or btc_exec_price <= 0:
                self._consecutive_close_failures += 1
                logger.error("btc_neutral_btc_close_not_filled", symbol=self.BTC_SYMBOL, status=btc_status,
                             consecutive=self._consecutive_close_failures)
                # ALT 레그는 이미 청산됨 → 고아 포지션 경고
                await emit_event("error", "engine",
                                 f"⚠️ BTCNeutral 부분 청산 desync: {alt_symbol} ALT 레그 청산 완료, BTC 레그 미체결",
                                 detail=f"BTC {pos.btc_side} qty={pos.btc_qty} 수동 청산 필요. ALT {pos.alt_side} {alt_symbol} 이미 청산됨.")
                if self._consecutive_close_failures >= 3:
                    self._paused = True
                    await emit_event("error", "engine",
                                     f"🚨 BTCNeutral 청산 {self._consecutive_close_failures}회 연속 실패 — 자동 중지",
                                     detail=f"BTC 레그 청산 실패 {self.BTC_SYMBOL} qty={pos.btc_qty} 수동 확인 필요")
                return

            self._consecutive_close_failures = 0

            if pos.alt_side == "long":
                alt_pnl = (alt_exec_price - pos.alt_entry) * alt_filled
            else:
                alt_pnl = (pos.alt_entry - alt_exec_price) * alt_filled

            if pos.btc_side == "long":
                btc_pnl = (btc_exec_price - pos.btc_entry) * btc_filled
            else:
                btc_pnl = (pos.btc_entry - btc_exec_price) * btc_filled

            total_pnl = alt_pnl + btc_pnl
            self._cumulative_pnl += total_pnl
            self._daily_pnl += total_pnl
            del self._positions[alt_symbol]

            # DB 기록
            exit_alt_side = "sell" if pos.alt_side == "long" else "buy"
            exit_btc_side = "sell" if pos.btc_side == "long" else "buy"
            await self._record_order(alt_symbol, exit_alt_side,
                                     alt_exec_price, alt_filled,
                                     pnl=alt_pnl,
                                     reason=f"btcneutral_{pos.alt_side}_alt_exit_{reason}")
            await self._record_order(self.BTC_SYMBOL, exit_btc_side,
                                     btc_exec_price, btc_filled,
                                     pnl=btc_pnl,
                                     reason=f"btcneutral_{pos.btc_side}_btc_exit_{reason}")

            emoji = "💰" if total_pnl > 0 else "💸"
            await emit_event("info", "rnd_trade",
                             f"{emoji} BTCNeutral exit: {alt_symbol} PnL {total_pnl:+.2f} ({reason})",
                             metadata={"engine": "BTCNeutral", "symbol": alt_symbol,
                                       "realized_pnl": total_pnl, "reason": reason})
        except Exception as e:
            self._consecutive_close_failures += 1
            logger.error("btc_neutral_close_error", symbol=alt_symbol, error=str(e),
                         consecutive=self._consecutive_close_failures)
            if self._consecutive_close_failures >= 3:
                self._paused = True
                await emit_event("error", "engine",
                                 f"🚨 BTCNeutral 청산 예외 {self._consecutive_close_failures}회 — 자동 중지",
                                 detail=f"{alt_symbol} {str(e)[:100]}")

    async def _check_loss_limits(self):
        if self._cumulative_pnl <= -self._initial_capital * MAX_TOTAL_LOSS_PCT:
            self._paused = True
            await emit_event("error", "engine",
                             f"🚨 BTCNeutral 누적 손실 한도 ({self._cumulative_pnl:.2f}) — 자동 중지")
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

            # 미청산 alt 포지션 복원 (간략화)
            alt_entries: dict[str, dict] = {}
            btc_entries: dict[str, dict] = {}
            cum_pnl = 0.0
            closed_alts: set[str] = set()

            for o in orders:
                q = float(o.executed_quantity or 0)
                p = float(o.executed_price or 0)
                reason = o.signal_reason or ""

                if "alt_entry" in reason:
                    side = "long" if o.side == "buy" else "short"
                    alt_entries[o.symbol] = {"side": side, "qty": q, "price": p}
                elif "btc_entry" in reason and "alt_entry" not in reason:
                    side = "long" if o.side == "buy" else "short"
                    # BTC entry 는 가장 최근 alt_entry 와 쌍
                    btc_entries[o.symbol] = {"side": side, "qty": q, "price": p}
                elif "alt_exit" in reason:
                    sym = o.symbol
                    closed_alts.add(sym)
                    cum_pnl += float(o.realized_pnl or 0)
                elif "btc_exit" in reason:
                    cum_pnl += float(o.realized_pnl or 0)

            self._cumulative_pnl = cum_pnl

            # 미청산 포지션 복원
            for sym, info in alt_entries.items():
                if sym in closed_alts:
                    continue
                # BTC entry 는 최근 것만 사용
                btc_info = btc_entries.get(self.BTC_SYMBOL)
                if btc_info is None:
                    continue
                self._positions[sym] = NeutralPosition(
                    alt_symbol=sym, alt_side=info["side"],
                    alt_qty=info["qty"], alt_entry=info["price"],
                    btc_side=btc_info["side"], btc_qty=btc_info["qty"],
                    btc_entry=btc_info["price"],
                )
            logger.info("btc_neutral_restored", positions=len(self._positions),
                        pnl=round(cum_pnl, 2))

    def get_status(self) -> dict:
        positions_list = []
        for sym, pos in self._positions.items():
            positions_list.append({
                "alt_symbol": pos.alt_symbol,
                "alt_side": pos.alt_side,
                "alt_qty": pos.alt_qty,
                "alt_entry": pos.alt_entry,
                "btc_side": pos.btc_side,
                "btc_qty": pos.btc_qty,
                "btc_entry": pos.btc_entry,
                "entry_z": round(pos.entry_z, 2),
                "hold_hours": round((datetime.now(timezone.utc) - pos.entered_at).total_seconds() / 3600, 1),
            })
        return {
            "exchange": self.EXCHANGE_NAME,
            "is_running": self._is_running,
            "leverage": self._leverage,
            "capital_usdt": self._initial_capital,
            "coins": self._coins,
            "z_entry": self._z_entry,
            "z_exit": self._z_exit,
            "max_hold_days": self._max_hold_days,
            "max_concurrent": self._max_concurrent,
            "positions": positions_list,
            "cumulative_pnl": round(self._cumulative_pnl, 2),
            "daily_pnl": round(self._daily_pnl, 2),
            "paused": self._paused,
        }
