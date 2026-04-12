"""
Fear & Greed DCA 백테스트.

컨셉:
- 공포 구간(RSI < 30, 30일 변동 < -20%) → 적극 매수 (자본의 5%)
- 중립 구간 → 소량 매수 (자본의 1%)
- 탐욕 구간(RSI > 70, 30일 변동 > 20%) → 매수 중지 + 보유분 50% 매도
- 매주 1회 평가
- 장기 보유 전략 (트레이딩 아닌 accumulation)

학술: Fear-weighted DCA 7년 +1,100% (B&H 대비 +100%)
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
SPOT_FEE = 0.001
SLIPPAGE = 0.0002
TOTAL_COST = SPOT_FEE + SLIPPAGE


@dataclass
class FearGreedDCAResult:
    coin: str
    days: int
    initial: float
    final_value: float  # cash + holdings
    return_pct: float
    sharpe: float
    max_drawdown: float
    n_buys: int
    n_sells: int
    total_fees: float
    total_invested: float
    bh_return: float
    fear_buys: int
    greed_sells: int


@lru_cache(maxsize=16)
def load_daily(coin: str) -> pd.DataFrame:
    path = CACHE_DIR / f"{coin}_USDT_1d.csv"
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce", utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df = df[df.index.notna()]
    df.sort_index(inplace=True)
    return df


def simulate_fear_greed_dca(
    coin: str,
    days: int,
    initial_capital: float = 1000.0,
    fear_rsi: float = 30.0,
    greed_rsi: float = 70.0,
    fear_change_pct: float = -20.0,
    greed_change_pct: float = 20.0,
    fear_buy_pct: float = 0.05,   # 공포 시 현금의 5%
    normal_buy_pct: float = 0.01,  # 평상시 현금의 1%
    greed_sell_pct: float = 0.50,  # 탐욕 시 보유의 50%
    eval_interval_days: int = 7,
) -> FearGreedDCAResult:
    df = load_daily(coin)
    end_ts = pd.Timestamp(datetime.now(timezone.utc))
    start_ts = end_ts - pd.Timedelta(days=days + 60)
    df = df[(df.index >= start_ts) & (df.index <= end_ts)].copy()

    # RSI(14)
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi_14"] = 100 - (100 / (1 + rs))
    # 30일 변동
    df["change_30d"] = df["close"].pct_change(30) * 100

    sim_start = end_ts - pd.Timedelta(days=days)

    cash = initial_capital
    holdings = 0.0
    total_invested = 0.0
    total_fees = 0.0
    n_buys = 0
    n_sells = 0
    fear_buys = 0
    greed_sells = 0
    last_eval_idx = -9999
    equity_curve = []

    for idx, (ts, row) in enumerate(df.iterrows()):
        price = float(row["close"])
        equity = cash + holdings * price
        if ts >= sim_start:
            equity_curve.append((ts, equity))
        if ts < sim_start:
            continue

        # 매주 평가
        if idx - last_eval_idx < eval_interval_days:
            continue
        last_eval_idx = idx

        rsi = float(row["rsi_14"]) if pd.notna(row["rsi_14"]) else 50
        change = float(row["change_30d"]) if pd.notna(row["change_30d"]) else 0

        # 공포 구간 → 적극 매수
        if rsi < fear_rsi or change < fear_change_pct:
            buy_amount = cash * fear_buy_pct
            if buy_amount > 5:  # 최소 5 USDT
                fee = buy_amount * TOTAL_COST
                qty = (buy_amount - fee) / price
                cash -= buy_amount
                holdings += qty
                total_invested += buy_amount
                total_fees += fee
                n_buys += 1
                fear_buys += 1

        # 탐욕 구간 → 부분 매도
        elif rsi > greed_rsi and change > greed_change_pct:
            if holdings > 0:
                sell_qty = holdings * greed_sell_pct
                proceeds = sell_qty * price
                fee = proceeds * TOTAL_COST
                cash += proceeds - fee
                holdings -= sell_qty
                total_fees += fee
                n_sells += 1
                greed_sells += 1

        # 중립 구간 → 소량 매수
        else:
            buy_amount = cash * normal_buy_pct
            if buy_amount > 5:
                fee = buy_amount * TOTAL_COST
                qty = (buy_amount - fee) / price
                cash -= buy_amount
                holdings += qty
                total_invested += buy_amount
                total_fees += fee
                n_buys += 1

    last_price = float(df["close"].iloc[-1])
    final_value = cash + holdings * last_price
    ret = (final_value - initial_capital) / initial_capital * 100

    sim_start_idx = df.index.searchsorted(sim_start)
    bh_s = float(df["close"].iloc[sim_start_idx])
    bh_e = float(df["close"].iloc[-1])
    bh_ret = (bh_e - bh_s) / bh_s * 100

    if len(equity_curve) >= 2:
        eq = np.array([e[1] for e in equity_curve])
        rets = np.diff(eq) / eq[:-1]
        sharpe = rets.mean() / rets.std() * np.sqrt(365) if rets.std() > 0 else 0
        peak = np.maximum.accumulate(eq)
        mdd = float(((peak - eq) / np.maximum(peak, 1e-9)).max() * 100)
    else:
        sharpe, mdd = 0.0, 0.0

    return FearGreedDCAResult(
        coin=coin, days=days, initial=initial_capital, final_value=final_value,
        return_pct=ret, sharpe=sharpe, max_drawdown=mdd,
        n_buys=n_buys, n_sells=n_sells, total_fees=total_fees,
        total_invested=total_invested, bh_return=bh_ret,
        fear_buys=fear_buys, greed_sells=greed_sells,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH"])
    parser.add_argument("--days", nargs="+", type=int, default=[180, 360, 540, 1000])
    parser.add_argument("--capital", type=float, default=1000.0)
    args = parser.parse_args()

    print(f"\n  Fear & Greed DCA 백테스트")
    for coin in args.coins:
        for d in args.days:
            try:
                r = simulate_fear_greed_dca(coin, d, args.capital)
                ann = r.return_pct * 365 / d
                print(f"  {coin} {d}d | ret={r.return_pct:+.2f}% (ann {ann:+.1f}%) | sharpe={r.sharpe:.2f} | "
                      f"mdd={r.max_drawdown:.2f}% | buys={r.n_buys}(fear {r.fear_buys}) sells={r.n_sells}(greed {r.greed_sells}) | "
                      f"fees={r.total_fees:.2f} | bh={r.bh_return:+.1f}% | invested={r.total_invested:.0f}")
            except Exception as e:
                print(f"  {coin} {d}d 실패: {e}")


if __name__ == "__main__":
    main()
