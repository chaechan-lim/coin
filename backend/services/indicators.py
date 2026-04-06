"""Unified indicator computation pipeline.

COIN-52: 백테스트/라이브 단일 지표 계산 모듈.

모든 기술적 지표 계산 + 컬럼 정규화를 단일 함수로 통합하여
백테스트(backtest.py, backtest_v2.py)와 라이브(MarketDataService) 모두
동일 경로를 사용하도록 함.

컬럼 규칙:
  - 출력 컬럼은 항상 lowercase (sma_20, rsi_14, adx_14 등)
  - pandas_ta 대문자 출력 → _RENAME_MAP으로 자동 변환
  - BB 컬럼은 pandas_ta 버전에 따라 suffix 변동 → 동적 매핑
"""

from __future__ import annotations

import pandas as pd
import pandas_ta as ta
import structlog

logger = structlog.get_logger(__name__)

# ── pandas_ta 대문자 → lowercase 통합 매핑 ──────────────────────────
# backtest_v2._RENAME_MAP + market_data._INDICATOR_RENAME 통합.
#
# NOTE: SMA/EMA/RSI/ATR 항목은 compute_indicators()에서 직접 lowercase로 할당되므로
# 함수 내부에서는 트리거되지 않음. 외부 소비자(backtest_v2 re-export)와
# pandas_ta .ta.xxx(append=True) 방식으로 생성된 컬럼을 위해 유지.
_RENAME_MAP: dict[str, str] = {
    # EMA
    "EMA_9": "ema_9",
    "EMA_12": "ema_12",
    "EMA_20": "ema_20",
    "EMA_21": "ema_21",
    "EMA_26": "ema_26",
    "EMA_50": "ema_50",
    # SMA
    "SMA_5": "sma_5",
    "SMA_9": "sma_9",
    "SMA_20": "sma_20",
    "SMA_50": "sma_50",
    "SMA_60": "sma_60",
    "SMA_200": "sma_200",
    # RSI
    "RSI_14": "rsi_14",
    # ATR (pandas_ta outputs ATRr_14)
    "ATRr_14": "atr_14",
    # ADX + DI
    "ADX_14": "adx_14",
    "DMP_14": "dmp_14",
    "DMN_14": "dmn_14",
    # MACD
    "MACD_12_26_9": "macd_line",
    "MACDs_12_26_9": "macd_signal",
    "MACDh_12_26_9": "macd_hist",
}

# BB 컬럼 prefix → lowercase 매핑 (pandas_ta 버전에 따라 suffix 변동)
_BB_PREFIX_MAP: dict[str, str] = {
    "BBU_20": "bb_upper_20",
    "BBL_20": "bb_lower_20",
    "BBM_20": "bb_mid_20",
    "BBB_20": "bb_bandwidth_20",
    "BBP_20": "bb_percent_20",
}

# ── 출력 기대 컬럼 목록 ─────────────────────────────────────────────
REQUIRED_COLUMNS: list[str] = [
    # SMA
    "sma_5",
    "sma_9",
    "sma_20",
    "sma_50",
    "sma_60",
    "sma_200",
    # EMA
    "ema_9",
    "ema_12",
    "ema_20",
    "ema_21",
    "ema_26",
    "ema_50",
    # RSI
    "rsi_14",
    # MACD
    "macd_line",
    "macd_signal",
    "macd_hist",
    # Bollinger Bands
    "bb_upper_20",
    "bb_lower_20",
    "bb_mid_20",
    # ATR
    "atr_14",
    # ADX
    "adx_14",
    "dmp_14",
    "dmn_14",
    # Volume
    "volume_sma_20",
]


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """모든 기술적 지표를 계산하고 컬럼명을 lowercase로 정규화.

    백테스트/라이브 모두 이 함수를 단일 진입점으로 사용.

    Args:
        df: OHLCV DataFrame (open, high, low, close, volume 컬럼 필수).
            index는 datetime이어야 함 (pandas_ta 호환).

    Returns:
        지표가 추가된 DataFrame. 모든 지표 컬럼은 lowercase.
    """
    if len(df) < 2:
        return df

    # 입력 DataFrame 복사 — pd.concat가 df를 리바인드하므로
    # 호출자의 원본이 부분적으로만 변이되는 것을 방지.
    df = df.copy()

    # ── SMA ───────────────────────────────────────────────────────
    df["sma_5"] = ta.sma(df["close"], length=5)
    df["sma_9"] = ta.sma(df["close"], length=9)
    df["sma_20"] = ta.sma(df["close"], length=20)
    df["sma_50"] = ta.sma(df["close"], length=50)
    df["sma_60"] = ta.sma(df["close"], length=60)
    df["sma_200"] = ta.sma(df["close"], length=200)

    # ── EMA ───────────────────────────────────────────────────────
    df["ema_9"] = ta.ema(df["close"], length=9)
    df["ema_12"] = ta.ema(df["close"], length=12)
    df["ema_20"] = ta.ema(df["close"], length=20)
    df["ema_21"] = ta.ema(df["close"], length=21)
    df["ema_26"] = ta.ema(df["close"], length=26)
    df["ema_50"] = ta.ema(df["close"], length=50)

    # ── RSI ───────────────────────────────────────────────────────
    df["rsi_14"] = ta.rsi(df["close"], length=14)

    # ── MACD ──────────────────────────────────────────────────────
    macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
    if macd is not None:
        df = pd.concat([df, macd], axis=1)

    # ── Bollinger Bands ───────────────────────────────────────────
    bbands = ta.bbands(df["close"], length=20, std=2.0)
    if bbands is not None:
        df = pd.concat([df, bbands], axis=1)

    # ── ATR ───────────────────────────────────────────────────────
    df["atr_14"] = ta.atr(df["high"], df["low"], df["close"], length=14)

    # ── ADX (with DMP/DMN) ────────────────────────────────────────
    adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
    if adx_df is not None:
        df = pd.concat([df, adx_df], axis=1)

    # ── Volume SMA (항상 lowercase) ───────────────────────────────
    df["volume_sma_20"] = ta.sma(df["volume"], length=20)

    # ── pandas_ta 대문자 컬럼 → lowercase 리네임 ──────────────────
    df.rename(columns=_RENAME_MAP, inplace=True)

    # BB 컬럼명은 pandas_ta 버전에 따라 suffix가 다를 수 있음
    # (BBU_20_2.0 vs BBU_20_2.0_2.0 등) — 동적 매핑
    bb_rename: dict[str, str] = {}
    for col in df.columns:
        for prefix, target in _BB_PREFIX_MAP.items():
            if col.startswith(prefix) and target not in df.columns:
                bb_rename[col] = target
                break
    if bb_rename:
        df.rename(columns=bb_rename, inplace=True)

    # ── 누락 컬럼 경고 (데이터 부족 시 debug, 그 외 warning) ────
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        # MACD는 최소 35봉 필요 — 데이터 부족은 debug 레벨
        data_shortage = len(df) < 35 and all(c.startswith("macd") for c in missing)
        log_fn = logger.debug if data_shortage else logger.warning
        log_fn(
            "indicator.missing_columns",
            missing=missing,
            bars=len(df),
        )

    return df
