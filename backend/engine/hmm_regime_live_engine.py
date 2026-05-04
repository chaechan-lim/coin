"""
HMM Regime 라이브 엔진 (선물, 멀티심볼).

전략:
- BTC/ETH 4h 캔들로 HMM 3-state 학습 (매일 1회 refit)
- bullish → long, bearish → short, neutral → flat
- 70% 상태 확률 필터
- TP 15% (레버리지 적용)
- 심볼별 독립 모델/포지션/자본
"""
from __future__ import annotations
import asyncio
import contextlib
import io
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

MAX_TOTAL_LOSS_PCT = 0.10
MAX_DAILY_LOSS_PCT = 0.05
MIN_NOTIONAL = 10
TRAIN_HOURS = 24 * 90
REFIT_INTERVAL_HOURS = 24
EVAL_INTERVAL_HOURS = 4
USE_4H_CANDLE = True
MIN_STATE_PROB = 0.7
TP_PCT = 15.0
DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT"]


@dataclass
class HMMPosition:
    symbol: str
    side: str
    quantity: float
    entry_price: float
    peak_price: float = 0.0


@dataclass
class HMMModelState:
    """심볼별 HMM 모델 상태."""
    model: object | None = None
    bullish_state: int = -1
    bearish_state: int = -1
    neutral_state: int = -1
    last_refit_at: datetime | None = None


class HMMRegimeLiveEngine:
    """HMM 3-state 체제전환 — 멀티심볼, 심볼별 독립 모델/포지션."""

    EXCHANGE_NAME = "binance_hmm"

    def __init__(
        self,
        config: AppConfig,
        futures_exchange: ExchangeAdapter,
        market_data: MarketDataService,
        initial_capital_usdt: float = 100.0,
        leverage: int = 2,
        symbol: str = "BTC/USDT",  # 하위 호환
        symbols: list[str] | None = None,
        entry_blocked: list[str] | None = None,
    ):
        self._config = config
        self._exchange = futures_exchange
        self._market_data = market_data
        self._initial_capital = initial_capital_usdt
        self._leverage = leverage
        self._symbols = symbols or [symbol]
        # 신규 진입 차단 — 기존 포지션 관리(SL/TP/regime exit)는 정상 동작
        self._entry_blocked: set[str] = set(entry_blocked or [])

        self._is_running = False
        self._task: asyncio.Task | None = None
        self._positions: dict[str, HMMPosition] = {}
        self._models: dict[str, HMMModelState] = {s: HMMModelState() for s in self._symbols}
        self._cumulative_pnl = 0.0
        self._daily_pnl = 0.0
        self._last_eval_date: Optional[datetime] = None
        self._paused = False
        self._daily_paused = False
        self._coordinator = None
        self._consecutive_close_failures: dict[str, int] = {}

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def tracked_coins(self) -> list[str]:
        return list(self._symbols)

    # 하위 호환 (단일 심볼 접근)
    @property
    def _symbol(self) -> str:
        return self._symbols[0]

    @property
    def _position(self) -> HMMPosition | None:
        return next(iter(self._positions.values()), None) if len(self._positions) == 1 else None

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
        for symbol in self._symbols:
            await self._refit_model(symbol)
        self._task = asyncio.create_task(self._loop(), name="hmm_regime_loop")
        syms = ", ".join(self._symbols)
        logger.info("hmm_regime_started", capital=self._initial_capital, symbols=self._symbols)
        await emit_event("info", "engine", f"HMM Regime 시작 ({syms}, {self._initial_capital} USDT)")

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
        logger.info("hmm_regime_stopped")

    async def _loop(self):
        while self._is_running:
            try:
                now = datetime.now(timezone.utc)
                for symbol in self._symbols:
                    ms = self._models[symbol]
                    if ms.last_refit_at is None or (now - ms.last_refit_at).total_seconds() > REFIT_INTERVAL_HOURS * 3600:
                        await self._refit_model(symbol)

                for symbol in self._symbols:
                    await self._evaluate(symbol)

                next_eval = (now + pd.Timedelta(hours=EVAL_INTERVAL_HOURS)).replace(minute=5, second=0, microsecond=0)
                wait = max(10, (next_eval - datetime.now(timezone.utc)).total_seconds())
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("hmm_loop_error", error=str(e), exc_info=True)
                await asyncio.sleep(300)

    async def evaluate_now(self):
        for symbol in self._symbols:
            await self._evaluate(symbol)

    async def _refit_model(self, symbol: str):
        try:
            from hmmlearn.hmm import GaussianHMM

            tf = "4h" if USE_4H_CANDLE else "1h"
            limit = TRAIN_HOURS // (4 if USE_4H_CANDLE else 1) + 50
            df = await self._market_data.get_ohlcv_df(symbol, tf, limit=limit)
            min_bars = TRAIN_HOURS // (4 if USE_4H_CANDLE else 1)
            if df is None or len(df) < min_bars:
                logger.warning("hmm_refit_insufficient_data", symbol=symbol,
                               available=len(df) if df is not None else 0, need=min_bars)
                return

            df = df.copy()
            df["log_return"] = np.log(df["close"] / df["close"].shift(1)).fillna(0.0)
            vol_window = 6 if USE_4H_CANDLE else 24
            mom_window = 6 if USE_4H_CANDLE else 24
            df["vol_24"] = df["log_return"].rolling(vol_window).std().fillna(0.0)
            df["mom_24"] = df["close"].pct_change(mom_window).fillna(0.0)

            X = df[["log_return", "vol_24", "mom_24"]].values[-min_bars:]
            model = GaussianHMM(n_components=3, covariance_type="full", n_iter=200, random_state=42)
            with contextlib.redirect_stderr(io.StringIO()):
                model.fit(X)

            states = model.predict(X)
            returns = df["log_return"].values[-min_bars:]
            state_mean = {}
            for s in range(3):
                mask = states == s
                state_mean[s] = float(returns[mask].mean()) if mask.any() else 0.0

            sorted_states = sorted(state_mean.items(), key=lambda x: x[1])
            ms = self._models[symbol]
            ms.bearish_state = sorted_states[0][0]
            ms.neutral_state = sorted_states[1][0]
            ms.bullish_state = sorted_states[2][0]
            ms.model = model
            ms.last_refit_at = datetime.now(timezone.utc)

            logger.info("hmm_refit_complete", symbol=symbol,
                        bullish=ms.bullish_state, bearish=ms.bearish_state, neutral=ms.neutral_state)
        except Exception as e:
            logger.error("hmm_refit_error", symbol=symbol, error=str(e), exc_info=True)

    async def _evaluate(self, symbol: str):
        now = datetime.now(timezone.utc)
        today = now.date()
        if self._last_eval_date != today:
            self._daily_pnl = 0.0
            self._daily_paused = False
            self._last_eval_date = today

        ms = self._models.get(symbol)
        if self._paused or self._daily_paused or not ms or ms.model is None:
            return

        try:
            tf = "4h" if USE_4H_CANDLE else "1h"
            df = await self._market_data.get_ohlcv_df(symbol, tf, limit=50)
            if df is None or len(df) < 10:
                return

            df = df.copy()
            df["log_return"] = np.log(df["close"] / df["close"].shift(1)).fillna(0.0)
            vol_window = 6 if USE_4H_CANDLE else 24
            mom_window = 6 if USE_4H_CANDLE else 24
            df["vol_24"] = df["log_return"].rolling(vol_window).std().fillna(0.0)
            df["mom_24"] = df["close"].pct_change(mom_window).fillna(0.0)

            X = df[["log_return", "vol_24", "mom_24"]].values[-1:]
            state = int(ms.model.predict(X)[0])
            state_prob = float(ms.model.predict_proba(X)[0][state])

            desired = 0
            if state_prob >= MIN_STATE_PROB:
                if state == ms.bullish_state:
                    desired = 1
                elif state == ms.bearish_state:
                    desired = -1
            else:
                pos = self._positions.get(symbol)
                if pos:
                    desired = 1 if pos.side == "long" else -1

            pos = self._positions.get(symbol)
            current = 0
            if pos:
                current = 1 if pos.side == "long" else -1

            price = float(df["close"].iloc[-1])
            high = float(df["high"].iloc[-1]) if "high" in df.columns else price
            low = float(df["low"].iloc[-1]) if "low" in df.columns else price

            # TP 체크
            if pos and TP_PCT > 0:
                if pos.side == "long":
                    tp_price = pos.entry_price * (1 + TP_PCT / 100 / self._leverage)
                    if high >= tp_price:
                        logger.info("hmm_tp_hit", symbol=symbol, side="long", tp_price=round(tp_price, 2))
                        await self._close_position(symbol, tp_price)
                        if symbol not in self._positions:  # 청산 성공 확인
                            await emit_event("info", "rnd_trade",
                                             f"🎯 HMM TP: {symbol} long @ {tp_price:.2f}",
                                             metadata={"engine": "HMM", "symbol": symbol, "reason": "tp_hit", "price": tp_price})
                            desired = current = 0
                else:
                    tp_price = pos.entry_price * (1 - TP_PCT / 100 / self._leverage)
                    if low <= tp_price:
                        logger.info("hmm_tp_hit", symbol=symbol, side="short", tp_price=round(tp_price, 2))
                        await self._close_position(symbol, tp_price)
                        if symbol not in self._positions:  # 청산 성공 확인
                            await emit_event("info", "rnd_trade",
                                             f"🎯 HMM TP: {symbol} short @ {tp_price:.2f}",
                                             metadata={"engine": "HMM", "symbol": symbol, "reason": "tp_hit", "price": tp_price})
                            desired = current = 0

            state_name = {ms.bullish_state: "bullish", ms.bearish_state: "bearish",
                          ms.neutral_state: "neutral"}.get(state, f"unknown({state})")
            logger.info("hmm_eval_tick", symbol=symbol, state=state_name,
                        desired={1: "long", -1: "short", 0: "flat"}[desired],
                        current={1: "long", -1: "short", 0: "flat"}[current],
                        price=round(price, 2), pnl=round(self._cumulative_pnl, 2))

            if desired != current:
                if self._positions.get(symbol):
                    await self._close_position(symbol, price)
                if desired != 0 and symbol not in self._positions:
                    if symbol in self._entry_blocked:
                        logger.info("hmm_entry_blocked", symbol=symbol,
                                    reason="entry_blocked_list")
                    else:
                        side = "long" if desired == 1 else "short"
                        await self._open_position(symbol, side, price)
                await self._check_loss_limits()
        except Exception as e:
            logger.error("hmm_eval_error", symbol=symbol, error=str(e), exc_info=True)

    def _capital_per_symbol(self) -> float:
        available = self._initial_capital + self._cumulative_pnl
        return available / len(self._symbols)

    async def _open_position(self, symbol: str, side: str, price: float):
        capital = self._capital_per_symbol()
        notional = capital * self._leverage * 0.9
        if notional < MIN_NOTIONAL:
            return
        qty = notional / price
        try:
            if side == "long":
                order = await self._exchange.create_market_buy(symbol, qty)
            else:
                order = await self._exchange.create_market_sell(symbol, qty)

            status = getattr(order, 'status', None)
            filled_qty = float(order.filled or 0)
            exec_price = float(order.price or 0)

            if status not in ('filled', 'closed') or filled_qty <= 0 or exec_price <= 0:
                logger.error("hmm_open_not_filled", side=side, symbol=symbol, status=status)
                return

            self._positions[symbol] = HMMPosition(
                symbol=symbol, side=side, quantity=filled_qty, entry_price=exec_price
            )
            await self._record_order(symbol, "buy" if side == "long" else "sell",
                                      exec_price, filled_qty, reason=f"hmm_{side}_entry")
            notional = filled_qty * exec_price
            await emit_event("info", "rnd_trade",
                             f"{'📈' if side=='long' else '📉'} HMM {side}: {symbol} @ {exec_price:.2f}",
                             detail=f"수량 {filled_qty:.6f} | 명목 {notional:.1f} USDT",
                             metadata={"engine": "HMM", "symbol": symbol, "direction": side,
                                       "price": exec_price, "quantity": filled_qty, "leverage": self._leverage})
        except Exception as e:
            logger.error("hmm_open_error", symbol=symbol, side=side, error=str(e))

    async def _close_position(self, symbol: str, price: float):
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
                fails = self._consecutive_close_failures[symbol] = self._consecutive_close_failures.get(symbol, 0) + 1
                logger.error("hmm_close_not_filled", side=pos.side, symbol=symbol,
                             status=status, consecutive=fails)
                if fails >= 3:
                    self._paused = True
                    await emit_event("error", "engine",
                                     f"🚨 HMM 청산 {fails}회 연속 실패 — 자동 중지",
                                     detail=f"포지션 {pos.side} {symbol} qty={pos.quantity} 수동 확인 필요")
                return

            self._consecutive_close_failures.pop(symbol, None)

            if pos.side == "long":
                pnl = (exec_price - pos.entry_price) * filled_qty
            else:
                pnl = (pos.entry_price - exec_price) * filled_qty

            self._cumulative_pnl += pnl
            self._daily_pnl += pnl
            del self._positions[symbol]

            await self._record_order(symbol, "sell" if pos.side == "long" else "buy",
                                      exec_price, filled_qty, pnl=pnl,
                                      reason=f"hmm_{pos.side}_exit")
            emoji = "💰" if pnl > 0 else "💸"
            await emit_event("info", "rnd_trade",
                             f"{emoji} HMM exit {pos.side}: {symbol} PnL {pnl:+.2f}",
                             metadata={"engine": "HMM", "symbol": symbol, "direction": pos.side,
                                       "price": exec_price, "entry_price": pos.entry_price,
                                       "realized_pnl": pnl, "reason": "regime_change"})
        except Exception as e:
            fails = self._consecutive_close_failures[symbol] = self._consecutive_close_failures.get(symbol, 0) + 1
            logger.error("hmm_close_error", symbol=symbol, error=str(e), consecutive=fails)
            if fails >= 3:
                self._paused = True
                await emit_event("error", "engine",
                                 f"🚨 HMM 청산 예외 {fails}회 — 자동 중지",
                                 detail=f"{symbol} {str(e)[:100]}")

    async def _check_loss_limits(self):
        if self._cumulative_pnl <= -self._initial_capital * MAX_TOTAL_LOSS_PCT:
            self._paused = True
            await emit_event("error", "engine",
                             f"🚨 HMM 누적 손실 한도 ({self._cumulative_pnl:.2f}) — 자동 중지")
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
                is_paper=False, strategy_name="hmm_regime",
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
                .where(Order.strategy_name == "hmm_regime")
                .order_by(Order.created_at)
            )
            orders = result.scalars().all()
            cum_pnl = 0.0
            open_entries: dict[str, Order] = {}
            for o in orders:
                sym = o.symbol
                if "entry" in (o.signal_reason or ""):
                    open_entries[sym] = o
                elif "exit" in (o.signal_reason or ""):
                    cum_pnl += float(o.realized_pnl or 0)
                    open_entries.pop(sym, None)
            self._cumulative_pnl = cum_pnl
            for sym, entry in open_entries.items():
                if sym not in self._symbols:
                    logger.warning("hmm_orphan_position_skipped", symbol=sym)
                    continue
                side = "long" if entry.side == "buy" else "short"
                self._positions[sym] = HMMPosition(
                    symbol=sym, side=side,
                    quantity=float(entry.executed_quantity or 0),
                    entry_price=float(entry.executed_price or 0),
                )
            logger.info("hmm_restored", positions=len(self._positions), pnl=round(cum_pnl, 2))

    def get_status(self) -> dict:
        positions = []
        for sym, pos in self._positions.items():
            tp = 0.0
            if TP_PCT > 0:
                if pos.side == "long":
                    tp = round(pos.entry_price * (1 + TP_PCT / 100 / self._leverage), 2)
                else:
                    tp = round(pos.entry_price * (1 - TP_PCT / 100 / self._leverage), 2)
            positions.append({
                "symbol": sym, "side": pos.side,
                "entry_price": pos.entry_price, "qty": pos.quantity,
                "tp_price": tp,
            })

        return {
            "exchange": self.EXCHANGE_NAME,
            "is_running": self._is_running,
            "symbols": self._symbols,
            "leverage": self._leverage,
            "capital_usdt": self._initial_capital,
            "positions": positions,
            "cumulative_pnl": round(self._cumulative_pnl, 2),
            "daily_pnl": round(self._daily_pnl, 2),
            "paused": self._paused,
            "models_fitted": {s: ms.model is not None for s, ms in self._models.items()},
        }
