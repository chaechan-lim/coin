"""
ML Signal Filter Strategy.

기존 6전략 시그널 + 기술적 지표를 feature로,
향후 수익 여부를 예측하는 LightGBM 분류기.

사용법:
  - 학습: ml_filter.train(historical_data) → 모델 저장
  - 추론: combiner 결과를 ML이 필터링 (수익 확률 < threshold → HOLD)

Walk-forward 방식:
  - 백테스터에서 rolling window로 학습 → 다음 구간 예측
  - 과적합 방지를 위해 학습/검증 분리 필수
"""
import os
import json
import pickle
from pathlib import Path
from dataclasses import dataclass

import numpy as np
import pandas as pd

try:
    import lightgbm as lgb
    HAS_LGB = True
except ImportError:
    HAS_LGB = False

try:
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import accuracy_score, precision_score, f1_score
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False


MODEL_DIR = Path(__file__).parent.parent / "data" / "ml_models"


@dataclass
class MLPrediction:
    """ML 필터 예측 결과."""
    should_trade: bool
    win_probability: float
    feature_importance: dict | None = None


class MLSignalFilter:
    """LightGBM 기반 시그널 필터.

    기존 전략 시그널 조합의 수익 확률을 예측하여
    낮은 확률 거래를 필터링.
    """

    FEATURE_NAMES = [
        # 전략 시그널 (7개 × 2 = 14 features)
        "sig_ma_crossover", "conf_ma_crossover",
        "sig_rsi", "conf_rsi",
        "sig_macd_crossover", "conf_macd_crossover",
        "sig_bollinger_rsi", "conf_bollinger_rsi",
        "sig_stochastic_rsi", "conf_stochastic_rsi",
        "sig_obv_divergence", "conf_obv_divergence",
        "sig_bb_squeeze", "conf_bb_squeeze",
        # 기술적 지표 (8 features)
        "rsi_14", "atr_pct", "band_width",
        "sma20_dist", "sma20_sma50_gap",
        "volume_ratio", "price_change_7d",
        # 시장 상태 (1 feature, encoded)
        "market_state_rank",
        # 결합 시그널 (3 features)
        "combined_confidence", "active_weight", "num_buy_signals",
    ]

    def __init__(
        self,
        min_win_prob: float = 0.55,
        model_path: str | None = None,
    ):
        self._min_win_prob = min_win_prob
        self._model = None
        self._model_path = model_path

        if model_path and os.path.exists(model_path):
            self.load(model_path)

    @staticmethod
    def extract_features(
        signals: list,
        row: pd.Series,
        price: float,
        market_state: str,
        combined_confidence: float,
    ) -> dict:
        """전략 시그널 + 기술적 지표에서 feature 추출."""
        from core.enums import SignalType

        features = {}

        # 전략별 시그널 인코딩: BUY=1, HOLD=0, SELL=-1
        _sig_map = {SignalType.BUY: 1, SignalType.HOLD: 0, SignalType.SELL: -1}
        strategy_names = [
            "ma_crossover", "rsi", "macd_crossover",
            "bollinger_rsi", "stochastic_rsi", "obv_divergence",
            "bb_squeeze",
        ]

        signal_dict = {s.strategy_name: s for s in signals}
        num_buy = 0
        active_w = 0.0

        for name in strategy_names:
            sig = signal_dict.get(name)
            if sig:
                features[f"sig_{name}"] = _sig_map.get(sig.signal_type, 0)
                features[f"conf_{name}"] = sig.confidence
                if sig.signal_type != SignalType.HOLD:
                    active_w += 1
                if sig.signal_type == SignalType.BUY:
                    num_buy += 1
            else:
                features[f"sig_{name}"] = 0
                features[f"conf_{name}"] = 0.0

        # 기술적 지표
        rsi = row.get("RSI_14", row.get("rsi_14", 50))
        atr = row.get("ATRr_14", row.get("atr_14", 0))
        sma20 = row.get("SMA_20", row.get("sma_20", price))
        sma50 = row.get("SMA_50", row.get("sma_50", price))
        vol = row.get("volume", 0)
        vol_sma = row.get("Volume_SMA_20", vol if vol > 0 else 1)

        # BBands
        bb_upper = None
        bb_lower = None
        bb_middle = None
        for col in row.index:
            if col.startswith("BBU_"):
                bb_upper = row[col]
            elif col.startswith("BBL_"):
                bb_lower = row[col]
            elif col.startswith("BBM_"):
                bb_middle = row[col]

        if bb_upper and bb_lower and bb_middle and not pd.isna(bb_middle) and bb_middle > 0:
            band_width = (float(bb_upper) - float(bb_lower)) / float(bb_middle)
        else:
            band_width = 0.0

        features["rsi_14"] = float(rsi) if not pd.isna(rsi) else 50.0
        features["atr_pct"] = (float(atr) / price * 100) if (atr and not pd.isna(atr) and price > 0) else 0.0
        features["band_width"] = band_width
        features["sma20_dist"] = ((price - float(sma20)) / float(sma20) * 100) if (sma20 and not pd.isna(sma20) and float(sma20) > 0) else 0.0
        features["sma20_sma50_gap"] = ((float(sma20) - float(sma50)) / float(sma50) * 100) if (sma20 and sma50 and not pd.isna(sma20) and not pd.isna(sma50) and float(sma50) > 0) else 0.0
        features["volume_ratio"] = (float(vol) / float(vol_sma)) if (vol_sma and not pd.isna(vol_sma) and float(vol_sma) > 0) else 1.0

        # 7일 가격 변동 (approx from SMA)
        features["price_change_7d"] = features["sma20_dist"]  # proxy

        # 시장 상태 인코딩
        _state_rank = {"crash": 0, "downtrend": 1, "sideways": 2, "uptrend": 3, "strong_uptrend": 4}
        features["market_state_rank"] = _state_rank.get(market_state, 2)

        # 결합 시그널 정보
        features["combined_confidence"] = combined_confidence
        features["active_weight"] = active_w / len(strategy_names)
        features["num_buy_signals"] = num_buy

        return features

    def predict(self, features: dict) -> MLPrediction:
        """시그널 조합의 수익 확률 예측."""
        if self._model is None:
            return MLPrediction(should_trade=True, win_probability=0.5)

        X = np.array([[features.get(f, 0) for f in self.FEATURE_NAMES]])
        prob = self._model.predict_proba(X)[0][1]  # P(win)

        return MLPrediction(
            should_trade=prob >= self._min_win_prob,
            win_probability=float(prob),
        )

    def train(
        self,
        X: np.ndarray,
        y: np.ndarray,
        n_splits: int = 3,
    ) -> dict:
        """Walk-forward cross-validation으로 학습.

        Args:
            X: Feature matrix (n_samples, n_features)
            y: Labels (1=win, 0=loss)
            n_splits: TimeSeriesSplit 분할 수

        Returns:
            성능 메트릭 dict
        """
        if not HAS_LGB or not HAS_SKLEARN:
            raise ImportError("lightgbm and scikit-learn required")

        tscv = TimeSeriesSplit(n_splits=n_splits)
        metrics = {"accuracy": [], "precision": [], "f1": []}

        best_model = None
        best_f1 = 0

        for train_idx, val_idx in tscv.split(X):
            X_train, X_val = X[train_idx], X[val_idx]
            y_train, y_val = y[train_idx], y[val_idx]

            model = lgb.LGBMClassifier(
                n_estimators=100,
                max_depth=4,
                learning_rate=0.05,
                num_leaves=15,
                min_child_samples=10,
                subsample=0.8,
                colsample_bytree=0.8,
                reg_alpha=0.1,
                reg_lambda=0.1,
                random_state=42,
                verbose=-1,
            )
            model.fit(X_train, y_train)

            y_pred = model.predict(X_val)
            acc = accuracy_score(y_val, y_pred)
            prec = precision_score(y_val, y_pred, zero_division=0)
            f1 = f1_score(y_val, y_pred, zero_division=0)

            metrics["accuracy"].append(acc)
            metrics["precision"].append(prec)
            metrics["f1"].append(f1)

            if f1 > best_f1:
                best_f1 = f1
                best_model = model

        # 최종: 전체 데이터로 재학습
        final_model = lgb.LGBMClassifier(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.05,
            num_leaves=15,
            min_child_samples=10,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_alpha=0.1,
            reg_lambda=0.1,
            random_state=42,
            verbose=-1,
        )
        final_model.fit(X, y)
        self._model = final_model

        # Feature importance
        importance = dict(zip(self.FEATURE_NAMES, final_model.feature_importances_))

        result = {
            "accuracy_mean": float(np.mean(metrics["accuracy"])),
            "precision_mean": float(np.mean(metrics["precision"])),
            "f1_mean": float(np.mean(metrics["f1"])),
            "n_samples": len(y),
            "n_positive": int(y.sum()),
            "positive_rate": float(y.mean()),
            "feature_importance": importance,
        }

        return result

    def save(self, path: str | None = None):
        """모델 저장."""
        path = path or self._model_path
        if not path:
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            path = str(MODEL_DIR / "signal_filter.pkl")
        with open(path, "wb") as f:
            pickle.dump(self._model, f)

    def load(self, path: str):
        """모델 로드."""
        with open(path, "rb") as f:
            self._model = pickle.load(f)
