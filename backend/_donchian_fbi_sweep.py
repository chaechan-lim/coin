"""Donchian Futures Bi 파라미터 스윕."""
from donchian_futures_bi_backtest import simulate, DEFAULT_COINS

def fmt(r):
    return (f"ret={r['return_pct']:>+7.2f}%  mdd={r['max_dd_pct']:>5.2f}%  "
            f"n={r['n_trades']:>4d}  wr={r['win_rate']:>5.1f}%  pf={r['profit_factor']:>4.2f}")


def main():
    print("\n" + "=" * 80)
    print("DONCHIAN FUTURES BI 파라미터 스윕 (540일, 자본 300, 2x)")
    print("=" * 80)
    base = dict(coins=DEFAULT_COINS, days=540, initial_capital=300.0, leverage=2.0)
    r = simulate(**base, base_risk_pct=0.01, atr_stop_mult=2.0,
                 min_entry_signals=1, min_exit_signals=1)
    print(f"  [baseline]      ATR×2  signals=1/1  risk=1%   {fmt(r)}")

    cases = []
    # ATR stop multiplier
    for sm in [2.0, 2.5, 3.0, 4.0, 5.0]:
        r = simulate(**base, base_risk_pct=0.01, atr_stop_mult=sm,
                     min_entry_signals=1, min_exit_signals=1)
        cases.append(("atr_mult", f"ATR×{sm}  signals=1/1  risk=1%", r))

    # Entry signal threshold (5 채널 중 N개)
    for ne in [1, 2, 3]:
        r = simulate(**base, base_risk_pct=0.01, atr_stop_mult=3.0,
                     min_entry_signals=ne, min_exit_signals=1)
        cases.append(("entry_sig", f"ATR×3.0  signals={ne}/1  risk=1%", r))

    # Exit signal
    for nx in [1, 2, 3]:
        r = simulate(**base, base_risk_pct=0.01, atr_stop_mult=3.0,
                     min_entry_signals=2, min_exit_signals=nx)
        cases.append(("exit_sig", f"ATR×3  signals=2/{nx}  risk=1%", r))

    # Risk pct
    for rp in [0.005, 0.01, 0.015, 0.02]:
        r = simulate(**base, base_risk_pct=rp, atr_stop_mult=3.0,
                     min_entry_signals=2, min_exit_signals=1)
        cases.append(("risk", f"ATR×3  signals=2/1  risk={rp*100:.1f}%", r))

    cases.sort(key=lambda x: -x[2]['return_pct'])
    print("\n  [top 10]")
    for tag, label, r in cases[:10]:
        print(f"  [{tag:9s}]  {label:<40s}  {fmt(r)}")
    print("\n  [bottom 5]")
    for tag, label, r in cases[-5:]:
        print(f"  [{tag:9s}]  {label:<40s}  {fmt(r)}")

    # 코인별 baseline
    print("\n  [코인별 540d, baseline params]")
    for c in DEFAULT_COINS:
        r = simulate(coins=[c], days=540, initial_capital=300.0, leverage=2.0,
                     base_risk_pct=0.01, atr_stop_mult=2.0,
                     min_entry_signals=1, min_exit_signals=1)
        print(f"    {c:6s}  {fmt(r)}")


if __name__ == "__main__":
    main()
