"""
선물 포트폴리오 스냅샷 스파이크 삭제 스크립트.

미실현PnL 이중 계산 버그로 인해 비정상적으로 높거나 낮은 total_value 스냅샷을 삭제.
사용: cd backend && .venv/bin/python scripts/cleanup_futures_spikes.py [--dry-run]
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy import text


async def cleanup(dry_run: bool = True):
    from config import get_config
    config = get_config()
    engine = create_async_engine(config.database.url)

    async with engine.begin() as conn:
        # 1) 전체 선물 스냅샷 조회
        result = await conn.execute(text("""
            SELECT id, snapshot_at, total_value_krw, cash_balance_krw,
                   invested_value_krw, unrealized_pnl, peak_value
            FROM portfolio_snapshots
            WHERE exchange = 'binance_futures'
            ORDER BY snapshot_at ASC
        """))
        rows = result.fetchall()

        if len(rows) < 3:
            print(f"스냅샷 {len(rows)}개 — 분석 불가 (최소 3개 필요)")
            await engine.dispose()
            return

        # 2) 이웃 기반 스파이크 감지 (전후 평균 대비 10% 이상 차이)
        spike_ids = []
        THRESHOLD = 0.10  # 10%

        for i in range(1, len(rows) - 1):
            prev_val = rows[i - 1][2]  # total_value_krw
            curr_val = rows[i][2]
            next_val = rows[i + 1][2]

            if prev_val <= 0 or next_val <= 0:
                continue

            neighbor_avg = (prev_val + next_val) / 2
            deviation = abs(curr_val - neighbor_avg) / neighbor_avg

            if deviation > THRESHOLD:
                spike_ids.append(rows[i][0])
                print(f"  스파이크: id={rows[i][0]}, time={rows[i][1]}, "
                      f"value={curr_val:.2f}, 이웃평균={neighbor_avg:.2f}, "
                      f"편차={deviation:.1%}")

        # 첫/마지막 데이터도 확인
        if len(rows) >= 2:
            # 첫 번째: 두 번째와 비교
            if rows[1][2] > 0:
                dev = abs(rows[0][2] - rows[1][2]) / rows[1][2]
                if dev > THRESHOLD:
                    spike_ids.append(rows[0][0])
                    print(f"  스파이크(첫): id={rows[0][0]}, time={rows[0][1]}, "
                          f"value={rows[0][2]:.2f}, 다음={rows[1][2]:.2f}, 편차={dev:.1%}")
            # 마지막: 이전과 비교
            if rows[-2][2] > 0:
                dev = abs(rows[-1][2] - rows[-2][2]) / rows[-2][2]
                if dev > THRESHOLD:
                    spike_ids.append(rows[-1][0])
                    print(f"  스파이크(끝): id={rows[-1][0]}, time={rows[-1][1]}, "
                          f"value={rows[-1][2]:.2f}, 이전={rows[-2][2]:.2f}, 편차={dev:.1%}")

        if not spike_ids:
            print(f"스파이크 없음 (전체 {len(rows)}개 정상)")
            await engine.dispose()
            return

        print(f"\n총 {len(spike_ids)}개 스파이크 감지 (전체 {len(rows)}개 중)")

        if dry_run:
            print("\n[DRY RUN] 실제 삭제하려면 --delete 옵션 사용")
        else:
            # 삭제 실행
            placeholders = ", ".join(str(sid) for sid in spike_ids)
            await conn.execute(text(
                f"DELETE FROM portfolio_snapshots WHERE id IN ({placeholders})"
            ))
            print(f"\n{len(spike_ids)}개 스파이크 스냅샷 삭제 완료")

            # peak_value도 정상 범위로 보정
            result = await conn.execute(text("""
                SELECT MAX(total_value_krw) FROM portfolio_snapshots
                WHERE exchange = 'binance_futures'
            """))
            max_val = result.scalar()
            if max_val:
                await conn.execute(text(f"""
                    UPDATE portfolio_snapshots
                    SET peak_value = {max_val}
                    WHERE exchange = 'binance_futures'
                    AND peak_value > {max_val * 1.1}
                """))
                print(f"peak_value 보정 완료 (최대값: {max_val:.2f})")

    await engine.dispose()


if __name__ == "__main__":
    dry_run = "--delete" not in sys.argv
    asyncio.run(cleanup(dry_run))
