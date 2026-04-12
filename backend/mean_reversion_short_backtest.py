"""
Mean Reversion 단기 선물 백테스트 (15m 볼린저).

컨셉:
- BB(20) 하단 터치 + RSI < 30 → long (과매도 반등)
- BB(20) 상단 터치 + RSI > 70 → short (과매수 하락)
- BB 중간(SMA20) 도달 시 청산
- ATR SL 1.5배
- 15분봉 기반 — 하루 수회~수십회 거래
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
class MRShortResult:
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


@lru_cache(maxsize=16)
def load_5m(coin: str) -> pd.DataFrame:
    path = CACHE_DIR / f"{coin}_USDT_5m.csv"
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce", utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df = df[df.index.notna()]
    df.sort_index(inplace=True)
    return df


def compute_indicators(df: pd.DataFrame, bb_period: int = 20, rsi_period: int = 14) -> pd.DataFrame:
    df = df.copy()
    # BB
    df["sma_20"] = df["close"].rolling(bb_period).mean()
    df["bb_std"] = df["close"].rolling(bb_period).std()
    df["bb_upper"] = df["sma_20"] + 2 * df["bb_std"]
    df["bb_lower"] = df["sma_20"] - 2 * df["bb_std"]
    # RSI
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(rsi_period).mean()
    loss = (-delta.clip(upper=0)).rolling(rsi_period).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi_14"] = 100 - (100 / (1 + rs))
    # ATR
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    df["atr_14"] = tr.rolling(14).mean()
    return df


def simulate_mr_short(
    coin: str,
    days: int,
    initial_capital: float = 1000.0,
    leverage: float = 2.0,
    rsi_entry_low: float = 30.0,
    rsi_entry_high: float = 70.0,
    sl_atr_mult: float = 1.5,
    cooldown_candles: int = 6,  # 30분 쿨다운 (5m * 6)
) -> MRShortResult:
    df = load_5m(coin)
    end_ts = pd.Timestamp(datetime.now(timezone.utc))
    start_ts = end_ts - pd.Timedelta(days=days + 5)
    df = df[(df.index >= start_ts) & (df.index <= end_ts)]
    df = compute_indicators(df)
    df.dropna(subset=["sma_20", "rsi_14", "atr_14"], inplace=True)

    sim_start = end_ts - pd.Timedelta(days=days)

    cash = initial_capital
    pos_side = 0  # 0=flat, 1=long, -1=short
    pos_qty = 0.0
    pos_entry = 0.0
    pos_sl = 0.0
    n_trades = 0
    n_wins = 0
    n_losses = 0
    total_fees = 0.0
    last_trade_idx = -9999
    equity_curve = []

    for idx, (ts, row) in enumerate(df.iterrows()):
        price = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        rsi = float(row["rsi_14"])
        atr = float(row["atr_14"])
        bb_upper = float(row["bb_upper"])
        bb_lower = float(row["bb_lower"])
        sma = float(row["sma_20"])

        if pos_side == 1:
            equity = cash + (price - pos_entry) * pos_qty
        elif pos_side == -1:
            equity = cash + (pos_entry - price) * pos_qty
        else:
            equity = cash
        if ts >= sim_start:
            equity_curve.append((ts, equity))
        if ts < sim_start:
            continue

        # 보유 중 — SL/TP 체크
        if pos_side != 0:
            closed = False
            pnl = 0.0
            # SL (high/low intra-candle)
            if pos_side == 1 and low <= pos_sl:
                pnl = (pos_sl - pos_entry) * pos_qty
                closed = True
            elif pos_side == -1 and high >= pos_sl:
                pnl = (pos_entry - pos_sl) * pos_qty
                closed = True
            # TP — BB 중간(SMA) 도달
            elif pos_side == 1 and high >= sma:
                pnl = (sma - pos_entry) * pos_qty
                closed = True
            elif pos_side == -1 and low <= sma:
                pnl = (pos_entry - sma) * pos_qty
                closed = True

            if closed:
                fee = pos_qty * price * TOTAL_COST
                cash += pnl - fee
                total_fees += fee
                n_trades += 1
                if pnl - fee > 0:
                    n_wins += 1
                else:
                    n_losses += 1
                pos_side = 0
                pos_qty = 0.0
                last_trade_idx = idx
                continue

        # 미보유 — 진입
        if pos_side == 0 and idx - last_trade_idx >= cooldown_candles:
            # Long: BB 하단 + RSI 과매도
            if low <= bb_lower and rsi < rsi_entry_low and atr > 0:
                notional = cash * leverage * 0.1  # 10% per trade
                qty = notional / bb_lower
                fee = qty * bb_lower * TOTAL_COST
                if fee + 1 > cash:
                    continue
                cash -= fee
                total_fees += fee
                pos_side = 1
                pos_qty = qty
                pos_entry = bb_lower
                pos_sl = bb_lower - sl_atr_mult * atr

            # Short: BB 상단 + RSI 과매수
            elif high >= bb_upper and rsi > rsi_entry_high and atr > 0:
                notional = cash * leverage * 0.1
                qty = notional / bb_upper
                fee = qty * bb_upper * TOTAL_COST
                if fee + 1 > cash:
                    continue
                cash -= fee
                total_fees += fee
                pos_side = -1
                pos_qty = qty
                pos_entry = bb_upper
                pos_sl = bb_upper + sl_atr_mult * atr

    # 종료 청산
    if pos_side != 0:
        last_price = float(df["close"].iloc[-1])
        if pos_side == 1:
            pnl = (last_price - pos_entry) * pos_qty
        else:
            pnl = (pos_entry - last_price) * pos_qty
        fee = pos_qty * last_price * TOTAL_COST
        cash += pnl - fee
        total_fees += fee
        n_trades += 1
        if pnl - fee > 0:
            n_wins += 1
        else:
            n_losses += 1

    final = cash
    ret = (final - initial_capital) / initial_capital * 100

    sim_start_idx = df.index.searchsorted(sim_start)
    bh_s = float(df["close"].iloc[sim_start_idx])
    bh_e = float(df["close"].iloc[-1])
    bh_ret = (bh_e - bh_s) / bh_s * 100

    if len(equity_curve) >= 2:
        eq = np.array([e[1] for e in equity_curve])
        rets = np.diff(eq) / eq[:-1]
        sharpe = rets.mean() / rets.std() * np.sqrt(24 * 12 * 365) if rets.std() > 0 else 0
        peak = np.maximum.accumulate(eq)
        mdd = float(((peak - eq) / np.maximum(peak, 1e-9)).max() * 100)
    else:
        sharpe, mdd = 0.0, 0.0

    return MRShortResult(
        coin=coin, days=days, initial=initial_capital, final=final,
        return_pct=ret, sharpe=sharpe, max_drawdown=mdd,
        n_trades=n_trades, n_wins=n_wins, n_losses=n_losses,
        total_fees=total_fees, bh_return=bh_ret,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH"])
    parser.add_argument("--days", nargs="+", type=int, default=[30, 90, 180])
    parser.add_argument("--leverage", type=float, default=2.0)
    parser.add_argument("--capital", type=float, default=1000.0)
    args = parser.parse_args()

    print(f"\n  Mean Reversion 단기 (5m BB+RSI) 백테스트")
    for coin in args.coins:
        for d in args.days:
            try:
                r = simulate_mr_short(coin, d, args.capital, leverage=args.leverage)
                wr = r.n_wins / max(r.n_trades, 1) * 100
                ann = r.return_pct * 365 / d
                print(f"  {coin} {d}d | ret={r.return_pct:+.2f}% (ann {ann:+.1f}%) | sharpe={r.sharpe:.2f} | "
                      f"mdd={r.max_drawdown:.2f}% | trades={r.n_trades} (wr {wr:.0f}%) | "
                      f"fees={r.total_fees:.2f} | bh={r.bh_return:+.1f}%")
            except Exception as e:
                print(f"  {coin} {d}d 실패: {e}")


if __name__ == "__main__":
    main()
