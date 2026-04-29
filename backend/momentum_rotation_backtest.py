"""
Momentum Rotation 선물 백테스트 (Long/Short Equity 스타일).

컨셉:
- 5코인 중 가장 강한 N개 long + 가장 약한 N개 short
- 상대적 강약이 수익원 — 시장 방향 무관 (달러 뉴트럴)
- 매주 리밸런싱

학술: Cross-sectional momentum (Cambridge JFQA 2024)
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
class MomentumRotationResult:
    days: int
    initial: float
    final: float
    return_pct: float
    sharpe: float
    max_drawdown: float
    n_rebalances: int
    total_fees: float
    bh_avg_return: float
    long_pnl: float
    short_pnl: float


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


def simulate_momentum_rotation(
    coins: list[str],
    days: int,
    initial_capital: float = 1000.0,
    lookback_days: int = 14,
    rebalance_days: int = 7,
    top_n: int = 2,
    bottom_n: int = 2,
    leverage: float = 2.0,
    regime_filter: bool = False,           # A: BTC 30d momentum < 0이면 롱 차단
    regime_lookback_days: int = 30,
    short_only: bool = False,              # B: 롱 비활성
    drawdown_pause_pct: float = 0.0,       # D: 자본 대비 -X% 도달 시 정지
    drawdown_resume_recover_pct: float = 0.5,  # 정지 후 회복률 (drawdown 0.5x 회복 시 재개)
) -> MomentumRotationResult:
    """Long/Short Momentum Rotation.

    매 rebalance_days:
    1. 각 코인의 lookback_days 수익률 계산
    2. 가장 강한 top_n → long
    3. 가장 약한 bottom_n → short
    4. 포지션 사이즈: 자본의 leverage / (top_n + bottom_n)
    5. 달러 뉴트럴 (long notional ≈ short notional)
    """
    dfs = {c: load_daily(c) for c in coins}
    common = dfs[coins[0]].index
    for c in coins[1:]:
        common = common.intersection(dfs[c].index)
    common = common.sort_values()
    for c in coins:
        dfs[c] = dfs[c].loc[common]

    end_ts = pd.Timestamp(datetime.now(timezone.utc))
    sim_start = end_ts - pd.Timedelta(days=days)

    cash = initial_capital
    positions: dict[str, dict] = {}  # coin -> {side, qty, entry}
    n_rebalances = 0
    total_fees = 0.0
    long_pnl = 0.0
    short_pnl = 0.0
    equity_curve = []
    last_rebalance_ts = None
    paused = False
    peak_equity = initial_capital
    drawdown_threshold = initial_capital * (drawdown_pause_pct / 100.0) if drawdown_pause_pct > 0 else 0

    for ts in common:
        if ts < sim_start - pd.Timedelta(days=lookback_days + 10):
            continue

        # equity
        eq = cash
        for coin, p in positions.items():
            price = float(dfs[coin].loc[ts, "close"])
            if p["side"] == "long":
                eq += (price - p["entry"]) * p["qty"]
            else:
                eq += (p["entry"] - price) * p["qty"]
        if ts >= sim_start:
            equity_curve.append((ts, eq))
            peak_equity = max(peak_equity, eq)

        if ts < sim_start:
            continue

        # D. drawdown pause / resume
        if drawdown_threshold > 0:
            current_dd = peak_equity - eq
            if not paused and current_dd >= drawdown_threshold:
                # 정지 — 모든 포지션 청산
                for coin, p in positions.items():
                    price = float(dfs[coin].loc[ts, "close"])
                    pnl = (price - p["entry"]) * p["qty"] if p["side"] == "long" else (p["entry"] - price) * p["qty"]
                    if p["side"] == "long":
                        long_pnl += pnl
                    else:
                        short_pnl += pnl
                    fee = p["qty"] * price * TOTAL_COST
                    cash += pnl - fee
                    total_fees += fee
                positions.clear()
                paused = True
                continue
            if paused:
                # 회복 체크: drawdown이 (1 - resume_recover_pct)로 줄어들면 재개
                if current_dd <= drawdown_threshold * (1 - drawdown_resume_recover_pct):
                    paused = False
                    last_rebalance_ts = None  # 즉시 리밸 가능하게
                else:
                    continue

        # 리밸런싱 주기
        if last_rebalance_ts is not None:
            elapsed = (ts - last_rebalance_ts).days
            if elapsed < rebalance_days:
                continue
        last_rebalance_ts = ts

        # 1. momentum 계산
        lookback_start = ts - pd.Timedelta(days=lookback_days)
        coin_returns = {}
        for coin in coins:
            cdf = dfs[coin]
            lb_idx = cdf.index.searchsorted(lookback_start, side="left")
            if lb_idx >= len(cdf):
                continue
            past = float(cdf["close"].iloc[lb_idx])
            curr = float(cdf.loc[ts, "close"])
            coin_returns[coin] = (curr - past) / past

        if len(coin_returns) < top_n + bottom_n:
            continue

        sorted_coins = sorted(coin_returns.items(), key=lambda x: x[1], reverse=True)
        target_longs = [c for c, _ in sorted_coins[:top_n]]
        target_shorts = [c for c, _ in sorted_coins[-bottom_n:]]

        # A. regime filter — BTC 30일 모멘텀 음수면 롱 차단
        if regime_filter and "BTC" in dfs:
            btc_df = dfs["BTC"]
            rg_start = ts - pd.Timedelta(days=regime_lookback_days)
            rg_idx = btc_df.index.searchsorted(rg_start, side="left")
            if rg_idx < len(btc_df):
                btc_past = float(btc_df["close"].iloc[rg_idx])
                btc_curr = float(btc_df.loc[ts, "close"])
                btc_mom = (btc_curr - btc_past) / btc_past
                if btc_mom < 0:
                    target_longs = []  # 약세장 → 롱 차단

        # B. short_only — 롱 비활성
        if short_only:
            target_longs = []

        # 2. 기존 포지션 청산
        for coin in list(positions.keys()):
            p = positions[coin]
            price = float(dfs[coin].loc[ts, "close"])
            if p["side"] == "long":
                pnl = (price - p["entry"]) * p["qty"]
                long_pnl += pnl
            else:
                pnl = (p["entry"] - price) * p["qty"]
                short_pnl += pnl
            fee = p["qty"] * price * TOTAL_COST
            cash += pnl - fee
            total_fees += fee
        positions.clear()

        # 3. 새 포지션 진입
        n_sides = len(target_longs) + len(target_shorts)
        if n_sides == 0:
            n_rebalances += 1
            continue
        per_side_notional = (cash * leverage) / n_sides

        for coin in target_longs:
            price = float(dfs[coin].loc[ts, "close"])
            qty = per_side_notional / price
            fee = qty * price * TOTAL_COST
            cash -= fee
            total_fees += fee
            positions[coin] = {"side": "long", "qty": qty, "entry": price}

        for coin in target_shorts:
            price = float(dfs[coin].loc[ts, "close"])
            qty = per_side_notional / price
            fee = qty * price * TOTAL_COST
            cash -= fee
            total_fees += fee
            positions[coin] = {"side": "short", "qty": qty, "entry": price}

        n_rebalances += 1

    # 종료 청산
    last_price = {}
    for coin in coins:
        last_price[coin] = float(dfs[coin]["close"].iloc[-1])
    for coin, p in positions.items():
        price = last_price[coin]
        if p["side"] == "long":
            pnl = (price - p["entry"]) * p["qty"]
            long_pnl += pnl
        else:
            pnl = (p["entry"] - price) * p["qty"]
            short_pnl += pnl
        fee = p["qty"] * price * TOTAL_COST
        cash += pnl - fee
        total_fees += fee

    final = cash
    ret = (final - initial_capital) / initial_capital * 100

    # B&H 비교
    bh_rets = []
    for coin in coins:
        cdf = dfs[coin]
        si = cdf.index.searchsorted(sim_start)
        if si < len(cdf):
            bh_s = float(cdf["close"].iloc[si])
            bh_e = float(cdf["close"].iloc[-1])
            bh_rets.append((bh_e - bh_s) / bh_s * 100)
    bh_avg = np.mean(bh_rets) if bh_rets else 0

    if len(equity_curve) >= 2:
        eq = np.array([e[1] for e in equity_curve])
        rets = np.diff(eq) / eq[:-1]
        sharpe = rets.mean() / rets.std() * np.sqrt(365) if rets.std() > 0 else 0
        peak = np.maximum.accumulate(eq)
        mdd = float(((peak - eq) / np.maximum(peak, 1e-9)).max() * 100)
    else:
        sharpe, mdd = 0.0, 0.0

    return MomentumRotationResult(
        days=days, initial=initial_capital, final=final,
        return_pct=ret, sharpe=sharpe, max_drawdown=mdd,
        n_rebalances=n_rebalances, total_fees=total_fees,
        bh_avg_return=bh_avg, long_pnl=long_pnl, short_pnl=short_pnl,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL", "XRP", "BNB"])
    parser.add_argument("--days", nargs="+", type=int, default=[90, 180, 360])
    parser.add_argument("--lookback", type=int, default=14)
    parser.add_argument("--rebalance", type=int, default=7)
    parser.add_argument("--top-n", type=int, default=2)
    parser.add_argument("--bottom-n", type=int, default=2)
    parser.add_argument("--leverage", type=float, default=2.0)
    parser.add_argument("--capital", type=float, default=1000.0)
    args = parser.parse_args()

    print(f"\n  Momentum Rotation 백테스트 (선물 Long/Short)")
    print(f"  코인: {', '.join(args.coins)}, lookback {args.lookback}d, 리밸런싱 {args.rebalance}d")
    print(f"  top-{args.top_n} long + bottom-{args.bottom_n} short, leverage {args.leverage}x")

    for d in args.days:
        try:
            r = simulate_momentum_rotation(
                args.coins, d, args.capital,
                lookback_days=args.lookback,
                rebalance_days=args.rebalance,
                top_n=args.top_n,
                bottom_n=args.bottom_n,
                leverage=args.leverage,
            )
            ann = r.return_pct * 365 / d
            print(f"  {d}d | ret={r.return_pct:+.2f}% (ann {ann:+.1f}%) | sharpe={r.sharpe:.2f} | "
                  f"mdd={r.max_drawdown:.2f}% | rebal={r.n_rebalances} | fees={r.total_fees:.2f} | "
                  f"bh={r.bh_avg_return:+.1f}% | L_pnl={r.long_pnl:+.1f} S_pnl={r.short_pnl:+.1f}")
        except Exception as e:
            print(f"  {d}d 실패: {e}")


if __name__ == "__main__":
    main()
