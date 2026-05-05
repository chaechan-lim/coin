"""
BTC-Neutral MR 코인 조합 탐색 — WF robust 한 조합 찾기.

Stage 1: 각 alt 단독 운영 (lb=7d hold=21d 적용 — 라이브 파라미터)
Stage 2: 흑자 단독 코인 + 8 alts default 의 WF 비교
Stage 3: 조합 (top 2/3/4 모두 시도)
"""
from __future__ import annotations
import itertools
from btc_neutral_mr_backtest import simulate

ALL_ALTS = ["ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK"]


def fmt(r):
    return (f"ret={r['return_pct']:>+7.2f}%  mdd={r['max_dd_pct']:>5.2f}%  "
            f"n={r['n_trades']:>4d}  wr={r['win_rate']:>5.1f}%  pf={r['profit_factor']:>4.2f}")


def run(coins, days=540):
    return simulate(
        coins=coins, days=days, initial_capital=800.0, leverage=2.0,
        lookback_days=7, z_entry=2.0, z_exit=0.3, max_hold_days=21,
        max_concurrent=3, position_pct=0.15,
    )


def wf_eval(coins, label):
    """4 윈도우 (540/405/270/135d). 모두 양수면 robust."""
    print(f"  {label} ({coins}):")
    rets = []
    for d in [540, 405, 270, 135]:
        try:
            r = run(coins, d)
            rets.append(r['return_pct'])
            print(f"    {d}d: {fmt(r)}")
        except Exception as e:
            print(f"    {d}d: ERR {e}")
            rets.append(None)
    valid = [x for x in rets if x is not None]
    pos = sum(1 for x in valid if x > 0)
    print(f"    >> {pos}/{len(valid)} windows positive, avg={sum(valid)/len(valid):+.2f}%")
    return rets


def stage1_single():
    print("\n" + "=" * 80)
    print("STAGE 1 — 각 alt 단독 540d (라이브 lb=7d hold=21d 파라미터)")
    print("=" * 80)
    rows = []
    for c in ALL_ALTS:
        r = run([c])
        rows.append((c, r))
        print(f"  {c:6s}  {fmt(r)}")
    rows.sort(key=lambda x: -x[1]['return_pct'])
    return [c for c, _ in rows[:5]]  # top 5


def stage2_compare():
    print("\n" + "=" * 80)
    print("STAGE 2 — 8 alts default + 4 alts (현재 라이브) WF 검증")
    print("=" * 80)
    wf_eval(ALL_ALTS, "8 alts default")
    print()
    wf_eval(["ETH", "SOL", "LINK", "BNB"], "4 alts (현재 라이브)")


def stage3_combos(top5):
    print("\n" + "=" * 80)
    print(f"STAGE 3 — Top 5 단독 흑자 코인의 조합 ({top5})")
    print("=" * 80)
    # Top 1 단독, Top 2/3/4/5 조합
    for k in [1, 2, 3]:
        for combo in itertools.combinations(top5, k):
            wf_eval(list(combo), f"{k} coins")
            print()


if __name__ == "__main__":
    top5 = stage1_single()
    stage2_compare()
    stage3_combos(top5)
