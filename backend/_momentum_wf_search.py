"""
Momentum Rotation 재검토 — 파라미터 + 코인 + WF 검증.

Stage 1: WF baseline (현재 라이브 파라미터 540/405/270/135d)
Stage 2: regime filter ON/OFF 비교
Stage 3: 파라미터 스윕 (lookback / rebalance / top_n)
Stage 4: 코인 universe 축소 (24 → 10 / 5)
"""
from __future__ import annotations
import os
from momentum_rotation_backtest import simulate_momentum_rotation

ALL_COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "DOT",
             "UNI", "ATOM", "FIL", "APT", "ARB", "OP", "NEAR", "SUI", "TIA", "SEI",
             "INJ", "AAVE", "LTC", "ETC"]
AVAILABLE = [c for c in ALL_COINS if os.path.exists(f".cache/{c}_USDT_1d.csv")]


def fmt(r):
    return (f"ret={r.return_pct:>+7.2f}%  sharpe={r.sharpe:>+5.2f}  "
            f"mdd={r.max_drawdown:>5.2f}%  reb={r.n_rebalances:>3d}")


def wf(coins, lookback=7, rebalance=5, top_n=3, bottom_n=3, regime=True, label=""):
    print(f"  {label}")
    rets = []
    for d in [540, 405, 270, 135]:
        try:
            r = simulate_momentum_rotation(
                coins=coins, days=d, initial_capital=200.0,
                lookback_days=lookback, rebalance_days=rebalance,
                top_n=top_n, bottom_n=bottom_n, leverage=2.0,
                regime_filter=regime,
            )
            rets.append(r.return_pct)
            print(f"    {d:>3d}d: {fmt(r)}")
        except Exception as e:
            print(f"    {d:>3d}d: ERR {e}")
            rets.append(None)
    valid = [x for x in rets if x is not None]
    pos = sum(1 for x in valid if x > 0)
    avg = sum(valid) / len(valid) if valid else 0
    print(f"    >> {pos}/{len(valid)} pos, avg={avg:+.2f}%")
    return rets


def stage1_baseline():
    print("\n" + "=" * 80)
    print("STAGE 1 — 라이브 baseline (lb=7, reb=5, top=3, bot=3, regime ON)")
    print("=" * 80)
    wf(AVAILABLE, label="현재 라이브 (24 coins available, regime ON)")


def stage2_regime():
    print("\n" + "=" * 80)
    print("STAGE 2 — regime filter ON/OFF 비교")
    print("=" * 80)
    wf(AVAILABLE, regime=True, label="regime ON")
    print()
    wf(AVAILABLE, regime=False, label="regime OFF")


def stage3_params():
    print("\n" + "=" * 80)
    print("STAGE 3 — 파라미터 스윕 (regime ON)")
    print("=" * 80)
    cases = []
    for lb in [7, 14, 30]:
        for reb in [5, 7, 14]:
            wf(AVAILABLE, lookback=lb, rebalance=reb, regime=True,
               label=f"lb={lb}d reb={reb}d top=3 bot=3")
            print()


def stage4_coin_universe():
    print("\n" + "=" * 80)
    print("STAGE 4 — 코인 universe 축소")
    print("=" * 80)
    # Top 10 by typical liquidity
    top10 = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "DOT"]
    top10 = [c for c in top10 if c in AVAILABLE]
    wf(top10, regime=True, label=f"Top 10 ({top10})")
    print()
    # Top 5
    top5 = ["BTC", "ETH", "SOL", "XRP", "BNB"]
    top5 = [c for c in top5 if c in AVAILABLE]
    wf(top5, regime=True, top_n=2, bottom_n=2, label=f"Top 5 (top_n=2 bot_n=2): {top5}")


if __name__ == "__main__":
    print(f"AVAILABLE coins: {len(AVAILABLE)} — {AVAILABLE}")
    stage1_baseline()
    stage2_regime()
    stage3_params()
    stage4_coin_universe()
