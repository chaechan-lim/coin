"""
RegimeDetector — 1h 캔들 기반 시장 레짐 감지.

ADX + BB Width + ATR% + Volume Ratio + EMA slope로 레짐을 분류한다.
히스테리시스 + 연속 확인 + 최소 유지 시간으로 whipsaw를 방지.
"""
import structlog
import pandas as pd
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.enums import Regime
from core.event_bus import emit_event
from services.derivatives_data import DerivativesDataService

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
    derivatives_snapshot: dict | None = field(default=None)  # 선택적 파생상품 컨텍스트


class RegimeDetector:
    """레짐 감지기 — 1h 캔들 기반.

    히스테리시스:
    - 추세 진입: ADX >= 27 (adx_enter)
    - 추세 이탈: ADX <= 23 (adx_exit)
    - 연속 확인: 2회 (confirm_count)
    - 최소 유지: 3시간 (min_duration_h)

    파생상품 보조 시그널 (derivatives_data 주입 시):
    - OI 급증: 청산 캐스케이드 위험 신호 (+0.10)
    - 프리미엄 극단: 롱/숏 과열 신호 (+0.05)
    - 롱/숏 비율 극단: 쏠림 신호 (+0.05)
    - 펀딩 비율 극단: 쏠림 확인 신호 (+0.05)
    ※ 보조 시그널은 신뢰도 조정만 — 레짐 자체는 절대 변경하지 않음
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
        derivatives_data: DerivativesDataService | None = None,
    ):
        self._adx_enter = adx_enter
        self._adx_exit = adx_exit
        self._bb_width_volatile = bb_width_volatile
        self._atr_pct_volatile = atr_pct_volatile
        self._confirm_count = confirm_count
        self._min_duration_h = min_duration_h
        self._on_regime_change = on_regime_change
        self._derivatives_data = derivatives_data

        self._current: RegimeState | None = None
        self._pending_regime: Regime | None = None
        self._pending_count: int = 0
        self._last_transition: datetime | None = None
        self._per_coin: dict[str, RegimeState] = {}
        # OI 이력: symbol → deque of OI values (최대 6개 ≈ 1시간, 10분 간격)
        self._oi_history: dict[str, deque[float]] = {}

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
            derivatives_data가 주입된 경우 derivatives_snapshot 포함.
        """
        raw = self.detect(df)

        # 파생상품 보조 시그널 적용 (derivatives_data=None이면 기존 동작과 동일)
        if self._derivatives_data is not None:
            raw = self._apply_derivatives(raw, symbol)

        # 코인별 레짐 저장
        self._per_coin[symbol] = raw

        # 히스테리시스 적용 — 변경 여부 감지 후 이벤트 발행
        prev_regime = self._current.regime if self._current else None
        confirmed = self._apply_hysteresis(raw)

        # 신선한 파생상품 컨텍스트를 반환값에 항상 오버레이
        # (히스테리시스가 이전 상태를 반환하는 경우에도 최신 snapshot 첨부)
        if raw.derivatives_snapshot is not None:
            confirmed = RegimeState(
                regime=confirmed.regime,
                confidence=confirmed.confidence,
                adx=confirmed.adx,
                bb_width=confirmed.bb_width,
                atr_pct=confirmed.atr_pct,
                volume_ratio=confirmed.volume_ratio,
                trend_direction=confirmed.trend_direction,
                timestamp=confirmed.timestamp,
                derivatives_snapshot=raw.derivatives_snapshot,
            )

        if prev_regime is not None and confirmed.regime != prev_regime:
            await emit_event(
                "info", "strategy",
                f"레짐 변경: {prev_regime.value} → {confirmed.regime.value}",
                detail=f"신뢰도={confirmed.confidence:.0%}, ADX={confirmed.adx:.1f}",
                metadata={
                    "prev_regime": prev_regime.value,
                    "new_regime": confirmed.regime.value,
                    "confidence": confirmed.confidence,
                    "adx": confirmed.adx,
                    "symbol": symbol,
                },
            )

        return confirmed

    def _apply_derivatives(self, raw: RegimeState, symbol: str) -> RegimeState:
        """파생상품 보조 시그널로 신뢰도를 조정한다.

        레짐 자체는 절대 변경하지 않음. 신뢰도만 ±0.05-0.15 범위에서 조정.
        derivatives_snapshot dict를 반환 RegimeState에 첨부.
        """
        assert self._derivatives_data is not None  # 호출 전 반드시 확인
        snap = self._derivatives_data.get_snapshot(symbol)
        if snap is None:
            return raw

        signals: list[str] = []
        volatile_signal_strength: float = 0.0

        # --- OI 이력 추적 및 변화율 계산 ---
        oi_value = snap.get("open_interest_value")
        oi_change_rate = 0.0
        if oi_value is not None:
            hist = self._oi_history.setdefault(symbol, deque(maxlen=6))
            hist.append(float(oi_value))
            if len(hist) >= 2 and hist[0] > 0:
                oi_change_rate = (float(oi_value) - hist[0]) / hist[0]

        # OI 급증 → 청산 캐스케이드 위험 → VOLATILE 신뢰도 상승
        if oi_change_rate > 0.05:
            signals.append("oi_divergence")
            volatile_signal_strength += 0.10

        # 프리미엄 극단 → 롱/숏 과열 신호
        premium_pct = float(snap.get("premium_pct") or 0.0)
        if abs(premium_pct) > 0.5:
            signals.append("premium_extreme")
            volatile_signal_strength += 0.05

        # 롱/숏 비율 극단 → 쏠림 신호
        long_ratio = float(snap.get("long_account_ratio") or 0.0)
        short_ratio = float(snap.get("short_account_ratio") or 0.0)
        if short_ratio > 0:
            ls_ratio = long_ratio / short_ratio
            if ls_ratio > 3.0 or ls_ratio < 0.33:
                signals.append("ls_ratio_extreme")
                volatile_signal_strength += 0.05

        # 펀딩 비율 극단 → 쏠림 확인
        funding_rate = float(snap.get("last_funding_rate") or 0.0)
        if abs(funding_rate) > 0.001:  # 0.1% 초과
            signals.append("funding_rate_extreme")
            volatile_signal_strength += 0.05

        # 파생상품 스냅샷 dict 구성
        derivatives_snapshot: dict = {
            "oi_change_rate": oi_change_rate,
            "premium_pct": premium_pct,
            "funding_rate": funding_rate,
            "long_ratio": long_ratio,
            "short_ratio": short_ratio,
            "signals": signals,
        }

        # 신뢰도 조정 — 레짐 자체 변경 금지, [0.0, 1.0] 클램핑
        new_confidence = raw.confidence
        if volatile_signal_strength > 0.0:
            if raw.regime == Regime.VOLATILE:
                # 변동성 레짐 확인: 신뢰도 상승
                new_confidence = min(1.0, raw.confidence + volatile_signal_strength)
            else:
                # 비변동성 레짐에서 변동성 신호: 약한 드래그 (불안정성 시사)
                new_confidence = max(0.0, raw.confidence - volatile_signal_strength * 0.5)
            logger.debug(
                "derivatives_confidence_adjusted",
                symbol=symbol,
                regime=raw.regime.value,
                signals=signals,
                before=round(raw.confidence, 3),
                after=round(new_confidence, 3),
                delta=round(new_confidence - raw.confidence, 3),
            )

        return RegimeState(
            regime=raw.regime,
            confidence=new_confidence,
            adx=raw.adx,
            bb_width=raw.bb_width,
            atr_pct=raw.atr_pct,
            volume_ratio=raw.volume_ratio,
            trend_direction=raw.trend_direction,
            timestamp=raw.timestamp,
            derivatives_snapshot=derivatives_snapshot,
        )

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
                # ADX 높지만 방향 불명 — 실제 변동성으로 재판정
                if bb_width < self._bb_width_volatile and atr_pct < self._atr_pct_volatile:
                    # 실제 변동성 낮음 → RANGING (ADX는 이전 추세의 잔여값)
                    confidence = max(0.4, min(0.6, 1.0 - adx / 50))
                    return Regime.RANGING, confidence
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
            # 신뢰도 + 파생상품 스냅샷 업데이트
            self._current = RegimeState(
                regime=self._current.regime,
                confidence=raw.confidence,
                adx=raw.adx,
                bb_width=raw.bb_width,
                atr_pct=raw.atr_pct,
                volume_ratio=raw.volume_ratio,
                trend_direction=raw.trend_direction,
                timestamp=now,
                derivatives_snapshot=raw.derivatives_snapshot,
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
