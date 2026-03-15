"""
Tier2Scanner — 기회 포착 스캐너 (SurgeEngine 흡수).

거래량 급등 + 모멘텀 감지 → 단기 포지션.
Tier 1과 별도로 30개 코인을 스캔.
"""
import structlog
import pandas as pd
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.enums import Direction
from engine.safe_order_pipeline import SafeOrderPipeline, OrderRequest
from engine.position_state_tracker import PositionStateTracker, PositionState
from engine.portfolio_manager import PortfolioManager
from exchange.base import ExchangeAdapter

logger = structlog.get_logger(__name__)


# 기본 스캔 대상 (30코인)
DEFAULT_SCAN_COINS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT",
    "NEAR/USDT", "SUI/USDT", "1000PEPE/USDT", "WIF/USDT", "ATOM/USDT",
    "FIL/USDT", "ARB/USDT", "OP/USDT", "TRX/USDT", "AAVE/USDT",
    "ETC/USDT", "APT/USDT", "IMX/USDT", "INJ/USDT", "SEI/USDT",
    "FET/USDT", "RENDER/USDT", "TIA/USDT", "JUP/USDT", "PENDLE/USDT",
]


@dataclass
class ScanScore:
    """스캔 점수."""
    symbol: str
    vol_ratio: float
    price_chg_pct: float
    score: float
    direction: Direction
    rsi: float = 50.0


class Tier2Scanner:
    """Tier 2 코인 스캐너 — SurgeEngine 기능 통합."""

    def __init__(
        self,
        safe_order: SafeOrderPipeline,
        position_tracker: PositionStateTracker,
        exchange: ExchangeAdapter,
        portfolio_manager: PortfolioManager,
        *,
        scan_coins: list[str] | None = None,
        max_concurrent: int = 5,
        max_position_pct: float = 0.05,
        max_hold_minutes: int = 120,
        vol_threshold: float = 5.0,
        price_threshold: float = 1.5,
        sl_pct: float = 2.0,
        tp_pct: float = 4.0,
        trail_activation_pct: float = 1.0,
        trail_stop_pct: float = 0.8,
        daily_trade_limit: int = 20,
        cooldown_per_symbol_sec: int = 1800,
        leverage: int = 3,
    ):
        self._safe_order = safe_order
        self._positions = position_tracker
        self._exchange = exchange
        self._pm = portfolio_manager
        self._scan_coins = scan_coins or DEFAULT_SCAN_COINS
        self._max_concurrent = max_concurrent
        self._max_position_pct = max_position_pct
        self._max_hold_minutes = max_hold_minutes
        self._vol_threshold = vol_threshold
        self._price_threshold = price_threshold
        self._sl_pct = sl_pct
        self._tp_pct = tp_pct
        self._trail_activation_pct = trail_activation_pct
        self._trail_stop_pct = trail_stop_pct
        self._daily_trade_limit = daily_trade_limit
        self._cooldown_sec = cooldown_per_symbol_sec
        self._leverage = leverage

        self._cooldowns: dict[str, datetime] = {}
        self._daily_trades = 0
        self._scores: list[ScanScore] = []

    @property
    def scores(self) -> list[ScanScore]:
        return list(self._scores)

    @property
    def daily_trades(self) -> int:
        return self._daily_trades

    async def scan_cycle(self, session: AsyncSession) -> None:
        """Tier 2 스캔 사이클."""
        # 1. 기존 포지션 exit 체크
        await self._check_exits(session)

        # 2. 캔들 스캔 + 점수 계산
        self._scores = await self._scan_all()

        # 3. 진입
        tier2_count = self._positions.active_count("tier2")
        if tier2_count >= self._max_concurrent:
            return
        if self._daily_trades >= self._daily_trade_limit:
            return

        for score in self._scores:
            if tier2_count >= self._max_concurrent:
                break
            if score.score < self._vol_threshold:
                continue
            if self._positions.has_position(score.symbol):
                continue
            if self._in_cooldown(score.symbol):
                continue

            await self._enter_position(session, score)
            tier2_count += 1

    async def _scan_all(self) -> list[ScanScore]:
        """모든 코인 스캔 + 점수 계산."""
        scores = []
        for symbol in self._scan_coins:
            try:
                score = await self._scan_symbol(symbol)
                if score:
                    scores.append(score)
            except Exception as e:
                logger.debug("tier2_scan_error", symbol=symbol, error=str(e))

        scores.sort(key=lambda s: s.score, reverse=True)
        return scores

    async def _scan_symbol(self, symbol: str) -> ScanScore | None:
        """단일 코인 스캔."""
        try:
            candles = await self._exchange.fetch_ohlcv(symbol, "5m", 60)
        except Exception:
            return None

        if not candles or len(candles) < 20:
            return None

        volumes = [c.volume for c in candles]
        closes = [c.close for c in candles]

        vol_avg = sum(volumes[:-1]) / max(len(volumes) - 1, 1)
        vol_last = volumes[-1]
        vol_ratio = vol_last / vol_avg if vol_avg > 0 else 0.0

        price_first = closes[-12] if len(closes) >= 12 else closes[0]
        price_last = closes[-1]
        price_chg = (price_last - price_first) / price_first * 100 if price_first > 0 else 0.0

        score = vol_ratio * 0.6 + abs(price_chg) * 0.4
        direction = Direction.LONG if price_chg > 0 else Direction.SHORT

        return ScanScore(
            symbol=symbol,
            vol_ratio=vol_ratio,
            price_chg_pct=price_chg,
            score=score,
            direction=direction,
        )

    async def _enter_position(self, session: AsyncSession, score: ScanScore) -> None:
        """Tier 2 포지션 진입."""
        cash = self._pm.cash_balance
        margin = cash * self._max_position_pct
        if margin < 5.0:
            return

        try:
            ticker = await self._exchange.fetch_ticker(score.symbol)
            price = ticker.last
        except Exception:
            return

        if price <= 0:
            return

        quantity = (margin * self._leverage) / price

        request = OrderRequest(
            symbol=score.symbol,
            direction=score.direction,
            action="open",
            quantity=quantity,
            price=price,
            margin=margin,
            leverage=self._leverage,
            strategy_name="tier2_surge",
            confidence=min(1.0, score.score / 10.0),
            tier="tier2",
        )

        resp = await self._safe_order.execute_order(session, request)
        if resp.success:
            state = PositionState(
                symbol=score.symbol,
                direction=score.direction,
                quantity=resp.executed_quantity,
                entry_price=resp.executed_price,
                margin=margin,
                leverage=self._leverage,
                extreme_price=resp.executed_price,
                stop_loss_atr=self._sl_pct,  # % 기반 (ATR 아닌 고정)
                take_profit_atr=self._tp_pct,
                trailing_activation_atr=self._trail_activation_pct,
                trailing_stop_atr=self._trail_stop_pct,
                tier="tier2",
                strategy_name="tier2_surge",
            )
            self._positions.open_position(state)
            self._daily_trades += 1
            self._cooldowns[score.symbol] = datetime.now(timezone.utc)
            logger.info(
                "tier2_entered",
                symbol=score.symbol,
                direction=score.direction.value,
                score=round(score.score, 2),
            )

    async def _check_exits(self, session: AsyncSession) -> None:
        """기존 Tier 2 포지션 exit 체크."""
        now = datetime.now(timezone.utc)
        to_close = []

        for symbol, state in list(self._positions.positions.items()):
            if state.tier != "tier2":
                continue

            # 시간 초과
            elapsed = (now - state.entered_at).total_seconds() / 60
            if elapsed >= self._max_hold_minutes:
                to_close.append((symbol, state, "max_hold_time"))
                continue

            # 가격 기반 SL/TP
            try:
                ticker = await self._exchange.fetch_ticker(symbol)
                price = ticker.last
            except Exception:
                continue

            if price <= 0:
                continue

            state.update_extreme(price)

            # % 기반 SL/TP (Tier 2는 ATR 아닌 고정 %)
            entry = state.entry_price
            if entry <= 0:
                continue

            if state.is_long:
                pnl_pct = (price - entry) / entry * 100
            else:
                pnl_pct = (entry - price) / entry * 100

            # 레버리지 적용: 실제 PnL = raw price change × leverage
            pnl_pct *= self._leverage

            if pnl_pct <= -self._sl_pct:
                to_close.append((symbol, state, f"SL: {pnl_pct:.1f}%"))
            elif pnl_pct >= self._tp_pct:
                to_close.append((symbol, state, f"TP: {pnl_pct:.1f}%"))

        for symbol, state, reason in to_close:
            await self._close_tier2(session, symbol, state, reason)

    async def _close_tier2(
        self, session: AsyncSession, symbol: str, state: PositionState, reason: str,
    ) -> None:
        """Tier 2 포지션 청산."""
        try:
            ticker = await self._exchange.fetch_ticker(symbol)
            price = ticker.last
        except Exception:
            return

        request = OrderRequest(
            symbol=symbol,
            direction=state.direction,
            action="close",
            quantity=state.quantity,
            price=price,
            margin=state.margin,
            leverage=self._leverage,
            strategy_name="tier2_surge",
            confidence=0.5,
            tier="tier2",
            entry_price=state.entry_price,
        )

        resp = await self._safe_order.execute_order(session, request)
        if resp.success:
            self._positions.close_position(symbol)
            logger.info("tier2_closed", symbol=symbol, reason=reason)

    def _in_cooldown(self, symbol: str) -> bool:
        last = self._cooldowns.get(symbol)
        if last is None:
            return False
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        return elapsed < self._cooldown_sec

    def reset_daily(self) -> None:
        """일일 카운터 리셋."""
        self._daily_trades = 0
