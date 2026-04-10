"""
HMM Regime Detection 백테스트.

목표:
- BTC 1시간봉에서 HMM으로 시장 체제를 추정
- bullish / bearish / neutral state에 따라 long / short / flat 전환
- 운영 후보용 최소 검증용 메타-전략 백테스트
"""
from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import io
from pathlib import Path

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM


CACHE_DIR = Path(__file__).parent / ".cache"
FUTURES_FEE = 0.0004
SLIPPAGE = 0.0002
TOTAL_COST = FUTURES_FEE + SLIPPAGE


@dataclass
class HMMBacktestResult:
    coin: str
    days: int
    initial: float
    final: float
    return_pct: float
    sharpe: float
    max_drawdown: float
    n_trades: int
    total_fees: float
    bullish_state: int
    bearish_state: int
    neutral_state: int


@lru_cache(maxsize=16)
def load_hourly(coin: str) -> pd.DataFrame:
    path = CACHE_DIR / f"{coin}_USDT_1h.csv"
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce", utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df = df[df.index.notna()].sort_index()
    return df


def _features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["log_return"] = np.log(out["close"]).diff()
    out["vol_24"] = out["log_return"].rolling(24).std().fillna(0.0)
    out["mom_24"] = out["close"].pct_change(24).fillna(0.0)
    return out.dropna()


def simulate_hmm_regime(
    coin: str = "BTC",
    days: int = 180,
    initial_capital: float = 1000.0,
    leverage: float = 1.5,
    n_states: int = 3,
    warmup_days: int = 180,
) -> HMMBacktestResult:
    df = _features(load_hourly(coin))
    end_ts = pd.Timestamp(datetime.now(timezone.utc))
    start_ts = end_ts - pd.Timedelta(days=days + warmup_days)
    df = df[(df.index >= start_ts) & (df.index <= end_ts)].copy()
    if len(df) < (days + 30) * 24:
        raise ValueError(f"{coin} 데이터 부족: {len(df)}h")

    sim_start = end_ts - pd.Timedelta(days=days)
    train = df[df.index < sim_start]
    test = df[df.index >= sim_start].copy()
    if len(train) < 24 * 60 or len(test) < 24 * 30:
        raise ValueError("HMM 학습/평가 구간 부족")

    X_train = train[["log_return", "vol_24", "mom_24"]].values
    model = GaussianHMM(
        n_components=n_states,
        covariance_type="full",
        n_iter=200,
        random_state=42,
    )
    with contextlib.redirect_stderr(io.StringIO()):
        model.fit(X_train)

    train_states = model.predict(X_train)
    state_mean = {}
    for state in range(n_states):
        state_mean[state] = float(train["log_return"].values[train_states == state].mean()) if np.any(train_states == state) else 0.0

    sorted_states = sorted(state_mean.items(), key=lambda x: x[1])
    bearish_state = sorted_states[0][0]
    neutral_state = sorted_states[1][0] if len(sorted_states) > 2 else sorted_states[0][0]
    bullish_state = sorted_states[-1][0]

    X_test = test[["log_return", "vol_24", "mom_24"]].values
    test_states = model.predict(X_test)

    cash = initial_capital
    position = 0  # -1, 0, 1
    entry_price = 0.0
    qty = 0.0
    total_fees = 0.0
    n_trades = 0
    equity_curve = []

    for idx, (ts, row) in enumerate(test.iterrows()):
        price = float(row["close"])
        desired = 0
        state = int(test_states[idx])
        if state == bullish_state:
            desired = 1
        elif state == bearish_state:
            desired = -1

        equity = cash
        if position == 1:
            equity += (price - entry_price) * qty
        elif position == -1:
            equity += (entry_price - price) * qty
        equity_curve.append((ts, equity))

        if desired == position:
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
            notional = cash * leverage
            qty = notional / price
            fee = notional * TOTAL_COST
            cash -= fee
            total_fees += fee
            entry_price = price
            position = desired

    if position != 0:
        last_price = float(test["close"].iloc[-1])
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

    return HMMBacktestResult(
        coin=coin,
        days=days,
        initial=initial_capital,
        final=final_capital,
        return_pct=return_pct,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        n_trades=n_trades,
        total_fees=total_fees,
        bullish_state=bullish_state,
        bearish_state=bearish_state,
        neutral_state=neutral_state,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coin", default="BTC")
    parser.add_argument("--days", nargs="+", type=int, default=[180, 360])
    parser.add_argument("--capital", type=float, default=1000.0)
    args = parser.parse_args()

    print("\n  HMM Regime Detection 백테스트")
    for days in args.days:
        r = simulate_hmm_regime(args.coin, days, args.capital)
        print(
            f"{days}d | ret={r.return_pct:+.2f}% | sharpe={r.sharpe:.2f} | "
            f"mdd={r.max_drawdown:.2f}% | trades={r.n_trades}"
        )


if __name__ == "__main__":
    main()
