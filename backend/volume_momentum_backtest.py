"""
Volume Momentum 백테스트.

라이브 엔진 (engine/volume_momentum_engine.py) 로직 그대로 재현.

전략 (1h):
- 거래량 vol_mult x 평균 (20시간 윈도우)
- 6h close 모멘텀 방향
- RSI 필터 (long: < rsi_long_max, short: > rsi_short_min)
- ATR 기반 SL/TP (sl_atr_mult, tp_atr_mult)
- 매시간 평가, 동시 보유 제한 없음 (코인당 1개)

CLI:
    python volume_momentum_backtest.py --days 540
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
import numpy as np
import pandas as pd

CACHE_DIR = Path(__file__).parent / ".cache"
FUTURES_FEE = 0.0004
SLIPPAGE = 0.0002
COST = FUTURES_FEE + SLIPPAGE

DEFAULT_COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "DOT"]


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
    symbol: str
    side: str
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
    side: str
    qty: float
    entry_price: float
    sl_price: float
    tp_price: float
    entry_at: pd.Timestamp


def compute_rsi(closes: np.ndarray, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes[-(period + 1):])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = float(np.mean(gains))
    avg_loss = float(np.mean(losses))
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))


def compute_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float | None:
    if len(high) < period + 1:
        return None
    tr_list = []
    for i in range(1, len(high)):
        tr = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        tr_list.append(tr)
    return float(np.mean(tr_list[-period:])) if tr_list else None


def simulate(
    coins: list[str],
    days: int,
    initial_capital: float = 200.0,
    leverage: float = 2.0,
    vol_mult: float = 2.0,
    rsi_long_max: float = 60.0,
    rsi_short_min: float = 40.0,
    sl_atr_mult: float = 2.5,
    tp_atr_mult: float = 5.0,
    verbose: bool = False,
) -> dict:
    dfs = {c: load_hourly(c) for c in coins}
    common = dfs[coins[0]].index
    for c in coins[1:]:
        common = common.intersection(dfs[c].index)
    common = common.sort_values()
    if days and len(common) > days * 24:
        common = common[-(days * 24):]

    capital = initial_capital
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_curve = []
    peak = initial_capital
    max_dd_pct = 0.0

    # Pre-extract arrays per coin for speed (for the entire common index window)
    arr = {}
    common_set = list(common)
    common_idx = pd.DatetimeIndex(common_set)
    for c in coins:
        sub = dfs[c].reindex(common_idx)
        arr[c] = {
            "close": sub["close"].values,
            "high": sub["high"].values,
            "low": sub["low"].values,
            "volume": sub["volume"].values,
        }

    n_steps = len(common_idx)
    for i in range(n_steps):
        ts = common_idx[i]

        # 1) Position SL/TP using THIS bar's high/low
        to_close = []
        for sym, pos in positions.items():
            high = float(arr[sym]["high"][i])
            low = float(arr[sym]["low"][i])
            exit_reason = None
            exit_px = None
            if pos.side == "long":
                if low <= pos.sl_price:
                    exit_reason = "sl_hit"; exit_px = pos.sl_price
                elif high >= pos.tp_price:
                    exit_reason = "tp_hit"; exit_px = pos.tp_price
            else:
                if high >= pos.sl_price:
                    exit_reason = "sl_hit"; exit_px = pos.sl_price
                elif low <= pos.tp_price:
                    exit_reason = "tp_hit"; exit_px = pos.tp_price
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
                entry_at=pos.entry_at, exit_at=ts, exit_reason=reason,
            ))

        # 2) Look for new entries (need at least 21 bars history for vol ratio + 14 for RSI/ATR)
        if i < 25:
            equity_curve.append(capital)
            continue

        for sym in coins:
            if sym in positions:
                continue
            close = arr[sym]["close"]
            high = arr[sym]["high"]
            low = arr[sym]["low"]
            vol = arr[sym]["volume"]

            # Volume ratio: current bar / avg(last 20 bars excluding current)
            current_vol = float(vol[i])
            avg_vol = float(np.mean(vol[i - 20:i]))
            if avg_vol <= 0:
                continue
            vol_ratio = current_vol / avg_vol
            if vol_ratio < vol_mult:
                continue

            close_now = float(close[i])
            close_6h_ago = float(close[i - 6]) if i >= 6 else float(close[0])
            momentum = close_now - close_6h_ago

            rsi = compute_rsi(close[max(0, i - 15):i + 1])
            if rsi is None:
                continue
            atr = compute_atr(high[max(0, i - 15):i + 1], low[max(0, i - 15):i + 1], close[max(0, i - 15):i + 1])
            if atr is None or atr <= 0:
                continue

            side = None
            if momentum > 0 and rsi < rsi_long_max:
                side = "long"
            elif momentum < 0 and rsi > rsi_short_min:
                side = "short"
            if side is None:
                continue

            if side == "long":
                sl = close_now - atr * sl_atr_mult
                tp = close_now + atr * tp_atr_mult
            else:
                sl = close_now + atr * sl_atr_mult
                tp = close_now - atr * tp_atr_mult

            per_coin = capital / len(coins)
            notional = per_coin * leverage * 0.9
            if notional < 10:
                continue
            qty = notional / close_now
            positions[sym] = Position(
                symbol=sym, side=side, qty=qty, entry_price=close_now,
                sl_price=sl, tp_price=tp, entry_at=ts,
            )

        # 3) Equity
        unrealized = 0.0
        for pos in positions.values():
            close_val = float(arr[pos.symbol]["close"][i])
            if pos.side == "long":
                unrealized += (close_val - pos.entry_price) * pos.qty
            else:
                unrealized += (pos.entry_price - close_val) * pos.qty
        equity = capital + unrealized
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
    final_capital = equity_curve[-1] if equity_curve else initial_capital
    return_pct = (final_capital - initial_capital) / initial_capital * 100
    by_reason: dict[str, list[float]] = {}
    by_symbol: dict[str, list[float]] = {}
    by_side: dict[str, list[float]] = {}
    for t in trades:
        by_reason.setdefault(t.exit_reason, []).append(t.pnl)
        by_symbol.setdefault(t.symbol, []).append(t.pnl)
        by_side.setdefault(t.side, []).append(t.pnl)

    return {
        "days": len(common_idx) // 24,
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=540)
    p.add_argument("--initial-capital", type=float, default=200.0)
    p.add_argument("--leverage", type=float, default=2.0)
    p.add_argument("--vol-mult", type=float, default=2.0)
    p.add_argument("--rsi-long-max", type=float, default=60.0)
    p.add_argument("--rsi-short-min", type=float, default=40.0)
    p.add_argument("--sl-atr-mult", type=float, default=2.5)
    p.add_argument("--tp-atr-mult", type=float, default=5.0)
    p.add_argument("--coins", nargs="+", default=DEFAULT_COINS)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    res = simulate(
        coins=args.coins,
        days=args.days,
        initial_capital=args.initial_capital,
        leverage=args.leverage,
        vol_mult=args.vol_mult,
        rsi_long_max=args.rsi_long_max,
        rsi_short_min=args.rsi_short_min,
        sl_atr_mult=args.sl_atr_mult,
        tp_atr_mult=args.tp_atr_mult,
        verbose=args.verbose,
    )

    print("=" * 70)
    print(f"Volume Momentum {res['days']}일 백테스트 — {len(args.coins)} 코인")
    print(f"파라미터: vol_mult={args.vol_mult}x, RSI L<{args.rsi_long_max} S>{args.rsi_short_min}, "
          f"ATR SL/TP={args.sl_atr_mult}/{args.tp_atr_mult}, lev={args.leverage}x")
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
        print(f"  {reason:15s}  pnl={pnl:>+8.2f}  n={n:>4d}  avg={avg:+.2f}")
    print()
    print("방향별:")
    for side, (pnl, n) in res["by_side"].items():
        avg = pnl / n if n else 0
        print(f"  {side:6s}  pnl={pnl:>+8.2f}  n={n:>4d}  avg={avg:+.2f}")
    print()
    print("코인별:")
    for sym, (pnl, n) in sorted(res["by_symbol"].items(), key=lambda x: -x[1][0]):
        avg = pnl / n if n else 0
        print(f"  {sym:6s}  pnl={pnl:>+8.2f}  n={n:>4d}  avg={avg:+.2f}")


if __name__ == "__main__":
    main()
