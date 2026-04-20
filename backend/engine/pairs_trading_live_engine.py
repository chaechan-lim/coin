"""
Pairs Trading 라이브 R&D 엔진.

- BTC/ETH 1시간 스프레드 z-score 기반
- 델타 중립 성격의 양방향 선물 페어
- 소액 실거래 데이터 확보 목적
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import structlog
from sqlalchemy import func, select

from config import AppConfig
from core.event_bus import emit_event
from core.models import Order
from db.session import get_session_factory
from exchange.base import ExchangeAdapter
from services.market_data import MarketDataService

logger = structlog.get_logger(__name__)

FEE_RATE = 0.0004
SLIPPAGE_RATE = 0.0001
TOTAL_COST_RATE = FEE_RATE + SLIPPAGE_RATE
MIN_NOTIONAL_USDT = 10.0
EVALUATION_MINUTE_UTC = 5
MAX_TOTAL_LOSS_PCT = 0.10
MAX_DAILY_LOSS_PCT = 0.05
POSITION_NOTIONAL_PCT = 0.90


@dataclass
class PairPosition:
    trade_id: str
    pair_direction: str
    hedge_ratio: float
    qty_a: float
    qty_b: float
    entry_price_a: float
    entry_price_b: float
    margin_used: float
    entry_z: float
    entered_at: datetime


class PairsTradingLiveEngine:
    EXCHANGE_NAME = "binance_pairs"
    STRATEGY_NAME = "pairs_trading_live"

    def __init__(
        self,
        config: AppConfig,
        futures_exchange: ExchangeAdapter,
        market_data: MarketDataService,
        initial_capital_usdt: float = 75.0,
        leverage: int = 2,
        coin_a: str = "BTC/USDT",
        coin_b: str = "ETH/USDT",
        lookback_hours: int = 336,
        z_entry: float = 2.0,
        z_exit: float = 0.5,
        z_stop: float = 5.0,
    ):
        self._config = config
        self._exchange = futures_exchange
        self._market_data = market_data
        self._initial_capital = initial_capital_usdt
        self._leverage = leverage
        self._coin_a = coin_a
        self._coin_b = coin_b
        self._lookback_hours = lookback_hours
        self._z_entry = z_entry
        self._z_exit = z_exit
        self._z_stop = z_stop

        self._is_running = False
        self._task: asyncio.Task | None = None
        self._engine_registry = None
        self._rnd_coordinator = None
        self._position: Optional[PairPosition] = None
        self._daily_realized_pnl = 0.0
        self._cumulative_pnl = 0.0
        self._last_eval_hour: Optional[datetime] = None
        self._last_eval_date: Optional[datetime.date] = None
        self._last_evaluated_at: Optional[datetime] = None
        self._last_idle_reason: str = "다음 시간대 평가 대기 중"
        self._paused = False
        self._daily_paused = False
        self._consecutive_close_failures = 0

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def tracked_coins(self) -> list[str]:
        return [self._coin_a, self._coin_b]

    def set_engine_registry(self, registry):
        self._engine_registry = registry

    def set_broadcast_callback(self, callback):
        pass

    def set_agent_coordinator(self, coordinator):
        pass

    def set_futures_rnd_coordinator(self, coordinator):
        self._rnd_coordinator = coordinator

    def _build_reason(self, action: str, trade_id: str, pair_direction: str, leg: str) -> str:
        return (
            f"pairs_{action}:trade={trade_id}:pair_direction={pair_direction}:"
            f"leg={leg}:exchange={self.EXCHANGE_NAME}"
        )

    def _trade_group_type(self, action: str) -> str:
        return f"pairs_{action}"

    async def _emit_pairs_journal(
        self,
        trade_id: str,
        pair_direction: str,
        stage: str,
        title: str,
        detail: str | None = None,
        level: str = "info",
        extra: dict | None = None,
    ):
        metadata = {
            "trade_id": trade_id,
            "exchange": self.EXCHANGE_NAME,
            "pair_direction": pair_direction,
            "stage": stage,
            "symbols": [self._coin_a, self._coin_b],
        }
        if extra:
            metadata.update(extra)
        await emit_event(level, "pairs_trade", title, detail=detail, metadata=metadata)

    async def start(self):
        if self._is_running:
            return
        self._is_running = True
        if self._rnd_coordinator is not None:
            await self._rnd_coordinator.register_engine(self.EXCHANGE_NAME, self._initial_capital)
        await self._restore_state()
        await self._reconcile_exchange_state()
        await self._sync_rnd_coordinator_state()
        for symbol in self.tracked_coins:
            try:
                await self._exchange.set_leverage(symbol, self._leverage)
            except Exception:
                logger.warning("pairs_live_leverage_failed", symbol=symbol, leverage=self._leverage, exc_info=True)
        self._task = asyncio.create_task(self._loop(), name="pairs_trading_live_loop")
        logger.info(
            "pairs_live_started",
            coin_a=self._coin_a,
            coin_b=self._coin_b,
            capital=self._initial_capital,
            leverage=self._leverage,
            lookback_hours=self._lookback_hours,
            z_entry=self._z_entry,
            z_exit=self._z_exit,
            z_stop=self._z_stop,
        )
        await emit_event("info", "engine", f"Pairs Trading 라이브 엔진 시작 ({self._coin_a}-{self._coin_b}, {self._initial_capital} USDT)")

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
        logger.info("pairs_live_stopped")
        await emit_event("info", "engine", "Pairs Trading 라이브 엔진 중지")

    async def _loop(self):
        while self._is_running:
            try:
                now = datetime.now(timezone.utc)
                target = now.replace(minute=EVALUATION_MINUTE_UTC, second=0, microsecond=0)
                if target <= now:
                    target = target + pd.Timedelta(hours=1)
                await asyncio.sleep((target - now).total_seconds())
                if self._is_running:
                    await self._evaluation_cycle()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("pairs_live_loop_error", exc_info=True)
                await asyncio.sleep(60)

    async def evaluate_now(self):
        await self._evaluation_cycle(force=True)

    async def _evaluation_cycle(self, force: bool = False):
        now = datetime.now(timezone.utc)
        hour_bucket = now.replace(minute=0, second=0, microsecond=0)
        if self._last_eval_date != now.date():
            self._daily_realized_pnl = 0.0
            self._daily_paused = False
            self._last_eval_date = now.date()
        if not force and self._last_eval_hour == hour_bucket:
            return
        self._last_evaluated_at = now
        self._last_eval_hour = hour_bucket

        if self._paused or self._daily_paused:
            self._last_idle_reason = "손실 한도 도달로 진입 정지"
            return

        logger.info("pairs_live_eval_start", pair=f"{self._coin_a}-{self._coin_b}")
        signal = await self._build_signal()
        if signal is None:
            self._last_idle_reason = "스프레드 시그널 계산 대기 중 (데이터 부족 또는 분산 0)"
            return

        if self._position is not None:
            await self._check_exit(signal)
            if self._position is not None and not self._paused and not self._daily_paused:
                self._last_idle_reason = (
                    f"청산 조건 대기 중 (|z|={abs(float(signal['z_score'])):.2f}, exit={self._z_exit:.2f})"
                )
        else:
            await self._check_loss_limits()
            if self._paused or self._daily_paused:
                self._last_idle_reason = "손실 한도 도달로 진입 정지"
                logger.warning("pairs_live_entry_skipped_after_loss_limit")
                return
            await self._check_entry(signal)
            if self._position is None and not self._paused and not self._daily_paused:
                z = abs(float(signal["z_score"]))
                if z < self._z_entry:
                    self._last_idle_reason = (
                        f"진입 조건 대기 중 (|z|={z:.2f}, entry={self._z_entry:.2f}, gap={self._z_entry - z:.2f})"
                    )
                else:
                    self._last_idle_reason = "진입 검토 완료, 체결 없음"
            elif self._position is not None:
                self._last_idle_reason = f"포지션 보유 중 (entry z={float(signal['z_score']):.2f})"

        await self._check_loss_limits()
        logger.info(
            "pairs_live_eval_complete",
            position_open=self._position is not None,
            daily_pnl=round(self._daily_realized_pnl, 2),
            cumulative_pnl=round(self._cumulative_pnl, 2),
        )

    def _next_evaluation_at(self) -> datetime:
        now = datetime.now(timezone.utc)
        target = now.replace(minute=EVALUATION_MINUTE_UTC, second=0, microsecond=0)
        if target <= now:
            target = target + pd.Timedelta(hours=1)
        return target

    async def _fetch_hourly_df(self, symbol: str) -> pd.DataFrame | None:
        try:
            df = await self._market_data.get_ohlcv_df(symbol, "1h", limit=max(self._lookback_hours + 50, 500))
        except Exception:
            logger.warning("pairs_live_fetch_failed", symbol=symbol, exc_info=True)
            return None
        if df is None or len(df) < self._lookback_hours + 10:
            return None
        return df.sort_index()

    async def _build_signal(self) -> dict | None:
        df_a = await self._fetch_hourly_df(self._coin_a)
        df_b = await self._fetch_hourly_df(self._coin_b)
        if df_a is None or df_b is None:
            return None

        common = df_a.index.intersection(df_b.index).sort_values()
        if len(common) < self._lookback_hours + 10:
            return None
        df_a = df_a.loc[common]
        df_b = df_b.loc[common]
        log_a = np.log(df_a["close"].astype(float).values)
        log_b = np.log(df_b["close"].astype(float).values)
        win_a = log_a[-self._lookback_hours:]
        win_b = log_b[-self._lookback_hours:]
        hedge = self._calculate_hedge_ratio(pd.Series(win_a), pd.Series(win_b))
        spread = win_b - hedge * win_a
        spread_mean = float(np.mean(spread))
        spread_std = float(np.std(spread))
        if spread_std <= 0:
            return None

        price_a = float(df_a["close"].iloc[-1])
        price_b = float(df_b["close"].iloc[-1])
        current_spread = float(np.log(price_b) - hedge * np.log(price_a))
        z = (current_spread - spread_mean) / spread_std
        return {
            "timestamp": common[-1],
            "price_a": price_a,
            "price_b": price_b,
            "hedge_ratio": hedge,
            "z_score": float(z),
        }

    def _calculate_hedge_ratio(self, a: pd.Series, b: pd.Series) -> float:
        if len(a) < 30:
            return 1.0
        x_mean = a.mean()
        y_mean = b.mean()
        cov = ((a - x_mean) * (b - y_mean)).sum()
        var_x = ((a - x_mean) ** 2).sum()
        return float(cov / var_x) if var_x > 0 else 1.0

    async def _check_entry(self, signal: dict):
        if self._has_engine_conflict():
            return
        if await self._has_external_position_conflict():
            return

        z = float(signal["z_score"])
        if abs(z) < self._z_entry:
            return

        pair_direction = "long_a_short_b" if z > self._z_entry else "short_a_long_b"
        available_margin = await self._available_margin()
        if available_margin <= 0:
            return
        total_notional = available_margin * self._leverage * POSITION_NOTIONAL_PCT
        hedge = max(abs(float(signal["hedge_ratio"])), 0.1)
        leg_a_notional = total_notional / (1.0 + hedge)
        leg_b_notional = total_notional - leg_a_notional
        if min(leg_a_notional, leg_b_notional) < MIN_NOTIONAL_USDT:
            return

        qty_a = await self._normalize_quantity(self._coin_a, leg_a_notional / float(signal["price_a"]))
        qty_b = await self._normalize_quantity(self._coin_b, leg_b_notional / float(signal["price_b"]))
        if qty_a <= 0 or qty_b <= 0:
            return

        reservation_margin = (
            (qty_a * float(signal["price_a"])) + (qty_b * float(signal["price_b"]))
        ) / max(self._leverage, 1)
        reservation_token = None
        if self._rnd_coordinator is not None:
            reserved, reason, reservation_token = await self._rnd_coordinator.request_reservation(
                self.EXCHANGE_NAME,
                self._initial_capital,
                [self._coin_a, self._coin_b],
                reservation_margin,
            )
            if not reserved:
                logger.info("pairs_live_entry_rejected_by_coordinator", reason=reason)
                return

        open_a_side = "buy" if pair_direction == "long_a_short_b" else "sell"
        open_b_side = "sell" if pair_direction == "long_a_short_b" else "buy"
        trade_id = uuid.uuid4().hex[:12]
        order_a = None
        order_b = None
        await self._emit_pairs_journal(
            trade_id,
            pair_direction,
            "entry_attempt",
            "Pairs entry attempt",
            detail=f"z={z:.4f}",
            extra={"z_score": z, "hedge_ratio": hedge},
        )
        try:
            order_a = await self._submit_order(self._coin_a, open_a_side, qty_a)
            await self._emit_pairs_journal(
                trade_id,
                pair_direction,
                "entry_leg_filled",
                f"Pairs entry leg filled: {self._coin_a}",
                detail=f"side={open_a_side}, qty={qty_a}",
                extra={"leg": "a", "side": open_a_side, "symbol": self._coin_a},
            )
            order_b = await self._submit_order(self._coin_b, open_b_side, qty_b)
            await self._emit_pairs_journal(
                trade_id,
                pair_direction,
                "entry_leg_filled",
                f"Pairs entry leg filled: {self._coin_b}",
                detail=f"side={open_b_side}, qty={qty_b}",
                extra={"leg": "b", "side": open_b_side, "symbol": self._coin_b},
            )
        except Exception:
            logger.error("pairs_live_entry_failed", pair_direction=pair_direction, exc_info=True)
            if order_a is not None:
                try:
                    rollback_side = "sell" if open_a_side == "buy" else "buy"
                    await self._submit_order(self._coin_a, rollback_side, float(order_a.filled or qty_a))
                    await self._emit_pairs_journal(
                        trade_id,
                        pair_direction,
                        "entry_leg_rollback",
                        "Pairs entry rollback completed",
                        detail=f"symbol={self._coin_a}, side={rollback_side}",
                        extra={"leg": "a", "symbol": self._coin_a},
                    )
                except Exception:
                    logger.error("pairs_live_entry_rollback_failed", symbol=self._coin_a, exc_info=True)
                    await self._emit_pairs_journal(
                        trade_id,
                        pair_direction,
                        "entry_leg_rollback_failed",
                        "Pairs entry rollback failed",
                        detail=f"symbol={self._coin_a}, side={rollback_side}",
                        level="error",
                        extra={"leg": "a", "symbol": self._coin_a},
                    )
            if order_a is not None or order_b is not None:
                await self._emit_pairs_journal(
                    trade_id,
                    pair_direction,
                    "entry_attempt_failed",
                    "Pairs entry failed",
                    detail="one or more legs failed",
                    level="error",
                    extra={
                        "leg_a_filled": bool(order_a),
                        "leg_b_filled": bool(order_b),
                        "z_score": z,
                    },
                )
            if self._rnd_coordinator is not None:
                await self._rnd_coordinator.release_reservation(self.EXCHANGE_NAME, reservation_token)
                await self._sync_rnd_coordinator_state()
            await self._emit_pairs_journal(
                trade_id,
                pair_direction,
                "entry_failed",
                f"Pairs entry failed: {self._coin_a}-{self._coin_b}",
                detail=pair_direction,
                level="error",
            )
            return

        status_a = getattr(order_a, 'status', None)
        filled_a = float(getattr(order_a, 'executed_quantity', None) or getattr(order_a, 'filled', 0) or 0)
        exec_price_a = float(getattr(order_a, 'executed_price', None) or getattr(order_a, 'price', None) or getattr(order_a, 'average', 0) or 0)

        status_b = getattr(order_b, 'status', None)
        filled_b = float(getattr(order_b, 'executed_quantity', None) or getattr(order_b, 'filled', 0) or 0)
        exec_price_b = float(getattr(order_b, 'executed_price', None) or getattr(order_b, 'price', None) or getattr(order_b, 'average', 0) or 0)

        if (status_a not in ('filled', 'closed') or filled_a <= 0 or exec_price_a <= 0 or
                status_b not in ('filled', 'closed') or filled_b <= 0 or exec_price_b <= 0):
            logger.error("pairs_live_entry_not_filled", status_a=status_a, status_b=status_b)
            # 하나라도 체결 안 됨 → 체결된 레그 롤백
            if status_a in ('filled', 'closed') and filled_a > 0:
                try:
                    rollback_side = "sell" if open_a_side == "buy" else "buy"
                    await self._submit_order(self._coin_a, rollback_side, filled_a)
                except Exception:
                    logger.error("pairs_live_entry_not_filled_rollback_a_failed", exc_info=True)
            if status_b in ('filled', 'closed') and filled_b > 0:
                try:
                    rollback_side = "sell" if open_b_side == "buy" else "buy"
                    await self._submit_order(self._coin_b, rollback_side, filled_b)
                except Exception:
                    logger.error("pairs_live_entry_not_filled_rollback_b_failed", exc_info=True)
            if self._rnd_coordinator is not None:
                await self._rnd_coordinator.release_reservation(self.EXCHANGE_NAME, reservation_token)
                await self._sync_rnd_coordinator_state()
            return

        fee_a = float(getattr(order_a, 'fee', None) or (exec_price_a * filled_a * TOTAL_COST_RATE))
        fee_b = float(getattr(order_b, 'fee', None) or (exec_price_b * filled_b * TOTAL_COST_RATE))
        margin_used = (exec_price_a * filled_a + exec_price_b * filled_b) / max(self._leverage, 1)
        self._position = PairPosition(
            trade_id=trade_id,
            pair_direction=pair_direction,
            hedge_ratio=hedge,
            qty_a=filled_a,
            qty_b=filled_b,
            entry_price_a=exec_price_a,
            entry_price_b=exec_price_b,
            margin_used=margin_used,
            entry_z=z,
            entered_at=datetime.now(timezone.utc),
        )
        self._daily_realized_pnl -= fee_a + fee_b
        self._cumulative_pnl -= fee_a + fee_b
        if self._rnd_coordinator is not None:
            await self._rnd_coordinator.note_pnl(self.EXCHANGE_NAME, -(fee_a + fee_b))
            await self._sync_rnd_coordinator_state(reservation_token=reservation_token)
        await self._record_order(
            self._coin_a,
            open_a_side,
            "long" if open_a_side == "buy" else "short",
            exec_price_a,
            filled_a,
            margin_used / 2,
            0.0,
            0.0,
            exec_price_a,
            self._build_reason("entry", trade_id, pair_direction, "a"),
            trade_group_id=trade_id,
            trade_group_type=self._trade_group_type("entry"),
        )
        await self._record_order(
            self._coin_b,
            open_b_side,
            "long" if open_b_side == "buy" else "short",
            exec_price_b,
            filled_b,
            margin_used / 2,
            0.0,
            0.0,
            exec_price_b,
            self._build_reason("entry", trade_id, pair_direction, "b"),
            trade_group_id=trade_id,
            trade_group_type=self._trade_group_type("entry"),
        )
        await self._emit_pairs_journal(
            trade_id,
            pair_direction,
            "entry_opened",
            f"Pairs entry opened: {self._coin_a}-{self._coin_b}",
            detail=f"{pair_direction} z={z:.4f}",
            extra={
                "order_a_id": getattr(order_a, "id", None),
                "order_b_id": getattr(order_b, "id", None),
                "entry_order_qty_a": filled_a,
                "entry_order_qty_b": filled_b,
                "entry_order_price_a": exec_price_a,
                "entry_order_price_b": exec_price_b,
            },
        )
        await emit_event("info", "engine",
                         f"📊 Pairs {pair_direction}: {self._coin_a}-{self._coin_b}",
                         detail=f"z={z:.2f} (진입 ±{self._z_entry:.1f} / 청산 ±{self._z_exit:.1f} / 손절 ±{self._z_stop:.1f})")

    async def _check_exit(self, signal: dict):
        pos = self._position
        if pos is None:
            return
        z = float(signal["z_score"])
        should_exit = abs(z) <= self._z_exit or abs(z) >= self._z_stop
        if not should_exit:
            return

        if not await self._exchange_position_matches():
            self._paused = True
            logger.error("pairs_live_exit_blocked_mismatch")
            await emit_event("error", "engine", "Pairs Trading 포지션 불일치 감지. 수동 확인 전까지 일시정지.")
            await self._emit_pairs_journal(
                pos.trade_id,
                pos.pair_direction,
                "exit_blocked",
                f"Pairs exit blocked: {self._coin_a}-{self._coin_b}",
                detail="exchange position mismatch",
                level="error",
                extra={"reason": "exchange_position_mismatch"},
            )
            return

        close_a_side = "sell" if pos.pair_direction == "long_a_short_b" else "buy"
        close_b_side = "buy" if pos.pair_direction == "long_a_short_b" else "sell"

        await self._emit_pairs_journal(
            pos.trade_id,
            pos.pair_direction,
            "exit_attempt",
            f"Pairs exit attempt: {self._coin_a}-{self._coin_b}",
            detail=f"z={z:.4f}",
            extra={
                "reason": "z_exit_or_stop",
                "z_score": z,
                "z_exit": self._z_exit,
                "z_stop": self._z_stop,
            },
        )

        order_a = None
        order_b = None
        try:
            order_a = await self._submit_order(self._coin_a, close_a_side, pos.qty_a, reduce_only=True)
            await self._emit_pairs_journal(
                pos.trade_id,
                pos.pair_direction,
                "exit_leg_filled",
                f"Pairs exit leg filled: {self._coin_a}",
                detail=f"side={close_a_side}, qty={pos.qty_a}",
                extra={"leg": "a", "symbol": self._coin_a, "side": close_a_side},
            )
            order_b = await self._submit_order(self._coin_b, close_b_side, pos.qty_b, reduce_only=True)
            await self._emit_pairs_journal(
                pos.trade_id,
                pos.pair_direction,
                "exit_leg_filled",
                f"Pairs exit leg filled: {self._coin_b}",
                detail=f"side={close_b_side}, qty={pos.qty_b}",
                extra={"leg": "b", "symbol": self._coin_b, "side": close_b_side},
            )
        except Exception:
            logger.error("pairs_live_exit_failed", trade_id=pos.trade_id, exc_info=True)
            if order_a is not None:
                rollback_qty_a = float(order_a.filled or pos.qty_a)
                rollback_a_side = "buy" if close_a_side == "sell" else "sell"
                try:
                    await self._submit_order(self._coin_a, rollback_a_side, rollback_qty_a)
                    await self._emit_pairs_journal(
                        pos.trade_id,
                        pos.pair_direction,
                        "exit_leg_rollback",
                        "Pairs exit rollback succeeded",
                        detail=f"symbol={self._coin_a}, side={rollback_a_side}",
                        extra={"leg": "a", "symbol": self._coin_a},
                    )
                except Exception:
                    logger.error("pairs_live_exit_rollback_failed", symbol=self._coin_a, exc_info=True)
                    await self._emit_pairs_journal(
                        pos.trade_id,
                        pos.pair_direction,
                        "exit_leg_rollback_failed",
                        "Pairs exit rollback failed",
                        detail=f"symbol={self._coin_a}, side={rollback_a_side}",
                        level="error",
                        extra={"leg": "a", "symbol": self._coin_a},
                    )
            if order_b is not None:
                rollback_qty_b = float(order_b.filled or pos.qty_b)
                rollback_b_side = "buy" if close_b_side == "sell" else "sell"
                try:
                    await self._submit_order(self._coin_b, rollback_b_side, rollback_qty_b)
                    await self._emit_pairs_journal(
                        pos.trade_id,
                        pos.pair_direction,
                        "exit_leg_rollback",
                        "Pairs exit rollback succeeded",
                        detail=f"symbol={self._coin_b}, side={rollback_b_side}",
                        extra={"leg": "b", "symbol": self._coin_b},
                    )
                except Exception:
                    logger.error("pairs_live_exit_rollback_failed", symbol=self._coin_b, exc_info=True)
                    await self._emit_pairs_journal(
                        pos.trade_id,
                        pos.pair_direction,
                        "exit_leg_rollback_failed",
                        "Pairs exit rollback failed",
                        detail=f"symbol={self._coin_b}, side={rollback_b_side}",
                        level="error",
                        extra={"leg": "b", "symbol": self._coin_b},
                    )
            await self._emit_pairs_journal(
                pos.trade_id,
                pos.pair_direction,
                "exit_failed",
                "Pairs exit failed",
                detail=f"{pos.pair_direction}",
                level="error",
                extra={
                    "leg_a_filled": bool(order_a),
                    "leg_b_filled": bool(order_b),
                    "z_score": z,
                },
            )
            return

        exit_status_a = getattr(order_a, 'status', None)
        exit_filled_a = float(getattr(order_a, 'executed_quantity', None) or getattr(order_a, 'filled', 0) or 0)
        exec_price_a = float(getattr(order_a, 'executed_price', None) or getattr(order_a, 'price', None) or getattr(order_a, 'average', 0) or 0)

        exit_status_b = getattr(order_b, 'status', None)
        exit_filled_b = float(getattr(order_b, 'executed_quantity', None) or getattr(order_b, 'filled', 0) or 0)
        exec_price_b = float(getattr(order_b, 'executed_price', None) or getattr(order_b, 'price', None) or getattr(order_b, 'average', 0) or 0)

        if (exit_status_a not in ('filled', 'closed') or exit_filled_a <= 0 or exec_price_a <= 0 or
                exit_status_b not in ('filled', 'closed') or exit_filled_b <= 0 or exec_price_b <= 0):
            self._consecutive_close_failures += 1
            logger.error("pairs_live_exit_not_filled",
                         status_a=exit_status_a, status_b=exit_status_b,
                         trade_id=pos.trade_id, consecutive=self._consecutive_close_failures)
            if self._consecutive_close_failures >= 3:
                self._paused = True
                await emit_event("error", "engine",
                                 f"🚨 PairsTrading 청산 {self._consecutive_close_failures}회 연속 실패 — 자동 중지",
                                 detail=f"포지션 {pos.pair_direction} {self._coin_a}-{self._coin_b} 수동 확인 필요")
            await self._emit_pairs_journal(
                pos.trade_id, pos.pair_direction, "exit_not_filled",
                "Pairs exit not filled — position kept",
                detail=f"status_a={exit_status_a}, status_b={exit_status_b}",
                level="error",
            )
            return

        self._consecutive_close_failures = 0

        fee_a = float(getattr(order_a, 'fee', None) or (exec_price_a * exit_filled_a * TOTAL_COST_RATE))
        fee_b = float(getattr(order_b, 'fee', None) or (exec_price_b * exit_filled_b * TOTAL_COST_RATE))

        if pos.pair_direction == "long_a_short_b":
            pnl_a = (exec_price_a - pos.entry_price_a) * exit_filled_a
            pnl_b = (pos.entry_price_b - exec_price_b) * exit_filled_b
        else:
            pnl_a = (pos.entry_price_a - exec_price_a) * exit_filled_a
            pnl_b = (exec_price_b - pos.entry_price_b) * exit_filled_b
        realized = pnl_a + pnl_b - fee_a - fee_b
        pnl_pct = (realized / max(pos.margin_used, 1e-9)) * 100.0
        self._daily_realized_pnl += realized
        self._cumulative_pnl += realized
        leg_a_direction = "long" if pos.pair_direction == "long_a_short_b" else "short"
        leg_b_direction = "short" if pos.pair_direction == "long_a_short_b" else "long"
        await self._record_order(
            self._coin_a,
            close_a_side,
            leg_a_direction,
            exec_price_a,
            exit_filled_a,
            pos.margin_used / 2,
            pnl_a - fee_a,
            pnl_pct,
            pos.entry_price_a,
            self._build_reason("exit", pos.trade_id, pos.pair_direction, "a"),
            trade_group_id=pos.trade_id,
            trade_group_type=self._trade_group_type("exit"),
        )
        await self._record_order(
            self._coin_b,
            close_b_side,
            leg_b_direction,
            exec_price_b,
            exit_filled_b,
            pos.margin_used / 2,
            pnl_b - fee_b,
            pnl_pct,
            pos.entry_price_b,
            self._build_reason("exit", pos.trade_id, pos.pair_direction, "b"),
            trade_group_id=pos.trade_id,
            trade_group_type=self._trade_group_type("exit"),
        )
        await self._emit_pairs_journal(
            pos.trade_id,
            pos.pair_direction,
            "exit_closed",
            f"Pairs exit closed: {self._coin_a}-{self._coin_b}",
            detail=f"{pos.pair_direction} realized={realized:.2f} USDT",
            extra={
                "realized_pnl": round(realized, 4),
                "pnl_pct": round(pnl_pct, 4),
                "order_a_id": getattr(order_a, "id", None),
                "order_b_id": getattr(order_b, "id", None),
                "exit_qty_a": exit_filled_a,
                "exit_qty_b": exit_filled_b,
            },
        )
        self._position = None
        if self._rnd_coordinator is not None:
            await self._rnd_coordinator.note_pnl(self.EXCHANGE_NAME, realized)
            await self._sync_rnd_coordinator_state()

    async def _submit_order(self, symbol: str, side: str, qty: float, reduce_only: bool = False):
        if side == "buy":
            return await self._exchange.create_market_buy(symbol, qty, reduce_only=reduce_only)
        return await self._exchange.create_market_sell(symbol, qty, reduce_only=reduce_only)

    async def _normalize_quantity(self, symbol: str, raw_qty: float) -> float:
        try:
            qty = float(self._exchange.amount_to_precision(symbol, raw_qty))
        except Exception:
            qty = raw_qty
        try:
            market = self._exchange.market(symbol)
            min_amount = float(market.get("limits", {}).get("amount", {}).get("min", 0) or 0)
        except Exception:
            min_amount = 0.0
        if qty < max(min_amount, 0.0):
            return 0.0
        return qty

    async def _has_external_position_conflict(self) -> bool:
        try:
            pos_a = await self._exchange.fetch_futures_position(self._coin_a)
            pos_b = await self._exchange.fetch_futures_position(self._coin_b)
        except Exception:
            logger.warning("pairs_live_position_check_failed", exc_info=True)
            return True
        if self._position is not None:
            return False
        return any(pos is not None and abs(float(getattr(pos, "amount", 0.0) or 0.0)) > 0 for pos in (pos_a, pos_b))

    def _has_engine_conflict(self) -> bool:
        if self._engine_registry is None:
            return False
        for name in ("binance_futures", "binance_surge"):
            eng = self._engine_registry.get_engine(name)
            if eng is not None and getattr(eng, "is_running", False):
                return True
        return False

    async def _available_margin(self) -> float:
        used = self._position.margin_used if self._position is not None else 0.0
        budget_margin = max(0.0, self._initial_capital + self._cumulative_pnl - used)
        free_margin = await self._free_usdt_margin()
        return max(0.0, min(budget_margin, free_margin))

    async def _check_loss_limits(self):
        if self._cumulative_pnl <= -self._initial_capital * MAX_TOTAL_LOSS_PCT:
            self._paused = True
            await emit_event("error", "engine", f"🚨 Pairs Trading 누적 손실 한도 도달 ({self._cumulative_pnl:.2f} USDT)")
        if self._daily_realized_pnl <= -self._initial_capital * MAX_DAILY_LOSS_PCT:
            self._daily_paused = True
            await emit_event("warning", "engine", f"⚠️ Pairs Trading 일일 손실 한도 도달 ({self._daily_realized_pnl:.2f} USDT)")

    async def _record_order(
        self,
        symbol: str,
        side: str,
        direction: str,
        price: float,
        quantity: float,
        margin_used: float,
        pnl: float,
        pnl_pct: float,
        entry_price: float,
        reason: str,
        trade_group_id: str | None = None,
        trade_group_type: str | None = None,
    ):
        sf = get_session_factory()
        async with sf() as session:
            session.add(
                Order(
                    exchange=self.EXCHANGE_NAME,
                    symbol=symbol,
                    side=side,
                    order_type="market",
                    status="filled",
                    requested_price=price,
                    executed_price=price,
                    requested_quantity=quantity,
                    executed_quantity=quantity,
                    fee=quantity * price * FEE_RATE,
                    fee_currency="USDT",
                    is_paper=False,
                    direction=direction,
                    leverage=self._leverage,
                    margin_used=margin_used,
                    realized_pnl=pnl if reason.startswith("pairs_exit") else 0.0,
                    realized_pnl_pct=pnl_pct if reason.startswith("pairs_exit") else None,
                    entry_price=entry_price,
                    strategy_name=self.STRATEGY_NAME,
                    signal_confidence=1.0,
                    signal_reason=reason,
                    trade_group_id=trade_group_id,
                    trade_group_type=trade_group_type,
                    created_at=datetime.now(timezone.utc),
                    filled_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()

    async def _restore_state(self):
        sf = get_session_factory()
        async with sf() as session:
            result = await session.execute(
                select(Order)
                .where(Order.exchange == self.EXCHANGE_NAME)
                .where(Order.strategy_name == self.STRATEGY_NAME)
                .order_by(Order.created_at)
            )
            orders = result.scalars().all()
            today = datetime.now(timezone.utc).date()
            self._cumulative_pnl = 0.0
            self._daily_realized_pnl = 0.0
            self._last_eval_date = today
            for order in orders:
                order_day = (order.created_at or datetime.now(timezone.utc)).date()
                direction = order.direction or ""
                is_entry = (order.side == "buy" and direction == "long") or (order.side == "sell" and direction == "short")
                if is_entry:
                    self._cumulative_pnl -= float(order.fee or 0.0)
                    if order_day == today:
                        self._daily_realized_pnl -= float(order.fee or 0.0)
                else:
                    self._cumulative_pnl += float(order.realized_pnl or 0.0)
                    if order_day == today:
                        self._daily_realized_pnl += float(order.realized_pnl or 0.0)

        try:
            pos_a = await self._exchange.fetch_futures_position(self._coin_a)
            pos_b = await self._exchange.fetch_futures_position(self._coin_b)
        except Exception:
            logger.warning("pairs_live_restore_position_check_failed", exc_info=True)
            return

        if pos_a is None or pos_b is None:
            return

        side_a = getattr(pos_a, "side", "")
        side_b = getattr(pos_b, "side", "")
        if side_a == side_b:
            logger.warning("pairs_live_restore_side_conflict", side_a=side_a, side_b=side_b)
            return

        pair_direction = "long_a_short_b" if side_a == "long" else "short_a_long_b"
        self._position = PairPosition(
            trade_id=uuid.uuid4().hex[:12],
            pair_direction=pair_direction,
            hedge_ratio=1.0,
            qty_a=abs(float(getattr(pos_a, "amount", 0.0) or 0.0)),
            qty_b=abs(float(getattr(pos_b, "amount", 0.0) or 0.0)),
            entry_price_a=float(getattr(pos_a, "entry_price", 0.0) or 0.0),
            entry_price_b=float(getattr(pos_b, "entry_price", 0.0) or 0.0),
            margin_used=float(getattr(pos_a, "margin", 0.0) or 0.0) + float(getattr(pos_b, "margin", 0.0) or 0.0),
            entry_z=0.0,
            entered_at=datetime.now(timezone.utc),
        )
        logger.info("pairs_live_state_restored", pair_direction=pair_direction)

    async def _reconcile_exchange_state(self):
        pos_a = await self._exchange.fetch_futures_position(self._coin_a)
        pos_b = await self._exchange.fetch_futures_position(self._coin_b)
        if self._position is None:
            # 거래소에 포지션 존재해도 이 엔진이 소유하지 않으면 무시
            # (다른 R&D 엔진이 같은 계좌에서 거래 중일 수 있음)
            return
        if not await self._exchange_position_matches():
            self._paused = True
            logger.error("pairs_live_reconcile_failed", reason="local_exchange_mismatch")
            await emit_event("error", "engine", "Pairs Trading 거래소/DB 포지션 불일치. 수동 확인 전까지 정지.")

    async def _exchange_position_matches(self) -> bool:
        pos = self._position
        if pos is None:
            return False
        pos_a = await self._exchange.fetch_futures_position(self._coin_a)
        pos_b = await self._exchange.fetch_futures_position(self._coin_b)
        if pos_a is None or pos_b is None:
            return False
        if pos_a.side == pos_b.side:
            return False
        expected_side_a = "long" if pos.pair_direction == "long_a_short_b" else "short"
        expected_side_b = "short" if pos.pair_direction == "long_a_short_b" else "long"
        if pos_a.side != expected_side_a or pos_b.side != expected_side_b:
            return False
        qty_a = await self._normalize_quantity(self._coin_a, abs(float(getattr(pos_a, "amount", 0.0) or 0.0)))
        qty_b = await self._normalize_quantity(self._coin_b, abs(float(getattr(pos_b, "amount", 0.0) or 0.0)))
        if abs(qty_a - pos.qty_a) > max(qty_a, pos.qty_a, 1e-9) * 0.01:
            return False
        if abs(qty_b - pos.qty_b) > max(qty_b, pos.qty_b, 1e-9) * 0.01:
            return False
        return True

    async def _free_usdt_margin(self) -> float:
        try:
            balances = await self._exchange.fetch_balance()
        except Exception:
            logger.warning("pairs_live_fetch_balance_failed", exc_info=True)
            return 0.0
        usdt = balances.get("USDT")
        if usdt is None:
            return 0.0
        return max(0.0, float(getattr(usdt, "free", 0.0) or 0.0))

    async def _sync_rnd_coordinator_state(self, reservation_token: str | None = None):
        if self._rnd_coordinator is None:
            return
        symbols = []
        reserved_margin = 0.0
        if self._position is not None:
            symbols = [self._coin_a, self._coin_b]
            reserved_margin = self._position.margin_used
        await self._rnd_coordinator.sync_engine_state(
            self.EXCHANGE_NAME,
            symbols=symbols,
            reserved_margin=reserved_margin,
            cumulative_pnl=self._cumulative_pnl,
            daily_pnl=self._daily_realized_pnl,
            capital_limit=self._initial_capital,
            reservation_token=reservation_token,
        )

    def get_status(self) -> dict:
        position = None
        if self._position is not None:
            position = {
                "trade_id": self._position.trade_id,
                "pair_direction": self._position.pair_direction,
                "qty_a": round(self._position.qty_a, 6),
                "qty_b": round(self._position.qty_b, 6),
                "entry_price_a": round(self._position.entry_price_a, 4),
                "entry_price_b": round(self._position.entry_price_b, 4),
                "hedge_ratio": round(self._position.hedge_ratio, 4),
                "entry_z": round(self._position.entry_z, 4),
                "margin_used": round(self._position.margin_used, 2),
            }
        return {
            "exchange": self.EXCHANGE_NAME,
            "is_running": self._is_running,
            "capital_usdt": self._initial_capital,
            "leverage": self._leverage,
            "coin_a": self._coin_a,
            "coin_b": self._coin_b,
            "lookback_hours": self._lookback_hours,
            "z_entry": self._z_entry,
            "z_exit": self._z_exit,
            "z_stop": self._z_stop,
            "position": position,
            "daily_realized_pnl": round(self._daily_realized_pnl, 2),
            "cumulative_pnl": round(self._cumulative_pnl, 2),
            "available_margin": round(max(0.0, self._initial_capital + self._cumulative_pnl - (self._position.margin_used if self._position is not None else 0.0)), 2),
            "engine_conflict": self._has_engine_conflict(),
            "coordinator_enabled": self._rnd_coordinator is not None,
            "paused": self._paused,
            "daily_paused": self._daily_paused,
            "last_evaluated_at": self._last_evaluated_at.isoformat() if self._last_evaluated_at else None,
            "next_evaluation_at": self._next_evaluation_at().isoformat() if self._is_running else None,
            "recent_idle_reason": self._last_idle_reason,
        }
