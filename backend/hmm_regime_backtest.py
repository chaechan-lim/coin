"""
HMM Regime Detection 백테스트.

목표:
- BTC 1시간봉에서 HMM으로 시장 체제를 추정
- bullish / bearish / neutral state에 따라 long / short / flat 전환
- 운영 후보용 최소 검증용 메타-전략 백테스트
"""
from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import io
from pathlib import Path

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM


CACHE_DIR = Path(__file__).parent / ".cache"
FUTURES_FEE = 0.0004
SLIPPAGE = 0.0002
TOTAL_COST = FUTURES_FEE + SLIPPAGE


@dataclass
class HMMBacktestResult:
    coin: str
    days: int
    initial: float
    final: float
    return_pct: float
    sharpe: float
    max_drawdown: float
    n_trades: int
    total_fees: float
    bullish_state: int
    bearish_state: int
    neutral_state: int


@lru_cache(maxsize=16)
def load_hourly(coin: str) -> pd.DataFrame:
    path = CACHE_DIR / f"{coin}_USDT_1h.csv"
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce", utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df = df[df.index.notna()].sort_index()
    return df


def _features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["log_return"] = np.log(out["close"]).diff()
    out["vol_24"] = out["log_return"].rolling(24).std().fillna(0.0)
    out["mom_24"] = out["close"].pct_change(24).fillna(0.0)
    return out.dropna()


def simulate_hmm_regime(
    coin: str = "BTC",
    days: int = 180,
    initial_capital: float = 1000.0,
    leverage: float = 1.5,
    n_states: int = 3,
    warmup_days: int = 180,
    sl_pct: float = 0.0,
    tp_pct: float = 0.0,
    trail_act_pct: float = 0.0,
    trail_stop_pct: float = 0.0,
    use_4h: bool = True,
    min_state_prob: float = 0.7,
) -> HMMBacktestResult:
    df_1h = _features(load_hourly(coin))
    end_ts = pd.Timestamp(datetime.now(timezone.utc))
    start_ts = end_ts - pd.Timedelta(days=days + warmup_days)
    df_1h = df_1h[(df_1h.index >= start_ts) & (df_1h.index <= end_ts)].copy()

    # 4h 리샘플링 (라이브 엔진과 동일)
    if use_4h:
        df_4h = df_1h[["close"]].resample("4h").last().dropna()
        df_4h["log_return"] = np.log(df_4h["close"]).diff()
        df_4h["vol_24"] = df_4h["log_return"].rolling(6).std().fillna(0.0)   # 6 * 4h = 24h
        df_4h["mom_24"] = df_4h["close"].pct_change(6).fillna(0.0)
        # 1h high/low 를 4h 로 리샘플 (SL/TP 체크용)
        hl = df_1h[["high", "low"]].resample("4h").agg({"high": "max", "low": "min"})
        df_4h = df_4h.join(hl, how="left")
        df_4h = df_4h.dropna()
        df = df_4h
        min_bars = (days + 30) * 6  # 4h bars per day = 6
    else:
        df = df_1h
        min_bars = (days + 30) * 24

    if len(df) < min_bars:
        raise ValueError(f"{coin} 데이터 부족: {len(df)} bars")

    sim_start = end_ts - pd.Timedelta(days=days)
    train = df[df.index < sim_start]
    test = df[df.index >= sim_start].copy()
    bars_per_day = 6 if use_4h else 24
    min_train = 90 * bars_per_day
    min_test = 30 * bars_per_day
    if len(train) < min_train or len(test) < min_test:
        raise ValueError("HMM 학습/평가 구간 부족")

    features_cols = ["log_return", "vol_24", "mom_24"]
    refit_bars = 1 * bars_per_day  # 매일 refit (라이브와 동일)

    def _fit_model(train_df):
        X = train_df[features_cols].values
        m = GaussianHMM(n_components=n_states, covariance_type="full", n_iter=200, random_state=42)
        with contextlib.redirect_stderr(io.StringIO()):
            m.fit(X)
        st = m.predict(X)
        sm = {}
        for s in range(n_states):
            sm[s] = float(train_df["log_return"].values[st == s].mean()) if np.any(st == s) else 0.0
        ss = sorted(sm.items(), key=lambda x: x[1])
        return m, ss[0][0], ss[1][0] if len(ss) > 2 else ss[0][0], ss[-1][0]

    # 초기 학습
    model, bearish_state, neutral_state, bullish_state = _fit_model(train)

    # rolling refit: 매일 재학습하며 predict
    test_states = np.zeros(len(test), dtype=int)
    bars_since_refit = 0
    full_df = pd.concat([train, test])

    for idx in range(len(test)):
        bars_since_refit += 1
        if bars_since_refit >= refit_bars and idx > 0:
            # 현재 시점까지의 최근 90일로 재학습
            end_idx = len(train) + idx
            refit_start = max(0, end_idx - min_train)
            refit_df = full_df.iloc[refit_start:end_idx]
            if len(refit_df) >= min_train:
                try:
                    model, bearish_state, neutral_state, bullish_state = _fit_model(refit_df)
                    bars_since_refit = 0
                except Exception:
                    pass  # refit 실패 시 기존 모델 유지

        row_features = test.iloc[idx:idx+1][features_cols].values
        state = int(model.predict(row_features)[0])
        if min_state_prob > 0:
            probs = model.predict_proba(row_features)[0]
            if probs[state] < min_state_prob:
                state = neutral_state
        test_states[idx] = state

    cash = initial_capital
    position = 0  # -1, 0, 1
    entry_price = 0.0
    qty = 0.0
    total_fees = 0.0
    n_trades = 0
    equity_curve = []
    peak_price = 0.0
    trailing_active = False

    def _close(exit_price: float) -> None:
        nonlocal cash, position, qty, total_fees, n_trades, peak_price, trailing_active
        pnl = (exit_price - entry_price) * qty if position == 1 else (entry_price - exit_price) * qty
        fee = exit_price * qty * TOTAL_COST
        cash += pnl - fee
        total_fees += fee
        n_trades += 1
        position = 0
        qty = 0.0
        peak_price = 0.0
        trailing_active = False

    for idx, (ts, row) in enumerate(test.iterrows()):
        close = float(row["close"])
        high = float(row.get("high", close))
        low = float(row.get("low", close))

        # SL/TP/trailing 체크 (포지션 있을 때)
        if position != 0 and entry_price > 0:
            if position == 1:
                peak_price = max(peak_price, high)
                if sl_pct > 0:
                    sl_price = entry_price * (1 - sl_pct / 100 / leverage)
                    if low <= sl_price:
                        _close(sl_price)
                        continue
                if tp_pct > 0:
                    tp_price = entry_price * (1 + tp_pct / 100 / leverage)
                    if high >= tp_price:
                        _close(tp_price)
                        continue
                if trail_act_pct > 0 and trail_stop_pct > 0:
                    act_price = entry_price * (1 + trail_act_pct / 100 / leverage)
                    if peak_price >= act_price:
                        trailing_active = True
                    if trailing_active:
                        trail_price = peak_price * (1 - trail_stop_pct / 100 / leverage)
                        if low <= trail_price:
                            _close(trail_price)
                            continue
            else:  # short
                peak_price = min(peak_price, low) if peak_price > 0 else low
                if sl_pct > 0:
                    sl_price = entry_price * (1 + sl_pct / 100 / leverage)
                    if high >= sl_price:
                        _close(sl_price)
                        continue
                if tp_pct > 0:
                    tp_price = entry_price * (1 - tp_pct / 100 / leverage)
                    if low <= tp_price:
                        _close(tp_price)
                        continue
                if trail_act_pct > 0 and trail_stop_pct > 0:
                    act_price = entry_price * (1 - trail_act_pct / 100 / leverage)
                    if peak_price <= act_price:
                        trailing_active = True
                    if trailing_active:
                        trail_price = peak_price * (1 + trail_stop_pct / 100 / leverage)
                        if high >= trail_price:
                            _close(trail_price)
                            continue

        desired = 0
        state = int(test_states[idx])
        if state == bullish_state:
            desired = 1
        elif state == bearish_state:
            desired = -1

        equity = cash
        if position == 1:
            equity += (close - entry_price) * qty
        elif position == -1:
            equity += (entry_price - close) * qty
        equity_curve.append((ts, equity))

        if desired == position:
            continue

        if position != 0:
            _close(close)

        if desired != 0:
            notional = cash * leverage
            qty = notional / close
            fee = notional * TOTAL_COST
            cash -= fee
            total_fees += fee
            entry_price = close
            position = desired
            peak_price = close

    if position != 0:
        last_price = float(test["close"].iloc[-1])
        pnl = (last_price - entry_price) * qty if position == 1 else (entry_price - last_price) * qty
        fee = last_price * qty * TOTAL_COST
        cash += pnl - fee
        total_fees += fee
        n_trades += 1

    final_capital = cash
    return_pct = (final_capital - initial_capital) / initial_capital * 100
    equities = np.array([e[1] for e in equity_curve])
    if len(equities) >= 2:
        returns = np.diff(equities) / np.maximum(equities[:-1], 1e-9)
        sharpe = returns.mean() / returns.std() * np.sqrt(24 * 365) if len(returns) > 0 and returns.std() > 0 else 0.0
        peak = np.maximum.accumulate(equities)
        dd = (peak - equities) / np.maximum(peak, 1e-9)
        max_drawdown = float(dd.max() * 100)
    else:
        sharpe = 0.0
        max_drawdown = 0.0

    return HMMBacktestResult(
        coin=coin,
        days=days,
        initial=initial_capital,
        final=final_capital,
        return_pct=return_pct,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        n_trades=n_trades,
        total_fees=total_fees,
        bullish_state=bullish_state,
        bearish_state=bearish_state,
        neutral_state=neutral_state,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coin", default="BTC")
    parser.add_argument("--days", nargs="+", type=int, default=[180, 360])
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--leverage", type=float, default=2.0)
    parser.add_argument("--sl", type=float, default=0.0, help="SL %% (leveraged)")
    parser.add_argument("--tp", type=float, default=0.0, help="TP %% (leveraged)")
    parser.add_argument("--trail-act", type=float, default=0.0, help="Trailing activation %%")
    parser.add_argument("--trail-stop", type=float, default=0.0, help="Trailing stop %%")
    parser.add_argument("--compare", action="store_true", help="Run TP/SL/trailing comparison")
    parser.add_argument("--no-4h", action="store_true", help="Use 1h candles instead of 4h")
    parser.add_argument("--min-prob", type=float, default=0.7, help="Min state probability filter")
    args = parser.parse_args()

    use_4h = not args.no_4h
    candle_label = "4h" if use_4h else "1h"

    if args.compare:
        print(f"\n  HMM Regime TP/SL/Trailing 비교 ({args.coin}, {candle_label}, {args.leverage}x, prob>={args.min_prob})")
        configs = [
            ("baseline (no SL/TP)", 0, 0, 0, 0),
            ("SL 10%", 10, 0, 0, 0),
            ("SL 8%", 8, 0, 0, 0),
            ("TP 10%", 0, 10, 0, 0),
            ("TP 15%", 0, 15, 0, 0),
            ("TP 20%", 0, 20, 0, 0),
            ("SL 10% + TP 15%", 10, 15, 0, 0),
            ("SL 8% + TP 10%", 8, 10, 0, 0),
            ("SL 10% + trail 6%/3%", 10, 0, 6, 3),
            ("SL 8% + trail 5%/2.5%", 8, 0, 5, 2.5),
            ("SL 10% + TP 20% + trail 8%/4%", 10, 20, 8, 4),
        ]
        print(f"  {'config':<35s} {'180d ret':>10s} {'180d mdd':>9s} {'180d #':>6s} {'360d ret':>10s} {'360d mdd':>9s} {'360d #':>6s}")
        print("  " + "-" * 87)
        for label, sl, tp, ta, ts in configs:
            parts = []
            for days in [180, 360]:
                try:
                    r = simulate_hmm_regime(args.coin, days, args.capital, args.leverage,
                                            sl_pct=sl, tp_pct=tp, trail_act_pct=ta, trail_stop_pct=ts,
                                            use_4h=use_4h, min_state_prob=args.min_prob)
                    parts.append(f"{r.return_pct:+8.1f}% {r.max_drawdown:7.1f}% {r.n_trades:5d}")
                except Exception as e:
                    parts.append(f"{'err':>8s} {'':>7s} {'':>5s}")
            print(f"  {label:<35s} {parts[0]}  {parts[1]}")
        return

    print(f"\n  HMM Regime Detection 백테스트 ({candle_label}, {args.leverage}x, prob>={args.min_prob}, SL={args.sl}%, TP={args.tp}%)")
    for days in args.days:
        r = simulate_hmm_regime(args.coin, days, args.capital, args.leverage,
                                sl_pct=args.sl, tp_pct=args.tp,
                                trail_act_pct=args.trail_act, trail_stop_pct=args.trail_stop,
                                use_4h=use_4h, min_state_prob=args.min_prob)
        print(
            f"{days}d | ret={r.return_pct:+.2f}% | sharpe={r.sharpe:.2f} | "
            f"mdd={r.max_drawdown:.2f}% | trades={r.n_trades}"
        )


if __name__ == "__main__":
    main()
