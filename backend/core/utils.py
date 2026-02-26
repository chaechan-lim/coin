"""
공통 유틸리티
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """
    현재 UTC 시간을 timezone-aware datetime으로 반환.
    PostgreSQL TIMESTAMP WITH TIME ZONE 컬럼과 호환.
    (datetime.utcnow()는 deprecated → 이 함수를 사용)
    """
    return datetime.now(timezone.utc)


def ensure_aware(dt: datetime | None) -> datetime | None:
    """naive datetime을 UTC aware로 변환 (SQLite 호환)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
