"""Tests for ML Signal Filter."""
import os
os.environ.setdefault("DB_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("EXCHANGE_API_KEY", "test")
os.environ.setdefault("EXCHANGE_API_SECRET", "test")
os.environ.setdefault("TRADING_MODE", "paper")

import numpy as np
import pandas as pd
import pytest

from core.enums import SignalType
from strategies.base import Signal
from strategies.ml_filter import MLSignalFilter, MLPrediction


class TestMLSignalFilter:
    def test_predict_without_model_returns_true(self):
        """모델 없으면 항상 거래 허용."""
        ml = MLSignalFilter()
        pred = ml.predict({"rsi_14": 50})
        assert pred.should_trade is True
        assert pred.win_probability == 0.5

    def test_extract_features_buy_signals(self):
        """BUY 시그널에서 feature 추출."""
        signals = [
            Signal(signal_type=SignalType.BUY, confidence=0.75,
                   strategy_name="rsi", reason="test"),
            Signal(signal_type=SignalType.HOLD, confidence=0.3,
                   strategy_name="ma_crossover", reason="test"),
        ]
        row = pd.Series({
            "close": 50000, "RSI_14": 25.0, "ATRr_14": 1500,
            "SMA_20": 49000, "SMA_50": 48000, "volume": 100,
            "Volume_SMA_20": 80, "BBU_20_2.0": 52000,
            "BBL_20_2.0": 48000, "BBM_20_2.0": 50000,
        })
        features = MLSignalFilter.extract_features(
            signals=signals, row=row, price=50000,
            market_state="uptrend", combined_confidence=0.70,
        )
        assert features["sig_rsi"] == 1  # BUY
        assert features["conf_rsi"] == 0.75
        assert features["sig_ma_crossover"] == 0  # HOLD
        assert features["rsi_14"] == 25.0
        assert features["market_state_rank"] == 3  # uptrend
        assert features["combined_confidence"] == 0.70
        assert features["num_buy_signals"] == 1
        assert len(MLSignalFilter.FEATURE_NAMES) == len([
            k for k in features if k in MLSignalFilter.FEATURE_NAMES
        ])

    def test_extract_features_sell_signals(self):
        """SELL 시그널 인코딩 확인."""
        signals = [
            Signal(signal_type=SignalType.SELL, confidence=0.80,
                   strategy_name="bollinger_rsi", reason="test"),
        ]
        row = pd.Series({
            "close": 50000, "RSI_14": 75.0, "ATRr_14": 2000,
            "SMA_20": 51000, "SMA_50": 50000, "volume": 120,
            "Volume_SMA_20": 80,
        })
        features = MLSignalFilter.extract_features(
            signals=signals, row=row, price=50000,
            market_state="downtrend", combined_confidence=0.80,
        )
        assert features["sig_bollinger_rsi"] == -1  # SELL
        assert features["market_state_rank"] == 1  # downtrend

    def test_train_and_predict(self):
        """학습 후 예측 동작 확인."""
        try:
            import lightgbm
            import sklearn
        except ImportError:
            pytest.skip("lightgbm/sklearn not installed")

        np.random.seed(42)
        n = 100
        X = np.random.randn(n, len(MLSignalFilter.FEATURE_NAMES))
        # label: 첫 feature가 양수면 win (단순 패턴)
        y = (X[:, 0] > 0).astype(int)

        ml = MLSignalFilter(min_win_prob=0.50)
        metrics = ml.train(X, y, n_splits=2)

        assert metrics["accuracy_mean"] > 0.4  # 최소한 랜덤보다 나은
        assert metrics["n_samples"] == n
        assert "feature_importance" in metrics

        # 예측 테스트
        features = {f: 0.5 for f in MLSignalFilter.FEATURE_NAMES}
        pred = ml.predict(features)
        assert isinstance(pred, MLPrediction)
        assert 0 <= pred.win_probability <= 1

    def test_feature_names_complete(self):
        """FEATURE_NAMES가 23개인지 확인."""
        assert len(MLSignalFilter.FEATURE_NAMES) == 23
