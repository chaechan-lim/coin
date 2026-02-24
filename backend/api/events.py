"""
서버 이벤트 API — 페이징/필터 조회 + 레벨별 건수
"""
from fastapi import APIRouter, Query
from sqlalchemy import select, func

from db.session import get_session_factory
from core.models import ServerEvent
from core.schemas import ServerEventResponse

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=list[ServerEventResponse])
async def get_events(
    page: int = Query(1, ge=1),
    size: int = Query(30, ge=1, le=200),
    level: str | None = Query(None),
    category: str | None = Query(None),
):
    """이벤트 목록 (페이징 + 필터, created_at DESC)."""
    sf = get_session_factory()
    async with sf() as session:
        q = select(ServerEvent)
        if level:
            q = q.where(ServerEvent.level == level)
        if category:
            q = q.where(ServerEvent.category == category)
        q = q.order_by(ServerEvent.created_at.desc())
        q = q.offset((page - 1) * size).limit(size)
        result = await session.execute(q)
        events = result.scalars().all()
        return [
            ServerEventResponse(
                id=e.id,
                level=e.level,
                category=e.category,
                title=e.title,
                detail=e.detail,
                metadata=e.metadata_,
                created_at=e.created_at,
            )
            for e in events
        ]


@router.get("/counts")
async def get_event_counts():
    """레벨별 이벤트 건수 (배지용)."""
    sf = get_session_factory()
    async with sf() as session:
        q = select(ServerEvent.level, func.count(ServerEvent.id)).group_by(ServerEvent.level)
        result = await session.execute(q)
        counts = {row[0]: row[1] for row in result.all()}
        return counts
