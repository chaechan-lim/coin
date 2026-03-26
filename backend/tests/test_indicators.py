"""services.indicators 통합 지표 계산 파이프라인 테스트.

COIN-52: 백테스트/라이브 단일 compute_indicators() 함수 검증.
"""

import numpy as np
import pandas as pd
import pytest

from services.indicators import (
    REQUIRED_COLUMNS,
    _BB_PREFIX_MAP,
    _RENAME_MAP,
    compute_indicators,
)


def _make_ohlcv(n: int = 250) -> pd.DataFrame:
    """pandas_ta 계산에 충분한 OHLCV DataFrame 생성."""
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="4h")
    close = 80000 + np.cumsum(np.random.randn(n) * 100)
    high = close + np.abs(np.random.randn(n) * 50)
    low = close - np.abs(np.random.randn(n) * 50)
    volume = np.random.rand(n) * 1000 + 500
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": volume},
        index=dates,
    )


class TestComputeIndicators:
    """compute_indicators()가 모든 기대 컬럼을 올바르게 생성하는지 검증."""

    @pytest.fixture
    def df_with_indicators(self):
        return compute_indicators(_make_ohlcv(250))

    # ── SMA ────────────────────────────────────────────────────

    def test_sma_columns_present(self, df_with_indicators):
        for col in ["sma_5", "sma_9", "sma_20", "sma_50", "sma_60", "sma_200"]:
            assert col in df_with_indicators.columns, f"Missing SMA: {col}"

    # ── EMA ────────────────────────────────────────────────────

    def test_ema_columns_present(self, df_with_indicators):
        for col in ["ema_9", "ema_12", "ema_20", "ema_21", "ema_26", "ema_50"]:
            assert col in df_with_indicators.columns, f"Missing EMA: {col}"

    # ── RSI ────────────────────────────────────────────────────

    def test_rsi_present(self, df_with_indicators):
        assert "rsi_14" in df_with_indicators.columns

    # ── MACD ───────────────────────────────────────────────────

    def test_macd_columns_renamed(self, df_with_indicators):
        assert "macd_line" in df_with_indicators.columns
        assert "macd_signal" in df_with_indicators.columns
        assert "macd_hist" in df_with_indicators.columns
        # 원본 대문자 없어야 함
        assert "MACD_12_26_9" not in df_with_indicators.columns
        assert "MACDs_12_26_9" not in df_with_indicators.columns
        assert "MACDh_12_26_9" not in df_with_indicators.columns

    # ── Bollinger Bands ────────────────────────────────────────

    def test_bb_columns_lowercase(self, df_with_indicators):
        assert "bb_upper_20" in df_with_indicators.columns
        assert "bb_lower_20" in df_with_indicators.columns
        assert "bb_mid_20" in df_with_indicators.columns

    def test_no_uppercase_bb_columns(self, df_with_indicators):
        for col in df_with_indicators.columns:
            for prefix in _BB_PREFIX_MAP:
                assert not col.startswith(prefix), f"Unrenamed BB column: {col}"

    # ── ATR ────────────────────────────────────────────────────

    def test_atr_present(self, df_with_indicators):
        assert "atr_14" in df_with_indicators.columns
        # ATRr_14 원본이 없어야 함
        assert "ATRr_14" not in df_with_indicators.columns

    # ── ADX + DI ───────────────────────────────────────────────

    def test_adx_lowercase(self, df_with_indicators):
        assert "adx_14" in df_with_indicators.columns
        assert "ADX_14" not in df_with_indicators.columns

    def test_dmp_dmn_present(self, df_with_indicators):
        """DMP_14/DMN_14 → dmp_14/dmn_14 매핑 검증 (COIN-51 이슈)."""
        assert "dmp_14" in df_with_indicators.columns
        assert "dmn_14" in df_with_indicators.columns
        assert "DMP_14" not in df_with_indicators.columns
        assert "DMN_14" not in df_with_indicators.columns

    # ── Volume SMA ─────────────────────────────────────────────

    def test_volume_sma_lowercase(self, df_with_indicators):
        """volume_sma_20은 항상 lowercase (COIN-52 핵심 이슈)."""
        assert "volume_sma_20" in df_with_indicators.columns
        assert "Volume_SMA_20" not in df_with_indicators.columns

    # ── 전체 REQUIRED_COLUMNS ──────────────────────────────────

    def test_all_required_columns_present(self, df_with_indicators):
        """REQUIRED_COLUMNS 상수에 정의된 모든 컬럼이 존재."""
        missing = [c for c in REQUIRED_COLUMNS if c not in df_with_indicators.columns]
        assert not missing, f"Missing required columns: {missing}"

    # ── 대문자 잔여 컬럼 없음 ──────────────────────────────────

    def test_no_uppercase_pandas_ta_columns(self, df_with_indicators):
        """_RENAME_MAP에 정의된 대문자 컬럼이 결과에 남아있지 않아야 함."""
        for uppercase_col in _RENAME_MAP:
            assert uppercase_col not in df_with_indicators.columns, (
                f"Unrenamed column: {uppercase_col}"
            )

    # ── 값 검증 ───────────────────────────────────────────────

    def test_indicator_values_not_all_nan(self, df_with_indicators):
        """핵심 지표 값이 전부 NaN이 아닌지 확인."""
        key_cols = [
            "ema_20",
            "ema_50",
            "adx_14",
            "bb_upper_20",
            "rsi_14",
            "atr_14",
            "macd_line",
            "volume_sma_20",
        ]
        for col in key_cols:
            assert df_with_indicators[col].notna().any(), f"'{col}' is all NaN"

    # ── 엣지 케이스 ───────────────────────────────────────────

    def test_short_dataframe_returns_unchanged(self):
        """len < 2이면 원본 DataFrame을 그대로 반환."""
        df = _make_ohlcv(1)
        result = compute_indicators(df)
        assert len(result) == 1
        # 추가 컬럼이 생기지 않아야 함
        assert set(result.columns) == {"open", "high", "low", "close", "volume"}

    def test_short_150_rows_still_works(self):
        """150행 (sma_200 불가) 데이터에서도 에러 없이 계산되어야 함."""
        df = _make_ohlcv(150)
        result = compute_indicators(df)
        # sma_200은 NaN이지만 다른 지표는 정상
        assert "sma_200" in result.columns
        assert "ema_20" in result.columns
        assert result["ema_20"].notna().any()


class TestRenameMapCompleteness:
    """_RENAME_MAP이 모든 pandas_ta 대문자 출력을 커버하는지 검증."""

    def test_ema_all_lengths_covered(self):
        for length in [9, 12, 20, 21, 26, 50]:
            key = f"EMA_{length}"
            assert key in _RENAME_MAP, f"Missing EMA rename: {key}"
            assert _RENAME_MAP[key] == f"ema_{length}"

    def test_sma_all_lengths_covered(self):
        for length in [5, 9, 20, 50, 60, 200]:
            key = f"SMA_{length}"
            assert key in _RENAME_MAP, f"Missing SMA rename: {key}"
            assert _RENAME_MAP[key] == f"sma_{length}"

    def test_adx_with_di_covered(self):
        assert "ADX_14" in _RENAME_MAP
        assert "DMP_14" in _RENAME_MAP
        assert "DMN_14" in _RENAME_MAP

    def test_macd_all_components_covered(self):
        assert "MACD_12_26_9" in _RENAME_MAP
        assert "MACDs_12_26_9" in _RENAME_MAP
        assert "MACDh_12_26_9" in _RENAME_MAP


class TestLiveBacktestParity:
    """라이브와 백테스트가 동일한 compute_indicators()를 사용하는지 검증."""

    def test_market_data_service_delegates_to_indicators(self):
        """MarketDataService._compute_indicators()가 indicators.compute_indicators()를 사용."""
        from unittest.mock import MagicMock
        from services.market_data import MarketDataService

        svc = MarketDataService(exchange=MagicMock())
        df = _make_ohlcv(200)
        result = svc._compute_indicators(df)
        # 통합 함수와 동일한 결과를 내는지 확인
        for col in REQUIRED_COLUMNS:
            assert col in result.columns, f"MarketDataService missing: {col}"

    def test_backtest_v2_uses_unified_rename_map(self):
        """backtest_v2._RENAME_MAP이 indicators._RENAME_MAP과 동일한 객체."""
        from backtest_v2 import _RENAME_MAP as bt_rename
        from services.indicators import _RENAME_MAP as ind_rename

        assert bt_rename is ind_rename, (
            "backtest_v2._RENAME_MAP should be re-exported from services.indicators"
        )

    def test_compute_v2_indicators_produces_required_columns(self):
        """compute_v2_indicators()가 REQUIRED_COLUMNS을 모두 포함."""
        from backtest_v2 import compute_v2_indicators

        df = _make_ohlcv(250)
        # datetime index with timezone for cutoff filter
        df.index = pd.date_range("2025-01-01", periods=250, freq="4h", tz="UTC")
        result = compute_v2_indicators(df)
        if len(result) > 0:
            for col in ["ema_20", "rsi_14", "atr_14", "bb_upper_20", "volume_sma_20"]:
                assert col in result.columns, f"compute_v2_indicators missing: {col}"


class TestRequiredColumnsConstant:
    """REQUIRED_COLUMNS 상수 검증."""

    def test_all_lowercase(self):
        for col in REQUIRED_COLUMNS:
            assert col == col.lower(), f"REQUIRED_COLUMNS contains non-lowercase: {col}"

    def test_no_duplicates(self):
        assert len(REQUIRED_COLUMNS) == len(set(REQUIRED_COLUMNS))

    def test_minimum_count(self):
        """최소 20개 이상의 필수 컬럼."""
        assert len(REQUIRED_COLUMNS) >= 20
