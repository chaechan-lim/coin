"""
3개 idle 엔진의 파라미터 스윕 — 살릴 수 있는 조합 탐색.

기본 540일, 라이브 자본. 각 엔진별로 핵심 파라미터를 격자 탐색.
"""
from __future__ import annotations
import itertools
from breakout_pullback_backtest import simulate as bp_sim, DEFAULT_COINS as BP_COINS
from volume_momentum_backtest import simulate as vm_sim, DEFAULT_COINS as VM_COINS
from btc_neutral_mr_backtest import simulate as bn_sim, DEFAULT_COINS as BN_COINS


def fmt(r):
    return (f"ret={r['return_pct']:>+7.2f}%  mdd={r['max_dd_pct']:>5.2f}%  "
            f"n={r['n_trades']:>4d}  wr={r['win_rate']:>5.1f}%  pf={r['profit_factor']:>4.2f}")


def sweep_breakout():
    print("\n" + "=" * 80)
    print("BREAKOUT-PULLBACK 파라미터 스윕 (540일, 자본 400, 2x)")
    print("=" * 80)
    base = dict(coins=BP_COINS, days=540, initial_capital=400.0, leverage=2.0,
                trail_act=5.0, trail_stop=3.0)
    # baseline
    r = bp_sim(**base, lookback=20, pullback_pct=4.0, sl_pct=5.0, tp_pct=8.0)
    print(f"  [baseline]      LB=20 PB=4.0 SL=5.0 TP=8.0    {fmt(r)}")

    cases = []
    for sl in [5.0, 7.0, 10.0, 12.0]:
        for tp in [8.0, 12.0, 16.0, 20.0]:
            if tp <= sl: continue
            r = bp_sim(**base, lookback=20, pullback_pct=4.0, sl_pct=sl, tp_pct=tp)
            cases.append(("sl/tp", f"LB=20 PB=4.0 SL={sl} TP={tp}", r))

    for lb in [10, 20, 40, 55]:
        r = bp_sim(**base, lookback=lb, pullback_pct=4.0, sl_pct=10.0, tp_pct=16.0)
        cases.append(("lookback", f"LB={lb} PB=4.0 SL=10 TP=16", r))

    for pb in [2.0, 3.0, 4.0, 6.0, 8.0]:
        r = bp_sim(**base, lookback=20, pullback_pct=pb, sl_pct=10.0, tp_pct=16.0)
        cases.append(("pullback", f"LB=20 PB={pb} SL=10 TP=16", r))

    cases.sort(key=lambda x: -x[2]['return_pct'])
    print("\n  [sweep top 10]")
    for tag, label, r in cases[:10]:
        print(f"  [{tag:9s}]  {label:<35s}  {fmt(r)}")
    print("\n  [sweep bottom 5]")
    for tag, label, r in cases[-5:]:
        print(f"  [{tag:9s}]  {label:<35s}  {fmt(r)}")


def sweep_breakout_coins():
    """코인별 단일 시뮬 — 흑자 코인만 살리면?"""
    print("\n  [코인별 540d, baseline params]")
    base = dict(days=540, initial_capital=400.0, leverage=2.0,
                lookback=20, pullback_pct=4.0, sl_pct=5.0, tp_pct=8.0,
                trail_act=5.0, trail_stop=3.0)
    for c in BP_COINS:
        r = bp_sim(coins=[c], **base)
        print(f"    {c:6s}  {fmt(r)}")


def sweep_vol_mom():
    print("\n" + "=" * 80)
    print("VOLUME MOMENTUM 파라미터 스윕 (540일, 자본 200, 2x)")
    print("=" * 80)
    base = dict(coins=VM_COINS, days=540, initial_capital=200.0, leverage=2.0)
    r = vm_sim(**base, vol_mult=2.0, rsi_long_max=60, rsi_short_min=40,
               sl_atr_mult=2.5, tp_atr_mult=5.0)
    print(f"  [baseline]      vol=2.0  ATR SL/TP=2.5/5.0     {fmt(r)}")

    cases = []
    for vm in [3.0, 4.0, 5.0, 7.0, 10.0]:
        r = vm_sim(**base, vol_mult=vm, rsi_long_max=60, rsi_short_min=40,
                   sl_atr_mult=2.5, tp_atr_mult=5.0)
        cases.append(("vol_mult", f"vol={vm} SL/TP=2.5/5", r))

    for sl_m, tp_m in [(2.0, 4.0), (3.0, 6.0), (4.0, 8.0), (3.0, 9.0), (2.0, 8.0)]:
        r = vm_sim(**base, vol_mult=3.0, rsi_long_max=60, rsi_short_min=40,
                   sl_atr_mult=sl_m, tp_atr_mult=tp_m)
        cases.append(("atr_ratio", f"vol=3 SL/TP={sl_m}/{tp_m}", r))

    # RSI 더 엄격
    for rl, rs in [(50, 50), (55, 45), (40, 60)]:  # last is reversed: 추세 따라가기
        r = vm_sim(**base, vol_mult=3.0, rsi_long_max=rl, rsi_short_min=rs,
                   sl_atr_mult=3.0, tp_atr_mult=6.0)
        cases.append(("rsi", f"vol=3 RSI L<{rl} S>{rs}", r))

    cases.sort(key=lambda x: -x[2]['return_pct'])
    print("\n  [top 10]")
    for tag, label, r in cases[:10]:
        print(f"  [{tag:9s}]  {label:<35s}  {fmt(r)}")
    print("\n  [bottom 5]")
    for tag, label, r in cases[-5:]:
        print(f"  [{tag:9s}]  {label:<35s}  {fmt(r)}")


def sweep_vol_mom_coins():
    print("\n  [코인별 540d, vol=3.0, ATR SL/TP=3/6]")
    base = dict(days=540, initial_capital=200.0, leverage=2.0,
                vol_mult=3.0, rsi_long_max=60, rsi_short_min=40,
                sl_atr_mult=3.0, tp_atr_mult=6.0)
    for c in VM_COINS:
        r = vm_sim(coins=[c], **base)
        print(f"    {c:6s}  {fmt(r)}")


def sweep_btc_neutral():
    print("\n" + "=" * 80)
    print("BTC-NEUTRAL MR 파라미터 스윕 (540일, 자본 800, 2x)")
    print("=" * 80)
    base = dict(coins=BN_COINS, days=540, initial_capital=800.0, leverage=2.0,
                max_concurrent=3, position_pct=0.15)
    r = bn_sim(**base, lookback_days=7, z_entry=2.0, z_exit=0.3, max_hold_days=7)
    print(f"  [baseline]      LB=7d z_e=2.0 z_x=0.3 hold=7d  {fmt(r)}")

    cases = []
    for hold in [10, 14, 21, 30]:
        r = bn_sim(**base, lookback_days=7, z_entry=2.0, z_exit=0.3, max_hold_days=hold)
        cases.append(("hold", f"LB=7d z=2.0 hold={hold}d", r))

    for ze in [1.5, 2.0, 2.5, 3.0]:
        for zx in [0.0, 0.3, 0.5]:
            r = bn_sim(**base, lookback_days=14, z_entry=ze, z_exit=zx, max_hold_days=14)
            cases.append(("z_thresh", f"LB=14d z_e={ze} z_x={zx} hold=14d", r))

    for lb in [3, 7, 14, 21, 30]:
        r = bn_sim(**base, lookback_days=lb, z_entry=2.0, z_exit=0.3, max_hold_days=14)
        cases.append(("lookback", f"LB={lb}d z=2.0 hold=14d", r))

    cases.sort(key=lambda x: -x[2]['return_pct'])
    print("\n  [top 10]")
    for tag, label, r in cases[:10]:
        print(f"  [{tag:9s}]  {label:<35s}  {fmt(r)}")
    print("\n  [bottom 5]")
    for tag, label, r in cases[-5:]:
        print(f"  [{tag:9s}]  {label:<35s}  {fmt(r)}")


def sweep_btc_neutral_coins():
    """코인별 단독 운영"""
    print("\n  [코인 단독 운영 540d, lb=14d z=2 hold=14d]")
    base = dict(days=540, initial_capital=800.0, leverage=2.0,
                lookback_days=14, z_entry=2.0, z_exit=0.3, max_hold_days=14,
                max_concurrent=1, position_pct=0.15)
    for c in BN_COINS:
        r = bn_sim(coins=[c], **base)
        print(f"    {c:6s}  {fmt(r)}")


if __name__ == "__main__":
    sweep_breakout()
    sweep_breakout_coins()
    sweep_vol_mom()
    sweep_vol_mom_coins()
    sweep_btc_neutral()
    sweep_btc_neutral_coins()
