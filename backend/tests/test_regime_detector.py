"""RegimeDetector 테스트."""
import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from engine.regime_detector import RegimeDetector, RegimeState
from core.enums import Regime


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
        df = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state = detector.detect(df)
        assert state.regime == Regime.TRENDING_UP
        assert state.confidence > 0.5

    def test_trending_down(self):
        detector = RegimeDetector()
        df = _make_df(adx=30, ema_20=78000, ema_50=80000, ema_slope_dir=-1)
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
        df = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)

        state = detector.detect(df)
        confirmed = detector._apply_hysteresis(state)
        assert confirmed.regime == Regime.TRENDING_UP
        assert detector.current.regime == Regime.TRENDING_UP

    def test_same_regime_resets_pending(self):
        detector = RegimeDetector()
        df_up = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)

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
        df_up = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
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
        df_up = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
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
        df = _make_df(adx=26, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state = detector.detect(df)
        # ADX 26 < 27 → 추세 아님
        assert state.regime != Regime.TRENDING_UP

    def test_adx_hysteresis_exit(self):
        """추세 이탈: ADX <= 23 필요 (adx_exit)."""
        detector = RegimeDetector(adx_enter=27, adx_exit=23, confirm_count=1, min_duration_h=0)

        # 먼저 추세 상태로 설정
        df_up = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state = detector.detect(df_up)
        detector._apply_hysteresis(state)

        # ADX 24 — 추세 유지 (23 이하가 아님)
        df_mid = _make_df(adx=24, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state2 = detector.detect(df_mid)
        # in_trend 상태이므로 adx_exit(23) 적용 → 24 >= 23 → 여전히 추세
        assert state2.regime == Regime.TRENDING_UP


class TestPerCoin:
    @pytest.mark.asyncio
    async def test_per_coin_storage(self):
        detector = RegimeDetector()
        df_btc = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        df_eth = _make_df(adx=15, bb_upper=81000, bb_lower=79000, bb_mid=80000)

        await detector.update(df_btc, "BTC/USDT")
        await detector.update(df_eth, "ETH/USDT")

        assert "BTC/USDT" in detector.per_coin
        assert "ETH/USDT" in detector.per_coin
        assert detector.per_coin["BTC/USDT"].regime == Regime.TRENDING_UP
        assert detector.per_coin["ETH/USDT"].regime == Regime.RANGING


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
        df_up = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
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
        df_up = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)

        await detector.update(df_up, "BTC/USDT")  # 초기 설정

        with patch("engine.regime_detector.emit_event", new_callable=AsyncMock) as mock_emit:
            await detector.update(df_up, "BTC/USDT")  # 같은 레짐
            mock_emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_no_emit_on_first_detection(self):
        """첫 레짐 감지(prev_regime=None)는 emit 안 됨."""
        detector = RegimeDetector(confirm_count=1, min_duration_h=0)
        df_up = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)

        with patch("engine.regime_detector.emit_event", new_callable=AsyncMock) as mock_emit:
            await detector.update(df_up, "BTC/USDT")  # 최초 감지
            mock_emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_emit_metadata_contains_required_fields(self):
        """emit_event 메타데이터에 prev_regime, new_regime, confidence, adx, symbol 포함."""
        detector = RegimeDetector(confirm_count=1, min_duration_h=0)

        df_range = _make_df(adx=15, bb_upper=81000, bb_lower=79000, bb_mid=80000)
        await detector.update(df_range, "ETH/USDT")

        df_up = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        with patch("engine.regime_detector.emit_event", new_callable=AsyncMock) as mock_emit:
            await detector.update(df_up, "ETH/USDT")
            meta = mock_emit.call_args[1]["metadata"]
            assert "prev_regime" in meta
            assert "new_regime" in meta
            assert "confidence" in meta
            assert "adx" in meta
            assert meta["symbol"] == "ETH/USDT"
