"""
Pairs Trading 백테스트 — BTC-ETH 코인테그레이션 기반.

전략:
- BTC와 ETH의 z-score 스프레드 계산 (cointegration)
- z > 2: short BTC + long ETH (스프레드 축소 베팅)
- z < -2: long BTC + short ETH
- |z| < 0.5: 청산

참고: Sharpe 2.45, 연 16.34% (학술 검증)
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from pathlib import Path
import pandas as pd
import numpy as np

CACHE_DIR = Path(__file__).parent / ".cache"

# 비용 (선물 양쪽: BTC futures + ETH futures)
FUTURES_FEE = 0.0004  # 0.04%
SLIPPAGE = 0.0001  # 0.01%
TOTAL_COST = FUTURES_FEE + SLIPPAGE  # round-trip × 2 sides에서 양쪽 계산


@dataclass
class PairsBacktestResult:
    days: int
    initial: float
    final: float
    return_pct: float
    sharpe: float
    max_drawdown: float
    n_trades: int
    n_wins: int
    n_losses: int
    total_fees: float


@dataclass
class PairsGridRow:
    lookback_hours: int
    z_entry: float
    z_exit: float
    z_stop: float
    leverage: float
    days: int
    return_pct: float
    sharpe: float
    max_drawdown: float
    n_trades: int


@dataclass
class WalkForwardWindow:
    train_end: pd.Timestamp
    test_end: pd.Timestamp
    lookback_hours: int
    z_entry: float
    z_exit: float
    z_stop: float
    leverage: float
    train_return_pct: float
    train_sharpe: float
    train_max_drawdown: float
    test_return_pct: float
    test_sharpe: float
    test_max_drawdown: float


@lru_cache(maxsize=64)
def load_price(coin: str, tf: str = "1h") -> pd.DataFrame:
    path = CACHE_DIR / f"{coin}_USDT_{tf}.csv"
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce", utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df = df[df.index.notna()]
    df.sort_index(inplace=True)
    return df


def calculate_hedge_ratio(btc_log: pd.Series, eth_log: pd.Series) -> float:
    """OLS 회귀로 hedge ratio 계산."""
    # ETH = beta * BTC + alpha
    n = len(btc_log)
    if n < 30:
        return 1.0
    x_mean = btc_log.mean()
    y_mean = eth_log.mean()
    cov = ((btc_log - x_mean) * (eth_log - y_mean)).sum()
    var_x = ((btc_log - x_mean) ** 2).sum()
    return float(cov / var_x) if var_x > 0 else 1.0


def simulate_pairs_trading(
    days: int,
    initial_capital: float = 1000.0,
    lookback_hours: int = 168 * 2,  # 14일
    z_entry: float = 2.0,
    z_exit: float = 0.5,
    z_stop: float = 4.0,
    leverage: float = 2.0,
    coin_a: str = "BTC",
    coin_b: str = "ETH",
    end_time: pd.Timestamp | None = None,
) -> PairsBacktestResult:
    """BTC-ETH pairs trading.

    매 1h마다:
    1. lookback_hours 기간의 log price spread 계산
    2. hedge ratio (OLS) 추정
    3. spread = log(B) - hedge*log(A)
    4. z = (spread - mean) / std
    5. |z| > z_entry → 진입, |z| < z_exit → 청산, |z| > z_stop → 손절
    """
    df_a = load_price(coin_a, "1h")
    df_b = load_price(coin_b, "1h")

    # 공통 인덱스
    common = df_a.index.intersection(df_b.index)
    df_a = df_a.loc[common]
    df_b = df_b.loc[common]

    # 시작 시점
    end_ts = pd.Timestamp(end_time) if end_time is not None else pd.Timestamp(datetime.now(timezone.utc))
    start_ts = end_ts - pd.Timedelta(days=days)
    mask = (common >= start_ts) & (common <= end_ts)
    df_a = df_a[mask]
    df_b = df_b[mask]

    if len(df_a) < lookback_hours + 100:
        raise ValueError(f"데이터 부족: {len(df_a)}h < {lookback_hours+100}h")

    cash = initial_capital
    equity_curve = [(df_a.index[0], cash)]
    position = None  # None / "long_a_short_b" / "short_a_long_b"
    entry_z = 0.0
    entry_a_qty = 0.0
    entry_b_qty = 0.0
    entry_a_price = 0.0
    entry_b_price = 0.0
    hedge = 1.0

    n_trades = 0
    n_wins = 0
    n_losses = 0
    total_fees = 0.0

    log_a = np.log(df_a["close"].values)
    log_b = np.log(df_b["close"].values)

    for i in range(lookback_hours, len(df_a)):
        ts = df_a.index[i]
        price_a = float(df_a["close"].iloc[i])
        price_b = float(df_b["close"].iloc[i])

        # 윈도우
        win_log_a = log_a[i - lookback_hours:i]
        win_log_b = log_b[i - lookback_hours:i]

        # hedge ratio 추정
        hedge = calculate_hedge_ratio(pd.Series(win_log_a), pd.Series(win_log_b))

        # spread + z-score
        win_spread = win_log_b - hedge * win_log_a
        spread_mean = win_spread.mean()
        spread_std = win_spread.std()
        if spread_std == 0:
            equity_curve.append((ts, cash))
            continue

        current_spread = np.log(price_b) - hedge * np.log(price_a)
        z = (current_spread - spread_mean) / spread_std

        # equity 업데이트
        if position is not None:
            # 현재 포지션 P&L
            if position == "long_a_short_b":
                # long A → (price_a - entry_a) * qty_a
                # short B → (entry_b - price_b) * qty_b
                pnl = ((price_a - entry_a_price) * entry_a_qty
                       + (entry_b_price - price_b) * entry_b_qty)
            else:  # short_a_long_b
                pnl = ((entry_a_price - price_a) * entry_a_qty
                       + (price_b - entry_b_price) * entry_b_qty)
            current_equity = cash + pnl
        else:
            current_equity = cash
        equity_curve.append((ts, current_equity))

        # 포지션 관리
        if position is None:
            # 진입 조건
            if z > z_entry:
                # spread 너무 큼 → mean 회귀 베팅 → spread 축소: short B + long A
                position = "long_a_short_b"
                entry_z = z
                # 자본 절반씩 (delta neutral 비슷)
                notional = (cash * leverage) / 2
                entry_a_qty = notional / price_a
                entry_b_qty = notional / price_b
                entry_a_price = price_a
                entry_b_price = price_b
                fee = notional * 2 * TOTAL_COST  # 양쪽 수수료
                cash -= fee
                total_fees += fee
            elif z < -z_entry:
                position = "short_a_long_b"
                entry_z = z
                notional = (cash * leverage) / 2
                entry_a_qty = notional / price_a
                entry_b_qty = notional / price_b
                entry_a_price = price_a
                entry_b_price = price_b
                fee = notional * 2 * TOTAL_COST
                cash -= fee
                total_fees += fee
        else:
            # 청산 조건
            should_exit = False
            if abs(z) <= z_exit:
                should_exit = True
            elif abs(z) >= z_stop:
                should_exit = True

            if should_exit:
                # 청산 P&L
                if position == "long_a_short_b":
                    pnl = ((price_a - entry_a_price) * entry_a_qty
                           + (entry_b_price - price_b) * entry_b_qty)
                else:
                    pnl = ((entry_a_price - price_a) * entry_a_qty
                           + (price_b - entry_b_price) * entry_b_qty)

                exit_notional = entry_a_qty * price_a
                fee = exit_notional * 2 * TOTAL_COST
                total_fees += fee
                cash += pnl - fee
                n_trades += 1
                if pnl - fee > 0:
                    n_wins += 1
                else:
                    n_losses += 1
                position = None

    # 종료 시 강제 청산
    if position is not None:
        price_a = float(df_a["close"].iloc[-1])
        price_b = float(df_b["close"].iloc[-1])
        if position == "long_a_short_b":
            pnl = ((price_a - entry_a_price) * entry_a_qty
                   + (entry_b_price - price_b) * entry_b_qty)
        else:
            pnl = ((entry_a_price - price_a) * entry_a_qty
                   + (price_b - entry_b_price) * entry_b_qty)
        exit_notional = entry_a_qty * price_a
        fee = exit_notional * 2 * TOTAL_COST
        cash += pnl - fee
        total_fees += fee
        n_trades += 1
        if pnl - fee > 0:
            n_wins += 1
        else:
            n_losses += 1

    final_capital = cash
    return_pct = (final_capital - initial_capital) / initial_capital * 100

    if len(equity_curve) >= 2:
        equities = np.array([e[1] for e in equity_curve])
        returns = np.diff(equities) / equities[:-1]
        if len(returns) > 0 and returns.std() > 0:
            sharpe = returns.mean() / returns.std() * np.sqrt(24 * 365)
        else:
            sharpe = 0.0
        peak = np.maximum.accumulate(equities)
        dd = (peak - equities) / np.maximum(peak, 1e-9)
        max_drawdown = float(dd.max() * 100)
    else:
        sharpe = 0.0
        max_drawdown = 0.0

    return PairsBacktestResult(
        days=days, initial=initial_capital, final=final_capital,
        return_pct=return_pct, sharpe=sharpe, max_drawdown=max_drawdown,
        n_trades=n_trades, n_wins=n_wins, n_losses=n_losses, total_fees=total_fees,
    )


def parameter_score(r: PairsBacktestResult) -> float:
    return r.return_pct + (r.sharpe * 5.0) - (r.max_drawdown * 0.75)


def iter_param_grid(
    lookbacks: list[int],
    z_entries: list[float],
    z_exits: list[float],
    z_stops: list[float],
    leverages: list[float],
):
    for lookback in lookbacks:
        for z_entry in z_entries:
            for z_exit in z_exits:
                for z_stop in z_stops:
                    for leverage in leverages:
                        if z_exit >= z_entry or z_stop <= z_entry:
                            continue
                        yield lookback, z_entry, z_exit, z_stop, leverage


def run_grid_search(
    days: int,
    capital: float,
    coin_a: str,
    coin_b: str,
    lookbacks: list[int],
    z_entries: list[float],
    z_exits: list[float],
    z_stops: list[float],
    leverages: list[float],
) -> list[PairsGridRow]:
    rows: list[PairsGridRow] = []
    for lookback, z_entry, z_exit, z_stop, leverage in iter_param_grid(
        lookbacks, z_entries, z_exits, z_stops, leverages
    ):
        try:
            r = simulate_pairs_trading(
                days,
                capital,
                lookback_hours=lookback,
                z_entry=z_entry,
                z_exit=z_exit,
                z_stop=z_stop,
                leverage=leverage,
                coin_a=coin_a,
                coin_b=coin_b,
            )
        except Exception:
            continue
        rows.append(
            PairsGridRow(
                lookback_hours=lookback,
                z_entry=z_entry,
                z_exit=z_exit,
                z_stop=z_stop,
                leverage=leverage,
                days=days,
                return_pct=r.return_pct,
                sharpe=r.sharpe,
                max_drawdown=r.max_drawdown,
                n_trades=r.n_trades,
            )
        )
    rows.sort(key=lambda row: (row.return_pct, row.sharpe, -row.max_drawdown), reverse=True)
    return rows


def print_grid(rows: list[PairsGridRow], limit: int = 20):
    print(f"\n{'='*116}")
    print("  Pairs Trading Grid Search")
    print(f"{'='*116}")
    print("  rank  lookback  z_entry  z_exit  z_stop  lev   return   sharpe   max_dd  trades")
    for idx, row in enumerate(rows[:limit], start=1):
        print(
            f"  {idx:>4}  {row.lookback_hours:>8}  {row.z_entry:>7.2f}  {row.z_exit:>6.2f}  "
            f"{row.z_stop:>6.2f}  {row.leverage:>3.1f}  {row.return_pct:>7.2f}%  "
            f"{row.sharpe:>7.2f}  {row.max_drawdown:>7.2f}%  {row.n_trades:>6}"
        )
    print(f"{'='*116}")


def run_walk_forward(
    capital: float,
    coin_a: str,
    coin_b: str,
    train_days: int,
    test_days: int,
    max_windows: int,
    lookbacks: list[int],
    z_entries: list[float],
    z_exits: list[float],
    z_stops: list[float],
    leverages: list[float],
) -> list[WalkForwardWindow]:
    df_a = load_price(coin_a, "1h")
    df_b = load_price(coin_b, "1h")
    common = df_a.index.intersection(df_b.index).sort_values()
    latest = common[-1]
    earliest_required = common[0] + pd.Timedelta(days=train_days + test_days)

    windows: list[WalkForwardWindow] = []
    test_end = latest
    while test_end >= earliest_required and len(windows) < max_windows:
        train_end = test_end - pd.Timedelta(days=test_days)
        best_params = None
        best_train = None
        best_score = -float("inf")

        for lookback, z_entry, z_exit, z_stop, leverage in iter_param_grid(
            lookbacks, z_entries, z_exits, z_stops, leverages
        ):
            try:
                train_result = simulate_pairs_trading(
                    train_days,
                    capital,
                    lookback_hours=lookback,
                    z_entry=z_entry,
                    z_exit=z_exit,
                    z_stop=z_stop,
                    leverage=leverage,
                    coin_a=coin_a,
                    coin_b=coin_b,
                    end_time=train_end,
                )
            except Exception:
                continue
            score = parameter_score(train_result)
            if score > best_score:
                best_score = score
                best_params = (lookback, z_entry, z_exit, z_stop, leverage)
                best_train = train_result

        if best_params is None or best_train is None:
            break

        lookback, z_entry, z_exit, z_stop, leverage = best_params
        test_result = simulate_pairs_trading(
            test_days,
            capital,
            lookback_hours=lookback,
            z_entry=z_entry,
            z_exit=z_exit,
            z_stop=z_stop,
            leverage=leverage,
            coin_a=coin_a,
            coin_b=coin_b,
            end_time=test_end,
        )
        windows.append(
            WalkForwardWindow(
                train_end=train_end,
                test_end=test_end,
                lookback_hours=lookback,
                z_entry=z_entry,
                z_exit=z_exit,
                z_stop=z_stop,
                leverage=leverage,
                train_return_pct=best_train.return_pct,
                train_sharpe=best_train.sharpe,
                train_max_drawdown=best_train.max_drawdown,
                test_return_pct=test_result.return_pct,
                test_sharpe=test_result.sharpe,
                test_max_drawdown=test_result.max_drawdown,
            )
        )
        test_end = train_end

    windows.reverse()
    return windows


def print_walk_forward(windows: list[WalkForwardWindow], capital: float):
    print(f"\n{'='*136}")
    print("  Pairs Trading Walk-Forward")
    print(f"{'='*136}")
    print("  window  train_end              test_end               params                         train_ret  train_dd  test_ret  test_dd")
    compounded = capital
    for idx, window in enumerate(windows, start=1):
        compounded *= 1 + (window.test_return_pct / 100.0)
        params = (
            f"lb={window.lookback_hours}, ze={window.z_entry:.2f}, zx={window.z_exit:.2f}, "
            f"zs={window.z_stop:.2f}, lev={window.leverage:.1f}"
        )
        print(
            f"  {idx:>6}  {window.train_end.isoformat():<22}  {window.test_end.isoformat():<22}  "
            f"{params:<28}  {window.train_return_pct:>8.2f}%  {window.train_max_drawdown:>8.2f}%  "
            f"{window.test_return_pct:>7.2f}%  {window.test_max_drawdown:>7.2f}%"
        )
    if windows:
        avg_test_return = np.mean([w.test_return_pct for w in windows])
        avg_test_dd = np.mean([w.test_max_drawdown for w in windows])
        wins = sum(1 for w in windows if w.test_return_pct > 0)
        print(f"{'-'*136}")
        print(
            f"  windows={len(windows)}  avg_test_return={avg_test_return:+.2f}%  "
            f"avg_test_drawdown={avg_test_dd:.2f}%  winning_windows={wins}/{len(windows)}  "
            f"compounded_capital={compounded:,.2f}"
        )
    print(f"{'='*136}")


def print_result(r: PairsBacktestResult, label: str = ""):
    print(f"\n{'='*60}")
    print(f"  Pairs Trading 백테스트 {label}")
    print(f"{'='*60}")
    print(f"  기간:           {r.days}일")
    print(f"  초기 자본:      {r.initial:>12,.2f} USDT")
    print(f"  최종 자본:      {r.final:>12,.2f} USDT")
    print(f"  순수익:         {r.final - r.initial:>+12,.2f} USDT ({r.return_pct:+.2f}%)")
    print(f"  연환산:         {r.return_pct * 365 / r.days:>+12.2f}%")
    print(f"  거래 수:        {r.n_trades:>12} (승 {r.n_wins} / 패 {r.n_losses})")
    win_rate = r.n_wins / max(r.n_trades, 1) * 100
    print(f"  승률:           {win_rate:>11.1f}%")
    print(f"  총 수수료:      {r.total_fees:>12,.2f} USDT")
    print(f"  Sharpe Ratio:   {r.sharpe:>12.2f}")
    print(f"  Max Drawdown:   {r.max_drawdown:>12.2f}%")
    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coin-a", default="BTC")
    parser.add_argument("--coin-b", default="ETH")
    parser.add_argument("--lookback", type=int, default=336, help="lookback hours (336=14일)")
    parser.add_argument("--z-entry", type=float, default=2.0)
    parser.add_argument("--z-exit", type=float, default=0.5)
    parser.add_argument("--z-stop", type=float, default=4.0)
    parser.add_argument("--leverage", type=float, default=2.0)
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--days", nargs="+", type=int, default=[90, 180, 360, 540, 1000])
    parser.add_argument("--grid-search", action="store_true", help="파라미터 조합 탐색")
    parser.add_argument("--walk-forward", action="store_true", help="train/test rolling validation")
    parser.add_argument("--train-days", type=int, default=180)
    parser.add_argument("--test-days", type=int, default=60)
    parser.add_argument("--max-windows", type=int, default=6)
    parser.add_argument("--lookbacks", nargs="+", type=int, default=[168, 336, 504, 720])
    parser.add_argument("--z-entries", nargs="+", type=float, default=[1.5, 2.0, 2.5])
    parser.add_argument("--z-exits", nargs="+", type=float, default=[0.25, 0.5, 0.75])
    parser.add_argument("--z-stops", nargs="+", type=float, default=[3.0, 4.0, 5.0])
    parser.add_argument("--leverages", nargs="+", type=float, default=[2.0])
    parser.add_argument("--top-k", type=int, default=20)
    args = parser.parse_args()

    print(f"\n  Pairs Trading 백테스트 ({args.coin_a}-{args.coin_b})")
    print(f"  Lookback: {args.lookback}h, z_entry: ±{args.z_entry}, z_exit: ±{args.z_exit}, z_stop: ±{args.z_stop}")
    print(f"  Leverage: {args.leverage}x, Cost: {(FUTURES_FEE+SLIPPAGE)*100:.3f}% per side")

    if args.grid_search:
        rows = run_grid_search(
            days=args.test_days,
            capital=args.capital,
            coin_a=args.coin_a,
            coin_b=args.coin_b,
            lookbacks=args.lookbacks,
            z_entries=args.z_entries,
            z_exits=args.z_exits,
            z_stops=args.z_stops,
            leverages=args.leverages,
        )
        print_grid(rows, limit=args.top_k)
        return

    if args.walk_forward:
        windows = run_walk_forward(
            capital=args.capital,
            coin_a=args.coin_a,
            coin_b=args.coin_b,
            train_days=args.train_days,
            test_days=args.test_days,
            max_windows=args.max_windows,
            lookbacks=args.lookbacks,
            z_entries=args.z_entries,
            z_exits=args.z_exits,
            z_stops=args.z_stops,
            leverages=args.leverages,
        )
        print_walk_forward(windows, capital=args.capital)
        return

    for d in args.days:
        try:
            r = simulate_pairs_trading(
                d, args.capital,
                lookback_hours=args.lookback,
                z_entry=args.z_entry,
                z_exit=args.z_exit,
                z_stop=args.z_stop,
                leverage=args.leverage,
                coin_a=args.coin_a, coin_b=args.coin_b,
            )
            print_result(r, label=f"({d}d)")
        except Exception as e:
            print(f"  {d}d 실패: {e}")


if __name__ == "__main__":
    main()
