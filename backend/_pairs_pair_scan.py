"""
Pairs Trading 페어 스캐닝 — BTC-ETH 외 알파 있는 페어 탐색.

전략:
- 1단계: 모든 주요 페어에 대해 540d 단일 백테스트 (best params from BTC-ETH grid)
- 2단계: 상위 페어에 대해 그리드 서치
- 3단계: 최종 후보 WF 검증
"""
from __future__ import annotations
import itertools
from pairs_trading_backtest import simulate_pairs_trading

MAJORS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "LINK", "DOT", "DOGE", "ATOM", "ARB"]


def stage1_scan_pairs():
    """모든 majors 페어 조합에 대해 단일 540d 백테스트."""
    print("\n" + "=" * 100)
    print("STAGE 1 — 페어 스캔 (540d, lb=72, z_e=2.0, z_x=0.3, z_s=5.0, lev=2)")
    print("=" * 100)
    rows = []
    for a, b in itertools.combinations(MAJORS, 2):
        try:
            r = simulate_pairs_trading(
                days=540, initial_capital=300.0,
                lookback_hours=72, z_entry=2.0, z_exit=0.3, z_stop=5.0,
                leverage=2.0, coin_a=a, coin_b=b,
            )
            rows.append((a, b, r.return_pct, r.sharpe, r.max_drawdown, r.n_trades))
        except Exception as e:
            rows.append((a, b, None, None, None, None))

    rows.sort(key=lambda r: -(r[2] or -999))
    print(f"  {'pair':>12s}  {'return':>8s}  {'sharpe':>7s}  {'mdd':>6s}  {'trades':>7s}")
    print("  " + "-" * 60)
    for a, b, ret, sh, mdd, n in rows:
        if ret is None:
            print(f"  {a}-{b:6s}  ERR")
        else:
            mark = " ✅" if ret > 5 and mdd < 20 else ""
            print(f"  {a}-{b:6s}  {ret:>+7.2f}%  {sh:>+6.2f}  {mdd:>5.2f}%  {n:>7d}{mark}")
    return rows


def stage2_grid_top(top_pairs, n=5):
    """상위 페어 N개에 대해 미니 그리드."""
    from pairs_trading_backtest import iter_param_grid
    print("\n" + "=" * 100)
    print(f"STAGE 2 — 상위 {n}개 페어 미니 그리드")
    print("=" * 100)
    lookbacks = [48, 72, 96, 168]
    z_entries = [1.5, 2.0, 2.5]
    z_exits = [0.0, 0.3, 0.5]
    z_stops = [5.0]
    leverages = [2.0]

    for a, b, *_ in top_pairs[:n]:
        best = None
        for lb, ze, zx, zs, lv in iter_param_grid(lookbacks, z_entries, z_exits, z_stops, leverages):
            try:
                r = simulate_pairs_trading(
                    days=540, initial_capital=300.0,
                    lookback_hours=lb, z_entry=ze, z_exit=zx, z_stop=zs,
                    leverage=lv, coin_a=a, coin_b=b,
                )
                if best is None or r.return_pct > best[0]:
                    best = (r.return_pct, lb, ze, zx, r.sharpe, r.max_drawdown, r.n_trades)
            except Exception:
                continue
        if best:
            ret, lb, ze, zx, sh, mdd, n = best
            print(f"  {a}-{b:6s}  best: lb={lb} ze={ze} zx={zx}  ret={ret:+.2f}%  "
                  f"sharpe={sh:+.2f}  mdd={mdd:.2f}%  n={n}")
        else:
            print(f"  {a}-{b:6s}  ERR")


if __name__ == "__main__":
    rows = stage1_scan_pairs()
    # 흑자 페어만 stage2
    profit = [r for r in rows if r[2] is not None and r[2] > 0]
    if profit:
        stage2_grid_top(profit, n=min(5, len(profit)))
    else:
        print("\n  STAGE 1 흑자 페어 0 — STAGE 2 skip")
