"""
Tier2Scanner — 기회 포착 스캐너 (SurgeEngine 흡수).

거래량 급등 + 모멘텀 감지 → 단기 포지션.
Tier 1과 별도로 30개 코인을 스캔.

COIN-23: surge_backtest에서 안전 필터 포팅
- RSI 필터: 과매수(75) 롱 차단, 과매도(25) 숏 차단
- ATR% 필터: 0.5% 이하 횡보장 진입 차단
- 가속도(acceleration): 점수 계산에 25% 가중치
- 소진(exhaustion) 필터: 이미 8%+ 이동한 코인 차단
- 연속 SL 쿨다운: 2연속 SL → 180분 장기 쿨다운
- 정규화 점수: vol_signal*0.40 + price_signal*0.35 + accel_signal*0.25
- SL 3.5%, TP 4.5%, trail 1.5%/1.0%, max_concurrent 3, cooldown 60분
"""
import asyncio
import structlog
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.enums import Direction, Regime
from engine.safe_order_pipeline import SafeOrderPipeline, OrderRequest
from engine.position_state_tracker import PositionStateTracker, PositionState
from engine.regime_detector import RegimeDetector
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
    acceleration: float = 0.0
    atr_pct: float = 0.0


class Tier2Scanner:
    """Tier 2 코인 스캐너 — SurgeEngine 기능 통합."""

    def __init__(
        self,
        safe_order: SafeOrderPipeline,
        position_tracker: PositionStateTracker,
        exchange: ExchangeAdapter,
        portfolio_manager: PortfolioManager,
        regime_detector: RegimeDetector | None = None,
        *,
        scan_coins: list[str] | None = None,
        max_concurrent: int = 3,
        max_position_pct: float = 0.05,
        max_hold_minutes: int = 120,
        vol_threshold: float = 5.0,
        price_threshold: float = 1.5,
        sl_pct: float = 3.5,
        tp_pct: float = 4.5,
        trail_activation_pct: float = 1.5,
        trail_stop_pct: float = 1.0,
        daily_trade_limit: int = 20,
        cooldown_per_symbol_sec: int = 3600,
        leverage: int = 3,
        # COIN-23: 신규 필터 파라미터
        rsi_overbought: float = 75.0,
        rsi_oversold: float = 25.0,
        min_atr_pct: float = 0.5,
        exhaustion_pct: float = 8.0,
        min_score: float = 0.55,
        consecutive_sl_cooldown_sec: int = 10800,  # 180분
        close_lock: asyncio.Lock | None = None,
    ):
        self._safe_order = safe_order
        self._close_lock = close_lock or asyncio.Lock()
        self._positions = position_tracker
        self._exchange = exchange
        self._pm = portfolio_manager
        self._regime = regime_detector
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

        # COIN-23: 필터 파라미터
        self._rsi_overbought = rsi_overbought
        self._rsi_oversold = rsi_oversold
        self._min_atr_pct = min_atr_pct
        self._exhaustion_pct = exhaustion_pct
        self._min_score = min_score
        self._consecutive_sl_cooldown_sec = consecutive_sl_cooldown_sec

        self._cooldowns: dict[str, datetime] = {}
        self._cooldown_override_map: dict[str, datetime] = {}  # 연속 SL 장기 쿨다운
        self._consecutive_sl_count: dict[str, int] = {}  # 심볼별 연속 SL 횟수
        self._daily_trades = 0
        self._scores: list[ScanScore] = []

    @property
    def scores(self) -> list[ScanScore]:
        return list(self._scores)

    @property
    def daily_trades(self) -> int:
        return self._daily_trades

    # ─── 필터 계산 (staticmethod — 테스트 용이) ───

    @staticmethod
    def _calc_rsi(closes: list[float], period: int = 14) -> float:
        """간단 RSI 계산 (surge_backtest 포팅)."""
        if len(closes) < period + 1:
            return 50.0
        deltas = [closes[i] - closes[i - 1] for i in range(len(closes) - period, len(closes))]
        gains = sum(d for d in deltas if d > 0) / period
        losses = sum(-d for d in deltas if d < 0) / period
        if losses == 0:
            return 100.0
        rs = gains / losses
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _calc_atr_pct(candles: list, period: int = 14) -> float:
        """ATR% 계산 — 횡보장 필터 (surge_backtest 포팅)."""
        if len(candles) < period + 1:
            return 0.0
        tr_sum = 0.0
        for i in range(len(candles) - period, len(candles)):
            hi = candles[i].high
            lo = candles[i].low
            prev_c = candles[i - 1].close
            tr = max(hi - lo, abs(hi - prev_c), abs(lo - prev_c))
            tr_sum += tr
        atr = tr_sum / period
        close = candles[-1].close
        return (atr / close * 100) if close > 0 else 0.0

    @staticmethod
    def _calc_acceleration(volumes: list[float], vol_avg: float) -> float:
        """거래량 가속도: 현재 ratio vs 2캔들 전 ratio."""
        if len(volumes) < 3 or vol_avg <= 0:
            return 0.0
        ratio_now = volumes[-1] / vol_avg
        # 2캔들 전 ratio (avg에서 마지막 2개 제외하면 약간 부정확하지만 근사)
        ratio_prev = volumes[-3] / vol_avg
        return ratio_now - ratio_prev

    # ─── 스캔 사이클 ───

    async def scan_cycle(self, session: AsyncSession) -> None:
        """Tier 2 스캔 사이클."""
        # 1. 기존 포지션 exit 체크
        await self._check_exits(session)

        # 2. 레짐 체크 — RANGING에서는 신규 진입 금지 (exit만 수행)
        if self._regime is not None:
            regime_state = self._regime.current
            if regime_state and regime_state.regime == Regime.RANGING:
                logger.debug("tier2_skip_ranging", regime="RANGING")
                return

        # 3. 캔들 스캔 + 점수 계산
        self._scores = await self._scan_all()

        # 4. 진입
        tier2_count = self._positions.active_count("tier2")
        if tier2_count >= self._max_concurrent:
            return
        if self._daily_trades >= self._daily_trade_limit:
            return

        for score in self._scores:
            if tier2_count >= self._max_concurrent:
                break
            # COIN-23: min_score 임계값 (정규화 점수 기반)
            if score.score < self._min_score:
                continue
            if self._positions.has_position(score.symbol):
                continue
            if self._in_cooldown(score.symbol):
                continue
            # COIN-23: RSI 필터
            if not self._pass_rsi_filter(score):
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

        # COIN-23: ATR% 필터 — 횡보장 차단
        atr_pct = self._calc_atr_pct(candles)
        if atr_pct > 0 and atr_pct < self._min_atr_pct:
            return None

        # COIN-23: 소진(exhaustion) 필터 — 30분(6캔들) 변동 8%+ 차단
        lookback_30m = min(6, len(closes) - 1)
        if lookback_30m > 0:
            price_30m_ago = closes[-(lookback_30m + 1)]
            price_chg_30m = (price_last - price_30m_ago) / price_30m_ago * 100 if price_30m_ago > 0 else 0.0
            if abs(price_chg_30m) > self._exhaustion_pct:
                return None

        # COIN-23: RSI 계산
        rsi = self._calc_rsi(closes)

        # COIN-23: 가속도 계산
        accel = self._calc_acceleration(volumes, vol_avg)

        # COIN-23: 정규화 점수 (surge_backtest 포팅)
        vol_signal = min(vol_ratio / 10.0, 1.0)
        price_signal = min(abs(price_chg) / 5.0, 1.0)
        accel_signal = max(0, min(accel / 3.0, 1.0))

        score = (
            0.40 * vol_signal
            + 0.35 * price_signal
            + 0.25 * accel_signal
        )

        direction = Direction.LONG if price_chg > 0 else Direction.SHORT

        return ScanScore(
            symbol=symbol,
            vol_ratio=vol_ratio,
            price_chg_pct=price_chg,
            score=score,
            direction=direction,
            rsi=rsi,
            acceleration=accel,
            atr_pct=atr_pct,
        )

    def _pass_rsi_filter(self, score: ScanScore) -> bool:
        """RSI 필터: 과매수 롱 차단, 과매도 숏 차단."""
        if score.direction == Direction.LONG and score.rsi > self._rsi_overbought:
            logger.debug(
                "tier2_rsi_filter",
                symbol=score.symbol,
                direction="LONG",
                rsi=round(score.rsi, 1),
            )
            return False
        if score.direction == Direction.SHORT and score.rsi < self._rsi_oversold:
            logger.debug(
                "tier2_rsi_filter",
                symbol=score.symbol,
                direction="SHORT",
                rsi=round(score.rsi, 1),
            )
            return False
        return True

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
            confidence=min(1.0, score.score / 1.0),  # 정규화 점수 그대로 사용
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
                score=round(score.score, 3),
                rsi=round(score.rsi, 1),
                atr_pct=round(score.atr_pct, 2),
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
        """Tier 2 포지션 청산. close_lock으로 WS/eval 동시 청산 방지 (COIN-48)."""
        try:
            ticker = await self._exchange.fetch_ticker(symbol)
            price = ticker.last
        except Exception:
            return

        async with self._close_lock:
            # 락 획득 후 인메모리 상태 재확인 — WS/Tier1이 먼저 청산했을 수 있음
            if not self._positions.get(symbol):
                logger.debug("tier2_close_skipped_already_closed", symbol=symbol)
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

                # COIN-23: 연속 SL 카운트 + 장기 쿨다운
                is_sl = reason.startswith("SL:")
                if is_sl:
                    count = self._consecutive_sl_count.get(symbol, 0) + 1
                    self._consecutive_sl_count[symbol] = count
                    if count >= 2:
                        self._cooldown_override_map[symbol] = datetime.now(timezone.utc)
                        logger.warning(
                            "tier2_consecutive_sl_cooldown",
                            symbol=symbol,
                            consecutive_sl=count,
                            cooldown_min=self._consecutive_sl_cooldown_sec // 60,
                        )
                else:
                    # SL이 아닌 경우 연속 카운트 리셋
                    self._consecutive_sl_count.pop(symbol, None)

                logger.info("tier2_closed", symbol=symbol, reason=reason)

    def _in_cooldown(self, symbol: str) -> bool:
        """쿨다운 확인 (일반 + 연속 SL 장기)."""
        now = datetime.now(timezone.utc)

        # 연속 SL 장기 쿨다운 (우선)
        override = self._cooldown_override_map.get(symbol)
        if override is not None:
            elapsed = (now - override).total_seconds()
            if elapsed < self._consecutive_sl_cooldown_sec:
                return True
            else:
                # 장기 쿨다운 만료 → 정리
                del self._cooldown_override_map[symbol]
                self._consecutive_sl_count.pop(symbol, None)

        # 일반 쿨다운
        last = self._cooldowns.get(symbol)
        if last is None:
            return False
        elapsed = (now - last).total_seconds()
        return elapsed < self._cooldown_sec

    def reset_daily(self) -> None:
        """일일 카운터 리셋."""
        self._daily_trades = 0
