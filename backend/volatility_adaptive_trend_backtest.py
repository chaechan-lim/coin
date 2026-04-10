"""
Volatility Adaptive Trend Following 백테스트.

- 1시간봉 EMA 추세 추종
- ATR 백분위에 따라 진입 임계치와 사이징을 조정
- long/short 양방향 선물형 모델
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd


CACHE_DIR = Path(__file__).parent / ".cache"
FUTURES_FEE = 0.0004
SLIPPAGE = 0.0002
TOTAL_COST = FUTURES_FEE + SLIPPAGE


@dataclass
class VolAdaptiveTrendResult:
    coin: str
    days: int
    initial: float
    final: float
    return_pct: float
    sharpe: float
    max_drawdown: float
    n_trades: int
    total_fees: float
    avg_vol_percentile: float


@lru_cache(maxsize=16)
def load_hourly(coin: str) -> pd.DataFrame:
    path = CACHE_DIR / f"{coin}_USDT_1h.csv"
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce", utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df[df.index.notna()].sort_index()


def _indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema_fast"] = out["close"].ewm(span=24, adjust=False).mean()
    out["ema_slow"] = out["close"].ewm(span=72, adjust=False).mean()
    prev_close = out["close"].shift(1)
    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr_24"] = tr.rolling(24).mean()
    out["atr_pct"] = out["atr_24"] / out["close"] * 100
    out["vol_pct"] = out["atr_pct"].rolling(120).apply(
        lambda x: float((x < x.iloc[-1]).sum() / len(x) * 100), raw=False
    )
    return out.dropna()


def simulate_volatility_adaptive_trend(
    coin: str,
    days: int,
    initial_capital: float = 1000.0,
    leverage: float = 2.0,
) -> VolAdaptiveTrendResult:
    df = _indicators(load_hourly(coin))
    end_ts = pd.Timestamp(datetime.now(timezone.utc))
    start_ts = end_ts - pd.Timedelta(days=days + 30)
    df = df[(df.index >= start_ts) & (df.index <= end_ts)].copy()
    sim_start = end_ts - pd.Timedelta(days=days)

    cash = initial_capital
    position = 0
    qty = 0.0
    entry_price = 0.0
    total_fees = 0.0
    n_trades = 0
    equity_curve = []

    for ts, row in df.iterrows():
        price = float(row["close"])
        spread = (float(row["ema_fast"]) - float(row["ema_slow"])) / max(float(row["ema_slow"]), 1e-9) * 100
        vol_pctile = float(row["vol_pct"])

        if vol_pctile >= 75:
            threshold = 0.60
            sizing = 0.50
        elif vol_pctile <= 25:
            threshold = 0.20
            sizing = 1.00
        else:
            threshold = 0.35
            sizing = 0.75

        desired = 0
        if spread > threshold:
            desired = 1
        elif spread < -threshold:
            desired = -1

        equity = cash
        if position == 1:
            equity += (price - entry_price) * qty
        elif position == -1:
            equity += (entry_price - price) * qty
        if ts >= sim_start:
            equity_curve.append((ts, equity))

        if ts < sim_start or desired == position:
            continue

        if position != 0:
            pnl = (price - entry_price) * qty if position == 1 else (entry_price - price) * qty
            fee = price * qty * TOTAL_COST
            cash += pnl - fee
            total_fees += fee
            n_trades += 1
            position = 0
            qty = 0.0

        if desired != 0:
            notional = cash * leverage * sizing
            qty = notional / price
            fee = notional * TOTAL_COST
            cash -= fee
            total_fees += fee
            position = desired
            entry_price = price

    if position != 0:
        last_price = float(df["close"].iloc[-1])
        pnl = (last_price - entry_price) * qty if position == 1 else (entry_price - last_price) * qty
        fee = last_price * qty * TOTAL_COST
        cash += pnl - fee
        total_fees += fee
        n_trades += 1

    final_capital = cash
    return_pct = (final_capital - initial_capital) / initial_capital * 100
    equities = np.array([e[1] for e in equity_curve])
    if len(equities) >= 2:
        returns = np.diff(equities) / np.maximum(equities[:-1], 1e-9)
        sharpe = returns.mean() / returns.std() * np.sqrt(24 * 365) if len(returns) > 0 and returns.std() > 0 else 0.0
        peak = np.maximum.accumulate(equities)
        dd = (peak - equities) / np.maximum(peak, 1e-9)
        max_drawdown = float(dd.max() * 100)
    else:
        sharpe = 0.0
        max_drawdown = 0.0

    return VolAdaptiveTrendResult(
        coin=coin,
        days=days,
        initial=initial_capital,
        final=final_capital,
        return_pct=return_pct,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        n_trades=n_trades,
        total_fees=total_fees,
        avg_vol_percentile=float(df[df.index >= sim_start]["vol_pct"].mean()) if len(df[df.index >= sim_start]) else 0.0,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL", "XRP", "BNB"])
    parser.add_argument("--days", nargs="+", type=int, default=[180, 360])
    parser.add_argument("--capital", type=float, default=1000.0)
    args = parser.parse_args()

    print("\n  Volatility Adaptive Trend Following 백테스트")
    for coin in args.coins:
        for days in args.days:
            r = simulate_volatility_adaptive_trend(coin, days, args.capital)
            print(
                f"{coin} {days}d | ret={r.return_pct:+.2f}% | sharpe={r.sharpe:.2f} | "
                f"mdd={r.max_drawdown:.2f}% | trades={r.n_trades}"
            )


if __name__ == "__main__":
    main()
