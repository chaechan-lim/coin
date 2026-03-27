"""MarketDataService._compute_indicators() 컬럼명 테스트.

COIN-51: 라이브 RegimeDetector + 레짐 전략이 필요한 컬럼이
_compute_indicators()에서 올바른 이름으로 생성되는지 검증.
"""

from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from services.market_data import MarketDataService


def _make_ohlcv(n: int = 200) -> pd.DataFrame:
    """pandas_ta 계산에 충분한 OHLCV DataFrame 생성."""
    np.random.seed(42)
    close = 80000 + np.cumsum(np.random.randn(n) * 100)
    high = close + np.abs(np.random.randn(n) * 50)
    low = close - np.abs(np.random.randn(n) * 50)
    volume = np.random.rand(n) * 1000 + 500
    df = pd.DataFrame(
        {
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )
    return df


class TestComputeIndicators:
    """_compute_indicators()가 v2 전략이 기대하는 컬럼을 올바르게 생성하는지 검증."""

    @pytest.fixture
    def svc(self):
        """MarketDataService 인스턴스 (MagicMock exchange — 네트워크 호출 없음)."""
        return MarketDataService(exchange=MagicMock())

    @pytest.fixture
    def df_with_indicators(self, svc):
        df = _make_ohlcv(200)
        return svc._compute_indicators(df)

    def test_ema_columns_present(self, df_with_indicators):
        """RegimeDetector + TrendFollower + VolBreakout이 필요한 EMA 컬럼."""
        for col in ["ema_9", "ema_12", "ema_20", "ema_21", "ema_26", "ema_50"]:
            assert col in df_with_indicators.columns, f"Missing EMA column: {col}"

    def test_sma_columns_present(self, df_with_indicators):
        for col in ["sma_9", "sma_20", "sma_50", "sma_60", "sma_200"]:
            assert col in df_with_indicators.columns, f"Missing SMA column: {col}"

    def test_adx_lowercase(self, df_with_indicators):
        """ADX_14 → adx_14 리네임 검증 (RegimeDetector 필수)."""
        assert "adx_14" in df_with_indicators.columns
        assert "ADX_14" not in df_with_indicators.columns, (
            "ADX_14 should be renamed to adx_14"
        )

    def test_adx_di_lowercase(self, df_with_indicators):
        """DMP_14 → dmp_14, DMN_14 → dmn_14 리네임 검증 (+DI/-DI)."""
        assert "dmp_14" in df_with_indicators.columns
        assert "dmn_14" in df_with_indicators.columns
        assert "DMP_14" not in df_with_indicators.columns
        assert "DMN_14" not in df_with_indicators.columns

    def test_bb_columns_lowercase(self, df_with_indicators):
        """BBU/BBL/BBM/BBB/BBP → lowercase 리네임 검증."""
        assert "bb_upper_20" in df_with_indicators.columns
        assert "bb_lower_20" in df_with_indicators.columns
        assert "bb_mid_20" in df_with_indicators.columns
        # 원본 대문자 컬럼이 남아있지 않은지 확인 (BBB_20, BBP_20 포함)
        for col in df_with_indicators.columns:
            assert not col.startswith("BBU_20"), f"Unrenamed BB column: {col}"
            assert not col.startswith("BBL_20"), f"Unrenamed BB column: {col}"
            assert not col.startswith("BBM_20"), f"Unrenamed BB column: {col}"
            assert not col.startswith("BBB_20"), f"Unrenamed BB column: {col}"
            assert not col.startswith("BBP_20"), f"Unrenamed BB column: {col}"

    def test_macd_columns_renamed(self, df_with_indicators):
        """MACD_12_26_9 → macd_line 등 리네임 검증."""
        assert "macd_line" in df_with_indicators.columns
        assert "macd_signal" in df_with_indicators.columns
        assert "macd_hist" in df_with_indicators.columns
        assert "MACD_12_26_9" not in df_with_indicators.columns

    def test_rsi_and_atr(self, df_with_indicators):
        assert "rsi_14" in df_with_indicators.columns
        assert "atr_14" in df_with_indicators.columns

    def test_volume_sma(self, df_with_indicators):
        assert "volume_sma_20" in df_with_indicators.columns

    def test_regime_detector_required_columns(self, df_with_indicators):
        """RegimeDetector.detect()가 필요한 모든 컬럼이 존재하는지 검증.

        필수 컬럼: close, volume, adx_14, atr_14, ema_20, ema_50,
                   bb_upper_20, bb_lower_20, bb_mid_20
        """
        required = [
            "close",
            "volume",
            "adx_14",
            "atr_14",
            "ema_20",
            "ema_50",
            "bb_upper_20",
            "bb_lower_20",
            "bb_mid_20",
        ]
        for col in required:
            assert col in df_with_indicators.columns, (
                f"RegimeDetector requires '{col}' but it's missing"
            )

    def test_trend_follower_required_columns(self, df_with_indicators):
        """TrendFollowerStrategy가 필요한 컬럼: ema_9, ema_21, rsi_14, atr_14."""
        required = ["ema_9", "ema_21", "rsi_14", "atr_14", "close"]
        for col in required:
            assert col in df_with_indicators.columns, (
                f"TrendFollower requires '{col}' but it's missing"
            )

    def test_mean_reversion_required_columns(self, df_with_indicators):
        """MeanReversionStrategy가 필요한 컬럼: bb_upper_20, bb_lower_20, rsi_14."""
        required = ["bb_upper_20", "bb_lower_20", "rsi_14", "atr_14", "close"]
        for col in required:
            assert col in df_with_indicators.columns, (
                f"MeanReversion requires '{col}' but it's missing"
            )

    def test_vol_breakout_required_columns(self, df_with_indicators):
        """VolBreakoutStrategy가 필요한 컬럼: ema_20, atr_14, rsi_14."""
        required = ["ema_20", "atr_14", "rsi_14", "close", "volume"]
        for col in required:
            assert col in df_with_indicators.columns, (
                f"VolBreakout requires '{col}' but it's missing"
            )

    def test_indicator_values_not_all_nan(self, df_with_indicators):
        """지표 값이 전부 NaN이 아닌지 확인 (충분한 데이터 제공 시)."""
        key_cols = ["ema_20", "ema_50", "adx_14", "bb_upper_20", "rsi_14", "atr_14"]
        for col in key_cols:
            assert df_with_indicators[col].notna().any(), f"Column '{col}' is all NaN"

    def test_short_dataframe_no_error(self, svc):
        """데이터 1행이어도 에러 없이 원본 반환 (len < 2 조기 반환)."""
        df = _make_ohlcv(1)
        result = svc._compute_indicators(df)
        assert len(result) == 1


class TestBacktestColumnParity:
    """COIN-52: 라이브/백테스트가 동일한 indicators._RENAME_MAP을 사용하는지 검증."""

    def test_unified_rename_map_covers_adx(self):
        from services.indicators import _RENAME_MAP
        assert _RENAME_MAP.get("ADX_14") == "adx_14"
        assert _RENAME_MAP.get("DMP_14") == "dmp_14"
        assert _RENAME_MAP.get("DMN_14") == "dmn_14"

    def test_unified_rename_map_covers_macd(self):
        from services.indicators import _RENAME_MAP
        assert _RENAME_MAP.get("MACD_12_26_9") == "macd_line"
        assert _RENAME_MAP.get("MACDs_12_26_9") == "macd_signal"
        assert _RENAME_MAP.get("MACDh_12_26_9") == "macd_hist"

    def test_backtest_v2_uses_unified_rename_map(self):
        """COIN-52: backtest_v2._RENAME_MAP이 indicators._RENAME_MAP과 동일 객체."""
        from backtest_v2 import _RENAME_MAP as backtest_rename
        from services.indicators import _RENAME_MAP as unified_rename

        assert backtest_rename is unified_rename, (
            "backtest_v2._RENAME_MAP should be the same object as indicators._RENAME_MAP"
        )
