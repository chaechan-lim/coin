"""
BTC-Neutral Alt Mean-Reversion 백테스트.

라이브 엔진 (engine/btc_neutral_alt_mr_engine.py) 로직 그대로 재현.

전략 (1h):
- ALT/BTC 가격비의 z-score가 lookback_days*24h 윈도우에서 |z| >= z_entry 도달
- z < -z_entry → alt long + BTC short (ALT 저평가)
- z > z_entry  → alt short + BTC long (ALT 고평가)
- 청산: |z| <= z_exit 또는 max_hold_days 초과
- 코인당 자본 position_pct (default 15%) × leverage / 2 (alt + BTC)

CLI:
    python btc_neutral_mr_backtest.py --days 540
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

BTC_SYMBOL = "BTC"
DEFAULT_COINS = ["ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK"]


@lru_cache(maxsize=32)
def load_hourly(coin: str) -> pd.DataFrame:
    path = CACHE_DIR / f"{coin}_USDT_1h.csv"
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce", utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df = df[df.index.notna()].sort_index()
    return df


@dataclass
class Trade:
    alt_symbol: str
    alt_side: str
    alt_pnl: float
    btc_pnl: float
    total_pnl: float
    entry_z: float
    exit_z: float
    hold_hours: float
    exit_reason: str


@dataclass
class NeutralPosition:
    alt_symbol: str
    alt_side: str
    alt_qty: float
    alt_entry: float
    btc_side: str
    btc_qty: float
    btc_entry: float
    entry_idx: int
    entry_z: float


def simulate(
    coins: list[str],
    days: int,
    initial_capital: float = 800.0,
    leverage: float = 2.0,
    lookback_days: int = 7,
    z_entry: float = 2.0,
    z_exit: float = 0.3,
    max_hold_days: int = 7,
    max_concurrent: int = 3,
    position_pct: float = 0.15,
    eval_every_hours: int = 24,  # 라이브는 24h 1회 평가 (UTC 01:00)
    verbose: bool = False,
) -> dict:
    btc_df = load_hourly(BTC_SYMBOL)
    alt_dfs = {c: load_hourly(c) for c in coins}
    common = btc_df.index
    for c in coins:
        common = common.intersection(alt_dfs[c].index)
    common = common.sort_values()
    if days and len(common) > days * 24:
        common = common[-(days * 24):]

    common_idx = pd.DatetimeIndex(common)
    n = len(common_idx)
    btc_close = btc_df.reindex(common_idx)["close"].values
    alt_arr = {c: alt_dfs[c].reindex(common_idx)["close"].values for c in coins}

    capital = initial_capital
    positions: dict[str, NeutralPosition] = {}
    trades: list[Trade] = []
    equity_curve = []
    peak = initial_capital
    max_dd_pct = 0.0
    lookback_n = lookback_days * 24
    max_hold_h = max_hold_days * 24

    def compute_z(sym: str, i: int) -> float | None:
        if i < lookback_n:
            return None
        btc_w = btc_close[i - lookback_n + 1: i + 1]
        alt_w = alt_arr[sym][i - lookback_n + 1: i + 1]
        if np.any(btc_w <= 0):
            return None
        ratio = alt_w / btc_w
        mean = float(np.mean(ratio))
        std = float(np.std(ratio))
        if std < 1e-15:
            return None
        return (float(ratio[-1]) - mean) / std

    for i in range(n):
        # Only evaluate on the eval cadence (matches live: 24h)
        do_eval = (i % eval_every_hours == 0) and (i >= lookback_n)

        if do_eval:
            # 1) Exit checks
            for sym in list(positions.keys()):
                pos = positions[sym]
                hold_hours = i - pos.entry_idx
                if hold_hours >= max_hold_h:
                    _close(positions, trades, sym, i, alt_arr, btc_close, leverage,
                           "max_hold_exceeded", capital_ref=[capital])
                    capital = capital + trades[-1].total_pnl
                    continue
                z = compute_z(sym, i)
                if z is None:
                    continue
                if abs(z) <= z_exit:
                    _close(positions, trades, sym, i, alt_arr, btc_close, leverage,
                           f"z_reverted({z:.2f})", capital_ref=[capital])
                    capital = capital + trades[-1].total_pnl

            # 2) Entry scans
            if len(positions) < max_concurrent:
                for sym in coins:
                    if sym in positions:
                        continue
                    if len(positions) >= max_concurrent:
                        break
                    z = compute_z(sym, i)
                    if z is None:
                        continue
                    if z < -z_entry:
                        alt_side = "long"
                    elif z > z_entry:
                        alt_side = "short"
                    else:
                        continue
                    notional = capital * position_pct * leverage
                    if notional < 20:
                        continue
                    half = notional / 2
                    alt_p = float(alt_arr[sym][i])
                    btc_p = float(btc_close[i])
                    alt_q = half / alt_p
                    btc_q = half / btc_p
                    btc_side = "short" if alt_side == "long" else "long"
                    # entry fees
                    fee_in = (alt_p * alt_q + btc_p * btc_q) * COST
                    capital -= fee_in
                    positions[sym] = NeutralPosition(
                        alt_symbol=sym, alt_side=alt_side, alt_qty=alt_q, alt_entry=alt_p,
                        btc_side=btc_side, btc_qty=btc_q, btc_entry=btc_p,
                        entry_idx=i, entry_z=z,
                    )

        # 3) Equity (mark-to-market)
        unrealized = 0.0
        for pos in positions.values():
            alt_now = float(alt_arr[pos.alt_symbol][i])
            btc_now = float(btc_close[i])
            if pos.alt_side == "long":
                unrealized += (alt_now - pos.alt_entry) * pos.alt_qty
            else:
                unrealized += (pos.alt_entry - alt_now) * pos.alt_qty
            if pos.btc_side == "long":
                unrealized += (btc_now - pos.btc_entry) * pos.btc_qty
            else:
                unrealized += (pos.btc_entry - btc_now) * pos.btc_qty
        equity = capital + unrealized
        equity_curve.append(equity)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd_pct:
            max_dd_pct = dd

    # Final liquidation of remaining positions at last close
    for sym in list(positions.keys()):
        _close(positions, trades, sym, n - 1, alt_arr, btc_close, leverage,
               "end_of_period", capital_ref=[capital])
        capital = capital + trades[-1].total_pnl

    n_trades = len(trades)
    wins = [t for t in trades if t.total_pnl > 0]
    losses = [t for t in trades if t.total_pnl <= 0]
    win_rate = len(wins) / n_trades * 100 if n_trades else 0.0
    gross_profit = sum(t.total_pnl for t in wins)
    gross_loss = -sum(t.total_pnl for t in losses)
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0
    final_capital = equity_curve[-1] if equity_curve else initial_capital
    return_pct = (final_capital - initial_capital) / initial_capital * 100
    by_reason: dict[str, list[float]] = {}
    by_symbol: dict[str, list[float]] = {}
    by_side: dict[str, list[float]] = {}
    leg_alt = sum(t.alt_pnl for t in trades)
    leg_btc = sum(t.btc_pnl for t in trades)
    for t in trades:
        by_reason.setdefault(t.exit_reason, []).append(t.total_pnl)
        by_symbol.setdefault(t.alt_symbol, []).append(t.total_pnl)
        by_side.setdefault(t.alt_side, []).append(t.total_pnl)

    return {
        "days": len(common_idx) // 24,
        "initial_capital": initial_capital,
        "final_capital": final_capital,
        "return_pct": return_pct,
        "max_dd_pct": max_dd_pct,
        "n_trades": n_trades,
        "win_rate": win_rate,
        "profit_factor": pf,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "leg_alt_pnl": leg_alt,
        "leg_btc_pnl": leg_btc,
        "by_reason": {k: (sum(v), len(v)) for k, v in by_reason.items()},
        "by_symbol": {k: (sum(v), len(v)) for k, v in by_symbol.items()},
        "by_side": {k: (sum(v), len(v)) for k, v in by_side.items()},
        "trades": trades if verbose else None,
    }


def _close(positions, trades, sym, i, alt_arr, btc_close, leverage, reason, capital_ref):
    pos = positions.pop(sym)
    alt_p = float(alt_arr[sym][i])
    btc_p = float(btc_close[i])

    if pos.alt_side == "long":
        alt_g = (alt_p - pos.alt_entry) * pos.alt_qty
    else:
        alt_g = (pos.alt_entry - alt_p) * pos.alt_qty
    if pos.btc_side == "long":
        btc_g = (btc_p - pos.btc_entry) * pos.btc_qty
    else:
        btc_g = (pos.btc_entry - btc_p) * pos.btc_qty

    fee_out = (alt_p * pos.alt_qty + btc_p * pos.btc_qty) * COST
    total = alt_g + btc_g - fee_out

    # exit z (informational)
    btc_w = btc_close[max(0, i - 7 * 24 + 1): i + 1]
    alt_w = alt_arr[sym][max(0, i - 7 * 24 + 1): i + 1]
    if len(btc_w) > 1 and not np.any(btc_w <= 0):
        ratio = alt_w / btc_w
        std = float(np.std(ratio))
        ez = (float(ratio[-1]) - float(np.mean(ratio))) / std if std > 1e-15 else 0.0
    else:
        ez = 0.0

    trades.append(Trade(
        alt_symbol=sym, alt_side=pos.alt_side, alt_pnl=alt_g, btc_pnl=btc_g,
        total_pnl=total, entry_z=pos.entry_z, exit_z=ez,
        hold_hours=float(i - pos.entry_idx), exit_reason=reason,
    ))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=540)
    p.add_argument("--initial-capital", type=float, default=800.0)
    p.add_argument("--leverage", type=float, default=2.0)
    p.add_argument("--lookback-days", type=int, default=7)
    p.add_argument("--z-entry", type=float, default=2.0)
    p.add_argument("--z-exit", type=float, default=0.3)
    p.add_argument("--max-hold-days", type=int, default=7)
    p.add_argument("--max-concurrent", type=int, default=3)
    p.add_argument("--position-pct", type=float, default=0.15)
    p.add_argument("--coins", nargs="+", default=DEFAULT_COINS)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    res = simulate(
        coins=args.coins,
        days=args.days,
        initial_capital=args.initial_capital,
        leverage=args.leverage,
        lookback_days=args.lookback_days,
        z_entry=args.z_entry,
        z_exit=args.z_exit,
        max_hold_days=args.max_hold_days,
        max_concurrent=args.max_concurrent,
        position_pct=args.position_pct,
        verbose=args.verbose,
    )

    print("=" * 70)
    print(f"BTC-Neutral MR {res['days']}일 백테스트 — {len(args.coins)} alt 코인")
    print(f"파라미터: lookback={args.lookback_days}d, z_entry={args.z_entry}, "
          f"z_exit={args.z_exit}, max_hold={args.max_hold_days}d, "
          f"concurrent={args.max_concurrent}, pos={args.position_pct}, lev={args.leverage}x")
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
    print("레그별:")
    print(f"  alt 레그 PnL: {res['leg_alt_pnl']:>+10.2f}")
    print(f"  BTC 레그 PnL: {res['leg_btc_pnl']:>+10.2f}")
    print()
    print("청산 사유별:")
    for reason, (pnl, n) in sorted(res["by_reason"].items(), key=lambda x: -x[1][0]):
        avg = pnl / n if n else 0
        print(f"  {reason:25s}  pnl={pnl:>+8.2f}  n={n:>3d}  avg={avg:+.2f}")
    print()
    print("방향별 (alt):")
    for side, (pnl, n) in res["by_side"].items():
        avg = pnl / n if n else 0
        print(f"  {side:6s}  pnl={pnl:>+8.2f}  n={n:>3d}  avg={avg:+.2f}")
    print()
    print("코인별 (alt):")
    for sym, (pnl, n) in sorted(res["by_symbol"].items(), key=lambda x: -x[1][0]):
        avg = pnl / n if n else 0
        print(f"  {sym:6s}  pnl={pnl:>+8.2f}  n={n:>3d}  avg={avg:+.2f}")


if __name__ == "__main__":
    main()
