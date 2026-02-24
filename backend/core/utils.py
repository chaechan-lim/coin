"""
공통 유틸리티
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """
    현재 UTC 시간을 timezone-naive datetime으로 반환.
    PostgreSQL TIMESTAMP WITHOUT TIME ZONE 컬럼과 호환.
    (datetime.utcnow()는 deprecated → 이 함수를 사용)
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
