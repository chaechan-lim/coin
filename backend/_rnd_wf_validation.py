"""
재활성 4엔진 Walk-Forward 검증.

각 엔진을 동일 라이브 파라미터로 4분할 시간 윈도우 (135일씩)에서 반복 실행.
모든 윈도우 흑자면 robust, 일부만 흑자면 overfit 의심.
"""
from __future__ import annotations
import pandas as pd

# 1. Volume Momentum
from volume_momentum_backtest import simulate as vm_sim
# 2. BTC-Neutral MR
from btc_neutral_mr_backtest import simulate as bn_sim
# 3. Breakout-Pullback
from breakout_pullback_backtest import simulate as bp_sim
# 4. Donchian Futures Bi
from donchian_futures_bi_backtest import simulate as df_sim


VM_COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "LINK"]
BN_COINS = ["ETH", "SOL", "LINK", "BNB"]
BP_COINS = ["XRP"]
DF_COINS = ["XRP"]


def header(name):
    print("\n" + "=" * 80)
    print(f"WF — {name}")
    print("=" * 80)


def fmt(r):
    return (f"ret={r['return_pct']:>+7.2f}%  mdd={r['max_dd_pct']:>5.2f}%  "
            f"n={r['n_trades']:>4d}  wr={r['win_rate']:>5.1f}%  pf={r['profit_factor']:>5.2f}")


def wf_volume_momentum():
    header("Volume Momentum (vol=3, SL/TP=4/8, 7 coins)")
    # 135d × 4 windows
    for window_days in [540, 405, 270, 135]:
        r = vm_sim(coins=VM_COINS, days=window_days, initial_capital=200.0,
                   leverage=2.0, vol_mult=3.0, sl_atr_mult=4.0, tp_atr_mult=8.0)
        print(f"  last {window_days}d: {fmt(r)}")


def wf_btc_neutral():
    header("BTC-Neutral MR (lb=7d z=2 z_x=0.3 hold=21d, 4 alts)")
    for window_days in [540, 405, 270, 135]:
        r = bn_sim(coins=BN_COINS, days=window_days, initial_capital=800.0,
                   leverage=2.0, lookback_days=7, z_entry=2.0, z_exit=0.3,
                   max_hold_days=21, max_concurrent=3, position_pct=0.15)
        print(f"  last {window_days}d: {fmt(r)}")


def wf_breakout_pullback():
    header("Breakout-Pullback (XRP only, PB=4% SL=5% TP=8%)")
    for window_days in [540, 405, 270, 135]:
        r = bp_sim(coins=BP_COINS, days=window_days, initial_capital=100.0,
                   leverage=2.0, lookback=20, pullback_pct=4.0,
                   sl_pct=5.0, tp_pct=8.0)
        print(f"  last {window_days}d: {fmt(r)}")


def wf_donchian_futures_bi():
    header("Donchian Futures Bi (XRP only, baseline)")
    for window_days in [540, 405, 270, 135]:
        r = df_sim(coins=DF_COINS, days=window_days, initial_capital=100.0,
                   leverage=2.0, base_risk_pct=0.01, atr_stop_mult=2.0,
                   min_entry_signals=1, min_exit_signals=1)
        print(f"  last {window_days}d: {fmt(r)}")


if __name__ == "__main__":
    wf_volume_momentum()
    wf_btc_neutral()
    wf_breakout_pullback()
    wf_donchian_futures_bi()
