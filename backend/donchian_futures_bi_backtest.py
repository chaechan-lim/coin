"""
Donchian Futures Bi 양방향 백테스트.

라이브 엔진 (engine/donchian_futures_bi_engine.py) 로직 그대로 재현.

전략 (1d):
- 5개 lookback 채널 (10/20/40/55/90) — 어느 하나라도 high N일 돌파 → long, low → short
- 같은 일에 long_signals >= short_signals 면 long, 아니면 short
- Stop: ATR(14) × 2.0
- Exit: lb//2 half-window 역돌파 1개 이상 OR stop hit
- 포지션 사이즈: risk = margin × 1%, qty = risk / stop_distance (capped at margin × lev × 0.95)
- 2x leverage, 10코인, 일봉 평가

CLI:
    python donchian_futures_bi_backtest.py --days 540
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import numpy as np
import pandas as pd

CACHE_DIR = Path(__file__).parent / ".cache"
FUTURES_FEE = 0.0004
SLIPPAGE = 0.0002
COST = FUTURES_FEE + SLIPPAGE

LOOKBACKS = [10, 20, 40, 55, 90]
MIN_ENTRY_SIGNALS = 1
MIN_EXIT_SIGNALS = 1
ATR_PERIOD = 14
ATR_STOP_MULT = 2.0
BASE_RISK_PCT = 0.01
MIN_NOTIONAL = 10.0
DEFAULT_COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "ATOM", "ARB", "SUI", "ADA", "AVAX"]


@lru_cache(maxsize=32)
def load_daily(coin: str) -> pd.DataFrame:
    path = CACHE_DIR / f"{coin}_USDT_1d.csv"
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce", utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df = df[df.index.notna()].sort_index()
    # ATR
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    df["atr_14"] = tr.rolling(ATR_PERIOD).mean()
    # Donchian channels (entry: full lookback, exit: half lookback)
    for lb in LOOKBACKS:
        df[f"high_{lb}"] = df["high"].rolling(lb).max().shift(1)
        df[f"low_{lb}"] = df["low"].rolling(lb).min().shift(1)
        df[f"low_exit_{lb}"] = df["low"].rolling(lb // 2).min().shift(1)
        df[f"high_exit_{lb}"] = df["high"].rolling(lb // 2).max().shift(1)
    return df


@dataclass
class Trade:
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    entry_at: pd.Timestamp
    exit_at: pd.Timestamp
    exit_reason: str


@dataclass
class Position:
    symbol: str
    direction: str
    qty: float
    entry_price: float
    stop_price: float
    entry_at: pd.Timestamp


def simulate(
    coins: list[str],
    days: int,
    initial_capital: float = 300.0,
    leverage: float = 2.0,
    base_risk_pct: float = BASE_RISK_PCT,
    atr_stop_mult: float = ATR_STOP_MULT,
    min_entry_signals: int = MIN_ENTRY_SIGNALS,
    min_exit_signals: int = MIN_EXIT_SIGNALS,
    verbose: bool = False,
) -> dict:
    dfs = {c: load_daily(c) for c in coins}
    common = dfs[coins[0]].index
    for c in coins[1:]:
        common = common.intersection(dfs[c].index)
    common = common.sort_values()
    if days and len(common) > days:
        common = common[-days:]

    # 라이브 PnL 추적은 capital + cumulative; available_margin 모델
    cash = initial_capital
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_curve = []
    peak = initial_capital
    max_dd_pct = 0.0

    for date in common:
        # 1) Exit 체크 (이 날의 high/low 기반)
        to_close = []
        for sym, pos in positions.items():
            row = dfs[sym].loc[date]
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])

            exit_reason = None
            exit_px = None
            if pos.direction == "long":
                # SL 먼저 (보수적)
                if low <= pos.stop_price:
                    exit_reason = "sl_hit"; exit_px = pos.stop_price
                else:
                    exit_signals = sum(1 for lb in LOOKBACKS
                                       if pd.notna(row.get(f"low_exit_{lb}"))
                                       and low <= float(row.get(f"low_exit_{lb}")))
                    if exit_signals >= min_exit_signals:
                        exit_reason = "exit_signal"; exit_px = close
            else:
                if high >= pos.stop_price:
                    exit_reason = "sl_hit"; exit_px = pos.stop_price
                else:
                    exit_signals = sum(1 for lb in LOOKBACKS
                                       if pd.notna(row.get(f"high_exit_{lb}"))
                                       and high >= float(row.get(f"high_exit_{lb}")))
                    if exit_signals >= min_exit_signals:
                        exit_reason = "exit_signal"; exit_px = close
            if exit_reason:
                to_close.append((sym, exit_px, exit_reason))

        for sym, exit_px, reason in to_close:
            pos = positions.pop(sym)
            if pos.direction == "long":
                gross = (exit_px - pos.entry_price) * pos.qty
            else:
                gross = (pos.entry_price - exit_px) * pos.qty
            entry_notional = pos.entry_price * pos.qty
            exit_notional = exit_px * pos.qty
            fee = (entry_notional + exit_notional) * COST
            pnl = gross - fee
            cash += pnl
            pnl_pct = pnl / (entry_notional / leverage) * 100
            trades.append(Trade(
                symbol=sym, direction=pos.direction, entry_price=pos.entry_price,
                exit_price=exit_px, qty=pos.qty, pnl=pnl, pnl_pct=pnl_pct,
                entry_at=pos.entry_at, exit_at=date, exit_reason=reason,
            ))

        # 2) Entry 체크
        for sym in coins:
            if sym in positions:
                continue
            row = dfs[sym].loc[date]
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            atr = float(row["atr_14"]) if pd.notna(row["atr_14"]) else 0.0
            if atr <= 0:
                continue

            long_signals = 0
            short_signals = 0
            for lb in LOOKBACKS:
                el = row.get(f"high_{lb}")
                es = row.get(f"low_{lb}")
                if pd.notna(el) and high >= float(el):
                    long_signals += 1
                if pd.notna(es) and low <= float(es):
                    short_signals += 1

            direction = None
            if long_signals >= min_entry_signals and long_signals >= short_signals:
                direction = "long"
            elif short_signals >= min_entry_signals:
                direction = "short"
            if direction is None:
                continue

            # available margin 모델: cash + cumulative_pnl, but margin_used 차감 가능
            # 단순화: cash가 margin pool. risk = cash * 1%
            risk_amount = cash * base_risk_pct
            stop_distance = atr * atr_stop_mult
            if stop_distance <= 0:
                continue
            qty = risk_amount / stop_distance
            notional = qty * close
            max_notional = cash * leverage * 0.95
            if notional > max_notional:
                notional = max_notional
                qty = notional / close
            if qty <= 0 or notional < MIN_NOTIONAL:
                continue

            if direction == "long":
                stop = close - atr * atr_stop_mult
            else:
                stop = close + atr * atr_stop_mult

            positions[sym] = Position(
                symbol=sym, direction=direction, qty=qty,
                entry_price=close, stop_price=stop, entry_at=date,
            )

        # 3) Equity curve
        unrealized = 0.0
        for pos in positions.values():
            row = dfs[pos.symbol].loc[date]
            close = float(row["close"])
            if pos.direction == "long":
                unrealized += (close - pos.entry_price) * pos.qty
            else:
                unrealized += (pos.entry_price - close) * pos.qty
        equity = cash + unrealized
        equity_curve.append(equity)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd_pct:
            max_dd_pct = dd

    n = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate = len(wins) / n * 100 if n else 0.0
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0
    final = equity_curve[-1] if equity_curve else initial_capital
    return_pct = (final - initial_capital) / initial_capital * 100
    by_reason: dict[str, list[float]] = {}
    by_symbol: dict[str, list[float]] = {}
    by_dir: dict[str, list[float]] = {}
    for t in trades:
        by_reason.setdefault(t.exit_reason, []).append(t.pnl)
        by_symbol.setdefault(t.symbol, []).append(t.pnl)
        by_dir.setdefault(t.direction, []).append(t.pnl)

    return {
        "days": len(common),
        "initial_capital": initial_capital,
        "final_capital": final,
        "return_pct": return_pct,
        "max_dd_pct": max_dd_pct,
        "n_trades": n,
        "win_rate": win_rate,
        "profit_factor": pf,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "by_reason": {k: (sum(v), len(v)) for k, v in by_reason.items()},
        "by_symbol": {k: (sum(v), len(v)) for k, v in by_symbol.items()},
        "by_dir": {k: (sum(v), len(v)) for k, v in by_dir.items()},
        "trades": trades if verbose else None,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=540)
    p.add_argument("--initial-capital", type=float, default=300.0)
    p.add_argument("--leverage", type=float, default=2.0)
    p.add_argument("--risk-pct", type=float, default=BASE_RISK_PCT)
    p.add_argument("--atr-stop-mult", type=float, default=ATR_STOP_MULT)
    p.add_argument("--coins", nargs="+", default=DEFAULT_COINS)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    res = simulate(
        coins=args.coins,
        days=args.days,
        initial_capital=args.initial_capital,
        leverage=args.leverage,
        base_risk_pct=args.risk_pct,
        atr_stop_mult=args.atr_stop_mult,
        verbose=args.verbose,
    )

    print("=" * 70)
    print(f"Donchian Futures Bi {res['days']}일 — {len(args.coins)} 코인")
    print(f"파라미터: lookbacks={LOOKBACKS}, ATR×{args.atr_stop_mult}, "
          f"risk={args.risk_pct*100:.1f}%, lev={args.leverage}x")
    print("=" * 70)
    print(f"  초기:         {res['initial_capital']:>10.2f}")
    print(f"  최종:         {res['final_capital']:>10.2f}")
    print(f"  수익률:       {res['return_pct']:>+9.2f}%")
    print(f"  최대낙폭:     {res['max_dd_pct']:>9.2f}%")
    print(f"  거래수:       {res['n_trades']:>10d}")
    print(f"  승률:         {res['win_rate']:>9.2f}%")
    print(f"  Profit Factor:{res['profit_factor']:>9.2f}")
    print()
    print("청산 사유별:")
    for r, (pnl, n) in sorted(res["by_reason"].items(), key=lambda x: -x[1][0]):
        avg = pnl / n if n else 0
        print(f"  {r:15s}  pnl={pnl:>+8.2f}  n={n:>3d}  avg={avg:+.2f}")
    print()
    print("방향별:")
    for d, (pnl, n) in res["by_dir"].items():
        avg = pnl / n if n else 0
        print(f"  {d:6s}  pnl={pnl:>+8.2f}  n={n:>3d}  avg={avg:+.2f}")
    print()
    print("코인별:")
    for sym, (pnl, n) in sorted(res["by_symbol"].items(), key=lambda x: -x[1][0]):
        avg = pnl / n if n else 0
        print(f"  {sym:6s}  pnl={pnl:>+8.2f}  n={n:>3d}  avg={avg:+.2f}")


if __name__ == "__main__":
    main()
