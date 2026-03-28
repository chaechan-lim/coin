"""
입출금 자동 감지 모듈
- 바이낸스: fetch_deposits('USDT') API로 USDT 외부 입금 자동 감지
- 바이낸스: Universal Transfer API로 spot↔futures 내부 이체 자동 감지
- 빗썸: KRW 잔고 변동 감지 (설명 불가능한 증가 → 입금 후보)
"""
import structlog
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.models import CapitalTransaction
from core.event_bus import emit_event

logger = structlog.get_logger(__name__)

# Binance Universal Transfer 타입 (USDM 선물 관련)
_TRANSFER_SPOT_TO_FUTURES = "MAIN_UMFUTURE"   # 현물 → USDM 선물
_TRANSFER_FUTURES_TO_SPOT = "UMFUTURE_MAIN"   # USDM 선물 → 현물

_INTERNAL_TRANSFER_PAGE_SIZE = 100  # Binance Universal Transfer API max rows per page


async def sync_binance_deposits(
    session: AsyncSession, adapter, exchange_name: str = "binance_futures"
) -> list[CapitalTransaction]:
    """바이낸스 USDT 입금을 자동 감지하여 미확인 CapitalTransaction 생성."""
    try:
        deposits = await adapter._exchange.fetch_deposits("USDT")
    except Exception as e:
        logger.warning("fetch_binance_deposits_failed", error=str(e))
        return []

    # 기존 DB에서 exchange_tx_id로 이미 기록된 건 필터
    result = await session.execute(
        select(CapitalTransaction.exchange_tx_id)
        .where(
            CapitalTransaction.exchange == exchange_name,
            CapitalTransaction.exchange_tx_id.isnot(None),
        )
    )
    existing_ids = {row for row in result.scalars()}

    new_txs = []
    for dep in deposits:
        tx_id = dep.get("id") or dep.get("txid")
        if not tx_id or str(tx_id) in existing_ids:
            continue
        if dep.get("status") != "ok":
            continue
        amount = float(dep["amount"])
        tx = CapitalTransaction(
            exchange=exchange_name,
            tx_type="deposit",
            amount=amount,
            currency="USDT",
            note=f"자동 감지 (txid: {tx_id})",
            source="auto_detected",
            confirmed=False,
            exchange_tx_id=str(tx_id),
        )
        session.add(tx)
        new_txs.append(tx)
        logger.info("binance_deposit_detected", tx_id=tx_id, amount=amount)

    if new_txs:
        await session.flush()
        for tx in new_txs:
            await emit_event(
                "info", "capital",
                f"바이낸스 USDT 입금 감지: {tx.amount:.2f} USDT",
                detail=f"txid: {tx.exchange_tx_id}",
                metadata={"tx_id": tx.id, "amount": tx.amount, "currency": "USDT"},
            )

    return new_txs


async def sync_binance_internal_transfers(
    session: AsyncSession,
    adapter,
    exchange_name: str = "binance_futures",
    last_sync_time: datetime | None = None,
) -> tuple[list[CapitalTransaction], bool]:
    """바이낸스 spot↔futures 내부 이체를 자동 감지.

    Binance Universal Transfer API (/sapi/v1/asset/transfer)로 다음 두 방향 조회:
    - MAIN_UMFUTURE: 현물 → USDM 선물 (선물 PM 입금)
    - UMFUTURE_MAIN: USDM 선물 → 현물 (선물 PM 출금)

    각 이체는 exchange_tx_id='transfer_{tranId}' 형식으로 중복 방지.
    새 CapitalTransaction을 flush하고 반환.
    PM cash 조정은 호출자가 session.commit() 성공 후 수행해야 함
    (커밋 실패 시 재시도에서 이중 조정 방지).

    Returns:
        (new_txs, all_ok): all_ok=False이면 1개 이상의 방향에서 API 오류 발생.
        호출자는 all_ok=False 시 last_sync_time을 전진시키지 않아야 함.
    """
    # 기존 DB에서 이미 기록된 transfer exchange_tx_id 로드
    result = await session.execute(
        select(CapitalTransaction.exchange_tx_id)
        .where(
            CapitalTransaction.exchange == exchange_name,
            CapitalTransaction.exchange_tx_id.like("transfer_%"),
        )
    )
    existing_ids = set(result.scalars())

    # last_sync_time 기준 타임스탬프 (ms).
    # None이면 DB에서 가장 최근 transfer_* TX의 created_at을 조회해 재시작 후 누락 방지.
    # DB에 기록이 없으면 24시간 이내로 fallback.
    if last_sync_time is None:
        latest_at = await session.scalar(
            select(func.max(CapitalTransaction.created_at))
            .where(
                CapitalTransaction.exchange == exchange_name,
                CapitalTransaction.exchange_tx_id.like("transfer_%"),
            )
        )
        # SQLite는 timezone=True 컬럼을 naive datetime으로 반환 → UTC로 명시 보정
        if latest_at is not None and latest_at.tzinfo is None:
            latest_at = latest_at.replace(tzinfo=timezone.utc)
        last_sync_time = latest_at or datetime.now(timezone.utc) - timedelta(hours=24)
    start_ts = int(last_sync_time.timestamp() * 1000)

    new_txs: list[CapitalTransaction] = []
    had_error = False

    for transfer_type in (_TRANSFER_SPOT_TO_FUTURES, _TRANSFER_FUTURES_TO_SPOT):
        page = 1
        while True:
            try:
                resp = await adapter._exchange.sapiGetAssetTransfer({
                    "type": transfer_type,
                    "startTime": start_ts,
                    "size": _INTERNAL_TRANSFER_PAGE_SIZE,
                    "current": page,
                })
            except Exception as e:
                logger.warning(
                    "fetch_binance_internal_transfers_failed",
                    transfer_type=transfer_type,
                    error=str(e),
                )
                had_error = True
                break

            rows = (resp.get("rows") or []) if isinstance(resp, dict) else []
            for row in rows:
                # USDT만 처리
                if row.get("asset", "").upper() != "USDT":
                    continue
                # CONFIRMED 상태만 처리
                if row.get("status", "").upper() != "CONFIRMED":
                    continue

                # sapiGetAssetTransfer는 raw Binance JSON 반환 → 항상 "tranId"
                tran_id = row.get("tranId")
                if not tran_id:
                    continue
                exchange_tx_id = f"transfer_{tran_id}"
                if exchange_tx_id in existing_ids:
                    continue

                try:
                    amount = float(row.get("amount") or 0)
                except (TypeError, ValueError):
                    logger.warning("invalid_transfer_amount", tran_id=tran_id, row=row)
                    continue
                if amount <= 0:
                    continue

                # 방향 결정: spot→futures = 선물 PM 입금, futures→spot = 선물 PM 출금
                if transfer_type == _TRANSFER_SPOT_TO_FUTURES:
                    tx_type = "deposit"
                    direction_note = "현물→선물 내부 이체"
                else:
                    tx_type = "withdrawal"
                    direction_note = "선물→현물 내부 이체"

                tx = CapitalTransaction(
                    exchange=exchange_name,
                    tx_type=tx_type,
                    amount=amount,
                    currency="USDT",
                    note=f"자동 감지 ({direction_note}, tranId: {tran_id})",
                    source="auto_detected",
                    confirmed=False,
                    exchange_tx_id=exchange_tx_id,
                )
                session.add(tx)
                existing_ids.add(exchange_tx_id)
                new_txs.append(tx)

                logger.info(
                    "binance_internal_transfer_detected",
                    transfer_type=transfer_type,
                    tran_id=tran_id,
                    amount=amount,
                    tx_type=tx_type,
                )

            # 마지막 페이지면 루프 종료
            if len(rows) < _INTERNAL_TRANSFER_PAGE_SIZE:
                break
            page += 1

    if new_txs:
        await session.flush()

        for tx in new_txs:
            direction = "입금" if tx.tx_type == "deposit" else "출금"
            await emit_event(
                "info", "capital",
                f"바이낸스 선물 내부 이체 {direction} 감지: {tx.amount:.2f} USDT",
                detail=f"tranId: {tx.exchange_tx_id}",
                metadata={"exchange_tx_id": tx.exchange_tx_id, "amount": tx.amount, "currency": "USDT", "tx_type": tx.tx_type},
            )

    return new_txs, not had_error


async def detect_bithumb_balance_change(
    session: AsyncSession, pm, adapter, exchange_name: str = "bithumb",
) -> CapitalTransaction | None:
    """빗썸 KRW 잔고의 설명 불가능한 증가를 감지."""
    try:
        balances = await adapter.fetch_balance()
    except Exception as e:
        logger.warning("fetch_bithumb_balance_failed", error=str(e))
        return None

    cash_bal = balances.get("KRW")
    actual_krw = cash_bal.free if cash_bal else 0

    expected = pm.cash_balance
    diff = actual_krw - expected

    # 10,000원 이상 설명 불가능한 증가 → 입금 후보
    if diff > 10_000:
        tx = CapitalTransaction(
            exchange=exchange_name,
            tx_type="deposit",
            amount=diff,
            currency="KRW",
            note=f"잔고 변동 감지 ({expected:,.0f} → {actual_krw:,.0f})",
            source="auto_detected",
            confirmed=False,
        )
        session.add(tx)
        await session.flush()

        logger.info("bithumb_balance_increase_detected", diff=diff, expected=expected, actual=actual_krw)
        await emit_event(
            "info", "capital",
            f"빗썸 KRW 잔고 증가 감지: +{diff:,.0f}원",
            detail=f"예상 {expected:,.0f} → 실제 {actual_krw:,.0f}",
            metadata={"tx_id": tx.id, "amount": diff, "currency": "KRW"},
        )
        return tx

    return None
