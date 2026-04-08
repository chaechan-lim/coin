"""RegimeDetector 테스트."""
import pytest
import pandas as pd
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from engine.regime_detector import RegimeDetector, RegimeState
from engine.futures_engine_v2 import FuturesEngineV2
from core.enums import Regime
from config import AppConfig
from exchange.data_models import Balance


def _make_df(
    n=100,
    close=80000.0,
    adx=30.0,
    atr=1000.0,
    ema_20=80000.0,
    ema_50=79000.0,
    bb_upper=82000.0,
    bb_lower=78000.0,
    bb_mid=80000.0,
    volume=1000.0,
    ema_slope_dir=1,
) -> pd.DataFrame:
    """테스트용 DataFrame 생성.

    ema_slope_dir: +1이면 상승 slope(~1%/5bar), -1이면 하락, 0이면 flat.
    """
    # EMA20에 충분한 기울기 부여 (5bar당 ~1% 변동 → slope > 0.5)
    ema_values = []
    for i in range(n):
        pct = ema_slope_dir * 0.002 * (i - (n - 1))
        ema_values.append(ema_20 * (1 + pct))
    data = {
        "close": [close] * n,
        "adx_14": [adx] * n,
        "atr_14": [atr] * n,
        "ema_20": ema_values,
        "ema_50": [ema_50] * n,
        "bb_upper_20": [bb_upper] * n,
        "bb_lower_20": [bb_lower] * n,
        "bb_mid_20": [bb_mid] * n,
        "volume": [volume] * n,
    }
    return pd.DataFrame(data)


class TestDetect:
    def test_trending_up(self):
        detector = RegimeDetector()
        df = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state = detector.detect(df)
        assert state.regime == Regime.TRENDING_UP
        assert state.confidence > 0.5

    def test_trending_down(self):
        detector = RegimeDetector()
        df = _make_df(close=77000, adx=30, ema_20=78000, ema_50=80000, ema_slope_dir=-1)
        state = detector.detect(df)
        assert state.regime == Regime.TRENDING_DOWN

    def test_ranging(self):
        detector = RegimeDetector()
        df = _make_df(adx=15, bb_upper=81000, bb_lower=79000, bb_mid=80000)
        state = detector.detect(df)
        assert state.regime == Regime.RANGING

    def test_volatile_high_bb_width(self):
        detector = RegimeDetector()
        df = _make_df(adx=18, bb_upper=90000, bb_lower=70000, bb_mid=80000)
        state = detector.detect(df)
        assert state.regime == Regime.VOLATILE

    def test_volatile_high_atr(self):
        detector = RegimeDetector()
        df = _make_df(adx=18, atr=4000, close=80000)
        state = detector.detect(df)
        assert state.regime == Regime.VOLATILE

    def test_insufficient_data_fallback(self):
        detector = RegimeDetector()
        df = _make_df(n=10)
        state = detector.detect(df)
        assert state.regime == Regime.RANGING
        assert state.confidence == 0.3


class TestHysteresis:
    def test_first_detection_immediate(self):
        detector = RegimeDetector()
        df = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)

        state = detector.detect(df)
        confirmed = detector._apply_hysteresis(state)
        assert confirmed.regime == Regime.TRENDING_UP
        assert detector.current.regime == Regime.TRENDING_UP

    def test_same_regime_resets_pending(self):
        detector = RegimeDetector()
        df_up = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)

        # 초기화
        state1 = detector.detect(df_up)
        detector._apply_hysteresis(state1)

        # 같은 레짐 반복
        state2 = detector.detect(df_up)
        detector._apply_hysteresis(state2)

        assert detector._pending_regime is None
        assert detector._pending_count == 0

    def test_regime_change_needs_confirmation(self):
        detector = RegimeDetector(confirm_count=2, min_duration_h=0)
        df_up = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        df_range = _make_df(adx=15, bb_upper=81000, bb_lower=79000, bb_mid=80000)

        # 초기: 상승 추세
        state1 = detector.detect(df_up)
        detector._apply_hysteresis(state1)
        assert detector.current.regime == Regime.TRENDING_UP

        # 1회 횡보 감지 — 아직 변경 안 됨
        state2 = detector.detect(df_range)
        result = detector._apply_hysteresis(state2)
        assert result.regime == Regime.TRENDING_UP  # 아직 유지
        assert detector._pending_count == 1

        # 2회 연속 횡보 감지 → 변경
        state3 = detector.detect(df_range)
        result = detector._apply_hysteresis(state3)
        assert result.regime == Regime.RANGING

    def test_min_duration_prevents_change(self):
        detector = RegimeDetector(confirm_count=1, min_duration_h=3)
        df_up = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        df_range = _make_df(adx=15, bb_upper=81000, bb_lower=79000, bb_mid=80000)

        # 초기화
        state1 = detector.detect(df_up)
        detector._apply_hysteresis(state1)

        # 즉시 변경 시도 — min_duration에 의해 차단
        state2 = detector.detect(df_range)
        result = detector._apply_hysteresis(state2)
        assert result.regime == Regime.TRENDING_UP  # 변경 안 됨

    def test_adx_hysteresis_entry(self):
        """추세 진입: ADX >= 27 필요 (adx_enter)."""
        detector = RegimeDetector(adx_enter=27, adx_exit=23)
        df = _make_df(close=82000, adx=26, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state = detector.detect(df)
        # ADX 26 < 27 → 추세 아님
        assert state.regime != Regime.TRENDING_UP

    def test_adx_hysteresis_exit(self):
        """추세 이탈: ADX <= 23 필요 (adx_exit)."""
        detector = RegimeDetector(adx_enter=27, adx_exit=23, confirm_count=1, min_duration_h=0)

        # 먼저 추세 상태로 설정
        df_up = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state = detector.detect(df_up)
        detector._apply_hysteresis(state)

        # ADX 24 — 추세 유지 (23 이하가 아님)
        df_mid = _make_df(close=82000, adx=24, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state2 = detector.detect(df_mid)
        # in_trend 상태이므로 adx_exit(23) 적용 → 24 >= 23 → 여전히 추세
        assert state2.regime == Regime.TRENDING_UP


class TestPerCoin:
    @pytest.mark.asyncio
    async def test_per_coin_storage(self):
        detector = RegimeDetector()
        df_btc = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        df_eth = _make_df(adx=15, bb_upper=81000, bb_lower=79000, bb_mid=80000)

        await detector.update(df_btc, "BTC/USDT")
        await detector.update(df_eth, "ETH/USDT")

        assert "BTC/USDT" in detector.per_coin
        assert "ETH/USDT" in detector.per_coin
        assert detector.per_coin["BTC/USDT"].regime == Regime.TRENDING_UP
        assert detector.per_coin["ETH/USDT"].regime == Regime.RANGING


class TestVolatileRanging:
    """ADX 높지만 실제 변동성 낮을 때 RANGING 재분류 테스트."""

    def test_high_adx_low_vol_becomes_ranging(self):
        """ADX>=27 + flat EMA slope + 낮은 BB/ATR → RANGING."""
        detector = RegimeDetector()
        # ADX 33 (높음), BB width 1% (낮음), ATR 0.3% (낮음), slope flat
        df = _make_df(
            adx=33, ema_20=80000, ema_50=79500,
            bb_upper=80400, bb_lower=79600, bb_mid=80000,
            atr=240, close=80000, ema_slope_dir=0,
        )
        state = detector.detect(df)
        assert state.regime == Regime.RANGING
        assert 0.4 <= state.confidence <= 0.6

    def test_high_adx_high_vol_stays_volatile(self):
        """ADX>=27 + flat slope + 높은 BB/ATR → VOLATILE 유지."""
        detector = RegimeDetector()
        # ADX 33, BB width 12.5% (높음), slope flat
        df = _make_df(
            adx=33, ema_20=80000, ema_50=79500,
            bb_upper=85000, bb_lower=75000, bb_mid=80000,
            atr=4000, close=80000, ema_slope_dir=0,
        )
        state = detector.detect(df)
        assert state.regime == Regime.VOLATILE

    def test_high_adx_with_trend_not_affected(self):
        """ADX 높고 EMA slope 명확 → 기존 TRENDING 분류 영향 없음."""
        detector = RegimeDetector()
        df = _make_df(
            adx=33, ema_20=81000, ema_50=79000,
            bb_upper=80400, bb_lower=79600, bb_mid=80000,
            atr=240, close=82000, ema_slope_dir=1,
        )
        state = detector.detect(df)
        assert state.regime == Regime.TRENDING_UP

    def test_ranging_reclassification_confidence_scales_with_adx(self):
        """RANGING 재분류 시 신뢰도가 ADX 반비례."""
        detector = RegimeDetector()
        # ADX 28 → confidence closer to 0.6
        df_low = _make_df(
            adx=28, ema_20=80000, ema_50=79500,
            bb_upper=80400, bb_lower=79600, bb_mid=80000,
            atr=240, close=80000, ema_slope_dir=0,
        )
        # ADX 38 → confidence closer to 0.4
        df_high = _make_df(
            adx=38, ema_20=80000, ema_50=79500,
            bb_upper=80400, bb_lower=79600, bb_mid=80000,
            atr=240, close=80000, ema_slope_dir=0,
        )
        state_low = detector.detect(df_low)
        state_high = RegimeDetector().detect(df_high)  # fresh detector
        assert state_low.confidence > state_high.confidence


class TestSafeIloc:
    def test_missing_column(self):
        df = pd.DataFrame({"close": [100.0]})
        assert RegimeDetector._safe_iloc(df, "nonexistent") == 0.0

    def test_nan_value(self):
        df = pd.DataFrame({"col": [float("nan")]})
        assert RegimeDetector._safe_iloc(df, "col") == 0.0

    def test_valid_value(self):
        df = pd.DataFrame({"col": [1.0, 2.0, 3.0]})
        assert RegimeDetector._safe_iloc(df, "col") == 3.0

    def test_offset(self):
        df = pd.DataFrame({"col": [1.0, 2.0, 3.0, 4.0, 5.0]})
        assert RegimeDetector._safe_iloc(df, "col", offset=3) == 3.0


# ── COIN-54: emit_event 레짐 변경 알림 테스트 ─────────────────────

class TestRegimeChangeEmit:
    @pytest.mark.asyncio
    async def test_update_emits_on_regime_change(self):
        """레짐 변경 시 emit_event('info', 'strategy', ...) 호출됨."""
        detector = RegimeDetector(confirm_count=1, min_duration_h=0)

        # 초기 레짐 설정 (ranging)
        df_range = _make_df(adx=15, bb_upper=81000, bb_lower=79000, bb_mid=80000)
        await detector.update(df_range, "BTC/USDT")
        assert detector.current.regime == Regime.RANGING

        # 레짐 변경 (trending_up)
        df_up = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        with patch("engine.regime_detector.emit_event", new_callable=AsyncMock) as mock_emit:
            await detector.update(df_up, "BTC/USDT")
            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args
            # positional args: level, category, title
            assert call_kwargs[0][0] == "info"
            assert call_kwargs[0][1] == "strategy"
            assert "레짐 변경" in call_kwargs[0][2]
            assert "ranging" in call_kwargs[0][2]
            assert "trending_up" in call_kwargs[0][2]

    @pytest.mark.asyncio
    async def test_update_no_emit_on_same_regime(self):
        """같은 레짐 유지 시 emit_event 호출 안 됨."""
        detector = RegimeDetector(confirm_count=1, min_duration_h=0)
        df_up = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)

        await detector.update(df_up, "BTC/USDT")  # 초기 설정

        with patch("engine.regime_detector.emit_event", new_callable=AsyncMock) as mock_emit:
            await detector.update(df_up, "BTC/USDT")  # 같은 레짐
            mock_emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_no_emit_on_first_detection(self):
        """첫 레짐 감지(prev_regime=None)는 emit 안 됨."""
        detector = RegimeDetector(confirm_count=1, min_duration_h=0)
        df_up = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)

        with patch("engine.regime_detector.emit_event", new_callable=AsyncMock) as mock_emit:
            await detector.update(df_up, "BTC/USDT")  # 최초 감지
            mock_emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_emit_metadata_contains_required_fields(self):
        """emit_event 메타데이터에 prev_regime, new_regime, confidence, adx, symbol 포함."""
        detector = RegimeDetector(confirm_count=1, min_duration_h=0)

        df_range = _make_df(adx=15, bb_upper=81000, bb_lower=79000, bb_mid=80000)
        await detector.update(df_range, "ETH/USDT")

        df_up = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        with patch("engine.regime_detector.emit_event", new_callable=AsyncMock) as mock_emit:
            await detector.update(df_up, "ETH/USDT")
            meta = mock_emit.call_args[1]["metadata"]
            assert "prev_regime" in meta
            assert "new_regime" in meta
            assert "confidence" in meta
            assert "adx" in meta
            assert meta["symbol"] == "ETH/USDT"


# ── COIN-99: Derivatives data injection 테스트 ─────────────────────

def _make_derivatives_mock(snapshot: dict | None) -> MagicMock:
    """get_snapshot()이 고정값을 반환하는 DerivativesDataService mock."""
    mock = MagicMock()
    mock.get_snapshot.return_value = snapshot
    return mock


class TestDerivativesDataNone:
    """derivatives_data=None → 기존 동작 완전 동일 (회귀 방지)."""

    def test_detect_no_snapshot_field(self):
        """derivatives_data=None이면 detect() 반환 RegimeState.derivatives_snapshot=None."""
        detector = RegimeDetector()
        df = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state = detector.detect(df)
        assert state.derivatives_snapshot is None

    @pytest.mark.asyncio
    async def test_update_returns_no_snapshot(self):
        """derivatives_data=None이면 update() 반환 상태에 snapshot 없음."""
        detector = RegimeDetector()
        df = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state = await detector.update(df, "BTC/USDT")
        assert state.derivatives_snapshot is None

    @pytest.mark.asyncio
    async def test_confidence_unchanged_without_derivatives(self):
        """derivatives_data=None이면 신뢰도 조정 없음."""
        detector_plain = RegimeDetector()
        detector_with_none = RegimeDetector(derivatives_data=None)
        df = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state_plain = detector_plain.detect(df)
        state_with_none = detector_with_none.detect(df)
        assert state_plain.confidence == state_with_none.confidence


class TestDerivativesSnapshotAttached:
    """mock derivatives_data 주입 → snapshot이 반환 RegimeState에 첨부됨."""

    @pytest.mark.asyncio
    async def test_snapshot_attached_when_data_available(self):
        """derivatives_data.get_snapshot() 반환값이 derivatives_snapshot에 들어감."""
        snap = {
            "symbol": "BTC/USDT",
            "open_interest_value": 1000.0,
            "premium_pct": 0.1,
            "last_funding_rate": 0.0001,
            "long_account_ratio": 0.55,
            "short_account_ratio": 0.45,
        }
        mock_deriv = _make_derivatives_mock(snap)
        detector = RegimeDetector(derivatives_data=mock_deriv)
        df = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state = await detector.update(df, "BTC/USDT")
        assert state.derivatives_snapshot is not None
        assert "signals" in state.derivatives_snapshot
        assert "oi_change_rate" in state.derivatives_snapshot
        assert "premium_pct" in state.derivatives_snapshot

    @pytest.mark.asyncio
    async def test_snapshot_none_when_no_data(self):
        """get_snapshot()이 None 반환 → derivatives_snapshot=None."""
        mock_deriv = _make_derivatives_mock(None)
        detector = RegimeDetector(derivatives_data=mock_deriv)
        df = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state = await detector.update(df, "BTC/USDT")
        assert state.derivatives_snapshot is None

    @pytest.mark.asyncio
    async def test_regime_not_changed_by_derivatives(self):
        """파생상품 신호는 레짐 자체를 변경하지 않음."""
        # 강한 변동성 신호들을 모두 주입
        snap = {
            "open_interest_value": 1100.0,  # 높은 OI
            "premium_pct": 1.5,             # 극단 프리미엄
            "last_funding_rate": 0.005,     # 극단 펀딩
            "long_account_ratio": 0.90,
            "short_account_ratio": 0.10,
        }
        mock_deriv = _make_derivatives_mock(snap)
        detector = RegimeDetector(derivatives_data=mock_deriv)
        # TRENDING_UP 조건
        df = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state = await detector.update(df, "BTC/USDT")
        # 파생상품 신호가 있어도 레짐은 TRENDING_UP 유지
        assert state.regime == Regime.TRENDING_UP


class TestPremiumExtreme:
    """premium_pct 극단 → VOLATILE 신뢰도 상승."""

    @pytest.mark.asyncio
    async def test_premium_extreme_high_boosts_volatile_confidence(self):
        """premium_pct > 0.5% → VOLATILE 레짐에서 신뢰도 상승."""
        snap = {
            "premium_pct": 0.8,  # > 0.5% 임계값
            "last_funding_rate": 0.0,
            "long_account_ratio": 0.5,
            "short_account_ratio": 0.5,
        }
        mock_deriv = _make_derivatives_mock(snap)
        detector_plain = RegimeDetector()
        detector_with = RegimeDetector(derivatives_data=mock_deriv)

        # VOLATILE 조건: BB width ≈ 6.25% → confidence = 0.625 (1.0 미만)
        df = _make_df(adx=18, bb_upper=82500, bb_lower=77500, bb_mid=80000)

        plain_state = await detector_plain.update(df, "BTC/USDT")
        with_state = await detector_with.update(df, "BTC/USDT")

        assert plain_state.regime == Regime.VOLATILE
        assert with_state.regime == Regime.VOLATILE
        assert with_state.confidence > plain_state.confidence
        assert with_state.derivatives_snapshot is not None
        assert "premium_extreme" in with_state.derivatives_snapshot["signals"]

    @pytest.mark.asyncio
    async def test_premium_extreme_low_boosts_volatile_confidence(self):
        """premium_pct < -0.5% → VOLATILE 레짐에서 신뢰도 상승."""
        snap = {
            "premium_pct": -0.8,  # < -0.5% 임계값
            "last_funding_rate": 0.0,
            "long_account_ratio": 0.5,
            "short_account_ratio": 0.5,
        }
        mock_deriv = _make_derivatives_mock(snap)
        detector_plain = RegimeDetector()
        detector_with = RegimeDetector(derivatives_data=mock_deriv)

        # VOLATILE 조건: BB width ≈ 6.25% → confidence = 0.625 (1.0 미만)
        df = _make_df(adx=18, bb_upper=82500, bb_lower=77500, bb_mid=80000)

        plain_state = await detector_plain.update(df, "BTC/USDT")
        with_state = await detector_with.update(df, "BTC/USDT")

        assert with_state.regime == Regime.VOLATILE
        assert with_state.confidence > plain_state.confidence
        assert "premium_extreme" in with_state.derivatives_snapshot["signals"]

    @pytest.mark.asyncio
    async def test_premium_within_normal_range_no_signal(self):
        """premium_pct가 ±0.5% 이내 → premium_extreme 신호 없음."""
        snap = {
            "premium_pct": 0.2,
            "last_funding_rate": 0.0,
            "long_account_ratio": 0.5,
            "short_account_ratio": 0.5,
        }
        mock_deriv = _make_derivatives_mock(snap)
        detector = RegimeDetector(derivatives_data=mock_deriv)
        df = _make_df(adx=18, bb_upper=90000, bb_lower=70000, bb_mid=80000)
        state = await detector.update(df, "BTC/USDT")
        assert "premium_extreme" not in state.derivatives_snapshot["signals"]


class TestOIDivergence:
    """OI 급증 → oi_divergence 신호."""

    @pytest.mark.asyncio
    async def test_oi_divergence_detected_on_rapid_increase(self):
        """OI가 5% 이상 급증 → oi_divergence 신호 감지."""
        mock_deriv = MagicMock()
        # 첫 번째 호출: OI = 1000 (기준값 축적)
        mock_deriv.get_snapshot.return_value = {
            "open_interest_value": 1000.0,
            "premium_pct": 0.0,
            "last_funding_rate": 0.0,
            "long_account_ratio": 0.5,
            "short_account_ratio": 0.5,
        }
        detector = RegimeDetector(derivatives_data=mock_deriv)
        df = _make_df(adx=18, bb_upper=90000, bb_lower=70000, bb_mid=80000)

        # OI 이력 축적 (maxlen=6, 기준값 채우기)
        for oi_val in [1000.0, 1010.0, 1020.0, 1030.0, 1040.0]:
            mock_deriv.get_snapshot.return_value = {
                "open_interest_value": oi_val,
                "premium_pct": 0.0,
                "last_funding_rate": 0.0,
                "long_account_ratio": 0.5,
                "short_account_ratio": 0.5,
            }
            await detector.update(df, "BTC/USDT")

        # OI 급증: 1000 → 1080 (+8%)
        mock_deriv.get_snapshot.return_value = {
            "open_interest_value": 1080.0,
            "premium_pct": 0.0,
            "last_funding_rate": 0.0,
            "long_account_ratio": 0.5,
            "short_account_ratio": 0.5,
        }
        state = await detector.update(df, "BTC/USDT")
        assert state.derivatives_snapshot is not None
        assert "oi_divergence" in state.derivatives_snapshot["signals"]
        assert state.derivatives_snapshot["oi_change_rate"] > 0.05

    @pytest.mark.asyncio
    async def test_oi_change_rate_zero_on_first_call(self):
        """OI 이력 1개일 때 변화율은 0."""
        snap = {
            "open_interest_value": 1000.0,
            "premium_pct": 0.0,
            "last_funding_rate": 0.0,
            "long_account_ratio": 0.5,
            "short_account_ratio": 0.5,
        }
        mock_deriv = _make_derivatives_mock(snap)
        detector = RegimeDetector(derivatives_data=mock_deriv)
        df = _make_df(adx=18, bb_upper=90000, bb_lower=70000, bb_mid=80000)
        state = await detector.update(df, "BTC/USDT")
        # 이력이 1개 → 변화율 0, oi_divergence 없음
        assert state.derivatives_snapshot["oi_change_rate"] == 0.0
        assert "oi_divergence" not in state.derivatives_snapshot["signals"]


class TestLongShortRatioExtreme:
    """롱/숏 비율 극단 → ls_ratio_extreme 신호."""

    @pytest.mark.asyncio
    async def test_long_heavy_ratio_detected(self):
        """long/short > 3.0 → ls_ratio_extreme 신호."""
        snap = {
            "open_interest_value": 1000.0,
            "premium_pct": 0.0,
            "last_funding_rate": 0.0,
            "long_account_ratio": 0.76,
            "short_account_ratio": 0.24,  # ratio = 0.76/0.24 ≈ 3.17 > 3.0
        }
        mock_deriv = _make_derivatives_mock(snap)
        detector = RegimeDetector(derivatives_data=mock_deriv)
        df = _make_df(adx=18, bb_upper=90000, bb_lower=70000, bb_mid=80000)
        state = await detector.update(df, "BTC/USDT")
        assert "ls_ratio_extreme" in state.derivatives_snapshot["signals"]

    @pytest.mark.asyncio
    async def test_short_heavy_ratio_detected(self):
        """long/short < 0.33 → ls_ratio_extreme 신호."""
        snap = {
            "open_interest_value": 1000.0,
            "premium_pct": 0.0,
            "last_funding_rate": 0.0,
            "long_account_ratio": 0.24,
            "short_account_ratio": 0.76,  # ratio = 0.24/0.76 ≈ 0.316 < 0.33
        }
        mock_deriv = _make_derivatives_mock(snap)
        detector = RegimeDetector(derivatives_data=mock_deriv)
        df = _make_df(adx=18, bb_upper=90000, bb_lower=70000, bb_mid=80000)
        state = await detector.update(df, "BTC/USDT")
        assert "ls_ratio_extreme" in state.derivatives_snapshot["signals"]

    @pytest.mark.asyncio
    async def test_balanced_ratio_no_signal(self):
        """균형 잡힌 롱/숏 비율 → ls_ratio_extreme 없음."""
        snap = {
            "open_interest_value": 1000.0,
            "premium_pct": 0.0,
            "last_funding_rate": 0.0,
            "long_account_ratio": 0.52,
            "short_account_ratio": 0.48,  # ratio ≈ 1.08
        }
        mock_deriv = _make_derivatives_mock(snap)
        detector = RegimeDetector(derivatives_data=mock_deriv)
        df = _make_df(adx=18, bb_upper=90000, bb_lower=70000, bb_mid=80000)
        state = await detector.update(df, "BTC/USDT")
        assert "ls_ratio_extreme" not in state.derivatives_snapshot["signals"]


class TestFundingRateExtreme:
    """펀딩 비율 극단 → funding_rate_extreme 신호."""

    @pytest.mark.asyncio
    async def test_high_positive_funding_rate(self):
        """|funding_rate| > 0.1% (0.001) → funding_rate_extreme 신호."""
        snap = {
            "open_interest_value": 1000.0,
            "premium_pct": 0.0,
            "last_funding_rate": 0.0015,  # 0.15% > 0.1%
            "long_account_ratio": 0.5,
            "short_account_ratio": 0.5,
        }
        mock_deriv = _make_derivatives_mock(snap)
        detector = RegimeDetector(derivatives_data=mock_deriv)
        df = _make_df(adx=18, bb_upper=90000, bb_lower=70000, bb_mid=80000)
        state = await detector.update(df, "BTC/USDT")
        assert "funding_rate_extreme" in state.derivatives_snapshot["signals"]
        assert state.derivatives_snapshot["funding_rate"] == pytest.approx(0.0015)

    @pytest.mark.asyncio
    async def test_high_negative_funding_rate(self):
        """음수 극단 펀딩 비율도 감지."""
        snap = {
            "open_interest_value": 1000.0,
            "premium_pct": 0.0,
            "last_funding_rate": -0.002,  # -0.2%
            "long_account_ratio": 0.5,
            "short_account_ratio": 0.5,
        }
        mock_deriv = _make_derivatives_mock(snap)
        detector = RegimeDetector(derivatives_data=mock_deriv)
        df = _make_df(adx=18, bb_upper=90000, bb_lower=70000, bb_mid=80000)
        state = await detector.update(df, "BTC/USDT")
        assert "funding_rate_extreme" in state.derivatives_snapshot["signals"]

    @pytest.mark.asyncio
    async def test_normal_funding_rate_no_signal(self):
        """|funding_rate| <= 0.1% → funding_rate_extreme 없음."""
        snap = {
            "open_interest_value": 1000.0,
            "premium_pct": 0.0,
            "last_funding_rate": 0.0005,  # 0.05% < 0.1%
            "long_account_ratio": 0.5,
            "short_account_ratio": 0.5,
        }
        mock_deriv = _make_derivatives_mock(snap)
        detector = RegimeDetector(derivatives_data=mock_deriv)
        df = _make_df(adx=18, bb_upper=90000, bb_lower=70000, bb_mid=80000)
        state = await detector.update(df, "BTC/USDT")
        assert "funding_rate_extreme" not in state.derivatives_snapshot["signals"]


class TestRegimeStateSerialization:
    """RegimeState frozen dataclass 직렬화 및 derivatives_snapshot 필드 테스트."""

    def test_regime_state_with_derivatives_snapshot(self):
        """derivatives_snapshot을 직접 지정한 RegimeState 생성 가능."""
        snap = {"oi_change_rate": 0.08, "premium_pct": 0.6, "signals": ["oi_divergence"]}
        state = RegimeState(
            regime=Regime.VOLATILE,
            confidence=0.75,
            adx=20.0,
            bb_width=12.0,
            atr_pct=5.0,
            volume_ratio=1.5,
            trend_direction=0,
            timestamp=datetime.now(timezone.utc),
            derivatives_snapshot=snap,
        )
        assert state.derivatives_snapshot is snap
        assert state.derivatives_snapshot["signals"] == ["oi_divergence"]

    def test_regime_state_default_no_snapshot(self):
        """derivatives_snapshot 미지정 시 None 기본값."""
        state = RegimeState(
            regime=Regime.RANGING,
            confidence=0.6,
            adx=15.0,
            bb_width=3.0,
            atr_pct=1.0,
            volume_ratio=1.0,
            trend_direction=0,
            timestamp=datetime.now(timezone.utc),
        )
        assert state.derivatives_snapshot is None

    def test_regime_state_frozen_cannot_mutate_snapshot(self):
        """frozen dataclass: derivatives_snapshot 필드 직접 변경 불가."""
        state = RegimeState(
            regime=Regime.RANGING,
            confidence=0.6,
            adx=15.0,
            bb_width=3.0,
            atr_pct=1.0,
            volume_ratio=1.0,
            trend_direction=0,
            timestamp=datetime.now(timezone.utc),
        )
        with pytest.raises((AttributeError, TypeError)):
            state.derivatives_snapshot = {"signals": []}  # type: ignore[misc]


class TestFuturesEngineV2DerivativesWiring:
    """FuturesEngineV2가 derivatives_data를 RegimeDetector에 전달함을 검증."""

    def _make_engine(self, derivatives_data=None):
        exchange = AsyncMock()
        exchange.set_leverage = AsyncMock()
        exchange.fetch_balance = AsyncMock(return_value={
            "USDT": Balance(currency="USDT", free=500.0, used=0.0, total=500.0),
        })
        exchange.close_ws = AsyncMock()

        md = AsyncMock()
        md.get_current_price = AsyncMock(return_value=80000.0)
        md.get_ohlcv_df = AsyncMock(return_value=None)

        pm = MagicMock()
        pm.cash_balance = 500.0
        pm._is_paper = False
        pm._exchange_name = "binance_futures"
        pm.apply_income = AsyncMock()

        om = MagicMock()

        return FuturesEngineV2(
            config=AppConfig(),
            exchange=exchange,
            market_data=md,
            order_manager=om,
            portfolio_manager=pm,
            derivatives_data=derivatives_data,
        )

    def test_derivatives_data_passed_to_regime_detector(self):
        """FuturesEngineV2에 전달한 derivatives_data가 RegimeDetector에 주입됨."""
        mock_deriv = MagicMock()
        engine = self._make_engine(derivatives_data=mock_deriv)
        assert engine._regime._derivatives_data is mock_deriv

    def test_no_derivatives_data_defaults_to_none(self):
        """derivatives_data 미전달 시 RegimeDetector._derivatives_data=None."""
        engine = self._make_engine(derivatives_data=None)
        assert engine._regime._derivatives_data is None


class TestDerivativesRobustness:
    """파생상품 보조 시그널 내결함성 테스트."""

    @pytest.mark.asyncio
    async def test_ls_ratio_included_in_snapshot(self):
        """ls_ratio가 derivatives_snapshot에 사전 계산된 값으로 포함됨."""
        snap = {
            "open_interest_value": 1000.0,
            "premium_pct": 0.0,
            "last_funding_rate": 0.0,
            "long_account_ratio": 0.76,
            "short_account_ratio": 0.24,
        }
        mock_deriv = _make_derivatives_mock(snap)
        detector = RegimeDetector(derivatives_data=mock_deriv)
        df = _make_df(adx=18, bb_upper=90000, bb_lower=70000, bb_mid=80000)
        state = await detector.update(df, "BTC/USDT")
        assert state.derivatives_snapshot is not None
        assert "ls_ratio" in state.derivatives_snapshot
        ls = state.derivatives_snapshot["ls_ratio"]
        assert ls is not None
        assert ls == pytest.approx(0.76 / 0.24, rel=1e-3)

    @pytest.mark.asyncio
    async def test_ls_ratio_none_when_short_ratio_zero(self):
        """short_ratio=0일 때 ls_ratio=None으로 0 나누기 방지."""
        snap = {
            "open_interest_value": 1000.0,
            "premium_pct": 0.0,
            "last_funding_rate": 0.0,
            "long_account_ratio": 1.0,
            "short_account_ratio": 0.0,  # short_ratio = 0
        }
        mock_deriv = _make_derivatives_mock(snap)
        detector = RegimeDetector(derivatives_data=mock_deriv)
        df = _make_df(adx=18, bb_upper=90000, bb_lower=70000, bb_mid=80000)
        state = await detector.update(df, "BTC/USDT")
        assert state.derivatives_snapshot["ls_ratio"] is None

    @pytest.mark.asyncio
    async def test_malformed_snapshot_value_falls_back_gracefully(self):
        """snapshot 값이 비수치형(문자열 등)이어도 에러 없이 default 0.0 사용."""
        snap = {
            "open_interest_value": 1000.0,
            "premium_pct": "N/A",       # 비수치형 문자열
            "last_funding_rate": None,
            "long_account_ratio": {},   # dict (malformed)
            "short_account_ratio": 0.5,
        }
        mock_deriv = _make_derivatives_mock(snap)
        detector = RegimeDetector(derivatives_data=mock_deriv)
        df = _make_df(adx=18, bb_upper=90000, bb_lower=70000, bb_mid=80000)
        # 예외 없이 처리되어야 함
        state = await detector.update(df, "BTC/USDT")
        assert state.derivatives_snapshot is not None
        assert state.derivatives_snapshot["premium_pct"] == 0.0
        assert state.derivatives_snapshot["funding_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_derivatives_error_does_not_abort_regime_detection(self):
        """_apply_derivatives 내부 예외가 발생해도 regime 감지는 계속 동작."""
        mock_deriv = MagicMock()
        mock_deriv.get_snapshot.side_effect = RuntimeError("network timeout")
        detector = RegimeDetector(derivatives_data=mock_deriv)
        df = _make_df(close=82000, adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        # 예외 없이 정상 RegimeState 반환 (derivatives_snapshot=None)
        state = await detector.update(df, "BTC/USDT")
        assert state.regime == Regime.TRENDING_UP
        assert state.derivatives_snapshot is None

    def test_safe_float_handles_non_numeric_types(self):
        """_safe_float이 비수치형 값을 default로 폴백."""
        assert RegimeDetector._safe_float("N/A") == 0.0
        assert RegimeDetector._safe_float({}) == 0.0
        assert RegimeDetector._safe_float([1, 2]) == 0.0
        assert RegimeDetector._safe_float(None) == 0.0
        assert RegimeDetector._safe_float(None, default=99.0) == 99.0
        assert RegimeDetector._safe_float(1.5) == pytest.approx(1.5)
