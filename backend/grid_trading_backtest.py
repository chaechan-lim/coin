"""
Grid Trading 선물 백테스트.

컨셉: 가격 범위를 일정 간격 그리드로 나눠서
- 가격 하락 시 long 진입 (그리드 하단)
- 가격 상승 시 short 진입 (그리드 상단)
- 그리드 반대편 도달 시 청산 → 작은 수익 반복

변동성 자체가 수익원 — 추세 추종과 정반대.
횡보장/변동성 큰 시장에서 유리.
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
class GridBacktestResult:
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
    grid_count: int
    grid_spacing_pct: float
    bh_return: float


@lru_cache(maxsize=16)
def load_hourly(coin: str) -> pd.DataFrame:
    path = CACHE_DIR / f"{coin}_USDT_1h.csv"
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce", utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df = df[df.index.notna()]
    df.sort_index(inplace=True)
    return df


def simulate_grid(
    coin: str,
    days: int,
    initial_capital: float = 1000.0,
    grid_count: int = 10,
    grid_range_atr_mult: float = 4.0,
    leverage: float = 2.0,
    recalc_interval_hours: int = 24,
) -> GridBacktestResult:
    """Grid trading 시뮬레이션.

    매 recalc_interval마다:
    1. ATR(24) 기반 그리드 범위 재계산: center ± grid_range_atr_mult * ATR
    2. 그리드 간격 = 범위 / grid_count
    3. 가격이 그리드 하단 터치 → long 진입 (그리드 1칸 TP)
    4. 가격이 그리드 상단 터치 → short 진입 (그리드 1칸 TP)
    5. SL: 그리드 범위 이탈 시 청산
    """
    df = load_hourly(coin)
    end_ts = pd.Timestamp(datetime.now(timezone.utc))
    start_ts = end_ts - pd.Timedelta(days=days + 30)
    df = df[(df.index >= start_ts) & (df.index <= end_ts)]

    if len(df) < 200:
        raise ValueError(f"데이터 부족: {len(df)}h")

    # ATR(24)
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    df = df.copy()
    df["atr_24"] = tr.rolling(24).mean()

    sim_start = end_ts - pd.Timedelta(days=days)

    cash = initial_capital
    positions: list[dict] = []  # {side, entry, qty, tp, sl}
    n_trades = 0
    n_wins = 0
    n_losses = 0
    total_fees = 0.0
    equity_curve = []

    grid_center = 0.0
    grid_spacing = 0.0
    grid_upper = 0.0
    grid_lower = 0.0
    last_recalc_idx = -9999
    max_position_per_grid = initial_capital * leverage / grid_count

    for idx, (ts, row) in enumerate(df.iterrows()):
        price = float(row["close"])
        h = float(row["high"])
        l = float(row["low"])
        atr = float(row["atr_24"]) if pd.notna(row["atr_24"]) else 0

        # equity 계산
        unrealized = sum(
            (price - p["entry"]) * p["qty"] if p["side"] == "long"
            else (p["entry"] - price) * p["qty"]
            for p in positions
        )
        equity = cash + unrealized
        if ts >= sim_start:
            equity_curve.append((ts, equity))
        if ts < sim_start:
            continue

        # 그리드 재계산
        if atr > 0 and (idx - last_recalc_idx >= recalc_interval_hours):
            grid_center = price
            grid_range = atr * grid_range_atr_mult
            grid_upper = grid_center + grid_range
            grid_lower = grid_center - grid_range
            grid_spacing = (grid_upper - grid_lower) / grid_count
            last_recalc_idx = idx
            max_position_per_grid = (initial_capital + cash - initial_capital) * leverage / grid_count
            max_position_per_grid = max(max_position_per_grid, 10)

        if grid_spacing <= 0:
            continue

        # 기존 포지션 TP/SL 체크
        closed = []
        for i, p in enumerate(positions):
            pnl = 0.0
            close_reason = None
            if p["side"] == "long":
                if h >= p["tp"]:
                    pnl = (p["tp"] - p["entry"]) * p["qty"]
                    close_reason = "tp"
                elif l <= p["sl"]:
                    pnl = (p["sl"] - p["entry"]) * p["qty"]
                    close_reason = "sl"
            else:  # short
                if l <= p["tp"]:
                    pnl = (p["entry"] - p["tp"]) * p["qty"]
                    close_reason = "tp"
                elif h >= p["sl"]:
                    pnl = (p["entry"] - p["sl"]) * p["qty"]
                    close_reason = "sl"

            if close_reason:
                fee = abs(pnl) * 0.1 + p["qty"] * price * TOTAL_COST
                cash += pnl - fee
                total_fees += fee
                n_trades += 1
                if pnl - fee > 0:
                    n_wins += 1
                else:
                    n_losses += 1
                closed.append(i)

        for i in sorted(closed, reverse=True):
            positions.pop(i)

        # 새 그리드 진입 (가격이 그리드 레벨 터치)
        if grid_spacing > 0 and len(positions) < grid_count:
            # long: 가격이 center 아래 그리드 터치
            for g in range(1, grid_count // 2 + 1):
                level = grid_center - g * grid_spacing
                if l <= level and level >= grid_lower:
                    # 이미 이 레벨에 포지션 있는지
                    existing = any(abs(p["entry"] - level) < grid_spacing * 0.3 for p in positions)
                    if not existing:
                        qty = max_position_per_grid / level
                        fee = qty * level * TOTAL_COST
                        if fee + 1 > cash:
                            continue
                        cash -= fee
                        total_fees += fee
                        positions.append({
                            "side": "long",
                            "entry": level,
                            "qty": qty,
                            "tp": level + grid_spacing,
                            "sl": grid_lower - grid_spacing,
                        })

            # short: 가격이 center 위 그리드 터치
            for g in range(1, grid_count // 2 + 1):
                level = grid_center + g * grid_spacing
                if h >= level and level <= grid_upper:
                    existing = any(abs(p["entry"] - level) < grid_spacing * 0.3 for p in positions)
                    if not existing:
                        qty = max_position_per_grid / level
                        fee = qty * level * TOTAL_COST
                        if fee + 1 > cash:
                            continue
                        cash -= fee
                        total_fees += fee
                        positions.append({
                            "side": "short",
                            "entry": level,
                            "qty": qty,
                            "tp": level - grid_spacing,
                            "sl": grid_upper + grid_spacing,
                        })

    # 종료 시 강제 청산
    last_price = float(df["close"].iloc[-1])
    for p in positions:
        if p["side"] == "long":
            pnl = (last_price - p["entry"]) * p["qty"]
        else:
            pnl = (p["entry"] - last_price) * p["qty"]
        fee = p["qty"] * last_price * TOTAL_COST
        cash += pnl - fee
        total_fees += fee
        n_trades += 1
        if pnl - fee > 0:
            n_wins += 1
        else:
            n_losses += 1

    final = cash
    ret = (final - initial_capital) / initial_capital * 100

    # B&H
    sim_start_idx = df.index.searchsorted(sim_start)
    bh_start = float(df["close"].iloc[sim_start_idx])
    bh_end = float(df["close"].iloc[-1])
    bh_ret = (bh_end - bh_start) / bh_start * 100

    if len(equity_curve) >= 2:
        eq = np.array([e[1] for e in equity_curve])
        rets = np.diff(eq) / eq[:-1]
        sharpe = rets.mean() / rets.std() * np.sqrt(24 * 365) if rets.std() > 0 else 0
        peak = np.maximum.accumulate(eq)
        mdd = float(((peak - eq) / np.maximum(peak, 1e-9)).max() * 100)
    else:
        sharpe, mdd = 0.0, 0.0

    return GridBacktestResult(
        coin=coin, days=days, initial=initial_capital, final=final,
        return_pct=ret, sharpe=sharpe, max_drawdown=mdd,
        n_trades=n_trades, n_wins=n_wins, n_losses=n_losses,
        total_fees=total_fees, grid_count=grid_count,
        grid_spacing_pct=grid_spacing / grid_center * 100 if grid_center > 0 else 0,
        bh_return=bh_ret,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH"])
    parser.add_argument("--days", nargs="+", type=int, default=[90, 180, 360])
    parser.add_argument("--grids", type=int, default=10)
    parser.add_argument("--leverage", type=float, default=2.0)
    parser.add_argument("--capital", type=float, default=1000.0)
    args = parser.parse_args()

    print(f"\n  Grid Trading 백테스트 (선물)")
    print(f"  코인: {', '.join(args.coins)}, 그리드 {args.grids}개, 레버리지 {args.leverage}x")

    for coin in args.coins:
        for d in args.days:
            try:
                r = simulate_grid(coin, d, args.capital, grid_count=args.grids, leverage=args.leverage)
                wr = r.n_wins / max(r.n_trades, 1) * 100
                ann = r.return_pct * 365 / d
                print(f"  {coin} {d}d | ret={r.return_pct:+.2f}% (ann {ann:+.1f}%) | sharpe={r.sharpe:.2f} | "
                      f"mdd={r.max_drawdown:.2f}% | trades={r.n_trades} (wr {wr:.0f}%) | "
                      f"fees={r.total_fees:.2f} | bh={r.bh_return:+.1f}% | grid_sp={r.grid_spacing_pct:.2f}%")
            except Exception as e:
                print(f"  {coin} {d}d 실패: {e}")


if __name__ == "__main__":
    main()
