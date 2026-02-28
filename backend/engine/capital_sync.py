"""
입출금 자동 감지 모듈
- 바이낸스: fetch_deposits('USDT') API로 USDT 입금 자동 감지
- 빗썸: KRW 잔고 변동 감지 (설명 불가능한 증가 → 입금 후보)
"""
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.models import CapitalTransaction
from core.event_bus import emit_event

logger = structlog.get_logger(__name__)


async def sync_binance_deposits(session: AsyncSession, adapter) -> list[CapitalTransaction]:
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
            CapitalTransaction.exchange == "binance_futures",
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
            exchange="binance_futures",
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


async def detect_bithumb_balance_change(
    session: AsyncSession, pm, adapter,
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
            exchange="bithumb",
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
