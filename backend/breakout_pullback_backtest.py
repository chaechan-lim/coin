"""
Breakout-Pullback 백테스트.

라이브 엔진 (engine/breakout_pullback_engine.py) 로직 그대로 재현.

전략:
- 일봉 N일 high/low 돌파 감지 → pullback_pct% 풀백 후 진입 (long/short)
- SL pct / TP pct / 트레일링 스탑 (활성 + 후퇴)
- 풀백 신호 만료: 3일

CLI:
    python breakout_pullback_backtest.py --days 540
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import math
import pandas as pd

CACHE_DIR = Path(__file__).parent / ".cache"
FUTURES_FEE = 0.0004
SLIPPAGE = 0.0002
COST = FUTURES_FEE + SLIPPAGE

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
    return df


@dataclass
class Trade:
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    entry_at: datetime
    exit_at: datetime
    exit_reason: str


@dataclass
class Position:
    symbol: str
    side: str
    qty: float
    entry_price: float
    sl_price: float
    tp_price: float
    entry_at: datetime
    trail_activated: bool = False
    highest: float = 0.0
    lowest: float = float("inf")


@dataclass
class PendingSignal:
    symbol: str
    side: str
    breakout_price: float
    detected_at: datetime


def simulate(
    coins: list[str],
    days: int,
    initial_capital: float = 400.0,
    leverage: float = 2.0,
    lookback: int = 20,
    pullback_pct: float = 4.0,
    sl_pct: float = 5.0,
    tp_pct: float = 8.0,
    trail_act: float = 5.0,
    trail_stop: float = 3.0,
    signal_expire_days: int = 3,
    verbose: bool = False,
) -> dict:
    dfs = {c: load_daily(c) for c in coins}
    common = dfs[coins[0]].index
    for c in coins[1:]:
        common = common.intersection(dfs[c].index)
    common = common.sort_values()
    if days and len(common) > days:
        common = common[-days:]

    capital = initial_capital
    positions: dict[str, Position] = {}
    pending: dict[str, PendingSignal] = {}
    trades: list[Trade] = []
    equity_curve = []
    peak = initial_capital
    max_dd_pct = 0.0

    for date in common:
        # 1) Manage existing positions: SL/TP/Trailing on this day's high/low
        to_close = []
        for sym, pos in positions.items():
            row = dfs[sym].loc[date]
            high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
            exit_reason = None
            exit_px = None

            if pos.side == "long":
                # SL 먼저 (보수적)
                if low <= pos.sl_price:
                    exit_reason = "sl_hit"; exit_px = pos.sl_price
                elif high >= pos.tp_price:
                    exit_reason = "tp_hit"; exit_px = pos.tp_price
                else:
                    if high > pos.highest:
                        pos.highest = high
                    if (close - pos.entry_price) / pos.entry_price * 100 >= trail_act:
                        pos.trail_activated = True
                    if pos.trail_activated:
                        trail_px = pos.highest * (1 - trail_stop / 100)
                        if low <= trail_px:
                            exit_reason = "trail_stop"; exit_px = trail_px
            else:
                if high >= pos.sl_price:
                    exit_reason = "sl_hit"; exit_px = pos.sl_price
                elif low <= pos.tp_price:
                    exit_reason = "tp_hit"; exit_px = pos.tp_price
                else:
                    if low < pos.lowest:
                        pos.lowest = low
                    if (pos.entry_price - close) / pos.entry_price * 100 >= trail_act:
                        pos.trail_activated = True
                    if pos.trail_activated:
                        trail_px = pos.lowest * (1 + trail_stop / 100)
                        if high >= trail_px:
                            exit_reason = "trail_stop"; exit_px = trail_px

            if exit_reason:
                to_close.append((sym, exit_px, exit_reason))

        for sym, exit_px, reason in to_close:
            pos = positions.pop(sym)
            if pos.side == "long":
                gross = (exit_px - pos.entry_price) * pos.qty
            else:
                gross = (pos.entry_price - exit_px) * pos.qty
            entry_notional = pos.entry_price * pos.qty
            exit_notional = exit_px * pos.qty
            fee = (entry_notional + exit_notional) * COST
            pnl = gross - fee
            capital += pnl
            pnl_pct = pnl / (entry_notional / leverage) * 100
            trades.append(Trade(
                symbol=sym, side=pos.side, entry_price=pos.entry_price,
                exit_price=exit_px, qty=pos.qty, pnl=pnl, pnl_pct=pnl_pct,
                entry_at=pos.entry_at, exit_at=date, exit_reason=reason,
            ))

        # 2) Check pullback entries on pending signals using today's close
        for sym in list(pending.keys()):
            sig = pending[sym]
            elapsed_days = (date - sig.detected_at).days
            if elapsed_days > signal_expire_days:
                del pending[sym]
                continue
            if sym in positions:  # safety
                del pending[sym]
                continue
            row = dfs[sym].loc[date]
            close = float(row["close"])
            threshold = sig.breakout_price * (pullback_pct / 100)
            if sig.side == "long" and close <= sig.breakout_price - threshold:
                # 진입
                _open_position(positions, sym, "long", close, capital, len(coins),
                               leverage, sl_pct, tp_pct, date)
                del pending[sym]
            elif sig.side == "short" and close >= sig.breakout_price + threshold:
                _open_position(positions, sym, "short", close, capital, len(coins),
                               leverage, sl_pct, tp_pct, date)
                del pending[sym]

        # 3) Detect new breakouts on coins not in pending or positions
        for sym in coins:
            if sym in positions or sym in pending:
                continue
            df = dfs[sym]
            window = df.loc[:date].iloc[-(lookback + 1):]
            if len(window) < lookback + 1:
                continue
            current_close = float(window.iloc[-1]["close"])
            past = window.iloc[:-1]  # 최근 N일 (오늘 제외)
            high_n = float(past["high"].max())
            low_n = float(past["low"].min())
            if current_close > high_n:
                pending[sym] = PendingSignal(sym, "long", current_close, date)
            elif current_close < low_n:
                pending[sym] = PendingSignal(sym, "short", current_close, date)

        # 4) Equity curve
        unrealized = 0.0
        for pos in positions.values():
            row = dfs[pos.symbol].loc[date]
            close = float(row["close"])
            if pos.side == "long":
                unrealized += (close - pos.entry_price) * pos.qty
            else:
                unrealized += (pos.entry_price - close) * pos.qty
        equity = capital + unrealized
        equity_curve.append(equity)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd_pct:
            max_dd_pct = dd

    # Stats
    n = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate = len(wins) / n * 100 if n else 0.0
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0
    final_capital = equity_curve[-1] if equity_curve else initial_capital
    return_pct = (final_capital - initial_capital) / initial_capital * 100
    by_reason: dict[str, list[float]] = {}
    for t in trades:
        by_reason.setdefault(t.exit_reason, []).append(t.pnl)
    by_symbol: dict[str, list[float]] = {}
    for t in trades:
        by_symbol.setdefault(t.symbol, []).append(t.pnl)
    by_side: dict[str, list[float]] = {}
    for t in trades:
        by_side.setdefault(t.side, []).append(t.pnl)

    return {
        "days": len(common),
        "initial_capital": initial_capital,
        "final_capital": final_capital,
        "return_pct": return_pct,
        "max_dd_pct": max_dd_pct,
        "n_trades": n,
        "win_rate": win_rate,
        "profit_factor": pf,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "by_reason": {k: (sum(v), len(v)) for k, v in by_reason.items()},
        "by_symbol": {k: (sum(v), len(v)) for k, v in by_symbol.items()},
        "by_side": {k: (sum(v), len(v)) for k, v in by_side.items()},
        "trades": trades if verbose else None,
    }


def _open_position(positions, sym, side, price, capital, n_coins, leverage,
                   sl_pct, tp_pct, date):
    per_coin = capital / n_coins
    notional = per_coin * leverage * 0.9
    if notional < 10:
        return
    qty = notional / price
    if side == "long":
        sl = price * (1 - sl_pct / 100)
        tp = price * (1 + tp_pct / 100)
    else:
        sl = price * (1 + sl_pct / 100)
        tp = price * (1 - tp_pct / 100)
    positions[sym] = Position(
        symbol=sym, side=side, qty=qty, entry_price=price,
        sl_price=sl, tp_price=tp, entry_at=date,
        highest=price, lowest=price,
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=540)
    p.add_argument("--initial-capital", type=float, default=400.0)
    p.add_argument("--leverage", type=float, default=2.0)
    p.add_argument("--lookback", type=int, default=20)
    p.add_argument("--pullback-pct", type=float, default=4.0)
    p.add_argument("--sl-pct", type=float, default=5.0)
    p.add_argument("--tp-pct", type=float, default=8.0)
    p.add_argument("--trail-act", type=float, default=5.0)
    p.add_argument("--trail-stop", type=float, default=3.0)
    p.add_argument("--coins", nargs="+", default=DEFAULT_COINS)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    res = simulate(
        coins=args.coins,
        days=args.days,
        initial_capital=args.initial_capital,
        leverage=args.leverage,
        lookback=args.lookback,
        pullback_pct=args.pullback_pct,
        sl_pct=args.sl_pct,
        tp_pct=args.tp_pct,
        trail_act=args.trail_act,
        trail_stop=args.trail_stop,
        verbose=args.verbose,
    )

    print("=" * 70)
    print(f"Breakout-Pullback {res['days']}일 백테스트 — {len(args.coins)} 코인")
    print(f"파라미터: lookback={args.lookback}d, pullback={args.pullback_pct}%, "
          f"SL/TP={args.sl_pct}/{args.tp_pct}%, trail={args.trail_act}/{args.trail_stop}%, "
          f"lev={args.leverage}x")
    print("=" * 70)
    print(f"  초기자본:     {res['initial_capital']:>10.2f} USDT")
    print(f"  최종자본:     {res['final_capital']:>10.2f} USDT")
    print(f"  수익률:       {res['return_pct']:>+10.2f} %")
    print(f"  최대낙폭:     {res['max_dd_pct']:>10.2f} %")
    print(f"  거래수:       {res['n_trades']:>10d}")
    print(f"  승률:         {res['win_rate']:>10.2f} %")
    print(f"  Profit Factor:{res['profit_factor']:>10.2f}")
    print(f"  Gross Profit: {res['gross_profit']:>+10.2f}")
    print(f"  Gross Loss:   {res['gross_loss']:>10.2f}")
    print()
    print("청산 사유별:")
    for reason, (pnl, n) in sorted(res["by_reason"].items(), key=lambda x: -x[1][0]):
        avg = pnl / n if n else 0
        print(f"  {reason:15s}  pnl={pnl:>+8.2f}  n={n:>3d}  avg={avg:+.2f}")
    print()
    print("방향별:")
    for side, (pnl, n) in res["by_side"].items():
        avg = pnl / n if n else 0
        print(f"  {side:6s}  pnl={pnl:>+8.2f}  n={n:>3d}  avg={avg:+.2f}")
    print()
    print("코인별:")
    for sym, (pnl, n) in sorted(res["by_symbol"].items(), key=lambda x: -x[1][0]):
        avg = pnl / n if n else 0
        print(f"  {sym:6s}  pnl={pnl:>+8.2f}  n={n:>3d}  avg={avg:+.2f}")


if __name__ == "__main__":
    main()
