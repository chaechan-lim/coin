"""
서버 이벤트 버스 — DB 기록 + WebSocket 브로드캐스트
"""
import asyncio
import structlog
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from db.session import get_session_factory
from core.models import ServerEvent

logger = structlog.get_logger(__name__)

_broadcast_fn: Callable[..., Coroutine] | None = None
_notification_fn: Callable[..., Coroutine] | None = None


def set_broadcast(callback: Callable[..., Coroutine]) -> None:
    """WebSocket broadcast 함수 등록 (main.py lifespan에서 1회 호출)."""
    global _broadcast_fn
    _broadcast_fn = callback


def set_notification(callback: Callable[..., Coroutine]) -> None:
    """Discord/Telegram 등 알림 콜백 등록 (main.py lifespan에서 1회 호출)."""
    global _notification_fn
    _notification_fn = callback


async def emit_event(
    level: str,
    category: str,
    title: str,
    detail: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """이벤트를 DB에 저장하고 WebSocket으로 브로드캐스트.

    오류 시 silent fail — 이벤트 시스템이 엔진을 크래시시키면 안 됨.
    """
    for attempt in range(3):
        try:
            sf = get_session_factory()
            async with sf() as session:
                event = ServerEvent(
                    level=level,
                    category=category,
                    title=title,
                    detail=detail,
                    metadata_=metadata,
                    created_at=datetime.now(timezone.utc),
                )
                session.add(event)
                await session.commit()
                await session.refresh(event)

                # WebSocket broadcast
                if _broadcast_fn:
                    await _broadcast_fn({
                        "event": "server_event",
                        "data": {
                            "id": event.id,
                            "level": event.level,
                            "category": event.category,
                            "title": event.title,
                            "detail": event.detail,
                            "metadata": event.metadata_,
                            "created_at": event.created_at.isoformat(),
                        },
                    })

                # 외부 알림 (Discord 등) — fire-and-forget
                if _notification_fn:
                    try:
                        asyncio.create_task(
                            _notification_fn(level, category, title, detail, metadata)
                        )
                    except (TypeError, RuntimeError) as e:
                        logger.debug("notification_dispatch_error", error=str(e))
                return
        except Exception as e:
            if attempt < 2 and "database is locked" in str(e):
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            logger.warning("emit_event_failed", error=str(e), title=title)
