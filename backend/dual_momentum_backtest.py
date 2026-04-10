"""
Dual Momentum 백테스트 — Gary Antonacci 학술 검증 전략.

원리:
1. **Absolute momentum**: 자산 12개월 수익률 > 0 → 보유, 아니면 USDT (현금)
2. **Relative momentum**: 5코인 중 가장 강한 N개에 집중

매월 1회 재평가 → 매매 빈도 매우 낮음, 비용 거의 없음.
강세장 → 강한 코인 보유, 약세장 → 현금 회피 (drawdown 자동 차단).
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import pandas as pd
import numpy as np

CACHE_DIR = Path(__file__).parent / ".cache"

SPOT_FEE = 0.001       # 0.10%
SLIPPAGE = 0.0002      # 0.02%
TOTAL_COST = SPOT_FEE + SLIPPAGE


@dataclass
class DualMomentumResult:
    coins: list[str]
    days: int
    initial: float
    final: float
    return_pct: float
    sharpe: float
    max_drawdown: float
    n_rebalances: int
    total_fees: float
    bh_avg_return: float  # 5코인 균등 B&H
    cash_periods: int    # 현금 보유 기간 (월)


@dataclass
class DualMomentumSweepRow:
    lookback_days: int
    rebalance_days: int
    top_n: int
    days: int
    return_pct: float
    sharpe: float
    max_drawdown: float
    cash_periods: int
    total_fees: float


@lru_cache(maxsize=32)
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


def simulate_dual_momentum(
    coins: list[str],
    days: int,
    initial_capital: float = 1000.0,
    lookback_days: int = 365,    # 12개월 momentum
    top_n: int = 1,               # 가장 강한 코인 N개에 집중
    rebalance_days: int = 30,     # 매월 재평가
) -> DualMomentumResult:
    """Dual Momentum 시뮬레이션.

    매 rebalance_days마다:
    1. 각 코인의 lookback_days 수익률 계산
    2. 양수 수익률 코인 중 top_n 선택
    3. 양수 코인 없으면 USDT 보유
    4. 동일 비중 분배
    """
    dfs = {c: load_daily(c) for c in coins}

    # 공통 인덱스
    common = dfs[coins[0]].index
    for c in coins[1:]:
        common = common.intersection(dfs[c].index)
    for c in coins:
        dfs[c] = dfs[c].loc[common]

    end_ts = pd.Timestamp(datetime.now(timezone.utc))
    start_ts = end_ts - pd.Timedelta(days=days + lookback_days + 30)
    common = common[(common >= start_ts) & (common <= end_ts)]

    sim_start_ts = end_ts - pd.Timedelta(days=days)

    cash = initial_capital
    holdings: dict[str, float] = {}  # coin → quantity
    n_rebalances = 0
    total_fees = 0.0
    cash_periods = 0
    equity_curve = []
    last_rebalance_ts: pd.Timestamp | None = None

    for ts in common:
        # 현재 자산 평가
        equity = cash
        for coin, qty in holdings.items():
            price = float(dfs[coin].loc[ts, "close"])
            equity += qty * price
        if ts >= sim_start_ts:
            equity_curve.append((ts, equity))

        # 시뮬레이션 시작 전이면 평가만
        if ts < sim_start_ts:
            continue

        # 재평가 주기 체크
        if last_rebalance_ts is not None:
            elapsed_days = (ts - last_rebalance_ts).days
            if elapsed_days < rebalance_days:
                continue
        last_rebalance_ts = ts

        # 1. 각 코인의 lookback 수익률 계산
        lookback_start = ts - pd.Timedelta(days=lookback_days)
        coin_returns = {}
        for coin in coins:
            coin_df = dfs[coin]
            # lookback 시점 가격
            lookback_idx = coin_df.index.searchsorted(lookback_start, side="left")
            if lookback_idx >= len(coin_df):
                continue
            past_price = float(coin_df["close"].iloc[lookback_idx])
            current_price = float(coin_df.loc[ts, "close"])
            coin_returns[coin] = (current_price - past_price) / past_price

        # 2. 양수 수익률만 + top_n 선택
        positive_coins = {c: r for c, r in coin_returns.items() if r > 0}
        sorted_coins = sorted(positive_coins.items(), key=lambda x: -x[1])
        target_coins = [c for c, _ in sorted_coins[:top_n]]

        if not target_coins:
            cash_periods += 1

        # 3. 리밸런싱
        # 3a. 청산 (target에 없는 보유)
        for coin in list(holdings.keys()):
            if coin not in target_coins:
                qty = holdings.pop(coin)
                price = float(dfs[coin].loc[ts, "close"])
                cost = qty * price
                fee = cost * TOTAL_COST
                cash += cost - fee
                total_fees += fee
                n_rebalances += 1

        # 3b. 신규 매수 (target 중 미보유)
        if target_coins:
            per_coin_cash = cash / len(target_coins) / max(1, len(target_coins) - len(holdings) + 1)
            # 더 정확히: 균등 분배 → 각 코인이 (자산 총합 / N) 비중
            current_equity = cash + sum(
                qty * float(dfs[c].loc[ts, "close"]) for c, qty in holdings.items()
            )
            target_value = current_equity / len(target_coins)

            for coin in target_coins:
                price = float(dfs[coin].loc[ts, "close"])
                current_value = holdings.get(coin, 0) * price
                diff_value = target_value - current_value

                if diff_value > 5:  # 매수 (5 USDT 이상 차이)
                    buy_amount = min(diff_value, cash * 0.99)
                    if buy_amount <= 0:
                        continue
                    qty = buy_amount / price
                    fee = buy_amount * TOTAL_COST
                    if buy_amount + fee > cash:
                        buy_amount = cash / (1 + TOTAL_COST)
                        qty = buy_amount / price
                        fee = buy_amount * TOTAL_COST
                    cash -= buy_amount + fee
                    total_fees += fee
                    holdings[coin] = holdings.get(coin, 0) + qty
                    n_rebalances += 1
                elif diff_value < -5:  # 매도 (초과 분량)
                    sell_qty = -diff_value / price
                    sell_qty = min(sell_qty, holdings.get(coin, 0))
                    if sell_qty <= 0:
                        continue
                    cost = sell_qty * price
                    fee = cost * TOTAL_COST
                    cash += cost - fee
                    total_fees += fee
                    holdings[coin] -= sell_qty
                    if holdings[coin] <= 0:
                        del holdings[coin]
                    n_rebalances += 1

    # 종료 시 청산
    for coin, qty in list(holdings.items()):
        price = float(dfs[coin]["close"].iloc[-1])
        cost = qty * price
        fee = cost * TOTAL_COST
        cash += cost - fee
        total_fees += fee

    final_capital = cash
    return_pct = (final_capital - initial_capital) / initial_capital * 100

    # 5코인 균등 B&H 비교
    bh_returns = []
    for coin in coins:
        df = dfs[coin]
        start_idx = df.index.searchsorted(sim_start_ts)
        if start_idx >= len(df):
            continue
        bh_start = float(df["close"].iloc[start_idx])
        bh_end = float(df["close"].iloc[-1])
        bh_returns.append((bh_end - bh_start) / bh_start * 100)
    bh_avg = np.mean(bh_returns) if bh_returns else 0

    if len(equity_curve) >= 2:
        equities = np.array([e[1] for e in equity_curve])
        returns = np.diff(equities) / equities[:-1]
        if len(returns) > 0 and returns.std() > 0:
            sharpe = returns.mean() / returns.std() * np.sqrt(365)
        else:
            sharpe = 0.0
        peak = np.maximum.accumulate(equities)
        dd = (peak - equities) / np.maximum(peak, 1e-9)
        max_drawdown = float(dd.max() * 100)
    else:
        sharpe = 0.0
        max_drawdown = 0.0

    return DualMomentumResult(
        coins=coins, days=days, initial=initial_capital, final=final_capital,
        return_pct=return_pct, sharpe=sharpe, max_drawdown=max_drawdown,
        n_rebalances=n_rebalances, total_fees=total_fees,
        bh_avg_return=bh_avg, cash_periods=cash_periods,
    )


def print_result(r: DualMomentumResult, label: str = ""):
    print(f"\n{'='*60}")
    print(f"  Dual Momentum {label}")
    print(f"{'='*60}")
    print(f"  코인:           {', '.join(r.coins)}")
    print(f"  기간:           {r.days}일")
    print(f"  초기:           {r.initial:>10,.2f}")
    print(f"  최종:           {r.final:>10,.2f}")
    print(f"  순수익:         {r.final - r.initial:>+10,.2f} ({r.return_pct:+.2f}%)")
    print(f"  연환산:         {r.return_pct * 365 / r.days:>+10.2f}%")
    print(f"  B&H 균등 비교:  {r.bh_avg_return:>+10.2f}% (alpha: {r.return_pct - r.bh_avg_return:+.2f}%)")
    print(f"  리밸런싱 수:    {r.n_rebalances:>10}")
    print(f"  현금 보유 기간: {r.cash_periods:>10} 회")
    print(f"  총 수수료:      {r.total_fees:>10,.2f}")
    print(f"  Sharpe:         {r.sharpe:>10.2f}")
    print(f"  Max Drawdown:   {r.max_drawdown:>10.2f}%")
    print(f"{'='*60}")


def run_sweep(
    coins: list[str],
    days: int,
    capital: float,
    lookbacks: list[int],
    rebalances: list[int],
    top_ns: list[int],
) -> list[DualMomentumSweepRow]:
    rows: list[DualMomentumSweepRow] = []
    for lookback in lookbacks:
        for rebalance in rebalances:
            for top_n in top_ns:
                r = simulate_dual_momentum(
                    coins,
                    days,
                    capital,
                    lookback_days=lookback,
                    top_n=top_n,
                    rebalance_days=rebalance,
                )
                rows.append(
                    DualMomentumSweepRow(
                        lookback_days=lookback,
                        rebalance_days=rebalance,
                        top_n=top_n,
                        days=days,
                        return_pct=r.return_pct,
                        sharpe=r.sharpe,
                        max_drawdown=r.max_drawdown,
                        cash_periods=r.cash_periods,
                        total_fees=r.total_fees,
                    )
                )
    rows.sort(key=lambda row: (row.return_pct, row.sharpe, -row.max_drawdown), reverse=True)
    return rows


def print_sweep(rows: list[DualMomentumSweepRow], limit: int = 20):
    print(f"\n{'='*92}")
    print("  Dual Momentum Sweep")
    print(f"{'='*92}")
    print("  rank  lookback  rebalance  top_n   return    sharpe   max_dd   cash  fees")
    for idx, row in enumerate(rows[:limit], start=1):
        print(
            f"  {idx:>4}  {row.lookback_days:>8}  {row.rebalance_days:>9}  "
            f"{row.top_n:>5}  {row.return_pct:>7.2f}%  {row.sharpe:>8.2f}  "
            f"{row.max_drawdown:>7.2f}%  {row.cash_periods:>4}  {row.total_fees:>5.2f}"
        )
    print(f"{'='*92}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL", "XRP", "BNB"])
    parser.add_argument("--top-n", type=int, default=1, help="가장 강한 N개 보유")
    parser.add_argument("--lookback", type=int, default=365, help="momentum lookback (일)")
    parser.add_argument("--rebalance", type=int, default=30, help="재평가 주기 (일)")
    parser.add_argument("--periods", nargs="+", type=int, default=[180, 360, 540, 1000])
    parser.add_argument("--days", type=int, default=180, help="sweep 모드 평가 기간 (일)")
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--sweep", action="store_true", help="여러 lookback/rebalance/top-n 조합 일괄 평가")
    parser.add_argument("--sweep-lookbacks", nargs="+", type=int, default=[60, 90, 120, 180])
    parser.add_argument("--sweep-rebalances", nargs="+", type=int, default=[7, 14, 30])
    parser.add_argument("--sweep-top-n-options", nargs="+", type=int, default=[1, 2])
    parser.add_argument("--top-k", type=int, default=20, help="sweep 출력 상위 개수")
    args = parser.parse_args()

    print(f"\n  Dual Momentum 백테스트")
    print(f"  코인: {', '.join(args.coins)}, top-{args.top_n}, lookback {args.lookback}일, 재평가 {args.rebalance}일마다")

    if args.sweep:
        rows = run_sweep(
            args.coins,
            args.days,
            args.capital,
            args.sweep_lookbacks,
            args.sweep_rebalances,
            args.sweep_top_n_options,
        )
        print_sweep(rows, limit=args.top_k)
        return

    for d in args.periods:
        try:
            r = simulate_dual_momentum(
                args.coins, d, args.capital,
                lookback_days=args.lookback,
                top_n=args.top_n,
                rebalance_days=args.rebalance,
            )
            print_result(r, label=f"({d}d)")
        except Exception as e:
            print(f"  {d}d 실패: {e}")


if __name__ == "__main__":
    main()
