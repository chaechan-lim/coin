"""
Fear & Greed DCA 라이브 엔진 (현물).

전략:
- 매주 BTC/ETH RSI(14) + 30일 변동 평가
- 공포(RSI<30 OR 30d변동<-20%) → 현금의 5% 매수
- 중립 → 현금의 1% 매수
- 탐욕(RSI>70 AND 30d변동>20%) → 보유의 50% 매도
- 장기 accumulation 전략

백테스트: 180d BTC alpha +28.7%, ETH alpha +37.5% (약세장 방어)
안전: DCA라 급락 리스크 분산, 극소 수수료
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np
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

COINS = ["BTC/USDT", "ETH/USDT"]
EVAL_DAY_OF_WEEK = 0  # 월요일
EVAL_HOUR_UTC = 1
MIN_BUY_USDT = 5

FEAR_RSI = 30.0
GREED_RSI = 70.0
FEAR_CHANGE_PCT = -20.0
GREED_CHANGE_PCT = 20.0
FEAR_BUY_PCT = 0.05    # 공포: 현금의 5%
NORMAL_BUY_PCT = 0.03  # 중립: 현금의 3% (200*0.03=6, 최소 5 이상)
GREED_SELL_PCT = 0.50   # 탐욕: 보유의 50%

MAX_TOTAL_LOSS_PCT = 0.15  # DCA는 장기라 15%


@dataclass
class DCAHolding:
    symbol: str
    quantity: float
    avg_price: float
    total_cost: float


class FearGreedDCAEngine:
    """Fear & Greed 기반 DCA — 현물 매수/매도."""

    EXCHANGE_NAME = "binance_fgdca"

    def __init__(
        self,
        config: AppConfig,
        spot_exchange: ExchangeAdapter,
        market_data: MarketDataService,
        initial_capital_usdt: float = 200.0,
        coins: list[str] | None = None,
    ):
        self._config = config
        self._exchange = spot_exchange
        self._market_data = market_data
        self._initial_capital = initial_capital_usdt
        self._coins = coins or COINS
        self._is_running = False
        self._task: asyncio.Task | None = None
        self._holdings: dict[str, DCAHolding] = {}
        self._cash = initial_capital_usdt
        self._cumulative_pnl = 0.0
        self._total_invested = 0.0
        self._total_fees = 0.0
        self._paused = False

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def tracked_coins(self) -> list[str]:
        return list(self._coins)

    def set_engine_registry(self, r): pass
    def set_broadcast_callback(self, c): pass
    def set_agent_coordinator(self, c): pass

    async def start(self):
        if self._is_running:
            return
        self._is_running = True
        await self._restore_state()
        self._task = asyncio.create_task(self._loop(), name="fgdca_loop")
        logger.info("fgdca_started", capital=self._initial_capital, coins=self._coins)
        await emit_event("info", "engine", f"Fear&Greed DCA 시작 ({self._initial_capital} USDT)")

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
        logger.info("fgdca_stopped")

    async def _loop(self):
        """매주 월요일 UTC 01:00 평가."""
        while self._is_running:
            try:
                now = datetime.now(timezone.utc)
                days_until = (EVAL_DAY_OF_WEEK - now.weekday()) % 7
                if days_until == 0 and now.hour >= EVAL_HOUR_UTC:
                    days_until = 7
                target = (now + pd.Timedelta(days=days_until)).replace(
                    hour=EVAL_HOUR_UTC, minute=0, second=0, microsecond=0
                )
                wait = (target - now).total_seconds()
                logger.info("fgdca_next_eval", at=target.isoformat(), wait_hours=round(wait / 3600, 1))
                await asyncio.sleep(wait)
                if self._is_running:
                    await self._evaluate()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("fgdca_loop_error", error=str(e), exc_info=True)
                await asyncio.sleep(300)

    async def evaluate_now(self):
        await self._evaluate()

    async def _evaluate(self):
        if self._paused:
            return

        for symbol in self._coins:
            try:
                df = await self._market_data.get_ohlcv_df(symbol, "1d", limit=50)
                if df is None or len(df) < 35:
                    continue

                df = df.copy()
                delta = df["close"].diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss = (-delta.clip(upper=0)).rolling(14).mean()
                rs = gain / loss.replace(0, np.nan)
                df["rsi_14"] = 100 - (100 / (1 + rs))
                df["change_30d"] = df["close"].pct_change(30) * 100

                last = df.iloc[-1]
                rsi = float(last["rsi_14"]) if pd.notna(last["rsi_14"]) else 50
                change = float(last["change_30d"]) if pd.notna(last["change_30d"]) else 0
                price = float(last["close"])

                is_fear = rsi < FEAR_RSI or change < FEAR_CHANGE_PCT
                is_greed = rsi > GREED_RSI and change > GREED_CHANGE_PCT

                if is_fear:
                    buy_amount = self._cash * FEAR_BUY_PCT
                    if buy_amount >= MIN_BUY_USDT:
                        await self._buy(symbol, buy_amount, price, "fear")
                elif is_greed:
                    holding = self._holdings.get(symbol)
                    if holding and holding.quantity > 0:
                        sell_qty = holding.quantity * GREED_SELL_PCT
                        await self._sell(symbol, sell_qty, price, "greed")
                else:
                    buy_amount = self._cash * NORMAL_BUY_PCT
                    if buy_amount >= MIN_BUY_USDT:
                        await self._buy(symbol, buy_amount, price, "normal")

                state = "FEAR" if is_fear else ("GREED" if is_greed else "NORMAL")
                logger.info("fgdca_eval", symbol=symbol, state=state,
                            rsi=round(rsi, 1), change_30d=round(change, 1),
                            price=round(price, 2), cash=round(self._cash, 2))
            except Exception as e:
                logger.error("fgdca_eval_error", symbol=symbol, error=str(e))

    async def _buy(self, symbol: str, amount: float, price: float, reason: str):
        try:
            qty = amount / price
            order = await self._exchange.create_market_buy(symbol, qty)

            status = getattr(order, 'status', None)
            exec_qty = float(getattr(order, 'executed_quantity', None) or getattr(order, 'filled', 0) or 0)
            exec_price = float(getattr(order, 'executed_price', None) or getattr(order, 'average', 0) or 0)

            if status not in ('filled', 'closed') or exec_qty <= 0 or exec_price <= 0:
                logger.error("fgdca_buy_not_filled", symbol=symbol, status=status)
                return

            fee = exec_qty * exec_price * 0.001

            self._cash -= amount
            self._total_invested += amount
            self._total_fees += fee

            h = self._holdings.get(symbol)
            if h:
                new_qty = h.quantity + exec_qty
                h.avg_price = (h.total_cost + amount) / new_qty if new_qty > 0 else exec_price
                h.quantity = new_qty
                h.total_cost += amount
            else:
                self._holdings[symbol] = DCAHolding(
                    symbol=symbol, quantity=exec_qty, avg_price=exec_price, total_cost=amount
                )

            await self._record_order(symbol, "buy", exec_price, exec_qty, reason=f"fgdca_{reason}_buy")
            holding = self._holdings.get(symbol)
            total_qty = holding.quantity if holding else exec_qty
            total_cost = holding.total_cost if holding else amount
            avg = holding.avg_price if holding else exec_price
            await emit_event("info", "engine",
                             f"🛒 DCA {reason} 매수: {symbol} ${amount:.0f} @ {exec_price:.2f}",
                             detail=f"보유 {total_qty:.6f}개 | 평단 {avg:.2f} | 총투자 ${total_cost:.0f} | 매도: 탐욕(RSI>70) 시")
            logger.info("fgdca_buy", symbol=symbol, reason=reason, amount=round(amount, 2),
                        price=exec_price, qty=exec_qty)
        except Exception as e:
            logger.error("fgdca_buy_error", symbol=symbol, error=str(e))

    async def _sell(self, symbol: str, qty: float, price: float, reason: str):
        try:
            order = await self._exchange.create_market_sell(symbol, qty)

            status = getattr(order, 'status', None)
            exec_qty = float(getattr(order, 'executed_quantity', None) or getattr(order, 'filled', 0) or 0)
            exec_price = float(getattr(order, 'executed_price', None) or getattr(order, 'average', 0) or 0)

            if status not in ('filled', 'closed') or exec_qty <= 0 or exec_price <= 0:
                logger.error("fgdca_sell_not_filled", symbol=symbol, status=status)
                return

            proceeds = exec_qty * exec_price
            fee = proceeds * 0.001

            h = self._holdings.get(symbol)
            pnl = 0.0
            if h:
                cost_basis = h.avg_price * exec_qty
                pnl = proceeds - cost_basis - fee
                h.quantity -= exec_qty
                h.total_cost -= cost_basis
                if h.quantity <= 0:
                    del self._holdings[symbol]

            self._cash += proceeds - fee
            self._cumulative_pnl += pnl
            self._total_fees += fee

            await self._record_order(symbol, "sell", exec_price, exec_qty,
                                      pnl=pnl, reason=f"fgdca_{reason}_sell")
            emoji = "💰" if pnl > 0 else "💸"
            await emit_event("info", "engine",
                             f"{emoji} DCA {reason} 매도: {symbol} PnL {pnl:+.2f}")
            logger.info("fgdca_sell", symbol=symbol, reason=reason, qty=exec_qty,
                        price=exec_price, pnl=round(pnl, 2))
        except Exception as e:
            logger.error("fgdca_sell_error", symbol=symbol, error=str(e))

    async def _record_order(self, symbol, side, price, qty, pnl=0.0, reason=""):
        sf = get_session_factory()
        async with sf() as session:
            order = Order(
                exchange=self.EXCHANGE_NAME, symbol=symbol, side=side,
                order_type="market", status="filled",
                executed_price=price, executed_quantity=qty,
                fee=qty * price * 0.001, fee_currency="USDT",
                is_paper=False, strategy_name="fear_greed_dca",
                signal_reason=reason, realized_pnl=pnl if side == "sell" else 0.0,
                created_at=datetime.now(timezone.utc), filled_at=datetime.now(timezone.utc),
            )
            session.add(order)
            await session.commit()

    async def _restore_state(self):
        sf = get_session_factory()
        async with sf() as session:
            result = await session.execute(
                select(Order).where(Order.exchange == self.EXCHANGE_NAME)
                .order_by(Order.created_at)
            )
            orders = result.scalars().all()
            spent = 0.0
            returned = 0.0
            net = {}
            for o in orders:
                q = float(o.executed_quantity or 0)
                p = float(o.executed_price or 0)
                if o.side == "buy":
                    spent += q * p
                    cur = net.get(o.symbol, {"qty": 0, "cost": 0})
                    cur["qty"] += q
                    cur["cost"] += q * p
                    net[o.symbol] = cur
                else:
                    returned += q * p
                    cur = net.get(o.symbol, {"qty": 0, "cost": 0})
                    cur["qty"] -= q
                    cur["cost"] -= float(o.realized_pnl or 0)
                    net[o.symbol] = cur
            self._cash = self._initial_capital - spent + returned
            for sym, info in net.items():
                if info["qty"] > 0:
                    self._holdings[sym] = DCAHolding(
                        symbol=sym, quantity=info["qty"],
                        avg_price=info["cost"] / info["qty"] if info["qty"] > 0 else 0,
                        total_cost=info["cost"],
                    )
            logger.info("fgdca_restored", cash=round(self._cash, 2),
                        holdings=len(self._holdings))

    def get_status(self) -> dict:
        return {
            "exchange": self.EXCHANGE_NAME,
            "is_running": self._is_running,
            "coins": self._coins,
            "cash": round(self._cash, 2),
            "holdings": {s: {"qty": h.quantity, "avg_price": h.avg_price, "cost": h.total_cost}
                          for s, h in self._holdings.items()},
            "total_invested": round(self._total_invested, 2),
            "total_fees": round(self._total_fees, 2),
            "cumulative_pnl": round(self._cumulative_pnl, 2),
            "initial_capital": self._initial_capital,
            "paused": self._paused,
        }
