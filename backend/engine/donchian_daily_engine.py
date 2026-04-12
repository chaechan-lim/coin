"""
Donchian Daily Ensemble 라이브 엔진.

전략 (학술 검증, SSRN 2025 Zarattini):
- 일봉 + 여러 lookback (10/20/40/55) Donchian Channel 앙상블
- N일 신고가 돌파 → long 진입
- N/2일 신저가 이탈 → 청산
- ATR 2.0 stop, 1% risk per trade
- Long-only (현물)
- 약세장 방어 (백테스트 alpha +44.71% / 180d)

운영 원칙:
- 매일 1회 평가 (UTC 00:30)
- 손실 한도 -10% → 자동 중지
- 일일 손실 -5% → 그날 매매 중지
- 모든 거래 DB 기록 + Discord 알림
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from config import AppConfig
from core.enums import OrderSide, OrderType, OrderStatus
from core.event_bus import emit_event
from core.models import Order, Position
from db.session import get_session_factory
from exchange.base import ExchangeAdapter
from services.market_data import MarketDataService
from sqlalchemy import select

logger = structlog.get_logger(__name__)

# 전략 파라미터 (백테스트와 동일)
LOOKBACKS = [10, 20, 40, 55, 90]  # Donchian 앙상블 lookback
MIN_ENTRY_SIGNALS = 1              # 최소 1개 lookback 동의
MIN_EXIT_SIGNALS = 1
ATR_PERIOD = 14
ATR_STOP_MULT = 2.0
BASE_RISK_PCT = 0.01             # 거래당 1% 리스크

# 안전 한도
MAX_TOTAL_LOSS_PCT = 0.10        # 누적 -10% → 자동 중지
MAX_DAILY_LOSS_PCT = 0.05        # 일일 -5% → 그날 매매 중지
MIN_NOTIONAL_USDT = 10           # 바이낸스 최소 주문 금액

# 운영
COINS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"]
EVALUATION_HOUR_UTC = 0          # 매일 UTC 00:30 평가
EVALUATION_MINUTE_UTC = 30


@dataclass
class DonchianPosition:
    """단일 코인 포지션 상태 (메모리 캐시)."""
    symbol: str
    quantity: float
    entry_price: float
    entry_atr: float
    stop_loss_price: float
    entered_at: datetime


class DonchianDailyEngine:
    """일봉 Donchian Channel Ensemble 엔진 — long-only 현물."""

    EXCHANGE_NAME = "binance_donchian"  # binance_spot과 별개로 추적

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

        # 메모리 상태 (DB에서 복원)
        self._positions: dict[str, DonchianPosition] = {}
        self._daily_realized_pnl: float = 0.0
        self._cumulative_pnl: float = 0.0
        self._last_eval_date: Optional[datetime.date] = None
        self._last_evaluated_at: Optional[datetime] = None
        self._last_idle_reason: str = "다음 일봉 평가 대기 중"
        self._paused: bool = False  # 손실 한도 도달 시
        self._daily_paused: bool = False

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def coins(self) -> list[str]:
        return list(self._coins)

    # ── EngineRegistry 호환 메서드 (no-op for 단순 엔진) ──
    def set_engine_registry(self, registry):
        """엔진 레지스트리 주입 (호환성)."""
        pass

    def set_broadcast_callback(self, callback):
        """WebSocket 브로드캐스트 콜백 (호환성, 미사용)."""
        pass

    def set_agent_coordinator(self, coordinator):
        """에이전트 코디네이터 주입 (호환성, 미사용)."""
        pass

    @property
    def tracked_coins(self) -> list[str]:
        return list(self._coins)

    async def start(self):
        if self._is_running:
            return
        self._is_running = True
        await self._restore_state()
        self._task = asyncio.create_task(self._loop(), name="donchian_daily_loop")
        logger.info("donchian_daily_started", coins=self._coins, capital=self._initial_capital)
        await emit_event("info", "engine",
                         f"Donchian Daily 엔진 시작 (자본 {self._initial_capital} USDT)")

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
        logger.info("donchian_daily_stopped")
        await emit_event("info", "engine", "Donchian Daily 엔진 중지")

    async def _loop(self):
        """매일 UTC 00:30에 평가 실행."""
        while self._is_running:
            try:
                now = datetime.now(timezone.utc)
                # 다음 평가 시각 계산
                target = now.replace(hour=EVALUATION_HOUR_UTC, minute=EVALUATION_MINUTE_UTC,
                                     second=0, microsecond=0)
                if target <= now:
                    target = target + pd.Timedelta(days=1)
                wait_seconds = (target - now).total_seconds()
                logger.info("donchian_next_eval", at=target.isoformat(), wait_sec=int(wait_seconds))
                await asyncio.sleep(wait_seconds)

                # 평가 실행
                if self._is_running:
                    await self._evaluation_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("donchian_loop_error", error=str(e), exc_info=True)
                await asyncio.sleep(300)  # 5분 대기 후 재시도

    async def evaluate_now(self):
        """수동 평가 트리거 (테스트/디버그용)."""
        await self._evaluation_cycle()

    async def _evaluation_cycle(self):
        """일일 평가 사이클."""
        now = datetime.now(timezone.utc)
        self._last_evaluated_at = now

        # 일일 리셋
        today = now.date()
        if self._last_eval_date != today:
            self._daily_realized_pnl = 0.0
            self._daily_paused = False
            self._last_eval_date = today

        # 안전 한도 체크
        if self._paused:
            self._last_idle_reason = "누적 손실 한도 도달로 정지"
            logger.warning("donchian_paused_total_loss", pnl=self._cumulative_pnl)
            return
        if self._daily_paused:
            self._last_idle_reason = "일일 손실 한도 도달로 당일 정지"
            logger.warning("donchian_daily_paused", pnl=self._daily_realized_pnl)
            return

        logger.info("donchian_eval_start", coins=len(self._coins))

        # 1. 보유 포지션: 청산 체크
        for symbol in list(self._positions.keys()):
            try:
                await self._check_exit(symbol)
            except Exception as e:
                logger.error("donchian_exit_error", symbol=symbol, error=str(e), exc_info=True)

        # 2. 미보유 코인: 진입 체크
        for symbol in self._coins:
            if symbol in self._positions:
                continue
            try:
                await self._check_entry(symbol)
            except Exception as e:
                logger.error("donchian_entry_error", symbol=symbol, error=str(e), exc_info=True)

        logger.info("donchian_eval_complete",
                    positions=len(self._positions),
                    daily_pnl=round(self._daily_realized_pnl, 2),
                    cumulative_pnl=round(self._cumulative_pnl, 2))
        if self._paused:
            self._last_idle_reason = "누적 손실 한도 도달로 정지"
        elif self._daily_paused:
            self._last_idle_reason = "일일 손실 한도 도달로 당일 정지"
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
        """200일 일봉 + ATR/Donchian 계산 (90일 lookback + 워밍업 버퍼)."""
        try:
            df = await self._market_data.get_ohlcv_df(symbol, "1d", limit=200)
        except Exception as e:
            logger.warning("donchian_fetch_failed", symbol=symbol, error=str(e))
            return None
        if df is None or len(df) < max(LOOKBACKS) + 10:
            return None
        # ATR 계산
        high = df["high"]
        low = df["low"]
        close = df["close"]
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        df["atr_14"] = tr.rolling(ATR_PERIOD).mean()
        # Donchian (어제 기준 — 오늘 close 시점에 평가)
        for lb in LOOKBACKS:
            df[f"high_{lb}"] = df["high"].rolling(lb).max().shift(1)
            df[f"low_exit_{lb}"] = df["low"].rolling(lb // 2).min().shift(1)
        return df

    async def _build_idle_reason(self) -> str:
        closest_symbol: str | None = None
        closest_gap_pct: float | None = None
        closest_level: float | None = None

        for symbol in self._coins:
            df = await self._fetch_daily_df(symbol)
            if df is None:
                continue
            last = df.iloc[-1]
            close = float(last["close"])
            if close <= 0:
                continue
            entry_levels = [
                float(level)
                for lb in LOOKBACKS
                if pd.notna(level := last.get(f"high_{lb}"))
            ]
            if not entry_levels:
                continue
            nearest_level = min(entry_levels)
            gap_pct = ((nearest_level - close) / close) * 100
            if closest_gap_pct is None or gap_pct < closest_gap_pct:
                closest_gap_pct = gap_pct
                closest_symbol = symbol
                closest_level = nearest_level

        if closest_symbol is None or closest_gap_pct is None or closest_level is None:
            return "신규 돌파 신호 대기 중"
        return (
            f"신규 돌파 대기 중 ({closest_symbol.replace('/USDT', '')} +{closest_gap_pct:.2f}% → "
            f"{closest_level:.2f})"
        )

    async def _check_entry(self, symbol: str):
        df = await self._fetch_daily_df(symbol)
        if df is None:
            return
        last = df.iloc[-1]
        high = float(last["high"])
        atr = float(last["atr_14"])
        if pd.isna(atr) or atr <= 0:
            return

        entry_signals = 0
        for lb in LOOKBACKS:
            entry_lvl = last.get(f"high_{lb}")
            if pd.notna(entry_lvl) and high >= entry_lvl:
                entry_signals += 1

        if entry_signals < MIN_ENTRY_SIGNALS:
            return

        # 진입 가격 — 마지막 close
        price = float(last["close"])

        # ATR 기반 사이징
        cash = await self._available_cash()
        risk_amount = cash * BASE_RISK_PCT
        stop_distance = atr * ATR_STOP_MULT
        if stop_distance <= 0:
            return
        notional = (risk_amount / stop_distance) * price
        if notional > cash * 0.95:
            notional = cash * 0.95
        if notional < MIN_NOTIONAL_USDT:
            logger.info("donchian_skip_min_notional", symbol=symbol, notional=round(notional, 2))
            return

        # 시장가 매수
        try:
            quantity = notional / price
            order = await self._exchange.create_market_buy(symbol, quantity)
            if order.status not in (OrderStatus.FILLED, OrderStatus.CLOSED):
                logger.warning("donchian_buy_not_filled", symbol=symbol, status=order.status)
                return
            executed_price = float(order.executed_price or price)
            executed_qty = float(order.executed_quantity or quantity)
            stop_loss = executed_price - atr * ATR_STOP_MULT

            self._positions[symbol] = DonchianPosition(
                symbol=symbol, quantity=executed_qty, entry_price=executed_price,
                entry_atr=atr, stop_loss_price=stop_loss, entered_at=datetime.now(timezone.utc),
            )

            await self._record_order(symbol, "buy", executed_price, executed_qty,
                                      reason=f"donchian_entry: signals={entry_signals}, atr={atr:.4f}")
            sl_pct = abs(stop_loss - executed_price) / executed_price * 100
            await emit_event("info", "engine",
                             f"📈 Donchian 매수: {symbol} {executed_qty:.4f} @ {executed_price:.2f}",
                             detail=f"SL {stop_loss:.2f} (-{sl_pct:.1f}%) | 청산: N/2일 저가 이탈 | ATR {atr:.2f}")
            logger.info("donchian_entry_executed", symbol=symbol,
                        qty=executed_qty, price=executed_price, sl=stop_loss)
        except Exception as e:
            logger.error("donchian_buy_error", symbol=symbol, error=str(e), exc_info=True)

    async def _check_exit(self, symbol: str):
        pos = self._positions.get(symbol)
        if not pos:
            return
        df = await self._fetch_daily_df(symbol)
        if df is None:
            return
        last = df.iloc[-1]
        low = float(last["low"])
        close = float(last["close"])

        exit_reason = None
        exit_price = close

        # 1. SL 체크 (entry ATR 기준 stop)
        if low <= pos.stop_loss_price:
            exit_reason = "stop_loss"
            exit_price = pos.stop_loss_price

        # 2. Donchian exit (N/2일 신저가 이탈)
        if exit_reason is None:
            exit_signals = 0
            for lb in LOOKBACKS:
                exit_lvl = last.get(f"low_exit_{lb}")
                if pd.notna(exit_lvl) and low <= exit_lvl:
                    exit_signals += 1
            if exit_signals >= MIN_EXIT_SIGNALS:
                exit_reason = "donchian_exit"
                # 가장 가까운 exit level
                exit_lvls = [last.get(f"low_exit_{lb}") for lb in LOOKBACKS
                             if pd.notna(last.get(f"low_exit_{lb}"))]
                if exit_lvls:
                    exit_price = max(low, min(exit_lvls))

        if exit_reason is None:
            return

        # 시장가 매도
        try:
            order = await self._exchange.create_market_sell(symbol, pos.quantity)
            if order.status not in (OrderStatus.FILLED, OrderStatus.CLOSED):
                logger.warning("donchian_sell_not_filled", symbol=symbol, status=order.status)
                return
            actual_price = float(order.executed_price or exit_price)
            actual_qty = float(order.executed_quantity or pos.quantity)
            pnl = (actual_price - pos.entry_price) * actual_qty

            self._daily_realized_pnl += pnl
            self._cumulative_pnl += pnl
            del self._positions[symbol]

            await self._record_order(symbol, "sell", actual_price, actual_qty,
                                      pnl=pnl, reason=f"donchian_exit: {exit_reason}")
            emoji = "💰" if pnl > 0 else "💸"
            await emit_event("info", "engine",
                             f"{emoji} Donchian 매도: {symbol} {actual_qty:.4f} @ {actual_price:.2f} ({exit_reason}, PnL {pnl:+.2f})")
            logger.info("donchian_exit_executed", symbol=symbol, reason=exit_reason,
                        qty=actual_qty, price=actual_price, pnl=pnl)

            # 손실 한도 체크
            await self._check_loss_limits()
        except Exception as e:
            logger.error("donchian_sell_error", symbol=symbol, error=str(e), exc_info=True)

    async def _check_loss_limits(self):
        """누적/일일 손실 한도 체크."""
        if self._cumulative_pnl <= -self._initial_capital * MAX_TOTAL_LOSS_PCT:
            self._paused = True
            await emit_event("error", "engine",
                             f"🚨 Donchian 누적 손실 한도 도달 ({self._cumulative_pnl:.2f} USDT) — 자동 중지")
            logger.error("donchian_total_loss_limit", pnl=self._cumulative_pnl)

        if self._daily_realized_pnl <= -self._initial_capital * MAX_DAILY_LOSS_PCT:
            self._daily_paused = True
            await emit_event("warning", "engine",
                             f"⚠️ Donchian 일일 손실 한도 도달 ({self._daily_realized_pnl:.2f} USDT) — 오늘 매매 중지")
            logger.warning("donchian_daily_loss_limit", pnl=self._daily_realized_pnl)

    async def _available_cash(self) -> float:
        """진입 가능 현금 — 단순 계산: initial_capital - 보유 notional - 누적 손실."""
        invested = sum(p.quantity * p.entry_price for p in self._positions.values())
        return max(0.0, self._initial_capital + self._cumulative_pnl - invested)

    async def _record_order(
        self, symbol: str, side: str, price: float, quantity: float,
        pnl: float = 0.0, reason: str = "",
    ):
        """orders 테이블에 거래 기록."""
        sf = get_session_factory()
        async with sf() as session:
            order = Order(
                exchange=self.EXCHANGE_NAME,
                symbol=symbol,
                side=side,
                order_type="market",
                status="filled",
                executed_price=price,
                executed_quantity=quantity,
                fee=quantity * price * 0.001,  # 0.10%
                fee_currency="USDT",
                is_paper=False,
                strategy_name="donchian_daily",
                signal_confidence=1.0,
                signal_reason=reason,
                realized_pnl=pnl if side == "sell" else 0.0,
                created_at=datetime.now(timezone.utc),
                filled_at=datetime.now(timezone.utc),
            )
            session.add(order)
            await session.commit()

    async def _restore_state(self):
        """DB에서 활성 포지션 복원."""
        sf = get_session_factory()
        async with sf() as session:
            # 미청산 포지션 찾기 (donchian 거래 중 매도 안 된 매수)
            result = await session.execute(
                select(Order)
                .where(Order.exchange == self.EXCHANGE_NAME)
                .where(Order.strategy_name == "donchian_daily")
                .order_by(Order.created_at)
            )
            orders = result.scalars().all()

            # 심볼별 net position 계산
            symbol_qty: dict[str, float] = {}
            symbol_avg_price: dict[str, float] = {}
            cumulative_pnl = 0.0
            for o in orders:
                qty = float(o.executed_quantity or 0)
                price = float(o.executed_price or 0)
                if o.side == "buy":
                    cur_qty = symbol_qty.get(o.symbol, 0)
                    cur_avg = symbol_avg_price.get(o.symbol, 0)
                    new_qty = cur_qty + qty
                    if new_qty > 0:
                        symbol_avg_price[o.symbol] = (cur_qty * cur_avg + qty * price) / new_qty
                    symbol_qty[o.symbol] = new_qty
                else:  # sell
                    symbol_qty[o.symbol] = symbol_qty.get(o.symbol, 0) - qty
                    cumulative_pnl += float(o.realized_pnl or 0)

            self._cumulative_pnl = cumulative_pnl

            # 활성 포지션 복원
            for symbol, qty in symbol_qty.items():
                if qty > 0:
                    avg_price = symbol_avg_price.get(symbol, 0)
                    # ATR/SL은 새 평가에서 갱신
                    df = await self._fetch_daily_df(symbol)
                    if df is None:
                        continue
                    atr = float(df["atr_14"].iloc[-1]) if pd.notna(df["atr_14"].iloc[-1]) else 0
                    self._positions[symbol] = DonchianPosition(
                        symbol=symbol, quantity=qty, entry_price=avg_price,
                        entry_atr=atr, stop_loss_price=avg_price - atr * ATR_STOP_MULT,
                        entered_at=datetime.now(timezone.utc),  # 복원이라 정확하지 않음
                    )

            logger.info("donchian_state_restored",
                        positions=len(self._positions),
                        cumulative_pnl=round(self._cumulative_pnl, 2))

    def get_status(self) -> dict:
        return {
            "exchange": self.EXCHANGE_NAME,
            "is_running": self._is_running,
            "coins": self._coins,
            "active_positions": len(self._positions),
            "positions": [
                {
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "entry_price": p.entry_price,
                    "stop_loss": p.stop_loss_price,
                }
                for p in self._positions.values()
            ],
            "daily_pnl": round(self._daily_realized_pnl, 2),
            "cumulative_pnl": round(self._cumulative_pnl, 2),
            "paused_total_loss": self._paused,
            "paused_daily_loss": self._daily_paused,
            "initial_capital": self._initial_capital,
            "last_evaluated_at": self._last_evaluated_at.isoformat() if self._last_evaluated_at else None,
            "next_evaluation_at": self._next_evaluation_at().isoformat() if self._is_running else None,
            "recent_idle_reason": self._last_idle_reason,
        }
