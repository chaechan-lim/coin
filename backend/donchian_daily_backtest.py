"""
Donchian Channel Daily Ensemble 백테스트.

학술 기반: Carlo Zarattini, Alberto Pagani, Andrea Barbon - "Catching Crypto Trends" (SSRN 2025)

전략:
- 일봉 + 여러 lookback (10/20/40/55/90일) Donchian Channel 앙상블
- N일 신고가 돌파 → long 진입
- N/2일 신저가 이탈 → 청산
- ATR 기반 사이징 (1% risk per trade)
- Long-only (현물)

비교: 단일 lookback의 over-fitting 회피 + 여러 시그널 합의
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import numpy as np

CACHE_DIR = Path(__file__).parent / ".cache"

# 비용 (현물 거래)
SPOT_FEE = 0.001       # 0.10%
SLIPPAGE = 0.0002      # 0.02%
TOTAL_COST = SPOT_FEE + SLIPPAGE

# Donchian 앙상블 lookback 후보
LOOKBACKS = [10, 20, 40, 55, 90]

# 리스크
BASE_RISK_PCT = 0.01   # 거래당 1% 리스크


@dataclass
class DonchianResult:
    coin: str
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
    bh_return: float


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


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR 계산."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def simulate_donchian(
    coin: str,
    days: int,
    initial_capital: float = 1000.0,
    lookbacks: list[int] = LOOKBACKS,
    min_signals: int = 1,           # 최소 동의 시그널 수 (1=OR, len(lookbacks)=AND)
    use_atr_sizing: bool = True,
) -> DonchianResult:
    """단일 코인 Donchian 앙상블 시뮬레이션.

    매일:
    1. 각 lookback의 N일 high 계산
    2. 현재 가격 > N일 high (어제 기준) → 매수 시그널
    3. min_signals 이상 동의 → 진입
    4. 보유 중: 어떤 lookback 의 N/2일 low 이탈 → 청산
    5. ATR 기반 포지션 사이징
    """
    df = load_daily(coin)
    end_ts = pd.Timestamp(datetime.now(timezone.utc))
    start_ts = end_ts - pd.Timedelta(days=days + max(lookbacks) + 50)  # 워밍업
    df = df[(df.index >= start_ts) & (df.index <= end_ts)]

    if len(df) < max(lookbacks) + 10:
        raise ValueError(f"{coin} 데이터 부족: {len(df)}일")

    df["atr_14"] = calculate_atr(df, 14)

    # 각 lookback의 high/low 계산
    for lb in lookbacks:
        df[f"high_{lb}"] = df["high"].rolling(lb).max().shift(1)  # 어제 기준
        df[f"low_{lb//2}"] = df["low"].rolling(lb // 2).max().shift(1)
        df[f"low_exit_{lb}"] = df["low"].rolling(lb // 2).min().shift(1)

    cash = initial_capital
    position_qty = 0.0
    entry_price = 0.0
    n_trades = 0
    n_wins = 0
    n_losses = 0
    total_fees = 0.0
    equity_curve = []

    # 시작점 (lookback 워밍업 이후)
    sim_start_ts = end_ts - pd.Timedelta(days=days)

    for ts, row in df.iterrows():
        price = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        atr = float(row["atr_14"]) if pd.notna(row["atr_14"]) else 0

        if pd.isna(row[f"high_{lookbacks[0]}"]):
            continue  # 워밍업 중

        # equity (보유 중이면 시가평가)
        equity = cash + (position_qty * price if position_qty > 0 else 0)
        if ts >= sim_start_ts:
            equity_curve.append((ts, equity))

        # 시뮬레이션 시작 전이면 진입 안 함
        if ts < sim_start_ts:
            continue

        # 보유 중 → 청산 체크
        if position_qty > 0:
            # 어떤 lookback의 N/2일 low 이탈?
            exit_signals = 0
            for lb in lookbacks:
                exit_lvl = row.get(f"low_exit_{lb}")
                if pd.notna(exit_lvl) and low <= exit_lvl:
                    exit_signals += 1
            if exit_signals >= min_signals:
                # 청산 (시가가 아니라 low 가까이로)
                exit_price = max(low, min(p for p in [
                    row.get(f"low_exit_{lb}") for lb in lookbacks
                    if pd.notna(row.get(f"low_exit_{lb}"))
                ]))
                cost = position_qty * exit_price
                fee = cost * TOTAL_COST
                proceeds = cost - fee
                pnl = proceeds - (entry_price * position_qty)
                cash += proceeds
                total_fees += fee
                n_trades += 1
                if pnl > 0:
                    n_wins += 1
                else:
                    n_losses += 1
                position_qty = 0
                entry_price = 0
                continue  # 같은 날 재진입 안 함

        # 미보유 → 진입 체크
        if position_qty == 0:
            entry_signals = 0
            for lb in lookbacks:
                entry_lvl = row.get(f"high_{lb}")
                if pd.notna(entry_lvl) and high >= entry_lvl:
                    entry_signals += 1
            if entry_signals >= min_signals:
                # 진입 (Donchian high 가까이로)
                entry_lvl_used = min(
                    row.get(f"high_{lb}") for lb in lookbacks
                    if pd.notna(row.get(f"high_{lb}")) and high >= row.get(f"high_{lb}")
                )
                exec_price = max(entry_lvl_used, low)
                # ATR 기반 사이징
                if use_atr_sizing and atr > 0:
                    risk_amount = cash * BASE_RISK_PCT
                    stop_distance = atr * 2.0  # 2 ATR stop
                    qty = risk_amount / stop_distance
                    notional = qty * exec_price
                    if notional > cash * 0.95:  # 자본 한도
                        notional = cash * 0.95
                        qty = notional / exec_price
                else:
                    qty = (cash * 0.95) / exec_price
                if qty <= 0:
                    continue
                cost = qty * exec_price
                fee = cost * TOTAL_COST
                if cost + fee > cash:
                    continue
                cash -= cost + fee
                total_fees += fee
                position_qty = qty
                entry_price = exec_price

    # 종료 시 청산
    if position_qty > 0:
        last_price = float(df["close"].iloc[-1])
        cost = position_qty * last_price
        fee = cost * TOTAL_COST
        proceeds = cost - fee
        pnl = proceeds - (entry_price * position_qty)
        cash += proceeds
        total_fees += fee
        n_trades += 1
        if pnl > 0:
            n_wins += 1
        else:
            n_losses += 1
        position_qty = 0

    final_capital = cash
    return_pct = (final_capital - initial_capital) / initial_capital * 100

    # B&H 비교
    bh_start_idx = df.index.searchsorted(sim_start_ts)
    bh_start = float(df["close"].iloc[bh_start_idx]) if bh_start_idx < len(df) else float(df["close"].iloc[0])
    bh_end = float(df["close"].iloc[-1])
    bh_return = (bh_end - bh_start) / bh_start * 100

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

    return DonchianResult(
        coin=coin, days=days, initial=initial_capital, final=final_capital,
        return_pct=return_pct, sharpe=sharpe, max_drawdown=max_drawdown,
        n_trades=n_trades, n_wins=n_wins, n_losses=n_losses,
        total_fees=total_fees, bh_return=bh_return,
    )


def print_result(r: DonchianResult, label: str = ""):
    print(f"\n{'─'*60}")
    print(f"  Donchian Daily Ensemble — {r.coin} {label}")
    print(f"{'─'*60}")
    print(f"  기간:           {r.days}일")
    print(f"  초기:           {r.initial:>10,.2f}")
    print(f"  최종:           {r.final:>10,.2f}")
    print(f"  순수익:         {r.final - r.initial:>+10,.2f} ({r.return_pct:+.2f}%)")
    print(f"  연환산:         {r.return_pct * 365 / r.days:>+10.2f}%")
    print(f"  B&H 비교:       {r.bh_return:>+10.2f}% (alpha: {r.return_pct - r.bh_return:+.2f}%)")
    print(f"  거래 수:        {r.n_trades:>10} (승 {r.n_wins} / 패 {r.n_losses})")
    print(f"  승률:           {r.n_wins / max(r.n_trades, 1) * 100:>9.1f}%")
    print(f"  Sharpe:         {r.sharpe:>10.2f}")
    print(f"  Max Drawdown:   {r.max_drawdown:>10.2f}%")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL", "XRP"])
    parser.add_argument("--periods", nargs="+", type=int, default=[180, 360, 540, 1000])
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--lookbacks", nargs="+", type=int, default=LOOKBACKS)
    parser.add_argument("--min-signals", type=int, default=1)
    args = parser.parse_args()

    print(f"\n  Donchian Daily Ensemble 백테스트")
    print(f"  코인: {', '.join(args.coins)}")
    print(f"  Lookbacks: {args.lookbacks}, Min signals: {args.min_signals}")

    all_results = {}
    for coin in args.coins:
        coin_results = {}
        for d in args.periods:
            try:
                r = simulate_donchian(coin, d, args.capital,
                                      lookbacks=args.lookbacks,
                                      min_signals=args.min_signals)
                coin_results[d] = r
                print_result(r, label=f"({d}d)")
            except Exception as e:
                print(f"  {coin} {d}d 실패: {e}")
        all_results[coin] = coin_results

    # 종합 (코인별 평균)
    print(f"\n{'='*60}")
    print(f"  종합 결과 (코인 평균)")
    print(f"{'='*60}")
    print(f"{'기간':>8} | {'평균 수익':>10} | {'평균 alpha':>11} | {'평균 Sharpe':>11} | {'평균 MDD':>9}")
    print("-" * 60)
    for d in args.periods:
        rets = [all_results[c][d].return_pct for c in args.coins if d in all_results.get(c, {})]
        bhs = [all_results[c][d].bh_return for c in args.coins if d in all_results.get(c, {})]
        sharpes = [all_results[c][d].sharpe for c in args.coins if d in all_results.get(c, {})]
        dds = [all_results[c][d].max_drawdown for c in args.coins if d in all_results.get(c, {})]
        if rets:
            print(f"{d:>6}d | {np.mean(rets):>+9.2f}% | {np.mean([r-b for r,b in zip(rets,bhs)]):>+10.2f}% | {np.mean(sharpes):>11.2f} | {np.mean(dds):>8.2f}%")


if __name__ == "__main__":
    main()
