"""
HMM Regime Detection 라이브 엔진 (선물).

전략:
- BTC 1h 캔들로 HMM 3-state 학습 (매일 1회 refit)
- 매시간 state predict → bullish=long, bearish=short, neutral=flat
- 백테스트: 180d +124.95%, Sharpe 2.99 (90d -83% 주의)

안전: 누적 -10%, 일일 -5% 자동 중지.
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
TRAIN_HOURS = 24 * 90  # 90일 학습 (4h 기준 540 캔들, 안정적 공분산)
REFIT_INTERVAL_HOURS = 24  # 매일 refit
EVAL_INTERVAL_HOURS = 4  # 4시간마다 predict (1h→4h, 노이즈 감소)
USE_4H_CANDLE = True  # 4h 캔들 기반 (백테스트: 360d +38.5%, 거래 23건)
MIN_STATE_PROB = 0.7  # state 확신도 70% 이상이어야 전환


@dataclass
class HMMPosition:
    symbol: str
    side: str  # "long" or "short"
    quantity: float
    entry_price: float


class HMMRegimeLiveEngine:
    """HMM Regime Detection → 자동 long/short/flat 전환."""

    EXCHANGE_NAME = "binance_hmm"

    def __init__(
        self,
        config: AppConfig,
        futures_exchange: ExchangeAdapter,
        market_data: MarketDataService,
        initial_capital_usdt: float = 100.0,
        leverage: int = 2,
        symbol: str = "BTC/USDT",
    ):
        self._config = config
        self._exchange = futures_exchange
        self._market_data = market_data
        self._initial_capital = initial_capital_usdt
        self._leverage = leverage
        self._symbol = symbol

        self._is_running = False
        self._task: asyncio.Task | None = None
        self._position: Optional[HMMPosition] = None
        self._cumulative_pnl = 0.0
        self._daily_pnl = 0.0
        self._last_eval_date: Optional[datetime] = None
        self._paused = False
        self._daily_paused = False
        self._coordinator = None

        # HMM 모델 상태
        self._model = None
        self._bullish_state: int = -1
        self._bearish_state: int = -1
        self._neutral_state: int = -1
        self._last_refit_at: Optional[datetime] = None

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def tracked_coins(self) -> list[str]:
        return [self._symbol]

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
        await self._refit_model()
        self._task = asyncio.create_task(self._loop(), name="hmm_regime_loop")
        logger.info("hmm_regime_started", capital=self._initial_capital, symbol=self._symbol)
        await emit_event("info", "engine", f"HMM Regime 시작 ({self._symbol}, {self._initial_capital} USDT)")

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
        """매시간 predict + 매일 refit."""
        while self._is_running:
            try:
                now = datetime.now(timezone.utc)

                # 매일 refit
                if (self._last_refit_at is None or
                        (now - self._last_refit_at).total_seconds() > REFIT_INTERVAL_HOURS * 3600):
                    await self._refit_model()

                # 4시간마다 predict + 포지션 전환
                await self._evaluate()

                # 다음 EVAL_INTERVAL까지 대기
                next_eval = (now + pd.Timedelta(hours=EVAL_INTERVAL_HOURS)).replace(minute=5, second=0, microsecond=0)
                wait = max(10, (next_eval - datetime.now(timezone.utc)).total_seconds())
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("hmm_loop_error", error=str(e), exc_info=True)
                await asyncio.sleep(300)

    async def evaluate_now(self):
        await self._evaluate()

    async def _refit_model(self):
        """HMM 모델 학습 (4h 캔들 기반)."""
        try:
            from hmmlearn.hmm import GaussianHMM

            tf = "4h" if USE_4H_CANDLE else "1h"
            limit = TRAIN_HOURS // (4 if USE_4H_CANDLE else 1) + 50
            df = await self._market_data.get_ohlcv_df(self._symbol, tf, limit=limit)
            min_bars = TRAIN_HOURS // (4 if USE_4H_CANDLE else 1)
            if df is None or len(df) < min_bars:
                logger.warning("hmm_refit_insufficient_data", available=len(df) if df is not None else 0, need=min_bars)
                return

            df = df.copy()
            df["log_return"] = np.log(df["close"] / df["close"].shift(1)).fillna(0.0)
            vol_window = 6 if USE_4H_CANDLE else 24  # 24h equivalent
            mom_window = 6 if USE_4H_CANDLE else 24
            df["vol_24"] = df["log_return"].rolling(vol_window).std().fillna(0.0)
            df["mom_24"] = df["close"].pct_change(mom_window).fillna(0.0)

            X = df[["log_return", "vol_24", "mom_24"]].values[-min_bars:]

            model = GaussianHMM(n_components=3, covariance_type="full", n_iter=200, random_state=42)
            with contextlib.redirect_stderr(io.StringIO()):
                model.fit(X)

            # state 분류 (수익률 평균으로 bullish/bearish/neutral)
            states = model.predict(X)
            returns = df["log_return"].values[-min_bars:]
            state_mean = {}
            for s in range(3):
                mask = states == s
                state_mean[s] = float(returns[mask].mean()) if mask.any() else 0.0

            sorted_states = sorted(state_mean.items(), key=lambda x: x[1])
            self._bearish_state = sorted_states[0][0]
            self._neutral_state = sorted_states[1][0]
            self._bullish_state = sorted_states[2][0]
            self._model = model
            self._last_refit_at = datetime.now(timezone.utc)

            logger.info("hmm_refit_complete",
                        bullish=self._bullish_state,
                        bearish=self._bearish_state,
                        neutral=self._neutral_state,
                        state_means={s: round(m * 100, 3) for s, m in state_mean.items()})
        except Exception as e:
            logger.error("hmm_refit_error", error=str(e), exc_info=True)

    async def _evaluate(self):
        now = datetime.now(timezone.utc)
        today = now.date()
        if self._last_eval_date != today:
            self._daily_pnl = 0.0
            self._daily_paused = False
            self._last_eval_date = today

        if self._paused or self._daily_paused or self._model is None:
            return

        try:
            tf = "4h" if USE_4H_CANDLE else "1h"
            df = await self._market_data.get_ohlcv_df(self._symbol, tf, limit=50)
            if df is None or len(df) < 10:
                return

            df = df.copy()
            df["log_return"] = np.log(df["close"] / df["close"].shift(1)).fillna(0.0)
            vol_window = 6 if USE_4H_CANDLE else 24
            mom_window = 6 if USE_4H_CANDLE else 24
            df["vol_24"] = df["log_return"].rolling(vol_window).std().fillna(0.0)
            df["mom_24"] = df["close"].pct_change(mom_window).fillna(0.0)

            X = df[["log_return", "vol_24", "mom_24"]].values[-1:]
            state = int(self._model.predict(X)[0])
            state_prob = float(self._model.predict_proba(X)[0][state])

            desired = 0  # flat
            if state_prob >= MIN_STATE_PROB:
                if state == self._bullish_state:
                    desired = 1  # long
                elif state == self._bearish_state:
                    desired = -1  # short
            else:
                # 확신도 부족 → 기존 포지션 유지
                desired = 0
                if self._position:
                    desired = 1 if self._position.side == "long" else -1

            current = 0
            if self._position:
                current = 1 if self._position.side == "long" else -1

            price = float(df["close"].iloc[-1])

            state_name = {self._bullish_state: "bullish", self._bearish_state: "bearish",
                          self._neutral_state: "neutral"}.get(state, f"unknown({state})")
            desired_name = {1: "long", -1: "short", 0: "flat"}[desired]
            current_name = {1: "long", -1: "short", 0: "flat"}[current]
            logger.info("hmm_eval_tick", state=state_name, desired=desired_name,
                        current=current_name, price=round(price, 2),
                        pnl=round(self._cumulative_pnl, 2))

            # 포지션 전환
            if desired != current:
                # 기존 청산
                if self._position:
                    await self._close_position(price)
                # 신규 진입
                if desired != 0:
                    side = "long" if desired == 1 else "short"
                    await self._open_position(side, price)

                await self._check_loss_limits()
                state_name = {self._bullish_state: "bullish", self._bearish_state: "bearish",
                              self._neutral_state: "neutral"}.get(state, "unknown")
                logger.info("hmm_state_change", state=state_name, desired=desired,
                            previous=current, price=price)
        except Exception as e:
            logger.error("hmm_eval_error", error=str(e), exc_info=True)

    async def _open_position(self, side: str, price: float):
        available = self._initial_capital + self._cumulative_pnl
        notional = available * self._leverage * 0.9  # 90% 사용
        if notional < MIN_NOTIONAL:
            return
        qty = notional / price
        try:
            if side == "long":
                order = await self._exchange.create_market_buy(self._symbol, qty)
            else:
                order = await self._exchange.create_market_sell(self._symbol, qty)

            exec_price = float(getattr(order, 'executed_price', None) or price)
            exec_qty = float(getattr(order, 'executed_quantity', None) or qty)

            self._position = HMMPosition(
                symbol=self._symbol, side=side, quantity=exec_qty, entry_price=exec_price
            )
            await self._record_order("buy" if side == "long" else "sell",
                                      exec_price, exec_qty, reason=f"hmm_{side}_entry")
            notional = exec_qty * exec_price
            max_loss = self._initial_capital * MAX_TOTAL_LOSS_PCT
            await emit_event("info", "engine",
                             f"{'📈' if side=='long' else '📉'} HMM {side}: {self._symbol} @ {exec_price:.2f}",
                             detail=f"수량 {exec_qty:.6f} | 명목 {notional:.1f} USDT | 청산: regime 전환 시 | 최대손실 한도 -{max_loss:.0f} USDT")
        except Exception as e:
            logger.error("hmm_open_error", side=side, error=str(e))

    async def _close_position(self, price: float):
        if not self._position:
            return
        pos = self._position
        try:
            if pos.side == "long":
                order = await self._exchange.create_market_sell(self._symbol, pos.quantity)
                pnl = (price - pos.entry_price) * pos.quantity
            else:
                order = await self._exchange.create_market_buy(self._symbol, pos.quantity)
                pnl = (pos.entry_price - price) * pos.quantity

            self._cumulative_pnl += pnl
            self._daily_pnl += pnl
            self._position = None

            exec_price = float(getattr(order, 'executed_price', None) or price)
            await self._record_order("sell" if pos.side == "long" else "buy",
                                      exec_price, pos.quantity, pnl=pnl,
                                      reason=f"hmm_{pos.side}_exit")
            emoji = "💰" if pnl > 0 else "💸"
            await emit_event("info", "engine",
                             f"{emoji} HMM exit {pos.side}: {self._symbol} PnL {pnl:+.2f}")
        except Exception as e:
            logger.error("hmm_close_error", error=str(e))

    async def _check_loss_limits(self):
        if self._cumulative_pnl <= -self._initial_capital * MAX_TOTAL_LOSS_PCT:
            self._paused = True
            await emit_event("error", "engine",
                             f"🚨 HMM 누적 손실 한도 ({self._cumulative_pnl:.2f}) — 자동 중지")
        if self._daily_pnl <= -self._initial_capital * MAX_DAILY_LOSS_PCT:
            self._daily_paused = True

    async def _record_order(self, side, price, qty, pnl=0.0, reason=""):
        sf = get_session_factory()
        async with sf() as session:
            order = Order(
                exchange=self.EXCHANGE_NAME, symbol=self._symbol, side=side,
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
            last_entry = None
            for o in orders:
                if "entry" in (o.signal_reason or ""):
                    last_entry = o
                elif "exit" in (o.signal_reason or ""):
                    cum_pnl += float(o.realized_pnl or 0)
                    last_entry = None
            self._cumulative_pnl = cum_pnl
            if last_entry:
                side = "long" if last_entry.side == "buy" else "short"
                self._position = HMMPosition(
                    symbol=self._symbol, side=side,
                    quantity=float(last_entry.executed_quantity or 0),
                    entry_price=float(last_entry.executed_price or 0),
                )
            logger.info("hmm_restored", position=self._position is not None, pnl=round(cum_pnl, 2))

    def get_status(self) -> dict:
        return {
            "exchange": self.EXCHANGE_NAME,
            "is_running": self._is_running,
            "symbol": self._symbol,
            "leverage": self._leverage,
            "capital_usdt": self._initial_capital,
            "position": {"symbol": self._symbol, "side": self._position.side,
                          "entry": self._position.entry_price,
                          "qty": self._position.quantity} if self._position else None,
            "cumulative_pnl": round(self._cumulative_pnl, 2),
            "daily_pnl": round(self._daily_pnl, 2),
            "paused": self._paused,
            "model_fitted": self._model is not None,
            "last_refit": self._last_refit_at.isoformat() if self._last_refit_at else None,
            "states": {"bullish": self._bullish_state, "bearish": self._bearish_state,
                        "neutral": self._neutral_state} if self._model else None,
        }
