"""
RegimeDetector — 1h 캔들 기반 시장 레짐 감지.

ADX + BB Width + ATR% + Volume Ratio + EMA slope로 레짐을 분류한다.
히스테리시스 + 연속 확인 + 최소 유지 시간으로 whipsaw를 방지.
"""
import structlog
import pandas as pd
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from core.enums import Regime
from core.event_bus import emit_event

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RegimeState:
    """불변 레짐 상태."""
    regime: Regime
    confidence: float       # 0.0-1.0
    adx: float
    bb_width: float
    atr_pct: float
    volume_ratio: float
    trend_direction: int    # +1, 0, -1
    timestamp: datetime


class RegimeDetector:
    """레짐 감지기 — 1h 캔들 기반.

    히스테리시스:
    - 추세 진입: ADX >= 27 (adx_enter)
    - 추세 이탈: ADX <= 23 (adx_exit)
    - 연속 확인: 2회 (confirm_count)
    - 최소 유지: 3시간 (min_duration_h)
    """

    def __init__(
        self,
        adx_enter: float = 27.0,
        adx_exit: float = 23.0,
        bb_width_volatile: float = 6.0,
        atr_pct_volatile: float = 4.0,
        confirm_count: int = 2,
        min_duration_h: int = 3,
        on_regime_change: Callable[["Regime", "Regime"], None] | None = None,
    ):
        self._adx_enter = adx_enter
        self._adx_exit = adx_exit
        self._bb_width_volatile = bb_width_volatile
        self._atr_pct_volatile = atr_pct_volatile
        self._confirm_count = confirm_count
        self._min_duration_h = min_duration_h
        self._on_regime_change = on_regime_change

        self._current: RegimeState | None = None
        self._pending_regime: Regime | None = None
        self._pending_count: int = 0
        self._last_transition: datetime | None = None
        self._per_coin: dict[str, RegimeState] = {}

    @property
    def current(self) -> RegimeState | None:
        return self._current

    @property
    def per_coin(self) -> dict[str, RegimeState]:
        return self._per_coin

    def detect(self, df: pd.DataFrame) -> RegimeState:
        """DataFrame(1h 캔들)에서 레짐을 감지한다.

        필수 컬럼: close, volume, adx_14, atr_14, ema_20, ema_50,
                   bb_upper_20, bb_lower_20, bb_mid_20
        """
        if len(df) < 50:
            return self._fallback_state()

        adx = self._safe_iloc(df, "adx_14")
        atr = self._safe_iloc(df, "atr_14")
        close = self._safe_iloc(df, "close")
        ema20 = self._safe_iloc(df, "ema_20")
        ema50 = self._safe_iloc(df, "ema_50")
        volume = self._safe_iloc(df, "volume")

        bb_upper = self._safe_iloc(df, "bb_upper_20")
        bb_lower = self._safe_iloc(df, "bb_lower_20")
        bb_mid = self._safe_iloc(df, "bb_mid_20")

        # BB Width
        bb_width = (bb_upper - bb_lower) / bb_mid * 100 if bb_mid > 0 else 0.0

        # ATR %
        atr_pct = atr / close * 100 if close > 0 else 0.0

        # Volume ratio
        vol_sma = df["volume"].rolling(20).mean().iloc[-1] if len(df) >= 20 else volume
        vol_ratio = volume / vol_sma if vol_sma > 0 else 1.0

        # EMA slope (5-bar)
        if len(df) >= 6:
            ema20_5 = self._safe_iloc(df, "ema_20", offset=5)
            ema_slope = (ema20 - ema20_5) / ema20_5 * 100 if ema20_5 > 0 else 0.0
        else:
            ema_slope = 0.0

        # EMA cross direction
        ema_cross = 1 if ema20 > ema50 else -1

        # 레짐 분류
        regime, confidence = self._classify(
            adx, bb_width, atr_pct, ema_slope, ema_cross,
        )

        return RegimeState(
            regime=regime,
            confidence=confidence,
            adx=adx,
            bb_width=bb_width,
            atr_pct=atr_pct,
            volume_ratio=vol_ratio,
            trend_direction=ema_cross,
            timestamp=datetime.now(timezone.utc),
        )

    async def update(self, df: pd.DataFrame, symbol: str = "BTC/USDT") -> RegimeState:
        """레짐 업데이트 + 히스테리시스 + DB 기록.

        Returns:
            확정된 현재 RegimeState (pending 아닌 confirmed).
        """
        raw = self.detect(df)

        # 코인별 레짐 저장
        self._per_coin[symbol] = raw

        # 히스테리시스 적용
        confirmed = self._apply_hysteresis(raw)
        return confirmed

    def _classify(
        self,
        adx: float,
        bb_width: float,
        atr_pct: float,
        ema_slope: float,
        ema_cross: int,
    ) -> tuple[Regime, float]:
        """원시 지표에서 레짐 + 신뢰도를 계산."""
        # 현재 추세 상태에 따라 히스테리시스 임계값 선택
        in_trend = (
            self._current is not None
            and self._current.regime in (Regime.TRENDING_UP, Regime.TRENDING_DOWN)
        )
        adx_threshold = self._adx_exit if in_trend else self._adx_enter

        if adx >= adx_threshold:
            # 추세 존재
            if ema_slope > 0.5 and ema_cross == 1:
                confidence = min(1.0, (adx - 20) / 30 * 0.5 + 0.5)
                return Regime.TRENDING_UP, confidence
            elif ema_slope < -0.5 and ema_cross == -1:
                confidence = min(1.0, (adx - 20) / 30 * 0.5 + 0.5)
                return Regime.TRENDING_DOWN, confidence
            else:
                return Regime.VOLATILE, 0.6
        else:
            # 비추세
            if bb_width > self._bb_width_volatile or atr_pct > self._atr_pct_volatile:
                confidence = min(1.0, bb_width / 10.0)
                return Regime.VOLATILE, confidence
            else:
                confidence = min(1.0, (25 - adx) / 15 * 0.5 + 0.5) if adx < 25 else 0.5
                return Regime.RANGING, confidence

    def _apply_hysteresis(self, raw: RegimeState) -> RegimeState:
        """히스테리시스 + 연속 확인으로 레짐 전환 안정화."""
        now = raw.timestamp

        if self._current is None:
            # 첫 감지: 즉시 확정
            self._current = raw
            self._last_transition = now
            logger.info("regime_initial", regime=raw.regime.value, confidence=raw.confidence)
            return raw

        # 같은 레짐이면 pending 리셋
        if raw.regime == self._current.regime:
            self._pending_regime = None
            self._pending_count = 0
            # 신뢰도만 업데이트
            self._current = RegimeState(
                regime=self._current.regime,
                confidence=raw.confidence,
                adx=raw.adx,
                bb_width=raw.bb_width,
                atr_pct=raw.atr_pct,
                volume_ratio=raw.volume_ratio,
                trend_direction=raw.trend_direction,
                timestamp=now,
            )
            return self._current

        # 최소 유지 시간 체크
        if self._last_transition:
            elapsed_h = (now - self._last_transition).total_seconds() / 3600
            if elapsed_h < self._min_duration_h:
                return self._current

        # 연속 확인
        if raw.regime == self._pending_regime:
            self._pending_count += 1
        else:
            self._pending_regime = raw.regime
            self._pending_count = 1

        if self._pending_count >= self._confirm_count:
            prev = self._current.regime
            self._current = raw
            self._last_transition = now
            self._pending_regime = None
            self._pending_count = 0
            logger.info(
                "regime_changed",
                prev=prev.value,
                new=raw.regime.value,
                confidence=raw.confidence,
                adx=round(raw.adx, 1),
            )
            if self._on_regime_change is not None:
                try:
                    self._on_regime_change(prev, raw.regime)
                except Exception as cb_err:
                    logger.warning("regime_change_callback_error", error=str(cb_err))
            return self._current

        # 아직 확인 중 — 기존 레짐 유지
        return self._current

    def _fallback_state(self) -> RegimeState:
        """데이터 부족 시 기본 레짐."""
        return RegimeState(
            regime=Regime.RANGING,
            confidence=0.3,
            adx=0.0,
            bb_width=0.0,
            atr_pct=0.0,
            volume_ratio=1.0,
            trend_direction=0,
            timestamp=datetime.now(timezone.utc),
        )

    @staticmethod
    def _safe_iloc(df: pd.DataFrame, col: str, offset: int = 1) -> float:
        """안전하게 마지막-offset 값을 가져온다."""
        if col not in df.columns:
            return 0.0
        val = df[col].iloc[-offset]
        if pd.isna(val):
            return 0.0
        return float(val)
