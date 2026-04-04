"""
FuturesEngineV2 백테스터 — 레짐 적응형 선물 엔진 Walk-Forward 검증.

실행:
  python backtest_v2.py --days 540
  python backtest_v2.py --days 540 --walk-forward
  python backtest_v2.py --days 360 --coins BTC ETH SOL
  python backtest_v2.py --days 180 --leverage 5

현물 4전략 모드 (라이브 V2 구성 검증):
  python backtest_v2.py --days 540 --spot-strategies

Walk-Forward (240-day train + 60-day val + 60-day test):
  python backtest_v2.py --days 540 --walk-forward
"""
import asyncio
import argparse
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# structlog 로그 레벨을 WARNING으로 올려서 불필요한 출력 제거
logging.basicConfig(level=logging.WARNING)
import structlog
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

from core.enums import Direction, Regime, SignalType
from engine.regime_detector import RegimeDetector, RegimeState
from engine.strategy_selector import StrategySelector
from strategies.regime_base import RegimeStrategy, StrategyDecision
from strategies.base import Signal
from exchange.data_models import Candle, Ticker

# ── 상수 ──────────────────────────────────────────────────────
FUTURES_FEE = 0.0004      # 0.04% maker/taker (바이낸스 선물)
FUNDING_RATE = 0.0001     # 0.01%/8h 평균 펀딩비
SLIPPAGE = 0.0002         # 0.02% 슬리피지
MIN_MARGIN_USDT = 5.0     # 최소 마진
BASE_RISK_PCT = 0.02      # 기본 리스크: 자본의 2%
LOOKBACK_WINDOW = 60      # 전략 평가용 슬라이스 크기

# ── 동적 SL/사이징 프로필 (라이브 tier1_manager와 동일) ─────────
# (multiplier, floor_atr_mult, cap_atr_mult)
DYNAMIC_SL_PROFILES: dict[Regime, tuple[float, float, float]] = {
    Regime.TRENDING_UP: (1.0, 1.0, 8.0),
    Regime.TRENDING_DOWN: (0.6, 0.8, 4.0),
    Regime.RANGING: (0.8, 1.0, 6.0),
    Regime.VOLATILE: (0.7, 0.8, 5.0),
}
DEFAULT_SL_PROFILE = (0.8, 1.0, 6.0)

REGIME_SIZING_FACTORS: dict[Regime, float] = {
    Regime.TRENDING_UP: 1.0,
    Regime.TRENDING_DOWN: 0.5,
    Regime.RANGING: 0.8,
    Regime.VOLATILE: 0.6,
}

# ATR% 기반 레버리지 스케일링 (라이브 tier1_manager와 동일)
ATR_LEVERAGE_TIERS: list[tuple[float, int]] = [
    (20.0, 1),
    (10.0, 2),
    (7.0, 3),
    (5.0, 4),
    (3.0, 5),
    (0.0, 5),
]
# 타이트 ATR 레버리지 (base 3x 기준: ATR>5%→2x, ATR>10%→1x)
ATR_LEVERAGE_TIERS_TIGHT: list[tuple[float, int]] = [
    (10.0, 1),
    (5.0, 2),
    (3.0, 3),
    (0.0, 3),
]
SPOT_1H_LOOKBACK = 400    # 현물 4전략 모드: 1h→4h 리샘플링용 ((30+59)×4 ≈ 356, +여유)

COINS_DEFAULT = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"]

# ── Tier 2 상수 (config.py FuturesV2Config / tier2_scanner.py 라이브 기본값) ──
TIER2_COINS_DEFAULT = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT",
    "NEAR/USDT", "SUI/USDT", "1000PEPE/USDT", "WIF/USDT", "ATOM/USDT",
    "FIL/USDT", "ARB/USDT", "OP/USDT", "TRX/USDT", "AAVE/USDT",
    "ETC/USDT", "APT/USDT", "IMX/USDT", "INJ/USDT", "SEI/USDT",
    "FET/USDT", "RENDER/USDT", "TIA/USDT", "JUP/USDT", "PENDLE/USDT",
]
TIER2_SL_PCT = 3.5
TIER2_TP_PCT = 4.5
TIER2_TRAIL_ACTIVATION_PCT = 1.5
TIER2_TRAIL_STOP_PCT = 1.0
TIER2_MAX_CONCURRENT = 3
TIER2_MAX_HOLD_CANDLES = 24    # 120min / 5min
TIER2_COOLDOWN_CANDLES = 12    # 60min / 5min
TIER2_DAILY_TRADE_LIMIT = 20
TIER2_POSITION_PCT = 0.05
TIER2_MIN_SCORE = 0.55
TIER2_VOL_LOOKBACK = 60        # 5h of 5m candles (스캔 기준)
TIER2_RSI_OVERBOUGHT = 75.0
TIER2_RSI_OVERSOLD = 25.0
TIER2_MIN_ATR_PCT = 0.5
TIER2_EXHAUSTION_PCT = 8.0
TIER2_CONSECUTIVE_SL_COOLDOWN_CANDLES = 36  # 180min / 5min

# ── US 마켓 오픈 시간 필터 ──
# KST 22:00-00:00 (ET 9:00-11:00) — 캐스케이드 청산 집중 시간대
US_OPEN_BLOCK_HOURS_KST = {22, 23}  # KST 기준

# ── 캐스케이드 역추세 상수 ──
CASCADE_DROP_THRESHOLD = 2.0      # 1시간 내 2% 이상 하락 시 역추세 롱
CASCADE_LOOKBACK_CANDLES = 12     # 1시간 (5m × 12)
CASCADE_SL_PCT = 1.5              # 타이트 SL
CASCADE_TP_PCT = 2.5              # 반등 목표
CASCADE_TRAIL_ACT_PCT = 1.0       # 트레일링 활성화
CASCADE_TRAIL_STOP_PCT = 0.6      # 트레일링 스탑
CASCADE_MAX_HOLD_CANDLES = 36     # 최대 3시간 보유
CASCADE_POSITION_PCT = 0.03       # 작은 포지션 (자본의 3%)
CASCADE_COOLDOWN_CANDLES = 36     # 3시간 쿨다운 (5m × 36)

# COIN-52: 지표 계산 → services.indicators 통합 모듈 사용
from services.indicators import compute_indicators, _RENAME_MAP  # noqa: F401 (테스트 호환)


# v1 전략 → v2 레짐 매핑 (미니 앙상블)
REGIME_STRATEGY_MAP: dict[Regime, list[tuple[str, float]]] = {
    # TRENDING: 추세추종 전략 (ma, macd, obv 중심)
    Regime.TRENDING_UP: [
        ("ma_crossover", 0.20),
        ("macd_crossover", 0.25),
        ("obv_divergence", 0.20),
        ("bollinger_rsi", 0.20),
        ("bb_squeeze", 0.15),
    ],
    Regime.TRENDING_DOWN: [
        ("ma_crossover", 0.20),
        ("macd_crossover", 0.25),
        ("obv_divergence", 0.20),
        ("bollinger_rsi", 0.20),
        ("bb_squeeze", 0.15),
    ],
    # RANGING: 평균회귀 전략 (bollinger_rsi, rsi, stochastic 중심)
    Regime.RANGING: [
        ("bollinger_rsi", 0.30),
        ("rsi", 0.25),
        ("stochastic_rsi", 0.20),
        ("bb_squeeze", 0.25),
    ],
    # VOLATILE: 돌파 전략 (bb_squeeze, obv 중심)
    Regime.VOLATILE: [
        ("bb_squeeze", 0.30),
        ("obv_divergence", 0.25),
        ("bollinger_rsi", 0.20),
        ("rsi", 0.15),
        ("stochastic_rsi", 0.10),
    ],
}


class V1StrategyAdapter(RegimeStrategy):
    """v1 BaseStrategy 래퍼 → v2 RegimeStrategy 인터페이스.

    v1 Signal(BUY/SELL/HOLD) → v2 StrategyDecision(LONG/SHORT/FLAT).
    레짐별 미니 앙상블: 2-5개 v1 전략 가중 투표.
    """

    def __init__(self, strategies: dict, regime_map: dict):
        self._strategies = strategies  # {name: BaseStrategy instance}
        self._regime_map = regime_map  # {Regime: [(name, weight), ...]}

    @property
    def name(self) -> str:
        return "v1_ensemble"

    @property
    def target_regimes(self) -> list[Regime]:
        return list(self._regime_map.keys())

    async def evaluate(
        self,
        df_5m: pd.DataFrame,
        df_1h: pd.DataFrame,
        regime: RegimeState,
        current_position: Direction | None,
    ) -> StrategyDecision:
        group = self._regime_map.get(regime.regime, [])
        if not group:
            return self._hold(current_position)

        # v1 전략은 4h 캔들용 → 1h 데이터 사용 (5m 노이즈 회피)
        df = df_1h if len(df_1h) >= 25 else df_5m
        close = float(df["close"].iloc[-1]) if "close" in df.columns else 0.0
        atr = float(df["atr_14"].iloc[-1]) if "atr_14" in df.columns else 0.0

        # 더미 Ticker (v1 전략의 analyze()에 필요)
        ticker = Ticker(
            symbol="X/USDT", last=close, bid=close * 0.999, ask=close * 1.001,
            high=close * 1.01, low=close * 0.99, volume=1000.0,
            timestamp=datetime.now(timezone.utc),
        )

        buy_score = 0.0
        sell_score = 0.0
        total_weight = 0.0
        active_signals: list[Signal] = []

        for strat_name, weight in group:
            strat = self._strategies.get(strat_name)
            if strat is None:
                continue
            try:
                signal = await strat.analyze(df, ticker)
            except Exception:
                continue

            if signal.signal_type == SignalType.HOLD:
                continue  # HOLD = 기권

            total_weight += weight
            if signal.signal_type == SignalType.BUY:
                buy_score += weight * signal.confidence
            elif signal.signal_type == SignalType.SELL:
                sell_score += weight * signal.confidence
            active_signals.append(signal)

        if total_weight < 0.10:  # 참여 전략 부족
            return self._hold(current_position)

        buy_norm = buy_score / total_weight if total_weight > 0 else 0
        sell_norm = sell_score / total_weight if total_weight > 0 else 0

        # BUY vs SELL 판정
        if buy_norm > sell_norm and buy_norm > 0.3:
            conf = min(1.0, buy_norm)
            sizing = self._calc_sizing(conf, atr, close) if atr > 0 and close > 0 else 0.5
            return StrategyDecision(
                direction=Direction.LONG,
                confidence=conf,
                sizing_factor=sizing,
                stop_loss_atr=1.5,
                take_profit_atr=3.0,
                reason=f"v1 ensemble BUY: {buy_norm:.2f} vs {sell_norm:.2f}",
                strategy_name="v1_ensemble",
                indicators={"buy_score": buy_norm, "sell_score": sell_norm,
                             "active": len(active_signals)},
            )
        elif sell_norm > buy_norm and sell_norm > 0.3:
            conf = min(1.0, sell_norm)
            sizing = self._calc_sizing(conf, atr, close) if atr > 0 and close > 0 else 0.5
            return StrategyDecision(
                direction=Direction.SHORT,
                confidence=conf,
                sizing_factor=sizing,
                stop_loss_atr=1.5,
                take_profit_atr=3.0,
                reason=f"v1 ensemble SELL: {sell_norm:.2f} vs {buy_norm:.2f}",
                strategy_name="v1_ensemble",
                indicators={"buy_score": buy_norm, "sell_score": sell_norm,
                             "active": len(active_signals)},
            )

        return self._hold(current_position)

    def _hold(self, current_position: Direction | None) -> StrategyDecision:
        return StrategyDecision(
            direction=current_position or Direction.FLAT,
            confidence=0.5,
            sizing_factor=0.0,
            stop_loss_atr=0,
            take_profit_atr=0,
            reason="v1_ensemble_hold",
            strategy_name="v1_ensemble",
        )


def create_v1_strategies() -> dict:
    """v1 전략 인스턴스 생성."""
    from strategies.bollinger_rsi import BollingerRSIStrategy
    from strategies.rsi_strategy import RSIStrategy
    from strategies.ma_crossover import MACrossoverStrategy
    from strategies.macd_crossover import MACDCrossoverStrategy
    from strategies.stochastic_rsi import StochasticRSIStrategy
    from strategies.obv_divergence import OBVDivergenceStrategy
    from strategies.bb_squeeze import BBSqueezeStrategy
    return {
        "bollinger_rsi": BollingerRSIStrategy(),
        "rsi": RSIStrategy(),
        "ma_crossover": MACrossoverStrategy(),
        "macd_crossover": MACDCrossoverStrategy(),
        "stochastic_rsi": StochasticRSIStrategy(),
        "obv_divergence": OBVDivergenceStrategy(),
        "bb_squeeze": BBSqueezeStrategy(),
    }


def create_spot_strategies() -> dict:
    """현물 4전략 인스턴스 생성."""
    from strategies.cis_momentum import CISMomentumStrategy
    from strategies.bnf_deviation import BNFDeviationStrategy
    from strategies.donchian_channel import DonchianChannelStrategy
    from strategies.larry_williams import LarryWilliamsStrategy
    return {
        "cis_momentum": CISMomentumStrategy(),
        "bnf_deviation": BNFDeviationStrategy(),
        "donchian_channel": DonchianChannelStrategy(),
        "larry_williams": LarryWilliamsStrategy(),
    }


# 현물 4전략 가중치 (combiner.py SPOT_WEIGHTS 복제)
SPOT_WEIGHTS: dict[str, float] = {
    "cis_momentum": 0.42,
    "bnf_deviation": 0.25,
    "donchian_channel": 0.23,
    "larry_williams": 0.10,
}

# 현물 전략 SL/TP/트레일링 ATR 배수 (SpotEvaluator 라이브 설정 일치)
SPOT_SL_ATR = 5.0
SPOT_TP_ATR = 14.0
SPOT_TRAIL_ACTIVATION_ATR = 3.0
SPOT_TRAIL_STOP_ATR = 1.5
SPOT_MIN_CONFIDENCE = 0.50

# ── 현물 시장 상태 감지 (라이브 _detect_market_state 포트) ──
SPOT_MARKET_STATES = ("strong_uptrend", "uptrend", "sideways", "downtrend", "crash")


def _detect_market_state_bt(df: pd.DataFrame) -> tuple[str, float]:
    """5요소 시장 상태 감지 (라이브 trading_engine._detect_market_state 포트).

    4h BTC 캔들 기반. 백테스트용 uppercase 컬럼 지원.
    """
    if df is None or len(df) < 60:
        return "sideways", 0.3

    scores = {"strong_uptrend": 0.0, "uptrend": 0.0, "sideways": 0.0, "downtrend": 0.0}
    row = df.iloc[-1]
    price = float(row["close"])

    # 1. Price vs SMA20
    sma20_col = "SMA_20" if "SMA_20" in df.columns else "sma_20"
    sma20 = row.get(sma20_col)
    if sma20 is not None and not pd.isna(sma20):
        sma20 = float(sma20)
        if sma20 > 0:
            dist = (price - sma20) / sma20
            if dist > 0.05:
                scores["strong_uptrend"] += 2
            elif dist > 0.01:
                scores["uptrend"] += 1.5
            elif dist < -0.05:
                scores["downtrend"] += 2
            elif dist < -0.01:
                scores["downtrend"] += 1.5
            else:
                scores["sideways"] += 1

    # 2. SMA20 vs SMA50
    sma50_col = "SMA_50" if "SMA_50" in df.columns else "sma_50"
    sma50 = row.get(sma50_col)
    if sma20 is not None and sma50 is not None and not pd.isna(sma20) and not pd.isna(sma50):
        if float(sma20) > float(sma50):
            scores["uptrend"] += 1
            scores["strong_uptrend"] += 0.5
        else:
            scores["downtrend"] += 1

    # 3. RSI
    rsi_col = "RSI_14" if "RSI_14" in df.columns else "rsi_14"
    rsi = row.get(rsi_col)
    if rsi is not None and not pd.isna(rsi):
        rsi = float(rsi)
        if rsi > 70:
            scores["strong_uptrend"] += 1
        elif rsi > 55:
            scores["uptrend"] += 1
        elif rsi < 30:
            scores["downtrend"] += 1.5
        elif rsi < 45:
            scores["downtrend"] += 1
        else:
            scores["sideways"] += 1.5

    # 4. 7일 변동 (4h=42캔들)
    lookback_idx = max(0, len(df) - 1 - 42)
    week_ago = float(df.iloc[lookback_idx]["close"])
    if week_ago > 0:
        chg = (price - week_ago) / week_ago * 100
        if chg > 10:
            scores["strong_uptrend"] += 2
        elif chg > 3:
            scores["uptrend"] += 1.5
        elif chg < -10:
            scores["downtrend"] += 2
        elif chg < -3:
            scores["downtrend"] += 1.5
        else:
            scores["sideways"] += 2

    # 5. 거래량
    vol_sma_col = "VOLUME_SMA_20" if "VOLUME_SMA_20" in df.columns else "volume_sma_20"
    vol_sma = row.get(vol_sma_col)
    vol = row.get("volume")
    if vol_sma is not None and vol is not None and not pd.isna(vol_sma) and not pd.isna(vol):
        if float(vol_sma) > 0:
            vr = float(vol) / float(vol_sma)
            if vr > 2.0:
                o = row.get("open")
                if o is not None and float(o) > 0:
                    if price >= float(o):
                        scores["strong_uptrend"] += 0.5
                    else:
                        scores["downtrend"] += 0.5

    best = max(scores, key=scores.get)
    total = sum(scores.values())
    conf = scores[best] / total if total > 0 else 0.3

    if best == "downtrend" and conf >= 0.55 and scores["downtrend"] >= 5.0:
        return "crash", round(conf, 2)
    return best, round(conf, 2)


class SpotStrategyAdapter(RegimeStrategy):
    """현물 4전략 → v2 RegimeStrategy 인터페이스 어댑터.

    현물 4전략(cis_momentum, bnf_deviation, donchian_channel, larry_williams)을
    SignalCombiner(SPOT_WEIGHTS)로 가중 투표하여 선물 백테스트에 적용.

    라이브 SpotEvaluator와 동일한 로직:
    - 4h 캔들 기반 (1h 데이터에서 합성)
    - BUY → LONG, SELL → SHORT 매핑
    - SL 5.0 / TP 14.0 ATR 배수
    """

    def __init__(
        self,
        strategies: dict,
        weights: dict[str, float],
        min_confidence: float = SPOT_MIN_CONFIDENCE,
    ):
        self._strategies = strategies  # {name: BaseStrategy instance}
        self._combiner = _create_spot_combiner(weights, min_confidence)
        self._min_confidence = min_confidence

    @property
    def name(self) -> str:
        return "spot_ensemble"

    @property
    def target_regimes(self) -> list[Regime]:
        return [Regime.TRENDING_UP, Regime.TRENDING_DOWN, Regime.RANGING, Regime.VOLATILE]

    async def evaluate(
        self,
        df_5m: pd.DataFrame,
        df_1h: pd.DataFrame,
        regime: RegimeState,
        current_position: Direction | None,
    ) -> StrategyDecision:
        """현물 4전략 가중 투표로 방향 결정.

        1h 데이터를 4h로 리샘플링하여 현물 전략에 전달.
        """
        # 1h → 4h 리샘플링
        df_4h = self._resample_1h_to_4h(df_1h)
        if df_4h is None or len(df_4h) < 30:
            return self._hold(current_position)

        close = float(df_4h["close"].iloc[-1]) if "close" in df_4h.columns else 0.0
        atr = float(df_4h["atr_14"].iloc[-1]) if "atr_14" in df_4h.columns else 0.0

        # 더미 Ticker (spot 전략 analyze() 인터페이스 요구)
        ticker = Ticker(
            symbol="X/USDT", last=close, bid=close * 0.999, ask=close * 1.001,
            high=close * 1.01, low=close * 0.99, volume=1000.0,
            timestamp=datetime.now(timezone.utc),
        )

        # 4전략 시그널 수집
        signals: list[Signal] = []
        for strat_name, strat in self._strategies.items():
            try:
                signal = await strat.analyze(df_4h, ticker)
                signals.append(signal)
            except Exception:
                continue

        if not signals:
            return self._hold(current_position)

        # SignalCombiner 가중 투표
        combined = self._combiner.combine(signals)

        # BUY → LONG, SELL → SHORT 매핑
        if combined.action == SignalType.BUY:
            conf = min(1.0, combined.combined_confidence)
            if conf < self._min_confidence:
                return self._hold(current_position)
            sizing = self._calc_sizing(conf, atr, close) if atr > 0 and close > 0 else 0.5
            return StrategyDecision(
                direction=Direction.LONG,
                confidence=conf,
                sizing_factor=sizing,
                stop_loss_atr=SPOT_SL_ATR,
                take_profit_atr=SPOT_TP_ATR,
                reason=f"spot_buy: {combined.final_reason}",
                strategy_name="spot_ensemble",
                indicators={"buy_conf": conf, "active": len(signals)},
            )
        elif combined.action == SignalType.SELL:
            conf = min(1.0, combined.combined_confidence)
            if conf < self._min_confidence:
                return self._hold(current_position)
            sizing = self._calc_sizing(conf, atr, close) if atr > 0 and close > 0 else 0.5
            return StrategyDecision(
                direction=Direction.SHORT,
                confidence=conf,
                sizing_factor=sizing,
                stop_loss_atr=SPOT_SL_ATR,
                take_profit_atr=SPOT_TP_ATR,
                reason=f"spot_sell: {combined.final_reason}",
                strategy_name="spot_ensemble",
                indicators={"sell_conf": conf, "active": len(signals)},
            )

        return self._hold(current_position)

    def _hold(self, current_position: Direction | None) -> StrategyDecision:
        return StrategyDecision(
            direction=current_position or Direction.FLAT,
            confidence=0.5,
            sizing_factor=0.0,
            stop_loss_atr=0,
            take_profit_atr=0,
            reason="spot_ensemble_hold",
            strategy_name="spot_ensemble",
        )

    @staticmethod
    def _resample_1h_to_4h(df_1h: pd.DataFrame) -> pd.DataFrame | None:
        """1h 캔들을 4h로 리샘플링 + 인디케이터 재계산."""
        if df_1h is None or len(df_1h) < 40:
            return None

        df = df_1h.copy()

        # OHLCV 리샘플링
        ohlcv = df[["open", "high", "low", "close", "volume"]].resample("4h").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna()

        if len(ohlcv) < 30:
            return None

        # COIN-52: 통합 지표 계산 파이프라인 사용
        ohlcv = compute_indicators(ohlcv)

        # sma_200 등 장기 지표는 데이터 부족 시 NaN — 전략에 필요한 지표만 기준으로 dropna
        _core_cols = [c for c in ["ema_20", "rsi_14", "atr_14", "sma_20", "sma_50", "sma_60"] if c in ohlcv.columns]
        ohlcv.dropna(subset=_core_cols, inplace=True)
        return ohlcv


def _create_spot_combiner(
    weights: dict[str, float],
    min_confidence: float = SPOT_MIN_CONFIDENCE,
):
    """현물 전략용 SignalCombiner 생성."""
    from strategies.combiner import SignalCombiner
    return SignalCombiner(
        strategy_weights=weights,
        min_confidence=min_confidence,
    )


def _tf_hours(tf: str) -> float:
    return {"1m": 1/60, "5m": 5/60, "15m": 15/60, "1h": 1, "4h": 4, "1d": 24}.get(tf, 1)


# ── 데이터 수집 ──────────────────────────────────────────────
async def fetch_ohlcv_cached(
    exchange,
    symbol: str,
    timeframe: str,
    days: int,
) -> pd.DataFrame:
    """OHLCV 데이터를 가져와 CSV 캐싱. 기존 fetch_history 패턴 재사용."""
    candles_needed = int(days * 24 / _tf_hours(timeframe)) + 200
    tf_ms = int(_tf_hours(timeframe) * 3600 * 1000)

    cache_dir = Path(__file__).parent / ".cache"
    cache_dir.mkdir(exist_ok=True)
    safe_symbol = symbol.replace("/", "_")
    cache_path = cache_dir / f"{safe_symbol}_{timeframe}.csv"

    cached_df = None
    last_cached_ts = 0
    if cache_path.exists():
        cached_df = pd.read_csv(
            cache_path, parse_dates=["timestamp"], index_col="timestamp",
        )
        # 손상된 인덱스 행 제거 (파싱 실패 시 Index가 object dtype이 됨)
        if not isinstance(cached_df.index, pd.DatetimeIndex):
            cached_df.index = pd.to_datetime(cached_df.index, errors="coerce", utc=True)
            cached_df = cached_df[cached_df.index.notna()]
        if cached_df.index.tz is None:
            cached_df.index = cached_df.index.tz_localize("UTC")
        cached_df.sort_index(inplace=True)
        last_cached_ts = int(cached_df.index[-1].timestamp() * 1000)

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - candles_needed * tf_ms

    # 캐시가 충분히 오래된 데이터를 가지고 있는지 확인
    needs_old_data = True
    if cached_df is not None:
        first_cached_ts = int(cached_df.index[0].timestamp() * 1000)
        if first_cached_ts <= start_ms + tf_ms * 10:  # 10캔들 허용 오차
            needs_old_data = False

    if cached_df is not None and not needs_old_data and last_cached_ts > start_ms:
        fetch_since = last_cached_ts + tf_ms
    else:
        fetch_since = start_ms

    # 페이지네이션 루프
    all_new: list[Candle] = []
    page_limit = 1000
    cursor = fetch_since

    while cursor < now_ms:
        raw = await exchange.fetch_ohlcv(
            symbol, timeframe, limit=page_limit, since=cursor,
        )
        if not raw:
            break
        all_new.extend(raw)
        last_ts = int(raw[-1].timestamp.timestamp() * 1000)
        if last_ts <= cursor:
            break
        cursor = last_ts + tf_ms
        if len(raw) < page_limit * 0.9:
            break

    # 새 데이터를 DataFrame으로
    if all_new:
        new_df = pd.DataFrame([{
            "timestamp": c.timestamp,
            "open": c.open, "high": c.high,
            "low": c.low, "close": c.close, "volume": c.volume,
        } for c in all_new])
        new_df.set_index("timestamp", inplace=True)
        new_df.sort_index(inplace=True)

        if cached_df is not None:
            df = pd.concat([cached_df, new_df])
            df = df[~df.index.duplicated(keep="last")]
            df.sort_index(inplace=True)
        else:
            df = new_df
    elif cached_df is not None:
        df = cached_df
    else:
        raise ValueError(f"{symbol} {timeframe} 데이터 없음")

    df.to_csv(cache_path)
    return df


def compute_v2_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """v2 전략 + v1 전략에 필요한 기술적 지표 계산.

    COIN-52: services.indicators.compute_indicators()로 위임.
    """
    df = df.copy()

    # 통합 지표 계산 파이프라인 사용
    df = compute_indicators(df)

    # sma_200 등 장기 지표는 데이터 부족 시 NaN — 전략에 필요한 지표만 기준으로 dropna
    _core_cols = [c for c in ["ema_20", "rsi_14", "atr_14", "sma_20", "sma_50", "sma_60"] if c in df.columns]
    df.dropna(subset=_core_cols, inplace=True)

    # 날짜 필터
    cutoff = datetime.now(timezone.utc) - timedelta(days=1000)
    if df.index.tz is not None:
        cutoff = cutoff.replace(tzinfo=df.index.tz)
    df = df[df.index >= cutoff]

    return df


# ── 데이터 클래스 ────────────────────────────────────────────

@dataclass
class V2Position:
    symbol: str
    direction: Direction
    quantity: float
    entry_price: float
    margin: float
    leverage: int
    sl_price: float
    tp_price: float
    trail_activation_price: float
    trail_stop_atr: float
    extreme_price: float
    atr_at_entry: float
    entered_idx: int
    strategy_name: str
    trailing_active: bool = False
    trail_stop_price: float | None = None
    tier: str = "tier1"
    entry_ts: datetime | None = None


@dataclass
class Tier2Signal:
    """Tier2 스캔 결과."""
    symbol: str
    score: float
    direction: Direction
    vol_ratio: float
    price_chg_pct: float
    rsi: float


@dataclass
class V2Trade:
    symbol: str
    direction: str       # "long" or "short"
    entry_price: float
    exit_price: float
    quantity: float
    margin: float
    pnl: float
    pnl_pct: float
    entry_time: datetime
    exit_time: datetime
    exit_reason: str
    strategy_name: str
    regime: str


@dataclass
class V2BacktestResult:
    coins: list[str]
    days: int
    initial_balance: float
    final_balance: float
    total_pnl: float
    total_pnl_pct: float
    max_drawdown_pct: float
    total_trades: int
    long_trades: int
    short_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float
    sharpe_ratio: float
    total_fees: float
    total_funding: float
    buy_hold_pnl_pct: float
    trades: list[V2Trade] = field(default_factory=list)
    equity_curve: list[tuple] = field(default_factory=list)
    regime_distribution: dict[str, int] = field(default_factory=dict)
    coin_stats: dict[str, dict] = field(default_factory=dict)
    # 회피 모드 (D) + 과열 필터 (C) 통계
    avoidance_daily_pauses: int = 0
    avoidance_consec_pauses: int = 0
    funding_blocks: int = 0
    funding_size_reductions: int = 0


# ── 백테스터 ─────────────────────────────────────────────────

class V2Backtester:

    def __init__(
        self,
        exchange,
        coins: list[str],
        leverage: int = 3,
        initial_balance: float = 1000.0,
        max_position_pct: float = 0.15,
        base_risk_pct: float = BASE_RISK_PCT,
        cooldown_candles: int = 36,    # 3시간 (5m × 36)
        min_confidence: float = 0.5,   # 최소 신뢰도
        trending_only: bool = False,   # True = TRENDING 레짐에서만 거래
        eval_interval: int = 12,       # 전략 평가 주기 (5m 캔들 수, 12=1h)
        regime_confirm: int = 2,       # 레짐 전환 연속 확인 횟수
        regime_min_hours: int = 3,     # 레짐 최소 유지 시간
        regime_adx_enter: float = 27.0,  # 추세 진입 ADX 임계값
        regime_adx_exit: float = 23.0,   # 추세 이탈 ADX 임계값
    ):
        self._exchange = exchange
        self._coins = coins
        self._leverage = leverage
        self._initial_balance = initial_balance
        self._max_position_pct = max_position_pct
        self._base_risk_pct = base_risk_pct
        self._cooldown_candles = cooldown_candles
        self._min_confidence = min_confidence
        self._trending_only = trending_only
        self._eval_interval = eval_interval
        self._use_v1 = False
        self._v1_adapter: V1StrategyAdapter | None = None
        self._use_spot = False
        self._spot_adapter: SpotStrategyAdapter | None = None
        self._use_tier2 = False
        self._tier2_coins: list[str] = []
        self._tier1_disabled = False
        self._us_open_filter: str = "off"  # "off", "no_long", "no_entry", "half_size"
        self._tight_leverage: bool = False  # 타이트 ATR 레버리지 스케일링
        self._divergence_filter: bool = False  # 히든 다이버전스 진입 필터
        self._divergence_lookback: int = 20   # 다이버전스 룩백 캔들 수
        self._cascade_contrarian: bool = False  # 캐스케이드 역추세 진입
        # 현물 비대칭 모드 (시장 상태 기반 매수 차단)
        self._spot_asymmetric: bool = False
        self._spot_hysteresis_candles: int = 0  # 히스테리시스: bearish→non-bearish 후 N캔들 매수 금지

        # 회피 모드 (D) — 일일 손실 한도 + 연속 손실 정지
        self._daily_loss_limit_pct: float = 0.0   # 0 = 비활성, e.g. 0.02 = 2%
        self._consecutive_loss_pause: int = 0     # 0 = 비활성, e.g. 3 = 3연패 시 정지
        self._consecutive_loss_pause_candles: int = 12  # 정지 기간 (5m, 12=1h)

        # 과열 필터 (C) — 펀딩비 극단 시 진입 차단/축소
        self._funding_filter: bool = False
        self._funding_extreme_threshold: float = 0.001  # 0.1% — 진입 완전 차단
        self._funding_high_threshold: float = 0.0005    # 0.05% — 사이즈 50% 축소
        self._funding_rsi_block: float = 80.0            # RSI > 이 값이면 롱 차단
        self._funding_rsi_reduce: float = 70.0           # RSI > 이 값이면 롱 축소
        self._funding_filter_regimes: set | None = None  # None=전체, set=해당 레짐에서만 적용

        # 연속 손실 사이즈 축소 (정지 대신)
        self._consecutive_loss_size_reduce: int = 0   # 0=비활성, e.g. 3 = 3연패 시 사이즈 50%
        self._consecutive_loss_reduce_pct: float = 0.5  # 축소 비율

        # 레짐 감지 파라미터 저장 (_precompute_regimes에서도 사용)
        self._regime_confirm = regime_confirm
        self._regime_min_hours = regime_min_hours
        self._regime_adx_enter = regime_adx_enter
        self._regime_adx_exit = regime_adx_exit

        self._regime_detector = RegimeDetector(
            confirm_count=regime_confirm,
            min_duration_h=regime_min_hours,
            adx_enter=regime_adx_enter,
            adx_exit=regime_adx_exit,
        )
        self._strategy_selector = StrategySelector()

    def enable_v1_strategies(self) -> None:
        """v1 전략 미니 앙상블 모드 활성화."""
        strategies = create_v1_strategies()
        self._v1_adapter = V1StrategyAdapter(strategies, REGIME_STRATEGY_MAP)
        self._use_v1 = True

    def enable_spot_strategies(self) -> None:
        """현물 4전략 SignalCombiner 가중 투표 모드 활성화."""
        strategies = create_spot_strategies()
        self._spot_adapter = SpotStrategyAdapter(strategies, SPOT_WEIGHTS)
        self._use_spot = True

    def enable_tier2(self, tier2_coins: list[str] | None = None) -> None:
        """Tier 2 서지 스캔 활성화."""
        self._use_tier2 = True
        self._tier2_coins = tier2_coins or TIER2_COINS_DEFAULT

    async def prefetch(self, days: int) -> dict[str, tuple[pd.DataFrame, pd.DataFrame]]:
        """모든 코인의 5m + 1h 데이터 프리페치."""
        result = {}
        for symbol in self._coins:
            try:
                print(f"  {symbol} 데이터 로딩...")
                df_5m_raw = await fetch_ohlcv_cached(self._exchange, symbol, "5m", days)
                df_1h_raw = await fetch_ohlcv_cached(self._exchange, symbol, "1h", days)

                df_5m = compute_v2_indicators(df_5m_raw)
                df_1h = compute_v2_indicators(df_1h_raw)

                print(f"    5m: {len(df_5m):,}캔들 | 1h: {len(df_1h):,}캔들")
                result[symbol] = (df_5m, df_1h)
            except Exception as e:
                print(f"    {symbol} 실패: {e}")

        # Tier2 전용 코인: 5m만 (1h 불필요)
        if self._use_tier2:
            tier2_only = [c for c in self._tier2_coins if c not in result]
            if tier2_only:
                print(f"\n  Tier2 전용 코인 ({len(tier2_only)}개) 로딩...")
            for symbol in tier2_only:
                try:
                    df_5m_raw = await fetch_ohlcv_cached(self._exchange, symbol, "5m", days)
                    df_5m = compute_v2_indicators(df_5m_raw)
                    print(f"    {symbol} 5m: {len(df_5m):,}캔들")
                    result[symbol] = (df_5m, pd.DataFrame())
                except Exception as e:
                    print(f"    {symbol} 실패: {e}")
        return result

    async def run(self, days: int) -> V2BacktestResult:
        """v2 백테스트 실행."""
        mode = "현물 4전략" if self._use_spot else ("v1 7전략" if self._use_v1 else "v2 레짐")
        print(f"\n{'='*60}")
        print(f"  FuturesEngine V2 백테스트 | {mode} | 5m+1h | {days}일")
        print(f"  코인: {', '.join(self._coins)}")
        print(f"  레버리지: {self._leverage}x | 수수료: {FUTURES_FEE*100:.2f}%")
        print(f"  최대 포지션: {self._max_position_pct*100:.0f}% | 리스크: {self._base_risk_pct*100:.0f}%")
        print(f"  쿨다운: {self._cooldown_candles}캔들 ({self._cooldown_candles*5}분) | 최소 신뢰도: {self._min_confidence}")
        if self._use_spot:
            print("  현물 전략: cis_momentum(0.42), bnf_deviation(0.25), donchian_channel(0.23), larry_williams(0.10)")
            print(f"  SL/TP: {SPOT_SL_ATR}/{SPOT_TP_ATR} ATR | Trail: {SPOT_TRAIL_ACTIVATION_ATR}/{SPOT_TRAIL_STOP_ATR} ATR")
        if self._use_tier2:
            print(f"  Tier2: {len(self._tier2_coins)}코인 | SL{TIER2_SL_PCT}%/TP{TIER2_TP_PCT}% | "
                  f"Trail {TIER2_TRAIL_ACTIVATION_PCT}%/{TIER2_TRAIL_STOP_PCT}% | "
                  f"Max{TIER2_MAX_CONCURRENT}동시 | CD{TIER2_COOLDOWN_CANDLES*5}분")
        print(f"{'='*60}")

        all_data = await self.prefetch(days)
        if not all_data:
            raise ValueError("사용 가능한 데이터 없음")

        return await self._simulate(all_data, days)

    async def _simulate(
        self,
        all_data: dict[str, tuple[pd.DataFrame, pd.DataFrame]],
        days: int,
    ) -> V2BacktestResult:
        """메인 시뮬레이션 루프."""
        # 유니온 5m 타임스탬프 (데이터 전체 사용 — 이미 prefetch/WF에서 슬라이싱됨)
        all_ts = sorted(set().union(*(
            df5m.index for df5m, _ in all_data.values()
        )))
        if not all_ts:
            raise ValueError("날짜 범위에 데이터 없음")

        print(f"\n  타임라인: {len(all_ts):,}개 5m 캔들 ({all_ts[0].date()} ~ {all_ts[-1].date()})")

        # 1h 레짐 사전 계산 (코인별, Tier2 전용 코인은 1h 없으므로 건너뜀)
        regimes_per_coin: dict[str, list[tuple[datetime, RegimeState]]] = {}
        for sym, (_, df_1h) in all_data.items():
            if len(df_1h) > 0:
                regimes_per_coin[sym] = self._precompute_regimes(df_1h)
        # BTC 기준 글로벌 레짐 (에쿼티 보고용)
        btc_key = "BTC/USDT" if "BTC/USDT" in all_data else list(all_data.keys())[0]
        regimes_by_hour = regimes_per_coin[btc_key]

        # 현물 비대칭: BTC 1h → 4h 리샘플링 + 지표 계산 (사전 준비)
        btc_4h_for_state: pd.DataFrame | None = None
        if self._spot_asymmetric and self._use_spot and "BTC/USDT" in all_data:
            _, btc_1h = all_data["BTC/USDT"]
            if len(btc_1h) > 0:
                import pandas_ta as pta
                btc_4h_for_state = btc_1h.resample("4h").agg({
                    "open": "first", "high": "max", "low": "min",
                    "close": "last", "volume": "sum",
                }).dropna(subset=["close"])
                btc_4h_for_state["SMA_20"] = pta.sma(btc_4h_for_state["close"], length=20)
                btc_4h_for_state["SMA_50"] = pta.sma(btc_4h_for_state["close"], length=50)
                btc_4h_for_state["RSI_14"] = pta.rsi(btc_4h_for_state["close"], length=14)
                btc_4h_for_state["VOLUME_SMA_20"] = pta.sma(btc_4h_for_state["volume"], length=20)
                print(f"  현물 비대칭: BTC 4h 캔들 {len(btc_4h_for_state)}개 준비")

        # 초기 상태
        cash = self._initial_balance
        positions: dict[str, V2Position] = {}
        trades: list[V2Trade] = []
        equity_curve: list[tuple] = []
        peak_equity = self._initial_balance
        max_drawdown = 0.0
        total_fees = 0.0
        total_funding = 0.0
        regime_counts: dict[str, int] = {}

        # 코인별 통계
        coin_stats: dict[str, dict] = {
            sym: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0,
                  "long_wins": 0, "long_losses": 0, "short_wins": 0, "short_losses": 0}
            for sym in all_data
        }

        # 쿨다운 추적 (코인별 마지막 청산 캔들 인덱스)
        last_exit_idx: dict[str, int] = {}

        # 현물 비대칭 모드: 시장 상태 추적
        spot_market_state = "sideways"
        spot_bearish_clear_idx = -99999  # 마지막으로 bearish→non-bearish 된 캔들 idx
        spot_state_eval_interval = 6     # 30분(5m×6)마다 시장 상태 갱신

        # 진입 가격 기록 (B&H 비교용)
        first_prices: dict[str, float] = {}
        last_prices: dict[str, float] = {}

        candles_per_8h = 8 * 12  # 5m 캔들 96개 = 8h

        # Tier 2 상태
        tier2_cooldowns: dict[str, int] = {}  # symbol → 쿨다운 만료 candle_idx
        tier2_daily_trades = 0
        tier2_consecutive_sl: dict[str, int] = {}  # symbol → 연속 SL 횟수
        tier2_long_cooldowns: dict[str, int] = {}   # 연속 SL 장기 쿨다운 만료 idx

        # 캐스케이드 역추세 상태
        cascade_cooldowns: dict[str, int] = {}  # symbol → 쿨다운 만료 candle_idx
        last_reset_day = None

        # 회피 모드 (D) 상태
        daily_realized_pnl = 0.0           # 당일 실현 PnL
        daily_loss_paused = False          # 일일 손실 한도 초과 플래그
        consecutive_losses = 0             # 연속 손실 횟수
        consecutive_loss_resume_idx = 0    # 연속 손실 정지 해제 캔들 idx
        avoidance_daily_pauses = 0         # 통계: 일일 손실 한도 발동 횟수
        avoidance_consec_pauses = 0        # 통계: 연속 손실 정지 발동 횟수

        # 과열 필터 (C) 상태
        funding_blocks = 0                 # 통계: 펀딩 과열 차단 횟수
        funding_size_reductions = 0        # 통계: 펀딩 사이즈 축소 횟수

        print("  시뮬레이션 진행 중...")

        for candle_idx, ts in enumerate(all_ts):
            # ─── 에쿼티 계산 ───
            equity = cash
            for sym, pos in positions.items():
                if sym in all_data:
                    df5m, _ = all_data[sym]
                    if ts in df5m.index:
                        price = float(df5m.loc[ts, "close"])
                        unrealized = self._calc_pnl(pos.direction, pos.entry_price, price, pos.quantity)
                        equity += pos.margin + unrealized

            equity_curve.append((ts, equity))
            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
            if dd > max_drawdown:
                max_drawdown = dd

            # 워밍업 (60캔들 = 5시간)
            if candle_idx < LOOKBACK_WINDOW:
                continue

            # ─── 현물 시장 상태 갱신 (30분 주기, 4h BTC 리샘플 데이터 사용) ───
            if self._spot_asymmetric and self._use_spot and btc_4h_for_state is not None and candle_idx % spot_state_eval_interval == 0:
                h4_idx = btc_4h_for_state.index.searchsorted(ts, side="right")
                if h4_idx > 60:
                    btc_4h_window = btc_4h_for_state.iloc[max(0, h4_idx - 100):h4_idx]
                    old_state = spot_market_state
                    spot_market_state, _ = _detect_market_state_bt(btc_4h_window)
                    if old_state in ("crash", "downtrend") and spot_market_state not in ("crash", "downtrend"):
                        spot_bearish_clear_idx = candle_idx

            # ─── 레짐 조회 ───
            regime = self._get_regime_at(regimes_by_hour, ts)
            if regime:
                regime_name = regime.regime.value
                regime_counts[regime_name] = regime_counts.get(regime_name, 0) + 1

            # ─── 펀딩비 (8시간마다) ───
            if candle_idx % candles_per_8h == 0:
                for sym, pos in positions.items():
                    if sym in all_data:
                        df5m, _ = all_data[sym]
                        if ts in df5m.index:
                            price = float(df5m.loc[ts, "close"])
                            notional = pos.quantity * price
                            if pos.direction == Direction.LONG:
                                funding = notional * FUNDING_RATE
                            else:
                                funding = -notional * FUNDING_RATE
                            cash -= funding
                            total_funding += funding

            # ─── 일일 카운터 리셋 ───
            day = ts.date() if hasattr(ts, 'date') else None
            if day and day != last_reset_day:
                if self._use_tier2:
                    tier2_daily_trades = 0
                # 회피 모드: 일일 PnL 리셋
                if self._daily_loss_limit_pct > 0:
                    daily_realized_pnl = 0.0
                    daily_loss_paused = False
                last_reset_day = day

            # ─── Tier1 코인별 평가 ───
            tier1_coins = [] if self._tier1_disabled else self._coins
            for sym in tier1_coins:
                if sym not in all_data:
                    continue
                df5m, df1h = all_data[sym]
                if ts not in df5m.index:
                    continue

                price = float(df5m.loc[ts, "close"])

                # B&H 가격 기록
                if sym not in first_prices:
                    first_prices[sym] = price
                last_prices[sym] = price

                has_position = sym in positions

                # ─── SL/TP/트레일링 체크 ───
                if has_position:
                    pos = positions[sym]
                    # 캐스케이드 포지션 max hold 체크
                    exit_reason = None
                    if pos.tier == "cascade" and candle_idx - pos.entered_idx >= CASCADE_MAX_HOLD_CANDLES:
                        exit_reason = "cascade_max_hold"
                    else:
                        exit_reason = self._check_stops(pos, price)
                    if exit_reason:
                        pnl, fee = self._close_position(pos, price)
                        cash += pos.margin + pnl - fee
                        total_fees += fee
                        trade = self._record_trade(pos, price, ts, exit_reason, pnl, regime)
                        trades.append(trade)
                        self._update_coin_stats(coin_stats, trade)
                        # 회피 모드 (D) PnL 추적
                        net_pnl = pnl - fee
                        daily_realized_pnl += net_pnl
                        if net_pnl < 0:
                            consecutive_losses += 1
                            if self._consecutive_loss_pause > 0 and consecutive_losses >= self._consecutive_loss_pause:
                                consecutive_loss_resume_idx = candle_idx + self._consecutive_loss_pause_candles
                                avoidance_consec_pauses += 1
                        else:
                            consecutive_losses = 0
                        if self._daily_loss_limit_pct > 0 and not daily_loss_paused:
                            if daily_realized_pnl < -(self._initial_balance * self._daily_loss_limit_pct):
                                daily_loss_paused = True
                                avoidance_daily_pauses += 1
                        if pos.tier == "cascade":
                            cascade_cooldowns[sym] = candle_idx + CASCADE_COOLDOWN_CANDLES
                        del positions[sym]
                        last_exit_idx[sym] = candle_idx
                        has_position = False
                    else:
                        # 트레일링 업데이트
                        self._update_trailing(pos, price)

                # ─── 전략 평가 (eval_interval 주기로만) ───
                if candle_idx % self._eval_interval != 0:
                    continue

                # 코인별 레짐 조회
                coin_regime = self._get_regime_at(
                    regimes_per_coin.get(sym, regimes_by_hour), ts,
                )
                if coin_regime is None:
                    continue

                # trending-only 모드: 비추세 레짐에서 기존 포지션 청산, 신규 진입 차단
                if self._trending_only and coin_regime.regime not in (Regime.TRENDING_UP, Regime.TRENDING_DOWN):
                    if sym in positions:
                        pos = positions[sym]
                        pnl, fee = self._close_position(pos, price)
                        cash += pos.margin + pnl - fee
                        total_fees += fee
                        trade = self._record_trade(pos, price, ts, "regime_exit", pnl, coin_regime)
                        trades.append(trade)
                        self._update_coin_stats(coin_stats, trade)
                        net_pnl = pnl - fee
                        daily_realized_pnl += net_pnl
                        if net_pnl < 0:
                            consecutive_losses += 1
                            if self._consecutive_loss_pause > 0 and consecutive_losses >= self._consecutive_loss_pause:
                                consecutive_loss_resume_idx = candle_idx + self._consecutive_loss_pause_candles
                                avoidance_consec_pauses += 1
                        else:
                            consecutive_losses = 0
                        if self._daily_loss_limit_pct > 0 and not daily_loss_paused:
                            if daily_realized_pnl < -(self._initial_balance * self._daily_loss_limit_pct):
                                daily_loss_paused = True
                                avoidance_daily_pauses += 1
                        del positions[sym]
                        last_exit_idx[sym] = candle_idx
                    continue

                if self._use_spot:
                    strategy = self._spot_adapter
                elif self._use_v1:
                    strategy = self._v1_adapter
                else:
                    strategy = self._strategy_selector.select(coin_regime.regime)
                current_dir = positions[sym].direction if sym in positions else None

                # 5m 윈도우 슬라이스
                idx = df5m.index.get_loc(ts)
                if isinstance(idx, slice):
                    idx = idx.start
                start = max(0, idx - LOOKBACK_WINDOW + 1)
                window_5m = df5m.iloc[start:idx + 1]

                # 1h 윈도우 (가장 가까운 1h 캔들까지)
                h_idx = df1h.index.searchsorted(ts, side="right")
                h_lookback = SPOT_1H_LOOKBACK if self._use_spot else LOOKBACK_WINDOW
                h_start = max(0, h_idx - h_lookback)
                window_1h = df1h.iloc[h_start:h_idx]

                if len(window_5m) < 21 or len(window_1h) < 5:
                    continue

                decision = await strategy.evaluate(window_5m, window_1h, coin_regime, current_dir)

                # ─── 시그널 처리 ───
                if decision.is_hold:
                    continue

                if decision.is_exit and sym in positions:
                    # 전략이 FLAT 시그널
                    pos = positions[sym]
                    pnl, fee = self._close_position(pos, price)
                    cash += pos.margin + pnl - fee
                    total_fees += fee
                    trade = self._record_trade(pos, price, ts, "strategy_exit", pnl, coin_regime)
                    trades.append(trade)
                    self._update_coin_stats(coin_stats, trade)
                    net_pnl = pnl - fee
                    daily_realized_pnl += net_pnl
                    if net_pnl < 0:
                        consecutive_losses += 1
                        if self._consecutive_loss_pause > 0 and consecutive_losses >= self._consecutive_loss_pause:
                            consecutive_loss_resume_idx = candle_idx + self._consecutive_loss_pause_candles
                            avoidance_consec_pauses += 1
                    else:
                        consecutive_losses = 0
                    if self._daily_loss_limit_pct > 0 and not daily_loss_paused:
                        if daily_realized_pnl < -(self._initial_balance * self._daily_loss_limit_pct):
                            daily_loss_paused = True
                            avoidance_daily_pauses += 1
                    del positions[sym]
                    last_exit_idx[sym] = candle_idx
                    continue

                if decision.is_entry:
                    # 최소 신뢰도 필터
                    if decision.confidence < self._min_confidence:
                        continue

                    # ─── 회피 모드 (D) 게이트 ───
                    if self._daily_loss_limit_pct > 0 and daily_loss_paused:
                        continue
                    if self._consecutive_loss_pause > 0 and candle_idx < consecutive_loss_resume_idx:
                        continue

                    # ─── 과열 필터 (C) — RSI 기반 펀딩 과열 프록시 ───
                    if self._funding_filter and "rsi_14" in window_5m.columns:
                        # 레짐 필터: 특정 레짐에서만 적용
                        apply_funding = True
                        if self._funding_filter_regimes is not None and coin_regime:
                            if coin_regime.regime.value not in self._funding_filter_regimes:
                                apply_funding = False
                        if apply_funding:
                            rsi_val = float(window_5m["rsi_14"].iloc[-1])
                            rsi_block = self._funding_rsi_block
                            rsi_reduce = self._funding_rsi_reduce
                            if decision.direction == Direction.LONG and rsi_val > rsi_block:
                                funding_blocks += 1
                                continue
                            if decision.direction == Direction.SHORT and rsi_val < (100 - rsi_block):
                                funding_blocks += 1
                                continue

                    # US 마켓 오픈 시간 필터
                    if self._us_open_filter != "off":
                        kst_hour = (ts + pd.Timedelta(hours=9)).hour
                        if kst_hour in US_OPEN_BLOCK_HOURS_KST:
                            if self._us_open_filter == "no_entry":
                                continue
                            elif self._us_open_filter == "no_long" and decision.direction == Direction.LONG:
                                continue

                    # 히든 다이버전스 필터
                    if self._divergence_filter:
                        if not self._detect_hidden_divergence(window_5m, decision.direction, self._divergence_lookback):
                            continue

                    # 현물 비대칭 모드: 하락장 매수 차단
                    if self._spot_asymmetric and self._use_spot and decision.direction == Direction.LONG:
                        if spot_market_state in ("crash", "downtrend"):
                            continue
                        # 히스테리시스: bearish 해제 후 N캔들 매수 금지
                        if self._spot_hysteresis_candles > 0:
                            if candle_idx < spot_bearish_clear_idx + self._spot_hysteresis_candles:
                                continue
                        # sideways 신뢰도 상향
                        if spot_market_state == "sideways":
                            if decision.confidence < self._min_confidence + 0.05:
                                continue

                    # SAR: 현재 포지션과 다른 방향 → 청산 후 신규 진입
                    if sym in positions and positions[sym].direction != decision.direction:
                        pos = positions[sym]
                        pnl, fee = self._close_position(pos, price)
                        cash += pos.margin + pnl - fee
                        total_fees += fee
                        trade = self._record_trade(pos, price, ts, "SAR", pnl, coin_regime)
                        trades.append(trade)
                        self._update_coin_stats(coin_stats, trade)
                        net_pnl = pnl - fee
                        daily_realized_pnl += net_pnl
                        if net_pnl < 0:
                            consecutive_losses += 1
                            if self._consecutive_loss_pause > 0 and consecutive_losses >= self._consecutive_loss_pause:
                                consecutive_loss_resume_idx = candle_idx + self._consecutive_loss_pause_candles
                                avoidance_consec_pauses += 1
                        else:
                            consecutive_losses = 0
                        if self._daily_loss_limit_pct > 0 and not daily_loss_paused:
                            if daily_realized_pnl < -(self._initial_balance * self._daily_loss_limit_pct):
                                daily_loss_paused = True
                                avoidance_daily_pauses += 1
                        del positions[sym]
                        last_exit_idx[sym] = candle_idx
                    elif sym not in positions:
                        # 신규 진입 시 쿨다운 체크 (SAR은 쿨다운 면제)
                        if sym in last_exit_idx:
                            elapsed = candle_idx - last_exit_idx[sym]
                            if elapsed < self._cooldown_candles:
                                continue

                    # 이미 같은 방향 포지션 있으면 스킵
                    if sym in positions:
                        continue

                    # 신규 진입
                    atr = float(window_5m["atr_14"].iloc[-1]) if "atr_14" in window_5m.columns else 0.0
                    if atr <= 0:
                        continue

                    # 레짐별 사이징 팩터 + ATR 레버리지 스케일링 (라이브 일치)
                    effective_leverage = self._leverage
                    if not self._use_spot:
                        effective_leverage = self._calc_atr_leverage(atr, price, self._leverage, self._tight_leverage)
                    margin = self._calc_margin(
                        decision, cash, price, atr,
                        regime=coin_regime.regime if not self._use_spot else None,
                    )
                    # US 오픈 half_size: 포지션 50% 축소
                    if self._us_open_filter == "half_size":
                        kst_hour = (ts + pd.Timedelta(hours=9)).hour
                        if kst_hour in US_OPEN_BLOCK_HOURS_KST:
                            margin *= 0.5
                    # 과열 필터 (C): 준과열 구간 사이즈 50% 축소
                    if self._funding_filter and "rsi_14" in window_5m.columns:
                        apply_reduce = True
                        if self._funding_filter_regimes is not None and coin_regime:
                            if coin_regime.regime.value not in self._funding_filter_regimes:
                                apply_reduce = False
                        if apply_reduce:
                            rsi_val = float(window_5m["rsi_14"].iloc[-1])
                            rsi_block = self._funding_rsi_block
                            rsi_reduce = self._funding_rsi_reduce
                            if (decision.direction == Direction.LONG and rsi_reduce < rsi_val <= rsi_block) or \
                               (decision.direction == Direction.SHORT and (100 - rsi_block) <= rsi_val < (100 - rsi_reduce)):
                                margin *= 0.5
                                funding_size_reductions += 1
                    # 연속 손실 사이즈 축소
                    if self._consecutive_loss_size_reduce > 0 and consecutive_losses >= self._consecutive_loss_size_reduce:
                        margin *= self._consecutive_loss_reduce_pct
                    if margin < MIN_MARGIN_USDT:
                        continue

                    quantity = margin * effective_leverage / price
                    fee = quantity * price * FUTURES_FEE
                    cash -= margin + fee
                    total_fees += fee

                    # 동적 SL 적용 (라이브 tier1_manager와 동일)
                    effective_sl = self._apply_dynamic_sl(
                        decision.stop_loss_atr,
                        coin_regime.regime if not self._use_spot else None,
                    )
                    sl_price, tp_price = self._calc_sl_tp(
                        decision.direction, price, atr,
                        effective_sl, decision.take_profit_atr,
                    )
                    # 트레일링: 라이브 기본값과 동일 (TP×0.5, SL×0.7)
                    if self._use_spot:
                        trail_act_mult = SPOT_TRAIL_ACTIVATION_ATR
                        trail_stop_mult = SPOT_TRAIL_STOP_ATR
                    else:
                        trail_act_mult = decision.take_profit_atr * 0.5  # 라이브: TP×0.5
                        trail_stop_mult = decision.stop_loss_atr * 0.7  # 라이브: SL×0.7
                    trail_act = price * (1 + trail_act_mult * atr / price) if decision.direction == Direction.LONG \
                        else price * (1 - trail_act_mult * atr / price)

                    positions[sym] = V2Position(
                        symbol=sym,
                        direction=decision.direction,
                        quantity=quantity,
                        entry_price=price,
                        margin=margin,
                        leverage=self._leverage,
                        sl_price=sl_price,
                        tp_price=tp_price,
                        trail_activation_price=trail_act,
                        trail_stop_atr=trail_stop_mult,
                        extreme_price=price,
                        atr_at_entry=atr,
                        entered_idx=candle_idx,
                        strategy_name=decision.strategy_name,
                    )

            # ─── 캐스케이드 역추세 (US 오픈 급락 시 롱 진입) ───
            if self._cascade_contrarian and candle_idx >= CASCADE_LOOKBACK_CANDLES:
                kst_hour = (ts + pd.Timedelta(hours=9)).hour
                if kst_hour in US_OPEN_BLOCK_HOURS_KST:
                    for sym in tier1_coins:
                        if sym in positions:
                            continue
                        # 캐스케이드 쿨다운
                        if candle_idx < cascade_cooldowns.get(sym, 0):
                            continue
                        if sym not in all_data:
                            continue
                        df5m_c, _ = all_data[sym]
                        if ts not in df5m_c.index:
                            continue
                        idx_loc_c = df5m_c.index.get_loc(ts)
                        if isinstance(idx_loc_c, slice):
                            idx_loc_c = idx_loc_c.start
                        if idx_loc_c < CASCADE_LOOKBACK_CANDLES:
                            continue
                        # 1시간 전 대비 하락률 체크
                        price_now = float(df5m_c.iloc[idx_loc_c]["close"])
                        price_1h_ago = float(df5m_c.iloc[idx_loc_c - CASCADE_LOOKBACK_CANDLES]["close"])
                        if price_1h_ago <= 0:
                            continue
                        drop_pct = (price_1h_ago - price_now) / price_1h_ago * 100
                        if drop_pct < CASCADE_DROP_THRESHOLD:
                            continue
                        # RSI 과매도 근처인지 확인 (추가 필터)
                        rsi_col = "rsi_14" if "rsi_14" in df5m_c.columns else None
                        if rsi_col:
                            rsi_val = float(df5m_c.iloc[idx_loc_c][rsi_col])
                            if rsi_val > 45:  # RSI 45 이하에서만 역추세
                                continue
                        # 역추세 롱 진입
                        c_margin = cash * CASCADE_POSITION_PCT
                        if c_margin < MIN_MARGIN_USDT:
                            continue
                        c_quantity = (c_margin * self._leverage) / price_now
                        c_fee = c_quantity * price_now * FUTURES_FEE
                        cash -= c_margin + c_fee
                        total_fees += c_fee
                        # %-기반 SL/TP (레버리지 반영)
                        sl_raw = CASCADE_SL_PCT / self._leverage / 100
                        tp_raw = CASCADE_TP_PCT / self._leverage / 100
                        trail_act_raw = CASCADE_TRAIL_ACT_PCT / self._leverage / 100
                        trail_stop_dist = price_now * (CASCADE_TRAIL_STOP_PCT / self._leverage / 100)
                        c_sl = price_now * (1 - sl_raw)
                        c_tp = price_now * (1 + tp_raw)
                        c_trail_act = price_now * (1 + trail_act_raw)
                        positions[sym] = V2Position(
                            symbol=sym,
                            direction=Direction.LONG,
                            quantity=c_quantity,
                            entry_price=price_now,
                            margin=c_margin,
                            leverage=self._leverage,
                            sl_price=c_sl,
                            tp_price=c_tp,
                            trail_activation_price=c_trail_act,
                            trail_stop_atr=trail_stop_dist,
                            extreme_price=price_now,
                            atr_at_entry=1.0,
                            entered_idx=candle_idx,
                            strategy_name="cascade_contrarian",
                            tier="cascade",
                            entry_ts=ts,
                        )
                        if sym not in coin_stats:
                            coin_stats[sym] = {
                                "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0,
                                "long_wins": 0, "long_losses": 0,
                                "short_wins": 0, "short_losses": 0,
                            }

            # ─── Tier 2 서지 스캔 ───
            if self._use_tier2 and candle_idx >= TIER2_VOL_LOOKBACK:
                # 1. Exit 체크 — 모든 Tier2 포지션
                for sym in list(positions.keys()):
                    pos = positions[sym]
                    if pos.tier != "tier2":
                        continue
                    if sym not in all_data:
                        continue
                    df5m_t2, _ = all_data[sym]
                    if ts not in df5m_t2.index:
                        continue
                    t2_price = float(df5m_t2.loc[ts, "close"])

                    # Max hold time
                    exit_reason_t2 = None
                    if candle_idx - pos.entered_idx >= TIER2_MAX_HOLD_CANDLES:
                        exit_reason_t2 = "t2_max_hold"
                    else:
                        exit_reason_t2 = self._check_tier2_stops(pos, t2_price)

                    if exit_reason_t2:
                        pnl, fee = self._close_position(pos, t2_price)
                        cash += pos.margin + pnl - fee
                        total_fees += fee
                        trade = self._record_trade(pos, t2_price, ts, exit_reason_t2, pnl, regime)
                        trades.append(trade)
                        self._update_coin_stats(coin_stats, trade)
                        del positions[sym]
                        tier2_cooldowns[sym] = candle_idx + TIER2_COOLDOWN_CANDLES
                        # 연속 SL 쿨다운
                        if exit_reason_t2 == "t2_stop_loss":
                            cnt = tier2_consecutive_sl.get(sym, 0) + 1
                            tier2_consecutive_sl[sym] = cnt
                            if cnt >= 2:
                                tier2_long_cooldowns[sym] = candle_idx + TIER2_CONSECUTIVE_SL_COOLDOWN_CANDLES
                        else:
                            tier2_consecutive_sl.pop(sym, None)
                    else:
                        self._update_tier2_trailing(pos, t2_price)

                # 2. 진입 — RANGING 차단
                regime_blocks_tier2 = regime and regime.regime == Regime.RANGING
                tier2_count = sum(1 for p in positions.values() if p.tier == "tier2")
                if (not regime_blocks_tier2
                        and tier2_count < TIER2_MAX_CONCURRENT
                        and tier2_daily_trades < TIER2_DAILY_TRADE_LIMIT):
                    candidates: list[Tier2Signal] = []
                    for sym in self._tier2_coins:
                        if sym not in all_data:
                            continue
                        if sym in positions:
                            continue
                        # 쿨다운 체크 (일반 + 장기)
                        if candle_idx < tier2_cooldowns.get(sym, 0):
                            continue
                        if candle_idx < tier2_long_cooldowns.get(sym, 0):
                            continue
                        df5m_t2, _ = all_data[sym]
                        if ts not in df5m_t2.index:
                            continue
                        idx_loc = df5m_t2.index.get_loc(ts)
                        if isinstance(idx_loc, slice):
                            idx_loc = idx_loc.start
                        if idx_loc < TIER2_VOL_LOOKBACK:
                            continue
                        signal = self._tier2_scan(df5m_t2, idx_loc)
                        if signal:
                            signal.symbol = sym
                            candidates.append(signal)

                    candidates.sort(key=lambda s: s.score, reverse=True)
                    slots = TIER2_MAX_CONCURRENT - tier2_count
                    for sig in candidates[:slots]:
                        if tier2_daily_trades >= TIER2_DAILY_TRADE_LIMIT:
                            break
                        t2_sym = sig.symbol
                        df5m_t2, _ = all_data[t2_sym]
                        t2_price = float(df5m_t2.loc[ts, "close"])
                        if t2_price <= 0:
                            continue

                        t2_margin = cash * TIER2_POSITION_PCT
                        if t2_margin < MIN_MARGIN_USDT:
                            continue
                        t2_quantity = (t2_margin * self._leverage) / t2_price
                        t2_fee = t2_quantity * t2_price * FUTURES_FEE
                        cash -= t2_margin + t2_fee
                        total_fees += t2_fee

                        # %-기반 SL/TP → 절대 가격 (레버리지 반영)
                        sl_raw = TIER2_SL_PCT / self._leverage / 100
                        tp_raw = TIER2_TP_PCT / self._leverage / 100
                        trail_act_raw = TIER2_TRAIL_ACTIVATION_PCT / self._leverage / 100
                        trail_stop_dist = t2_price * (TIER2_TRAIL_STOP_PCT / self._leverage / 100)
                        if sig.direction == Direction.LONG:
                            t2_sl = t2_price * (1 - sl_raw)
                            t2_tp = t2_price * (1 + tp_raw)
                            t2_trail_act = t2_price * (1 + trail_act_raw)
                        else:
                            t2_sl = t2_price * (1 + sl_raw)
                            t2_tp = t2_price * (1 - tp_raw)
                            t2_trail_act = t2_price * (1 - trail_act_raw)

                        positions[t2_sym] = V2Position(
                            symbol=t2_sym,
                            direction=sig.direction,
                            quantity=t2_quantity,
                            entry_price=t2_price,
                            margin=t2_margin,
                            leverage=self._leverage,
                            sl_price=t2_sl,
                            tp_price=t2_tp,
                            trail_activation_price=t2_trail_act,
                            trail_stop_atr=trail_stop_dist,  # 절대 거리 (Tier2)
                            extreme_price=t2_price,
                            atr_at_entry=1.0,  # Tier2는 %-기반, atr_at_entry 미사용
                            entered_idx=candle_idx,
                            strategy_name="tier2_surge",
                            tier="tier2",
                            entry_ts=ts,
                        )
                        tier2_daily_trades += 1
                        if t2_sym not in coin_stats:
                            coin_stats[t2_sym] = {
                                "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0,
                                "long_wins": 0, "long_losses": 0,
                                "short_wins": 0, "short_losses": 0,
                            }

            # 10% 진행 출력
            if candle_idx > 0 and candle_idx % (len(all_ts) // 10 + 1) == 0:
                pct = candle_idx / len(all_ts) * 100
                print(f"    {pct:.0f}% ({candle_idx:,}/{len(all_ts):,}) 에쿼티: {equity:,.1f}")

        # ─── 미청산 포지션 정리 ───
        final_ts = all_ts[-1]
        for sym in list(positions.keys()):
            pos = positions[sym]
            if sym in all_data:
                df5m, _ = all_data[sym]
                if final_ts in df5m.index:
                    price = float(df5m.loc[final_ts, "close"])
                else:
                    price = pos.entry_price
            else:
                price = pos.entry_price
            pnl, fee = self._close_position(pos, price)
            cash += pos.margin + pnl - fee
            total_fees += fee
            trade = self._record_trade(pos, price, final_ts, "backtest_end", pnl, regime)
            trades.append(trade)
            self._update_coin_stats(coin_stats, trade)
        positions.clear()

        # ─── 결과 계산 ───
        final_balance = cash
        total_pnl = final_balance - self._initial_balance
        total_pnl_pct = total_pnl / self._initial_balance * 100

        long_trades = [t for t in trades if t.direction == "long"]
        short_trades = [t for t in trades if t.direction == "short"]
        winning = [t for t in trades if t.pnl > 0]
        losing = [t for t in trades if t.pnl <= 0]

        total_win_amt = sum(t.pnl for t in winning)
        total_loss_amt = abs(sum(t.pnl for t in losing))
        profit_factor = total_win_amt / total_loss_amt if total_loss_amt > 0 else float('inf')

        win_rate = len(winning) / len(trades) * 100 if trades else 0
        avg_win_pct = sum(t.pnl_pct for t in winning) / len(winning) if winning else 0
        avg_loss_pct = sum(t.pnl_pct for t in losing) / len(losing) if losing else 0

        # Sharpe ratio (일별 수익률 기반)
        if len(equity_curve) > 1:
            daily_returns = []
            prev = equity_curve[0][1]
            for i in range(12 * 24, len(equity_curve), 12 * 24):  # 일단위
                r = (equity_curve[i][1] - prev) / prev if prev > 0 else 0
                daily_returns.append(r)
                prev = equity_curve[i][1]
            if daily_returns:
                avg_r = sum(daily_returns) / len(daily_returns)
                std_r = (sum((r - avg_r) ** 2 for r in daily_returns) / len(daily_returns)) ** 0.5
                sharpe = (avg_r / std_r * math.sqrt(365)) if std_r > 0 else 0
            else:
                sharpe = 0
        else:
            sharpe = 0

        # B&H 비교
        if first_prices and last_prices:
            bh_returns = []
            for sym in first_prices:
                if sym in last_prices and first_prices[sym] > 0:
                    bh_returns.append((last_prices[sym] - first_prices[sym]) / first_prices[sym] * 100)
            buy_hold_pct = sum(bh_returns) / len(bh_returns) if bh_returns else 0
        else:
            buy_hold_pct = 0

        return V2BacktestResult(
            coins=list(all_data.keys()),
            days=days,
            initial_balance=self._initial_balance,
            final_balance=final_balance,
            total_pnl=total_pnl,
            total_pnl_pct=total_pnl_pct,
            max_drawdown_pct=max_drawdown,
            total_trades=len(trades),
            long_trades=len(long_trades),
            short_trades=len(short_trades),
            winning_trades=len(winning),
            losing_trades=len(losing),
            win_rate=win_rate,
            avg_win_pct=avg_win_pct,
            avg_loss_pct=avg_loss_pct,
            profit_factor=profit_factor,
            sharpe_ratio=sharpe,
            total_fees=total_fees,
            total_funding=total_funding,
            buy_hold_pnl_pct=buy_hold_pct,
            trades=trades,
            equity_curve=equity_curve,
            regime_distribution=regime_counts,
            coin_stats=coin_stats,
            avoidance_daily_pauses=avoidance_daily_pauses,
            avoidance_consec_pauses=avoidance_consec_pauses,
            funding_blocks=funding_blocks,
            funding_size_reductions=funding_size_reductions,
        )

    # ── 내부 헬퍼 ─────────────────────────────────────────────

    def _precompute_regimes(self, df_1h: pd.DataFrame) -> list[tuple[datetime, RegimeState]]:
        """1h 캔들마다 레짐을 감지하여 시계열로 반환."""
        detector = RegimeDetector(
            confirm_count=self._regime_confirm,
            min_duration_h=self._regime_min_hours,
            adx_enter=self._regime_adx_enter,
            adx_exit=self._regime_adx_exit,
        )
        regimes = []
        for i in range(50, len(df_1h)):
            window = df_1h.iloc[:i + 1]
            state = detector.detect(window)
            regimes.append((df_1h.index[i], state))
        return regimes

    def _get_regime_at(
        self,
        regimes: list[tuple[datetime, RegimeState]],
        ts: datetime,
    ) -> RegimeState | None:
        """5m 타임스탬프에 해당하는 가장 최근 1h 레짐 반환."""
        if not regimes:
            return None
        # 이진 검색
        lo, hi = 0, len(regimes) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if regimes[mid][0] <= ts:
                lo = mid
            else:
                hi = mid - 1
        if regimes[lo][0] <= ts:
            return regimes[lo][1]
        return None

    def _calc_margin(
        self,
        decision: StrategyDecision,
        cash: float,
        price: float,
        atr: float,
        regime: Regime | None = None,
    ) -> float:
        """ATR 기반 마진 계산. 레짐별 사이징 팩터 적용 (라이브 일치)."""
        if cash <= 0 or atr <= 0 or price <= 0:
            return 0.0

        risk_per_unit = max(decision.stop_loss_atr, 0.5) * atr / price
        if risk_per_unit <= 0:
            return 0.0

        margin = (cash * self._base_risk_pct) / risk_per_unit
        margin *= decision.sizing_factor * decision.confidence

        # 레짐별 포지션 사이징 팩터 (라이브 tier1_manager와 동일)
        if regime is not None:
            margin *= REGIME_SIZING_FACTORS.get(regime, 0.8)

        # 최대 비율 제한
        max_margin = cash * self._max_position_pct
        margin = min(margin, max_margin)

        if margin < MIN_MARGIN_USDT:
            return 0.0
        return margin

    @staticmethod
    def _apply_dynamic_sl(base_sl_atr: float, regime: Regime | None) -> float:
        """레짐별 동적 SL ATR mult 계산 (라이브 tier1_manager와 동일)."""
        if regime is None:
            return base_sl_atr
        mult, floor, cap = DYNAMIC_SL_PROFILES.get(regime, DEFAULT_SL_PROFILE)
        adjusted = base_sl_atr * mult
        return max(floor, min(adjusted, cap))

    @staticmethod
    def _calc_atr_leverage(atr: float, close: float, max_leverage: int, tight: bool = False) -> int:
        """ATR% 기반 레버리지 스케일링 (라이브 tier1_manager와 동일)."""
        if close <= 0:
            return 1
        atr_pct = (atr / close) * 100
        tiers = ATR_LEVERAGE_TIERS_TIGHT if tight else ATR_LEVERAGE_TIERS
        for threshold, lev in tiers:
            if atr_pct > threshold:
                return min(lev, max_leverage)
        return max_leverage

    @staticmethod
    def _detect_hidden_divergence(
        df: pd.DataFrame, direction: Direction, lookback: int = 20, pivot_window: int = 5
    ) -> bool:
        """히든 다이버전스 감지.

        Bullish hidden: 가격 higher low + RSI lower low → 상승 추세 지속
        Bearish hidden: 가격 lower high + RSI higher high → 하락 추세 지속
        """
        if len(df) < lookback + pivot_window:
            return False

        closes = df["close"].iloc[-(lookback + pivot_window):].values
        rsi_col = "rsi_14" if "rsi_14" in df.columns else "RSI_14"
        if rsi_col not in df.columns:
            return False
        rsis = df[rsi_col].iloc[-(lookback + pivot_window):].values

        # 피벗 포인트 (로컬 극값) 찾기
        def find_swing_lows(arr, window=3):
            lows = []
            for i in range(window, len(arr) - window):
                if arr[i] == min(arr[i - window:i + window + 1]):
                    lows.append((i, arr[i]))
            return lows

        def find_swing_highs(arr, window=3):
            highs = []
            for i in range(window, len(arr) - window):
                if arr[i] == max(arr[i - window:i + window + 1]):
                    highs.append((i, arr[i]))
            return highs

        if direction == Direction.LONG:
            # Bullish hidden: price higher low, RSI lower low
            price_lows = find_swing_lows(closes)
            rsi_lows = find_swing_lows(rsis)
            if len(price_lows) >= 2 and len(rsi_lows) >= 2:
                # 최근 2개 스윙 로우 비교
                p_prev, p_last = price_lows[-2][1], price_lows[-1][1]
                r_prev, r_last = rsi_lows[-2][1], rsi_lows[-1][1]
                if p_last > p_prev and r_last < r_prev:
                    return True
        else:
            # Bearish hidden: price lower high, RSI higher high
            price_highs = find_swing_highs(closes)
            rsi_highs = find_swing_highs(rsis)
            if len(price_highs) >= 2 and len(rsi_highs) >= 2:
                p_prev, p_last = price_highs[-2][1], price_highs[-1][1]
                r_prev, r_last = rsi_highs[-2][1], rsi_highs[-1][1]
                if p_last < p_prev and r_last > r_prev:
                    return True

        return False

    def _calc_sl_tp(
        self,
        direction: Direction,
        price: float,
        atr: float,
        sl_mult: float,
        tp_mult: float,
    ) -> tuple[float, float]:
        """ATR 기반 SL/TP 가격 계산."""
        if direction == Direction.LONG:
            sl = price - sl_mult * atr
            tp = price + tp_mult * atr
        else:
            sl = price + sl_mult * atr
            tp = price - tp_mult * atr
        return sl, tp

    def _check_stops(self, pos: V2Position, price: float) -> str | None:
        """SL/TP/트레일링 체크. 히트 시 사유 문자열 반환."""
        if pos.direction == Direction.LONG:
            if price <= pos.sl_price:
                return "stop_loss"
            if price >= pos.tp_price:
                return "take_profit"
            if pos.trailing_active and pos.trail_stop_price and price <= pos.trail_stop_price:
                return "trailing_stop"
        else:  # SHORT
            if price >= pos.sl_price:
                return "stop_loss"
            if price <= pos.tp_price:
                return "take_profit"
            if pos.trailing_active and pos.trail_stop_price and price >= pos.trail_stop_price:
                return "trailing_stop"
        return None

    def _update_trailing(self, pos: V2Position, price: float) -> None:
        """트레일링 스탑 업데이트."""
        if pos.direction == Direction.LONG:
            if price > pos.extreme_price:
                pos.extreme_price = price
            if not pos.trailing_active and price >= pos.trail_activation_price:
                pos.trailing_active = True
            if pos.trailing_active:
                pos.trail_stop_price = pos.extreme_price - pos.trail_stop_atr * pos.atr_at_entry
        else:  # SHORT
            if price < pos.extreme_price:
                pos.extreme_price = price
            if not pos.trailing_active and price <= pos.trail_activation_price:
                pos.trailing_active = True
            if pos.trailing_active:
                pos.trail_stop_price = pos.extreme_price + pos.trail_stop_atr * pos.atr_at_entry

    def _close_position(
        self, pos: V2Position, price: float,
    ) -> tuple[float, float]:
        """포지션 청산. (pnl, fee) 반환."""
        pnl = self._calc_pnl(pos.direction, pos.entry_price, price, pos.quantity)
        fee = pos.quantity * price * (FUTURES_FEE + SLIPPAGE)
        return pnl, fee

    @staticmethod
    def _calc_pnl(
        direction: Direction,
        entry: float,
        exit_price: float,
        quantity: float,
    ) -> float:
        if direction == Direction.LONG:
            return (exit_price - entry) * quantity
        else:
            return (entry - exit_price) * quantity

    def _record_trade(
        self,
        pos: V2Position,
        exit_price: float,
        exit_time: datetime,
        exit_reason: str,
        pnl: float,
        regime: RegimeState | None,
    ) -> V2Trade:
        pnl_pct = pnl / pos.margin * 100 if pos.margin > 0 else 0
        return V2Trade(
            symbol=pos.symbol,
            direction=pos.direction.value,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=pos.quantity,
            margin=pos.margin,
            pnl=pnl,
            pnl_pct=pnl_pct,
            entry_time=pos.entry_ts if pos.entry_ts else exit_time,
            exit_time=exit_time,
            exit_reason=exit_reason,
            strategy_name=pos.strategy_name,
            regime=regime.regime.value if regime else "unknown",
        )

    @staticmethod
    def _update_coin_stats(stats: dict, trade: V2Trade) -> None:
        if trade.symbol not in stats:
            return
        s = stats[trade.symbol]
        s["trades"] += 1
        s["pnl"] += trade.pnl
        if trade.pnl > 0:
            s["wins"] += 1
            if trade.direction == "long":
                s["long_wins"] += 1
            else:
                s["short_wins"] += 1
        else:
            s["losses"] += 1
            if trade.direction == "long":
                s["long_losses"] += 1
            else:
                s["short_losses"] += 1

    # ── Tier 2 서지 스캔 헬퍼 ──────────────────────────────────

    @staticmethod
    def _tier2_calc_rsi(closes: list[float], period: int = 14) -> float:
        """간단 RSI 계산 (tier2_scanner와 동일)."""
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
    def _tier2_calc_atr_pct(highs: list[float], lows: list[float], closes: list[float],
                            period: int = 14) -> float:
        """ATR% 계산 (DataFrame 기반)."""
        n = len(closes)
        if n < period + 1:
            return 0.0
        tr_sum = 0.0
        for i in range(n - period, n):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            tr_sum += tr
        atr = tr_sum / period
        return (atr / closes[-1] * 100) if closes[-1] > 0 else 0.0

    def _tier2_scan(self, df5m: pd.DataFrame, idx: int) -> Tier2Signal | None:
        """5m 캔들 데이터에서 서지 시그널 스캔 (tier2_scanner._scan_symbol 로직)."""
        start = max(0, idx - TIER2_VOL_LOOKBACK + 1)
        window = df5m.iloc[start:idx + 1]
        if len(window) < 20:
            return None

        volumes = window["volume"].tolist()
        closes = window["close"].tolist()
        highs = window["high"].tolist()
        lows = window["low"].tolist()

        # 거래량 비율
        vol_avg = sum(volumes[:-1]) / max(len(volumes) - 1, 1)
        vol_last = volumes[-1]
        vol_ratio = vol_last / vol_avg if vol_avg > 0 else 0.0

        # 가격 변동 (12캔들 = 60분)
        lookback = min(12, len(closes) - 1)
        price_first = closes[-(lookback + 1)]
        price_last = closes[-1]
        price_chg = (price_last - price_first) / price_first * 100 if price_first > 0 else 0.0

        # ATR% 필터
        atr_pct = self._tier2_calc_atr_pct(highs, lows, closes)
        if 0 < atr_pct < TIER2_MIN_ATR_PCT:
            return None

        # 소진(exhaustion) 필터: 30분(6캔들) 변동
        lookback_30m = min(6, len(closes) - 1)
        if lookback_30m > 0:
            price_30m_ago = closes[-(lookback_30m + 1)]
            chg_30m = (price_last - price_30m_ago) / price_30m_ago * 100 if price_30m_ago > 0 else 0.0
            if abs(chg_30m) > TIER2_EXHAUSTION_PCT:
                return None

        # RSI
        rsi = self._tier2_calc_rsi(closes)

        # 가속도
        accel = 0.0
        if len(volumes) >= 3 and vol_avg > 0:
            ratio_now = volumes[-1] / vol_avg
            ratio_prev = volumes[-3] / vol_avg
            accel = ratio_now - ratio_prev

        # 정규화 점수
        vol_signal = min(vol_ratio / 10.0, 1.0)
        price_signal = min(abs(price_chg) / 5.0, 1.0)
        accel_signal = max(0, min(accel / 3.0, 1.0))
        score = 0.40 * vol_signal + 0.35 * price_signal + 0.25 * accel_signal

        if score < TIER2_MIN_SCORE:
            return None

        direction = Direction.LONG if price_chg > 0 else Direction.SHORT

        # RSI 필터
        if direction == Direction.LONG and rsi > TIER2_RSI_OVERBOUGHT:
            return None
        if direction == Direction.SHORT and rsi < TIER2_RSI_OVERSOLD:
            return None

        return Tier2Signal(
            symbol="",  # 호출자에서 설정
            score=score,
            direction=direction,
            vol_ratio=vol_ratio,
            price_chg_pct=price_chg,
            rsi=rsi,
        )

    def _check_tier2_stops(self, pos: V2Position, price: float) -> str | None:
        """Tier2 %-기반 SL/TP 체크 (라이브 tier2_scanner._check_exits와 동일)."""
        entry = pos.entry_price
        if entry <= 0:
            return None
        if pos.direction == Direction.LONG:
            pnl_pct = (price - entry) / entry * 100
        else:
            pnl_pct = (entry - price) / entry * 100
        pnl_pct *= pos.leverage

        if pnl_pct <= -TIER2_SL_PCT:
            return "t2_stop_loss"
        if pnl_pct >= TIER2_TP_PCT:
            return "t2_take_profit"
        # 트레일링
        if pos.trailing_active and pos.trail_stop_price is not None:
            if pos.direction == Direction.LONG and price <= pos.trail_stop_price:
                return "t2_trailing_stop"
            if pos.direction == Direction.SHORT and price >= pos.trail_stop_price:
                return "t2_trailing_stop"
        return None

    @staticmethod
    def _update_tier2_trailing(pos: V2Position, price: float) -> None:
        """Tier2 트레일링 스탑 업데이트 (%-기반)."""
        if pos.direction == Direction.LONG:
            if price > pos.extreme_price:
                pos.extreme_price = price
            if not pos.trailing_active and price >= pos.trail_activation_price:
                pos.trailing_active = True
            if pos.trailing_active:
                # trail_stop_atr에는 절대 거리가 저장됨 (Tier2)
                pos.trail_stop_price = pos.extreme_price - pos.trail_stop_atr
        else:
            if price < pos.extreme_price:
                pos.extreme_price = price
            if not pos.trailing_active and price <= pos.trail_activation_price:
                pos.trailing_active = True
            if pos.trailing_active:
                pos.trail_stop_price = pos.extreme_price + pos.trail_stop_atr

    # ── Walk-Forward ──────────────────────────────────────────

    async def walk_forward(
        self,
        days: int,
        train_days: int = 240,
        val_days: int = 60,
        test_days: int = 60,
    ) -> list[V2BacktestResult]:
        """Walk-Forward 검증. 슬라이딩 윈도우로 일관성 검증."""
        window_total = train_days + val_days + test_days
        stride = test_days

        if days < window_total:
            print(f"데이터 부족: {days}일 < 최소 {window_total}일 필요")
            return []

        num_windows = (days - window_total) // stride + 1
        print(f"\n{'='*60}")
        print(f"  Walk-Forward 검증")
        print(f"  학습: {train_days}일 | 검증: {val_days}일 | 테스트: {test_days}일")
        print(f"  윈도우: {num_windows}개 | 스트라이드: {stride}일")
        print(f"{'='*60}")

        # 전체 데이터 프리페치
        all_data = await self.prefetch(days)
        if not all_data:
            raise ValueError("데이터 없음")

        results = []
        for w in range(num_windows):
            offset_days = w * stride
            test_start_days_ago = days - offset_days - window_total + test_days
            test_end_days_ago = days - offset_days - window_total

            # 테스트 구간 데이터 슬라이싱
            now = datetime.now(timezone.utc)
            test_start = now - timedelta(days=days - offset_days - train_days - val_days)
            test_end = now - timedelta(days=days - offset_days - window_total)

            tz = list(all_data.values())[0][0].index.tz
            if tz:
                test_start = test_start.replace(tzinfo=tz)
                test_end = test_end.replace(tzinfo=tz)

            print(f"\n  ── 윈도우 {w+1}/{num_windows} ──")
            print(f"     테스트 구간: {test_start.date()} ~ {test_end.date()}")

            # 테스트 데이터 슬라이싱
            test_data = {}
            skipped = []
            for sym, (df5m, df1h) in all_data.items():
                df5m_test = df5m[(df5m.index >= test_start) & (df5m.index <= test_end)]
                df1h_test = df1h[(df1h.index >= test_start) & (df1h.index <= test_end)]
                if len(df5m_test) > LOOKBACK_WINDOW and len(df1h_test) > 50:
                    # 워밍업을 위해 앞부분 데이터 포함
                    warmup_start = test_start - timedelta(days=5)
                    df5m_slice = df5m[(df5m.index >= warmup_start) & (df5m.index <= test_end)]
                    df1h_slice = df1h[(df1h.index >= warmup_start) & (df1h.index <= test_end)]
                    test_data[sym] = (df5m_slice, df1h_slice)
                else:
                    skipped.append(sym)

            if skipped:
                print(f"     데이터 부족으로 제외: {', '.join(skipped)}")

            if test_data:
                result = await self._simulate(test_data, test_days + 5)
                results.append(result)
                _print_window_summary(w + 1, result)

        # 종합
        if results:
            _print_wf_summary(results)

        return results


# ── 결과 출력 ─────────────────────────────────────────────────

def print_results(r: V2BacktestResult) -> None:
    print(f"\n{'='*60}")
    print(f"  FuturesEngine V2 백테스트 결과")
    print(f"{'='*60}")
    print(f"  코인: {', '.join(r.coins)}")
    print(f"  기간: {r.days}일")
    print()
    print(f"  초기 잔고:     {r.initial_balance:>12,.2f} USDT")
    print(f"  최종 잔고:     {r.final_balance:>12,.2f} USDT")
    print(f"  총 손익:       {r.total_pnl:>12,.2f} USDT ({r.total_pnl_pct:+.1f}%)")
    print(f"  B&H 대비:      {r.buy_hold_pnl_pct:>11.1f}%")
    print()
    print(f"  총 거래:       {r.total_trades:>8d}")
    print(f"    롱:          {r.long_trades:>8d}")
    print(f"    숏:          {r.short_trades:>8d}")
    print(f"  승:            {r.winning_trades:>8d}")
    print(f"  패:            {r.losing_trades:>8d}")
    print(f"  승률:          {r.win_rate:>8.1f}%")
    print()
    print(f"  평균 수익:     {r.avg_win_pct:>8.2f}%")
    print(f"  평균 손실:     {r.avg_loss_pct:>8.2f}%")
    print(f"  Profit Factor: {r.profit_factor:>8.2f}")
    print(f"  Sharpe Ratio:  {r.sharpe_ratio:>8.2f}")
    print(f"  최대 낙폭:     {r.max_drawdown_pct:>8.2f}%")
    print()
    print(f"  총 수수료:     {r.total_fees:>12,.2f} USDT")
    print(f"  총 펀딩비:     {r.total_funding:>12,.2f} USDT")

    # 회피 모드 (D) + 과열 필터 (C) 통계
    if r.avoidance_daily_pauses or r.avoidance_consec_pauses or r.funding_blocks or r.funding_size_reductions:
        print()
        if r.avoidance_daily_pauses:
            print(f"  일일 손실 한도 발동:  {r.avoidance_daily_pauses:>5d}회")
        if r.avoidance_consec_pauses:
            print(f"  연속 손실 정지:       {r.avoidance_consec_pauses:>5d}회")
        if r.funding_blocks:
            print(f"  과열 진입 차단:       {r.funding_blocks:>5d}회")
        if r.funding_size_reductions:
            print(f"  과열 사이즈 축소:     {r.funding_size_reductions:>5d}회")

    if r.regime_distribution:
        print(f"\n  레짐 분포:")
        total = sum(r.regime_distribution.values())
        for regime, count in sorted(r.regime_distribution.items(), key=lambda x: -x[1]):
            pct = count / total * 100 if total > 0 else 0
            print(f"    {regime:<15s} {count:>8,d} ({pct:.1f}%)")

    if r.coin_stats:
        print(f"\n  코인별 성과:")
        print(f"  {'코인':<12s} {'거래':>6s} {'승':>4s} {'패':>4s} {'승률':>7s} {'PnL':>10s}")
        for sym, s in sorted(r.coin_stats.items(), key=lambda x: -x[1]["pnl"]):
            if s["trades"] == 0:
                continue
            wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
            print(f"  {sym:<12s} {s['trades']:>6d} {s['wins']:>4d} {s['losses']:>4d} {wr:>6.1f}% {s['pnl']:>+10.2f}")

    # 전략별/레짐별 성과
    if r.trades:
        print(f"\n  전략별 성과:")
        strat_stats: dict[str, dict] = {}
        for t in r.trades:
            key = t.strategy_name or "unknown"
            if key not in strat_stats:
                strat_stats[key] = {"trades": 0, "wins": 0, "pnl": 0.0, "win_amt": 0.0, "loss_amt": 0.0}
            s = strat_stats[key]
            s["trades"] += 1
            s["pnl"] += t.pnl
            if t.pnl > 0:
                s["wins"] += 1
                s["win_amt"] += t.pnl
            else:
                s["loss_amt"] += abs(t.pnl)
        print(f"  {'전략':<20s} {'거래':>5s} {'승률':>6s} {'PF':>6s} {'PnL':>10s}")
        for name, s in sorted(strat_stats.items(), key=lambda x: -x[1]["pnl"]):
            wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
            pf = s["win_amt"] / s["loss_amt"] if s["loss_amt"] > 0 else float('inf')
            print(f"  {name:<20s} {s['trades']:>5d} {wr:>5.1f}% {pf:>5.2f} {s['pnl']:>+10.2f}")

        print(f"\n  레짐별 성과:")
        regime_stats: dict[str, dict] = {}
        for t in r.trades:
            key = t.regime or "unknown"
            if key not in regime_stats:
                regime_stats[key] = {"trades": 0, "wins": 0, "pnl": 0.0, "win_amt": 0.0, "loss_amt": 0.0}
            s = regime_stats[key]
            s["trades"] += 1
            s["pnl"] += t.pnl
            if t.pnl > 0:
                s["wins"] += 1
                s["win_amt"] += t.pnl
            else:
                s["loss_amt"] += abs(t.pnl)
        print(f"  {'레짐':<20s} {'거래':>5s} {'승률':>6s} {'PF':>6s} {'PnL':>10s}")
        for name, s in sorted(regime_stats.items(), key=lambda x: -x[1]["pnl"]):
            wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
            pf = s["win_amt"] / s["loss_amt"] if s["loss_amt"] > 0 else float('inf')
            print(f"  {name:<20s} {s['trades']:>5d} {wr:>5.1f}% {pf:>5.2f} {s['pnl']:>+10.2f}")

        # 청산 사유별 통계
        print(f"\n  청산 사유별:")
        exit_stats: dict[str, dict] = {}
        for t in r.trades:
            key = t.exit_reason or "unknown"
            if key not in exit_stats:
                exit_stats[key] = {"trades": 0, "wins": 0, "pnl": 0.0}
            s = exit_stats[key]
            s["trades"] += 1
            s["pnl"] += t.pnl
            if t.pnl > 0:
                s["wins"] += 1
        print(f"  {'사유':<20s} {'거래':>5s} {'승률':>6s} {'PnL':>10s}")
        for name, s in sorted(exit_stats.items(), key=lambda x: -x[1]["pnl"]):
            wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
            print(f"  {name:<20s} {s['trades']:>5d} {wr:>5.1f}% {s['pnl']:>+10.2f}")

    # 타겟 달성 확인
    print(f"\n  ── 타겟 확인 ──")
    pf_ok = r.profit_factor >= 1.5
    mdd_ok = r.max_drawdown_pct <= 15.0
    wr_ok = r.win_rate >= 55.0
    print(f"  PF >= 1.5:    {'✓' if pf_ok else '✗'} ({r.profit_factor:.2f})")
    print(f"  MDD <= 15%:   {'✓' if mdd_ok else '✗'} ({r.max_drawdown_pct:.2f}%)")
    print(f"  Win% >= 55%:  {'✓' if wr_ok else '✗'} ({r.win_rate:.1f}%)")
    all_ok = pf_ok and mdd_ok and wr_ok
    print(f"  종합:         {'✓ PASS' if all_ok else '✗ FAIL'}")
    print(f"{'='*60}")


def _print_window_summary(window_num: int, r: V2BacktestResult) -> None:
    pf_ok = r.profit_factor >= 1.5
    mdd_ok = r.max_drawdown_pct <= 15.0
    wr_ok = r.win_rate >= 55.0
    status = "PASS" if (pf_ok and mdd_ok and wr_ok) else "FAIL"
    print(f"     결과: PnL={r.total_pnl_pct:+.1f}% PF={r.profit_factor:.2f} "
          f"MDD={r.max_drawdown_pct:.1f}% Win={r.win_rate:.0f}% "
          f"거래={r.total_trades} [{status}]")


def _print_wf_summary(results: list[V2BacktestResult]) -> None:
    print(f"\n{'='*60}")
    print(f"  Walk-Forward 종합")
    print(f"{'='*60}")

    pfs = [r.profit_factor for r in results]
    mdds = [r.max_drawdown_pct for r in results]
    wrs = [r.win_rate for r in results]
    pnls = [r.total_pnl_pct for r in results]

    print(f"  윈도우 수:     {len(results)}")
    print(f"  평균 PnL:      {sum(pnls)/len(pnls):+.1f}%")
    print(f"  평균 PF:       {sum(pfs)/len(pfs):.2f}")
    print(f"  평균 MDD:      {sum(mdds)/len(mdds):.1f}%")
    print(f"  평균 승률:     {sum(wrs)/len(wrs):.1f}%")
    print(f"  최악 PF:       {min(pfs):.2f}")
    print(f"  최악 MDD:      {max(mdds):.1f}%")

    pass_count = sum(1 for r in results
                     if r.profit_factor >= 1.5 and r.max_drawdown_pct <= 15.0 and r.win_rate >= 55.0)
    print(f"  통과 윈도우:   {pass_count}/{len(results)}")
    all_pass = pass_count == len(results)
    print(f"  종합:          {'✓ ALL PASS' if all_pass else '✗ SOME FAIL'}")
    print(f"{'='*60}")


# ── CLI ───────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="FuturesEngineV2 백테스터")
    parser.add_argument("--days", type=int, default=540, help="백테스트 기간 (일)")
    parser.add_argument("--coins", nargs="+", default=None,
                        help="코인 목록 (예: BTC ETH SOL)")
    parser.add_argument("--leverage", type=int, default=3, help="레버리지 배수")
    parser.add_argument("--balance", type=float, default=1000.0, help="초기 잔고 (USDT)")
    parser.add_argument("--max-position-pct", type=float, default=0.15, help="최대 포지션 비율")
    parser.add_argument("--cooldown", type=int, default=36, help="쿨다운 캔들 수 (5m 기준, 기본 36=3시간)")
    parser.add_argument("--min-confidence", type=float, default=0.5, help="최소 신뢰도 (0.0-1.0)")
    parser.add_argument("--trending-only", action="store_true", help="추세 레짐에서만 거래")
    parser.add_argument("--eval-interval", type=int, default=12, help="전략 평가 주기 (5m 캔들, 12=1h)")
    parser.add_argument("--v1-strategies", action="store_true", help="v1 7전략 레짐 미니앙상블 모드")
    parser.add_argument("--spot-strategies", action="store_true",
                        help="현물 4전략 SignalCombiner 가중 투표 모드 (라이브 V2 구성 검증)")
    parser.add_argument("--walk-forward", action="store_true", help="Walk-Forward 검증 모드")
    parser.add_argument("--train-days", type=int, default=240, help="WF 학습 기간")
    parser.add_argument("--val-days", type=int, default=60, help="WF 검증 기간")
    parser.add_argument("--test-days", type=int, default=60, help="WF 테스트 기간")
    # 레짐 감지 튜닝
    parser.add_argument("--regime-confirm", type=int, default=2,
                        help="레짐 전환 연속 확인 횟수 (기본 2)")
    parser.add_argument("--regime-min-hours", type=int, default=3,
                        help="레짐 최소 유지 시간 (기본 3시간)")
    parser.add_argument("--regime-adx-enter", type=float, default=27.0,
                        help="추세 진입 ADX 임계값 (기본 27)")
    parser.add_argument("--regime-adx-exit", type=float, default=23.0,
                        help="추세 이탈 ADX 임계값 (기본 23)")
    # Tier 2 서지
    parser.add_argument("--tier2", action="store_true",
                        help="Tier 2 서지 스캔 활성화 (30코인 볼륨 급등 감지)")
    parser.add_argument("--tier2-coins", type=int, default=30,
                        help="Tier 2 스캔 코인 수 (기본 30)")
    parser.add_argument("--tier2-only", action="store_true",
                        help="Tier 1 비활성화, Tier 2만 실행")
    # 시간대 필터
    parser.add_argument("--us-open-filter", type=str, default="off",
                        choices=["off", "no_long", "no_entry", "half_size"],
                        help="US 마켓 오픈 시간(KST 22-23) 필터: no_long=롱차단, no_entry=전체차단, half_size=50%%축소")
    parser.add_argument("--tight-leverage", action="store_true",
                        help="타이트 ATR 레버리지 스케일링 (ATR>5%%→2x, ATR>10%%→1x)")
    parser.add_argument("--divergence-filter", action="store_true",
                        help="히든 다이버전스 진입 필터 (방향 확인)")
    parser.add_argument("--divergence-lookback", type=int, default=20,
                        help="다이버전스 감지 룩백 캔들 수 (기본 20, 5m×20=100분)")
    parser.add_argument("--cascade-contrarian", action="store_true",
                        help="US 오픈(KST 22-23) 급락 시 역추세 롱 진입")
    parser.add_argument("--spot-asymmetric", action="store_true",
                        help="현물 비대칭 모드: 하락장/crash 시 매수 차단")
    parser.add_argument("--spot-hysteresis", type=int, default=0,
                        help="현물 비대칭 히스테리시스: bearish 해제 후 매수 금지 캔들 수 (5m 단위)")
    # 회피 모드 (D)
    parser.add_argument("--daily-loss-limit", type=float, default=0.0,
                        help="일일 손실 한도 (비율, e.g. 0.02=2%%). 0=비활성")
    parser.add_argument("--consecutive-loss-pause", type=int, default=0,
                        help="연속 N패 시 진입 정지. 0=비활성")
    parser.add_argument("--consecutive-loss-pause-duration", type=int, default=12,
                        help="연속 손실 정지 기간 (5m 캔들, 12=1시간)")
    # 과열 필터 (C)
    parser.add_argument("--funding-filter", action="store_true",
                        help="과열 필터: RSI 극단 시 진입 차단/사이즈 축소")
    parser.add_argument("--funding-rsi-block", type=float, default=80.0,
                        help="과열 RSI 차단 임계값 (기본 80)")
    parser.add_argument("--funding-rsi-reduce", type=float, default=70.0,
                        help="과열 RSI 축소 임계값 (기본 70)")
    parser.add_argument("--funding-regimes", type=str, default=None,
                        help="과열 필터 적용 레짐 (쉼표 구분, e.g. 'volatile,ranging')")
    # 연속 손실 사이즈 축소
    parser.add_argument("--consecutive-loss-reduce", type=int, default=0,
                        help="연속 N패 시 사이즈 축소 (0=비활성)")
    parser.add_argument("--consecutive-loss-reduce-pct", type=float, default=0.5,
                        help="연속 손실 축소 비율 (기본 0.5=50%%)")
    return parser.parse_args()


async def main():
    args = parse_args()

    coins = args.coins
    if coins:
        coins = [f"{c}/USDT" if "/" not in c else c for c in coins]
    else:
        coins = COINS_DEFAULT

    # 바이낸스 선물 public API (API 키 불필요)
    from exchange.binance_usdm_adapter import BinanceUSDMAdapter
    print("바이낸스 USDM 선물 연결 중...")
    exchange = BinanceUSDMAdapter(api_key="", api_secret="", testnet=False)
    await exchange.initialize()

    bt = V2Backtester(
        exchange=exchange,
        coins=coins,
        leverage=args.leverage,
        initial_balance=args.balance,
        max_position_pct=args.max_position_pct,
        cooldown_candles=args.cooldown,
        min_confidence=args.min_confidence,
        trending_only=args.trending_only,
        eval_interval=args.eval_interval,
        regime_confirm=args.regime_confirm,
        regime_min_hours=args.regime_min_hours,
        regime_adx_enter=args.regime_adx_enter,
        regime_adx_exit=args.regime_adx_exit,
    )

    if args.us_open_filter != "off":
        bt._us_open_filter = args.us_open_filter
        print(f"  US 마켓 오픈 시간 필터: {args.us_open_filter} (KST 22-23)")

    if args.tight_leverage:
        bt._tight_leverage = True
        print("  타이트 ATR 레버리지 스케일링: ATR>5%→2x, ATR>10%→1x")

    if args.divergence_filter:
        bt._divergence_filter = True
        bt._divergence_lookback = args.divergence_lookback
        print(f"  히든 다이버전스 진입 필터 활성화 (룩백 {args.divergence_lookback}캔들)")

    if args.cascade_contrarian:
        bt._cascade_contrarian = True
        print("  캐스케이드 역추세 활성화 (US 오픈 1%+ 급락 시 롱)")

    if args.spot_asymmetric:
        bt._spot_asymmetric = True
        bt._spot_hysteresis_candles = args.spot_hysteresis
        hyst_str = f", 히스테리시스 {args.spot_hysteresis}캔들 ({args.spot_hysteresis*5}분)" if args.spot_hysteresis > 0 else ""
        print(f"  현물 비대칭 모드 활성화 (crash/downtrend 매수 차단{hyst_str})")

    if args.daily_loss_limit > 0:
        bt._daily_loss_limit_pct = args.daily_loss_limit
        print(f"  회피 모드 (D): 일일 손실 한도 {args.daily_loss_limit*100:.1f}%")
    if args.consecutive_loss_pause > 0:
        bt._consecutive_loss_pause = args.consecutive_loss_pause
        bt._consecutive_loss_pause_candles = args.consecutive_loss_pause_duration
        print(f"  회피 모드 (D): 연속 {args.consecutive_loss_pause}패 시 {args.consecutive_loss_pause_duration*5}분 정지")
    if args.funding_filter:
        bt._funding_filter = True
        bt._funding_rsi_block = args.funding_rsi_block
        bt._funding_rsi_reduce = args.funding_rsi_reduce
        if args.funding_regimes:
            bt._funding_filter_regimes = set(args.funding_regimes.split(","))
            print(f"  과열 필터 (C): RSI {args.funding_rsi_reduce}/{args.funding_rsi_block} | 레짐: {args.funding_regimes}")
        else:
            print(f"  과열 필터 (C): RSI {args.funding_rsi_reduce}/{args.funding_rsi_block} | 전체 레짐")
    if args.consecutive_loss_reduce > 0:
        bt._consecutive_loss_size_reduce = args.consecutive_loss_reduce
        bt._consecutive_loss_reduce_pct = args.consecutive_loss_reduce_pct
        print(f"  연속 {args.consecutive_loss_reduce}패 시 사이즈 {args.consecutive_loss_reduce_pct*100:.0f}%로 축소")

    if args.spot_strategies and args.v1_strategies:
        print("오류: --spot-strategies와 --v1-strategies는 동시 사용 불가")
        sys.exit(1)

    if args.v1_strategies:
        bt.enable_v1_strategies()
        print("  v1 7전략 레짐 미니앙상블 모드 활성화")

    if args.spot_strategies:
        bt.enable_spot_strategies()
        print("  현물 4전략 SignalCombiner 가중 투표 모드 활성화")

    if args.tier2 or args.tier2_only:
        tier2_symbols = TIER2_COINS_DEFAULT[:args.tier2_coins]
        bt.enable_tier2(tier2_coins=tier2_symbols)
        if args.tier2_only:
            bt._tier1_disabled = True
        print(f"  Tier 2 서지 스캔 활성화 ({len(tier2_symbols)}코인)")

    try:
        if args.walk_forward:
            results = await bt.walk_forward(
                args.days,
                train_days=args.train_days,
                val_days=args.val_days,
                test_days=args.test_days,
            )
        else:
            result = await bt.run(args.days)
            print_results(result)
    finally:
        try:
            await exchange.close_ws()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
