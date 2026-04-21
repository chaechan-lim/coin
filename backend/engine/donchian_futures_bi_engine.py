"""
Donchian Futures Bi-Directional 라이브 엔진.

- 일봉 Donchian 앙상블 기반
- 신고가 돌파 long, 신저가 이탈 short
- 소액 live R&D 전용
"""
from __future__ import annotations

import asyncio
import uuid
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

LOOKBACKS = [10, 20, 40, 55, 90]
MIN_ENTRY_SIGNALS = 1
MIN_EXIT_SIGNALS = 1
ATR_PERIOD = 14
ATR_STOP_MULT = 2.0
BASE_RISK_PCT = 0.01

MAX_TOTAL_LOSS_PCT = 0.10
MAX_DAILY_LOSS_PCT = 0.05
MIN_NOTIONAL_USDT = 10
COINS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
         "ATOM/USDT", "ARB/USDT", "SUI/USDT", "ADA/USDT", "AVAX/USDT"]
EVALUATION_HOUR_UTC = 0
EVALUATION_MINUTE_UTC = 35
FEE_RATE = 0.0004


@dataclass
class DonchianFuturesPosition:
    trade_id: str
    symbol: str
    direction: str  # long / short
    quantity: float
    entry_price: float
    entry_atr: float
    stop_price: float
    margin_used: float
    entered_at: datetime


class DonchianFuturesBiEngine:
    EXCHANGE_NAME = "binance_donchian_futures"

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
        self._positions: dict[str, DonchianFuturesPosition] = {}
        self._engine_registry = None
        self._rnd_coordinator = None
        self._daily_realized_pnl = 0.0
        self._cumulative_pnl = 0.0
        self._last_eval_date: Optional[datetime.date] = None
        self._last_evaluated_at: Optional[datetime] = None
        self._last_idle_reason: str = "다음 일봉 평가 대기 중"
        self._paused = False
        self._daily_paused = False
        self._consecutive_close_failures = 0

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def coins(self) -> list[str]:
        return list(self._coins)

    @property
    def tracked_coins(self) -> list[str]:
        return list(self._coins)

    def set_engine_registry(self, registry):
        self._engine_registry = registry

    def set_broadcast_callback(self, callback):
        pass

    def set_agent_coordinator(self, coordinator):
        pass

    def set_futures_rnd_coordinator(self, coordinator):
        self._rnd_coordinator = coordinator

    def _parse_reason_tags(self, reason: str | None) -> dict[str, str]:
        if not reason:
            return {}
        tags: dict[str, str] = {}
        for token in reason.split(":"):
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            tags[key] = value
        return tags

    def _build_reason(self, action: str, trade_id: str, symbol: str, direction: str) -> str:
        return (
            f"donchian_futures_bi_{action}:trade={trade_id}:symbol={symbol}:"
            f"direction={direction}:exchange={self.EXCHANGE_NAME}"
        )

    def _trade_group_type(self, action: str) -> str:
        return f"donchian_futures_{action}"

    async def _emit_trade_journal(
        self,
        trade_id: str,
        symbol: str,
        direction: str,
        stage: str,
        title: str,
        detail: str | None = None,
        level: str = "info",
        extra: dict | None = None,
    ):
        metadata = {
            "trade_id": trade_id,
            "exchange": self.EXCHANGE_NAME,
            "symbol": symbol,
            "direction": direction,
            "stage": stage,
        }
        if extra:
            metadata.update(extra)
        await emit_event(level, "donchian_futures_trade", title, detail=detail, metadata=metadata)

    async def start(self):
        if self._is_running:
            return
        self._is_running = True
        if self._rnd_coordinator is not None:
            await self._rnd_coordinator.register_engine(self.EXCHANGE_NAME, self._initial_capital)
        await self._restore_state()
        await self._reconcile_exchange_state()
        await self._sync_rnd_coordinator_state()
        for symbol in self._coins:
            try:
                await self._exchange.set_leverage(symbol, self._leverage)
            except Exception:
                logger.warning("donchian_futures_leverage_failed", symbol=symbol, leverage=self._leverage, exc_info=True)
        self._task = asyncio.create_task(self._loop(), name="donchian_futures_bi_loop")
        logger.info("donchian_futures_bi_started", coins=self._coins, capital=self._initial_capital, leverage=self._leverage)
        await emit_event("info", "engine", f"Donchian Futures Bi 엔진 시작 (자본 {self._initial_capital} USDT, {self._leverage}x)")

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
        logger.info("donchian_futures_bi_stopped")
        await emit_event("info", "engine", "Donchian Futures Bi 엔진 중지")

    async def _loop(self):
        while self._is_running:
            try:
                now = datetime.now(timezone.utc)
                target = now.replace(hour=EVALUATION_HOUR_UTC, minute=EVALUATION_MINUTE_UTC, second=0, microsecond=0)
                if target <= now:
                    target = target + pd.Timedelta(days=1)
                await asyncio.sleep((target - now).total_seconds())
                if self._is_running:
                    await self._evaluation_cycle()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("donchian_futures_bi_loop_error", exc_info=True)
                await asyncio.sleep(300)

    async def evaluate_now(self):
        await self._evaluation_cycle()

    async def _evaluation_cycle(self):
        now = datetime.now(timezone.utc)
        self._last_evaluated_at = now
        today = now.date()
        if self._last_eval_date != today:
            self._daily_realized_pnl = 0.0
            self._daily_paused = False
            self._last_eval_date = today

        if self._paused or self._daily_paused:
            self._last_idle_reason = "손실 한도 도달로 진입 정지"
            return

        logger.info("donchian_futures_bi_eval_start", coins=len(self._coins))

        for symbol in list(self._positions.keys()):
            try:
                await self._check_exit(symbol)
            except Exception:
                logger.error("donchian_futures_bi_exit_error", symbol=symbol, exc_info=True)

        await self._check_loss_limits()
        if self._paused or self._daily_paused:
            self._last_idle_reason = "손실 한도 도달로 진입 정지"
            logger.warning("donchian_futures_bi_entries_skipped_after_loss_limit")
            return

        for symbol in self._coins:
            if symbol in self._positions:
                continue
            try:
                await self._check_entry(symbol)
            except Exception:
                logger.error("donchian_futures_bi_entry_error", symbol=symbol, exc_info=True)

        await self._check_loss_limits()
        logger.info(
            "donchian_futures_bi_eval_complete",
            positions=len(self._positions),
            daily_pnl=round(self._daily_realized_pnl, 2),
            cumulative_pnl=round(self._cumulative_pnl, 2),
        )
        if self._paused or self._daily_paused:
            self._last_idle_reason = "손실 한도 도달로 진입 정지"
        elif self._positions:
            self._last_idle_reason = f"포지션 보유 중 ({len(self._positions)}개)"
        else:
            self._last_idle_reason = await self._build_idle_reason()

    def _next_evaluation_at(self) -> datetime:
        now = datetime.now(timezone.utc)
        target = now.replace(
            hour=EVALUATION_HOUR_UTC,
            minute=EVALUATION_MINUTE_UTC,
            second=0,
            microsecond=0,
        )
        if target <= now:
            target = target + pd.Timedelta(days=1)
        return target

    async def _fetch_daily_df(self, symbol: str) -> pd.DataFrame | None:
        try:
            df = await self._market_data.get_ohlcv_df(symbol, "1d", limit=200)
        except Exception:
            logger.warning("donchian_futures_bi_fetch_failed", symbol=symbol, exc_info=True)
            return None
        if df is None or len(df) < max(LOOKBACKS) + 10:
            return None
        high = df["high"]
        low = df["low"]
        close = df["close"]
        tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
        df["atr_14"] = tr.rolling(ATR_PERIOD).mean()
        for lb in LOOKBACKS:
            df[f"high_{lb}"] = df["high"].rolling(lb).max().shift(1)
            df[f"low_{lb}"] = df["low"].rolling(lb).min().shift(1)
            df[f"low_exit_{lb}"] = df["low"].rolling(lb // 2).min().shift(1)
            df[f"high_exit_{lb}"] = df["high"].rolling(lb // 2).max().shift(1)
        return df

    async def _build_idle_reason(self) -> str:
        closest_desc: str | None = None
        closest_gap_pct: float | None = None

        for symbol in self._coins:
            df = await self._fetch_daily_df(symbol)
            if df is None:
                continue
            last = df.iloc[-1]
            close = float(last["close"])
            if close <= 0:
                continue

            high_levels = [
                float(level)
                for lb in LOOKBACKS
                if pd.notna(level := last.get(f"high_{lb}"))
            ]
            low_levels = [
                float(level)
                for lb in LOOKBACKS
                if pd.notna(level := last.get(f"low_{lb}"))
            ]
            candidates: list[tuple[str, float]] = []
            if high_levels:
                nearest_high = min(high_levels)
                candidates.append((f"{symbol.replace('/USDT', '')} long +{((nearest_high - close) / close) * 100:.2f}%", ((nearest_high - close) / close) * 100))
            if low_levels:
                nearest_low = max(low_levels)
                candidates.append((f"{symbol.replace('/USDT', '')} short -{((close - nearest_low) / close) * 100:.2f}%", ((close - nearest_low) / close) * 100))

            for desc, gap_pct in candidates:
                if closest_gap_pct is None or gap_pct < closest_gap_pct:
                    closest_gap_pct = gap_pct
                    closest_desc = desc

        if closest_desc is None:
            return "양방향 돌파 신호 대기 중"
        return f"가장 가까운 양방향 돌파 대기: {closest_desc}"

    async def _check_entry(self, symbol: str):
        if self._has_engine_conflict():
            return
        df = await self._fetch_daily_df(symbol)
        if df is None:
            return
        last = df.iloc[-1]
        high = float(last["high"])
        low = float(last["low"])
        close = float(last["close"])
        atr = float(last["atr_14"]) if pd.notna(last["atr_14"]) else 0.0
        if atr <= 0:
            return

        long_signals = 0
        short_signals = 0
        for lb in LOOKBACKS:
            entry_long = last.get(f"high_{lb}")
            entry_short = last.get(f"low_{lb}")
            if pd.notna(entry_long) and high >= entry_long:
                long_signals += 1
            if pd.notna(entry_short) and low <= entry_short:
                short_signals += 1

        direction = None
        if long_signals >= MIN_ENTRY_SIGNALS and long_signals >= short_signals:
            direction = "long"
        elif short_signals >= MIN_ENTRY_SIGNALS:
            direction = "short"
        if direction is None:
            return

        if await self._has_external_position_conflict(symbol):
            return

        available_margin = await self._available_margin()
        if available_margin < MIN_NOTIONAL_USDT / max(self._leverage, 1):
            return

        risk_amount = available_margin * BASE_RISK_PCT
        stop_distance = atr * ATR_STOP_MULT
        qty = risk_amount / max(stop_distance, 1e-9)
        notional = qty * close
        max_notional = available_margin * self._leverage * 0.95
        if notional > max_notional:
            notional = max_notional
            qty = notional / close
        qty = self._normalize_qty(symbol, qty)
        notional = qty * close
        if notional < MIN_NOTIONAL_USDT or qty <= 0:
            return

        trade_id = uuid.uuid4().hex[:12]
        await self._emit_trade_journal(
            trade_id,
            symbol,
            direction,
            "entry_attempt",
            f"Donchian futures entry attempt: {symbol}",
            detail=f"direction={direction}",
            extra={"atr": round(atr, 6), "entry_signals_long": long_signals, "entry_signals_short": short_signals},
        )

        reservation_token = None
        if self._rnd_coordinator is not None:
            reserved, reason, reservation_token = await self._rnd_coordinator.request_reservation(
                self.EXCHANGE_NAME,
                self._initial_capital,
                [symbol],
                (qty * close) / max(self._leverage, 1),
            )
            if not reserved:
                logger.info("donchian_futures_bi_entry_rejected_by_coordinator", symbol=symbol, reason=reason)
                await self._emit_trade_journal(
                    trade_id,
                    symbol,
                    direction,
                    "entry_rejected",
                    f"Donchian futures entry rejected: {symbol}",
                    detail=reason,
                    level="warning",
                )
                return

        if direction == "long":
            try:
                order = await self._exchange.create_market_buy(symbol, qty)
            except Exception:
                if self._rnd_coordinator is not None:
                    await self._rnd_coordinator.release_reservation(self.EXCHANGE_NAME, reservation_token)
                    await self._sync_rnd_coordinator_state()
                await self._emit_trade_journal(
                    trade_id,
                    symbol,
                    direction,
                    "entry_failed",
                    f"Donchian futures entry failed: {symbol}",
                    detail="market buy failed",
                    level="error",
                )
                raise
            stop_price = close - atr * ATR_STOP_MULT
            side = "buy"
        else:
            try:
                order = await self._exchange.create_market_sell(symbol, qty)
            except Exception:
                if self._rnd_coordinator is not None:
                    await self._rnd_coordinator.release_reservation(self.EXCHANGE_NAME, reservation_token)
                    await self._sync_rnd_coordinator_state()
                await self._emit_trade_journal(
                    trade_id,
                    symbol,
                    direction,
                    "entry_failed",
                    f"Donchian futures entry failed: {symbol}",
                    detail="market sell failed",
                    level="error",
                )
                raise
            stop_price = close + atr * ATR_STOP_MULT
            side = "sell"

        status = getattr(order, 'status', None)
        filled_qty = float(order.filled or 0)
        exec_price = float(order.price or 0)

        if status not in ('filled', 'closed') or filled_qty <= 0 or exec_price <= 0:
            logger.error("donchian_futures_bi_entry_not_filled", symbol=symbol, direction=direction, status=status)
            if self._rnd_coordinator is not None:
                await self._rnd_coordinator.release_reservation(self.EXCHANGE_NAME, reservation_token)
                await self._sync_rnd_coordinator_state()
            await self._emit_trade_journal(
                trade_id, symbol, direction, "entry_not_filled",
                f"Donchian futures entry not filled: {symbol}",
                detail=f"status={status}", level="error",
            )
            return

        fee = float(getattr(order, 'fee', None) or (exec_price * filled_qty * FEE_RATE))
        margin_used = exec_price * filled_qty / max(self._leverage, 1)
        self._positions[symbol] = DonchianFuturesPosition(
            trade_id=trade_id,
            symbol=symbol,
            direction=direction,
            quantity=filled_qty,
            entry_price=exec_price,
            entry_atr=atr,
            stop_price=stop_price,
            margin_used=margin_used,
            entered_at=datetime.now(timezone.utc),
        )
        self._cumulative_pnl -= fee
        self._daily_realized_pnl -= fee
        if self._rnd_coordinator is not None:
            await self._rnd_coordinator.note_pnl(self.EXCHANGE_NAME, -fee)
            await self._sync_rnd_coordinator_state(reservation_token=reservation_token)
        await self._record_order(
            symbol,
            side,
            direction,
            exec_price,
            filled_qty,
            margin_used,
            0.0,
            0.0,
            exec_price,
            self._build_reason("entry", trade_id, symbol, direction)
            + f":entry_atr={atr:.10f}:stop_price={stop_price:.10f}",
            trade_group_id=trade_id,
            trade_group_type=self._trade_group_type("entry"),
        )
        await self._emit_trade_journal(
            trade_id,
            symbol,
            direction,
            "entry_opened",
            f"Donchian futures entry opened: {symbol}",
            detail=f"direction={direction} qty={round(filled_qty, 6)}",
            extra={"entry_price": round(exec_price, 6), "stop_price": round(stop_price, 6)},
        )
        icon = "📈" if direction == "long" else "📉"
        sl_pct = abs(stop_price - exec_price) / exec_price * 100
        await emit_event("info", "engine",
                         f"{icon} DonchianF {direction}: {symbol} @ {exec_price:.2f}",
                         detail=f"SL {stop_price:.2f} (-{sl_pct:.1f}%) | 수량 {filled_qty:.6f} | 청산: N/2일 고저 회복")

    async def _check_exit(self, symbol: str):
        pos = self._positions.get(symbol)
        if pos is None:
            return
        df = await self._fetch_daily_df(symbol)
        if df is None:
            return
        last = df.iloc[-1]
        high = float(last["high"])
        low = float(last["low"])
        close = float(last["close"])
        should_exit = False

        if pos.direction == "long":
            exit_signals = sum(1 for lb in LOOKBACKS if pd.notna(last.get(f"low_exit_{lb}")) and low <= float(last.get(f"low_exit_{lb}")))
            should_exit = exit_signals >= MIN_EXIT_SIGNALS or low <= pos.stop_price
            close_side = "sell"
            pnl = (close - pos.entry_price) * pos.quantity
            pnl_pct = ((close - pos.entry_price) / pos.entry_price * self._leverage * 100) if pos.entry_price > 0 else 0.0
        else:
            exit_signals = sum(1 for lb in LOOKBACKS if pd.notna(last.get(f"high_exit_{lb}")) and high >= float(last.get(f"high_exit_{lb}")))
            should_exit = exit_signals >= MIN_EXIT_SIGNALS or high >= pos.stop_price
            close_side = "buy"
            pnl = (pos.entry_price - close) * pos.quantity
            pnl_pct = ((pos.entry_price - close) / pos.entry_price * self._leverage * 100) if pos.entry_price > 0 else 0.0

        if not should_exit:
            return

        if not await self._exchange_position_matches(symbol, pos):
            self._paused = True
            logger.error("donchian_futures_bi_exit_blocked_mismatch", symbol=symbol)
            await emit_event("error", "engine", f"Donchian Futures Bi 포지션 불일치 감지: {symbol}. 수동 확인 전까지 일시정지.")
            await self._emit_trade_journal(
                pos.trade_id,
                symbol,
                pos.direction,
                "exit_blocked",
                f"Donchian futures exit blocked: {symbol}",
                detail="exchange position mismatch",
                level="error",
            )
            return

        await self._emit_trade_journal(
            pos.trade_id,
            symbol,
            pos.direction,
            "exit_attempt",
            f"Donchian futures exit attempt: {symbol}",
            detail=f"direction={pos.direction}",
            extra={"stop_price": round(pos.stop_price, 6)},
        )
        order = await (self._exchange.create_market_sell(symbol, pos.quantity, reduce_only=True) if close_side == "sell" else self._exchange.create_market_buy(symbol, pos.quantity, reduce_only=True))

        exit_status = getattr(order, 'status', None)
        exit_filled = float(order.filled or 0)
        exec_price = float(order.price or 0)

        if exit_status not in ('filled', 'closed') or exit_filled <= 0 or exec_price <= 0:
            self._consecutive_close_failures += 1
            logger.error("donchian_futures_bi_exit_not_filled", symbol=symbol, direction=pos.direction,
                         status=exit_status, consecutive=self._consecutive_close_failures)
            if self._consecutive_close_failures >= 3:
                self._paused = True
                await emit_event("error", "engine",
                                 f"🚨 Donchian Futures Bi 청산 {self._consecutive_close_failures}회 연속 실패 — 자동 중지",
                                 detail=f"포지션 {pos.direction} {symbol} qty={pos.quantity} 수동 확인 필요")
            await self._emit_trade_journal(
                pos.trade_id, symbol, pos.direction, "exit_not_filled",
                f"Donchian futures exit not filled: {symbol}",
                detail=f"status={exit_status}", level="error",
            )
            return

        self._consecutive_close_failures = 0

        fee = float(getattr(order, 'fee', None) or (exec_price * exit_filled * FEE_RATE))
        if pos.direction == "long":
            pnl = (exec_price - pos.entry_price) * exit_filled
            pnl_pct = ((exec_price - pos.entry_price) / pos.entry_price * 100) if pos.entry_price > 0 else 0.0
        else:
            pnl = (pos.entry_price - exec_price) * exit_filled
            pnl_pct = ((pos.entry_price - exec_price) / pos.entry_price * 100) if pos.entry_price > 0 else 0.0

        realized = pnl - fee
        self._daily_realized_pnl += realized
        self._cumulative_pnl += realized
        await self._record_order(
            symbol,
            close_side,
            pos.direction,
            exec_price,
            exit_filled,
            pos.margin_used,
            realized,
            pnl_pct,
            pos.entry_price,
            self._build_reason("exit", pos.trade_id, symbol, pos.direction),
            trade_group_id=pos.trade_id,
            trade_group_type=self._trade_group_type("exit"),
        )
        await self._emit_trade_journal(
            pos.trade_id,
            symbol,
            pos.direction,
            "exit_closed",
            f"Donchian futures exit closed: {symbol}",
            detail=f"realized={round(realized, 4)} USDT",
            extra={"exit_price": round(exec_price, 6), "realized_pnl": round(realized, 6), "pnl_pct": round(pnl_pct, 6)},
        )
        del self._positions[symbol]
        if self._rnd_coordinator is not None:
            await self._rnd_coordinator.note_pnl(self.EXCHANGE_NAME, realized)
            await self._sync_rnd_coordinator_state()

    async def _available_margin(self) -> float:
        used = sum(p.margin_used for p in self._positions.values())
        budget_margin = max(0.0, self._initial_capital + self._cumulative_pnl - used)
        free_margin = await self._free_usdt_margin()
        return max(0.0, min(budget_margin, free_margin))

    async def _check_loss_limits(self):
        if self._cumulative_pnl <= -self._initial_capital * MAX_TOTAL_LOSS_PCT:
            self._paused = True
            await emit_event("error", "engine", f"🚨 Donchian Futures Bi 누적 손실 한도 도달 ({self._cumulative_pnl:.2f} USDT)")
        if self._daily_realized_pnl <= -self._initial_capital * MAX_DAILY_LOSS_PCT:
            self._daily_paused = True
            await emit_event("warning", "engine", f"⚠️ Donchian Futures Bi 일일 손실 한도 도달 ({self._daily_realized_pnl:.2f} USDT)")

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
            order = Order(
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
                realized_pnl=pnl if ((side == "sell" and direction == "long") or (side == "buy" and direction == "short")) else 0.0,
                realized_pnl_pct=pnl_pct if ((side == "sell" and direction == "long") or (side == "buy" and direction == "short")) else None,
                entry_price=entry_price,
                strategy_name="donchian_futures_bi",
                signal_confidence=1.0,
                signal_reason=reason,
                trade_group_id=trade_group_id,
                trade_group_type=trade_group_type,
                created_at=datetime.now(timezone.utc),
                filled_at=datetime.now(timezone.utc),
            )
            session.add(order)
            await session.commit()

    async def _restore_state(self):
        sf = get_session_factory()
        today = datetime.now(timezone.utc).date()
        async with sf() as session:
            result = await session.execute(
                select(Order)
                .where(Order.exchange == self.EXCHANGE_NAME)
                .where(Order.strategy_name == "donchian_futures_bi")
                .order_by(Order.created_at)
            )
            orders = result.scalars().all()

            open_positions: dict[str, DonchianFuturesPosition] = {}
            cumulative_pnl = 0.0
            daily_pnl = 0.0
            for o in orders:
                direction = o.direction or "long"
                qty = float(o.executed_quantity or 0.0)
                price = float(o.executed_price or 0.0)
                margin_used = float(o.margin_used or (price * qty / max(self._leverage, 1)))
                order_day = (o.created_at or datetime.now(timezone.utc)).date()
                is_open = (o.side == "buy" and direction == "long") or (o.side == "sell" and direction == "short")
                if is_open:
                    tags = self._parse_reason_tags(o.signal_reason)
                    open_positions[o.symbol] = DonchianFuturesPosition(
                        trade_id=o.trade_group_id or uuid.uuid4().hex[:12],
                        symbol=o.symbol,
                        direction=direction,
                        quantity=qty,
                        entry_price=price,
                        entry_atr=float(tags.get("entry_atr", 0.0) or 0.0),
                        stop_price=float(tags.get("stop_price", price) or price),
                        margin_used=margin_used,
                        entered_at=o.created_at or datetime.now(timezone.utc),
                    )
                    cumulative_pnl -= float(o.fee or 0.0)
                    if order_day == today:
                        daily_pnl -= float(o.fee or 0.0)
                else:
                    open_positions.pop(o.symbol, None)
                    cumulative_pnl += float(o.realized_pnl or 0.0)
                    if order_day == today:
                        daily_pnl += float(o.realized_pnl or 0.0)

            self._cumulative_pnl = cumulative_pnl
            self._daily_realized_pnl = daily_pnl
            self._last_eval_date = today
            for symbol, pos in open_positions.items():
                atr = pos.entry_atr
                stop_price = pos.stop_price
                if atr <= 0 or stop_price <= 0:
                    df = await self._fetch_daily_df(symbol)
                    atr = float(df["atr_14"].iloc[-1]) if df is not None and pd.notna(df["atr_14"].iloc[-1]) else 0.0
                    stop_price = pos.entry_price - atr * ATR_STOP_MULT if pos.direction == "long" else pos.entry_price + atr * ATR_STOP_MULT
                self._positions[symbol] = DonchianFuturesPosition(
                    trade_id=pos.trade_id,
                    symbol=symbol,
                    direction=pos.direction,
                    quantity=pos.quantity,
                    entry_price=pos.entry_price,
                    entry_atr=atr,
                    stop_price=stop_price,
                    margin_used=pos.margin_used,
                    entered_at=pos.entered_at,
                )

    async def _reconcile_exchange_state(self):
        mismatches: list[str] = []
        for symbol in self._coins:
            exchange_pos = await self._exchange.fetch_futures_position(symbol)
            local_pos = self._positions.get(symbol)
            if exchange_pos is None and local_pos is None:
                continue
            # 거래소에 포지션 있지만 이 엔진이 소유 안 함 → 다른 엔진 포지션이므로 무시
            if exchange_pos is not None and local_pos is None:
                continue
            # 이 엔진이 소유하지만 거래소에 없음 → 진짜 불일치
            if exchange_pos is None and local_pos is not None:
                mismatches.append(symbol)
                continue
            exchange_side = "long" if exchange_pos.side == "long" else "short"
            exchange_qty = self._normalize_qty(symbol, float(exchange_pos.amount))
            local_qty = self._normalize_qty(symbol, float(local_pos.quantity))
            if exchange_side != local_pos.direction or abs(exchange_qty - local_qty) > max(exchange_qty, local_qty, 1e-9) * 0.01:
                mismatches.append(symbol)
        if mismatches:
            self._paused = True
            logger.error("donchian_futures_bi_reconcile_failed", symbols=mismatches)
            await emit_event("error", "engine", f"Donchian Futures Bi 거래소/DB 포지션 불일치: {', '.join(mismatches)}. 수동 확인 전까지 정지.")

    async def _exchange_position_matches(self, symbol: str, pos: DonchianFuturesPosition) -> bool:
        exchange_pos = await self._exchange.fetch_futures_position(symbol)
        if exchange_pos is None:
            return False
        exchange_side = "long" if exchange_pos.side == "long" else "short"
        exchange_qty = self._normalize_qty(symbol, float(exchange_pos.amount))
        local_qty = self._normalize_qty(symbol, float(pos.quantity))
        if exchange_side != pos.direction:
            return False
        return abs(exchange_qty - local_qty) <= max(exchange_qty, local_qty, 1e-9) * 0.01

    async def _free_usdt_margin(self) -> float:
        try:
            balances = await self._exchange.fetch_balance()
        except Exception:
            logger.warning("donchian_futures_bi_fetch_balance_failed", exc_info=True)
            return 0.0
        usdt = balances.get("USDT")
        if usdt is None:
            return 0.0
        return max(0.0, float(getattr(usdt, "free", 0.0) or 0.0))

    def _normalize_qty(self, symbol: str, quantity: float) -> float:
        try:
            return float(self._exchange.amount_to_precision(symbol, quantity))
        except Exception:
            return float(quantity)

    def get_status(self) -> dict:
        return {
            "exchange": self.EXCHANGE_NAME,
            "is_running": self._is_running,
            "leverage": self._leverage,
            "capital_usdt": self._initial_capital,
            "tracked_coins": self._coins,
            "positions": [
                {
                    "symbol": p.symbol,
                    "direction": p.direction,
                    "trade_id": p.trade_id,
                    "quantity": round(p.quantity, 6),
                    "entry_price": round(p.entry_price, 4),
                    "margin_used": round(p.margin_used, 2),
                    "stop_price": round(p.stop_price, 4),
                }
                for p in self._positions.values()
            ],
            "daily_realized_pnl": round(self._daily_realized_pnl, 2),
            "cumulative_pnl": round(self._cumulative_pnl, 2),
            "available_margin": round(max(0.0, self._initial_capital + self._cumulative_pnl - sum(p.margin_used for p in self._positions.values())), 2),
            "engine_conflict": self._has_engine_conflict(),
            "coordinator_enabled": self._rnd_coordinator is not None,
            "paused": self._paused,
            "daily_paused": self._daily_paused,
            "last_evaluated_at": self._last_evaluated_at.isoformat() if self._last_evaluated_at else None,
            "next_evaluation_at": self._next_evaluation_at().isoformat() if self._is_running else None,
            "recent_idle_reason": self._last_idle_reason,
        }

    async def _has_external_position_conflict(self, symbol: str) -> bool:
        try:
            pos = await self._exchange.fetch_futures_position(symbol)
        except Exception:
            logger.warning("donchian_futures_bi_position_check_failed", symbol=symbol, exc_info=True)
            return True
        if pos is None:
            return False
        contracts = abs(float(getattr(pos, "contracts", 0.0) or 0.0))
        if contracts <= 0:
            return False
        logger.warning("donchian_futures_bi_external_conflict", symbol=symbol, contracts=contracts)
        return True

    def _has_engine_conflict(self) -> bool:
        if self._engine_registry is None:
            return False
        for name in ("binance_futures", "binance_surge"):
            eng = self._engine_registry.get_engine(name)
            if eng is not None and getattr(eng, "is_running", False):
                return True
        return False

    async def _sync_rnd_coordinator_state(self, reservation_token: str | None = None):
        if self._rnd_coordinator is None:
            return
        await self._rnd_coordinator.sync_engine_state(
            self.EXCHANGE_NAME,
            symbols=list(self._positions.keys()),
            reserved_margin=sum(p.margin_used for p in self._positions.values()),
            cumulative_pnl=self._cumulative_pnl,
            daily_pnl=self._daily_realized_pnl,
            capital_limit=self._initial_capital,
            reservation_token=reservation_token,
        )
