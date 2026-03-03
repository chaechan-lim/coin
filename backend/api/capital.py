"""
입출금(Capital Transaction) CRUD API
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, desc

from db.session import get_db
from core.models import CapitalTransaction
from core.schemas import (
    CapitalTransactionCreate,
    CapitalTransactionResponse,
    CapitalSummaryResponse,
)
from api.dependencies import engine_registry

router = APIRouter(prefix="/capital", tags=["capital"])

# 거래소 → 통화 매핑
_EXCHANGE_CURRENCY = {
    "bithumb": "KRW",
    "binance_futures": "USDT",
    "binance_spot": "USDT",
}


@router.post("/transactions", response_model=CapitalTransactionResponse)
async def create_transaction(
    body: CapitalTransactionCreate,
    session: AsyncSession = Depends(get_db),
):
    """수동 입출금 기록."""
    if body.tx_type not in ("deposit", "withdrawal"):
        raise HTTPException(400, "tx_type must be 'deposit' or 'withdrawal'")
    if body.amount <= 0:
        raise HTTPException(400, "amount must be positive")

    currency = _EXCHANGE_CURRENCY.get(body.exchange, "KRW")
    tx = CapitalTransaction(
        exchange=body.exchange,
        tx_type=body.tx_type,
        amount=body.amount,
        currency=currency,
        note=body.note,
        source="manual",
        confirmed=True,
    )
    session.add(tx)
    await session.flush()

    # initial_balance 즉시 반영
    pm = engine_registry.get_portfolio_manager(body.exchange)
    if pm:
        await pm.load_initial_balance_from_db(session)

    await session.commit()
    await session.refresh(tx)
    return _to_response(tx)


@router.get("/transactions", response_model=list[CapitalTransactionResponse])
async def list_transactions(
    exchange: str = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    """입출금 이력 조회."""
    result = await session.execute(
        select(CapitalTransaction)
        .where(CapitalTransaction.exchange == exchange)
        .order_by(desc(CapitalTransaction.created_at))
        .limit(200)
    )
    return [_to_response(tx) for tx in result.scalars()]


@router.post("/confirm/{tx_id}", response_model=CapitalTransactionResponse)
async def confirm_transaction(
    tx_id: int,
    session: AsyncSession = Depends(get_db),
):
    """자동 감지 건 확인 (confirmed → True)."""
    result = await session.execute(
        select(CapitalTransaction).where(CapitalTransaction.id == tx_id)
    )
    tx = result.scalar_one_or_none()
    if not tx:
        raise HTTPException(404, "Transaction not found")

    tx.confirmed = True
    await session.flush()

    # initial_balance 재계산
    pm = engine_registry.get_portfolio_manager(tx.exchange)
    if pm:
        await pm.load_initial_balance_from_db(session)

    await session.commit()
    await session.refresh(tx)
    return _to_response(tx)


@router.delete("/transactions/{tx_id}")
async def delete_transaction(
    tx_id: int,
    session: AsyncSession = Depends(get_db),
):
    """입출금 기록 삭제 (시드 레코드는 삭제 불가)."""
    result = await session.execute(
        select(CapitalTransaction).where(CapitalTransaction.id == tx_id)
    )
    tx = result.scalar_one_or_none()
    if not tx:
        raise HTTPException(404, "Transaction not found")
    if tx.source == "seed":
        raise HTTPException(400, "Cannot delete seed transaction")

    exchange = tx.exchange
    await session.delete(tx)
    await session.flush()

    # initial_balance 재계산
    pm = engine_registry.get_portfolio_manager(exchange)
    if pm:
        await pm.load_initial_balance_from_db(session)

    await session.commit()
    return {"ok": True}


@router.get("/summary", response_model=CapitalSummaryResponse)
async def get_summary(
    exchange: str = Query("bithumb"),
    session: AsyncSession = Depends(get_db),
):
    """거래소별 입출금 요약."""
    currency = _EXCHANGE_CURRENCY.get(exchange, "KRW")
    result = await session.execute(
        select(
            func.coalesce(func.sum(
                case((CapitalTransaction.tx_type == "deposit", CapitalTransaction.amount), else_=0)
            ), 0),
            func.coalesce(func.sum(
                case((CapitalTransaction.tx_type == "withdrawal", CapitalTransaction.amount), else_=0)
            ), 0),
            func.count(CapitalTransaction.id),
        ).where(
            CapitalTransaction.exchange == exchange,
            CapitalTransaction.confirmed == True,  # noqa: E712
        )
    )
    deposits, withdrawals, count = result.one()
    return CapitalSummaryResponse(
        exchange=exchange,
        total_deposits=float(deposits),
        total_withdrawals=float(withdrawals),
        net_capital=float(deposits) - float(withdrawals),
        currency=currency,
        transaction_count=int(count),
    )


def _to_response(tx: CapitalTransaction) -> CapitalTransactionResponse:
    return CapitalTransactionResponse(
        id=tx.id,
        exchange=tx.exchange,
        tx_type=tx.tx_type,
        amount=tx.amount,
        currency=tx.currency,
        note=tx.note,
        source=tx.source,
        confirmed=tx.confirmed,
        created_at=tx.created_at,
    )
