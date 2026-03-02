"""
1회성 스크립트: 선물 스냅샷에서 일시적 스파이크를 제거하고 peak_value 재계산.

알고리즘: 중앙값 기반 — 각 값의 주변 ±10개 중앙값 대비 15%+ 이탈이면 스파이크.
연속 스파이크 체인(403→393 등)도 정확히 걸러냄.

사용법:
    cd /home/chans/coin/backend
    .venv/bin/python scripts/fix_peak_value.py [--dry-run]
"""
import argparse
import asyncio
import statistics
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from db.session import get_session_factory
from core.models import PortfolioSnapshot


SPIKE_THRESHOLD_PCT = 15.0
MEDIAN_WINDOW = 10
EXCHANGE = "binance_futures"


async def fix_peak():
    parser = argparse.ArgumentParser(description="Fix futures peak_value from spike snapshots")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without modifying DB")
    args = parser.parse_args()

    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await session.execute(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.exchange == EXCHANGE)
            .order_by(PortfolioSnapshot.snapshot_at.asc())
        )
        snapshots = list(result.scalars().all())

        if not snapshots:
            print(f"No snapshots found for {EXCHANGE}")
            return

        totals = [s.total_value_krw for s in snapshots]
        print(f"Total snapshots: {len(snapshots)}")
        print(f"Current latest peak_value: {snapshots[-1].peak_value:.2f}")
        print(f"Current latest total_value: {totals[-1]:.2f}")

        # 중앙값 기반 스파이크 판별
        clean_peak = 0.0
        spike_count = 0
        spike_examples = []

        for i in range(len(totals)):
            lo = max(0, i - MEDIAN_WINDOW)
            hi = min(len(totals), i + MEDIAN_WINDOW + 1)
            neighborhood = sorted(totals[lo:hi])
            median = statistics.median(neighborhood)

            if median > 0 and abs(totals[i] - median) / median * 100 > SPIKE_THRESHOLD_PCT:
                spike_count += 1
                if len(spike_examples) < 10:
                    spike_examples.append(
                        f"  SPIKE at {snapshots[i].snapshot_at}: "
                        f"total={totals[i]:.2f}  median={median:.2f}  "
                        f"dev={abs(totals[i]-median)/median*100:.1f}%"
                    )
            else:
                if totals[i] > clean_peak:
                    clean_peak = totals[i]

        for line in spike_examples:
            print(line)
        if spike_count > 10:
            print(f"  ... and {spike_count - 10} more spikes")

        print(f"\nSpikes found: {spike_count}")
        print(f"Recalculated clean peak: {clean_peak:.2f}")
        print(f"Original peak in latest snapshot: {snapshots[-1].peak_value:.2f}")

        if clean_peak >= snapshots[-1].peak_value:
            print("Clean peak >= current peak — no fix needed.")
            return

        if args.dry_run:
            print(f"\n[DRY RUN] Would update latest snapshot peak_value: "
                  f"{snapshots[-1].peak_value:.2f} → {clean_peak:.2f}")
            return

        latest = snapshots[-1]
        old_peak = latest.peak_value
        latest.peak_value = clean_peak
        await session.commit()
        print(f"\nUpdated latest snapshot peak_value: {old_peak:.2f} → {clean_peak:.2f}")
        print("Restart server to apply (restore_state_from_db will load new peak).")


if __name__ == "__main__":
    asyncio.run(fix_peak())
