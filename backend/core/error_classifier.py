"""
에러 분류 시스템 — 예외를 카테고리별로 분류하여 복구 전략 결정

기존 core/exceptions.py 계층 활용:
- ExchangeConnectionError → TRANSIENT (백오프 후 재시도)
- ExchangeRateLimitError → TRANSIENT (긴 백오프)
- InsufficientBalanceError → RESOURCE (잔고 재계산)
- OrderNotFoundError → STATE (포지션 동기화)
- 상폐/심볼 없음 → PERMANENT (코인 억제)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.exceptions import (
    ExchangeConnectionError,
    ExchangeRateLimitError,
    ExchangeError,
    InsufficientBalanceError,
    OrderError,
    OrderNotFoundError,
    StrategyError,
)


class ErrorCategory(str, Enum):
    TRANSIENT = "transient"    # 네트워크, 타임아웃, 레이트리밋
    RESOURCE = "resource"      # 잔고 부족, 최소 주문 미달
    STATE = "state"            # 포지션 불일치, entry_price=0
    PERMANENT = "permanent"    # 심볼 상폐, 거래소 비활성


@dataclass(frozen=True)
class ClassifiedError:
    category: ErrorCategory
    original: Exception
    symbol: str | None
    context: str               # "buy_order", "sell_order", "price_fetch"
    retryable: bool
    max_retries: int
    backoff_base: float        # 초
    recovery_action: str | None  # "reconcile_cash", "sync_positions", "suppress_coin"


# 상폐/비활성 키워드 (거래소 에러 메시지 패턴)
_PERMANENT_KEYWORDS = (
    "delisted", "symbol not found", "not trading", "market is closed",
    "invalid symbol", "no such symbol", "trading halt",
)


def classify_error(
    exc: Exception,
    context: str,
    symbol: str | None = None,
) -> ClassifiedError:
    """예외를 분류하여 복구 전략이 담긴 ClassifiedError 반환."""
    msg = str(exc).lower()

    # ── 상폐/비활성 키워드 우선 체크 ──
    if any(kw in msg for kw in _PERMANENT_KEYWORDS):
        return ClassifiedError(
            category=ErrorCategory.PERMANENT,
            original=exc,
            symbol=symbol,
            context=context,
            retryable=False,
            max_retries=0,
            backoff_base=0,
            recovery_action="suppress_coin",
        )

    # ── core/exceptions.py 계층 기반 분류 ──

    if isinstance(exc, ExchangeRateLimitError):
        return ClassifiedError(
            category=ErrorCategory.TRANSIENT,
            original=exc,
            symbol=symbol,
            context=context,
            retryable=True,
            max_retries=3,
            backoff_base=5.0,
            recovery_action=None,
        )

    if isinstance(exc, ExchangeConnectionError):
        return ClassifiedError(
            category=ErrorCategory.TRANSIENT,
            original=exc,
            symbol=symbol,
            context=context,
            retryable=True,
            max_retries=3,
            backoff_base=2.0,
            recovery_action=None,
        )

    if isinstance(exc, InsufficientBalanceError):
        return ClassifiedError(
            category=ErrorCategory.RESOURCE,
            original=exc,
            symbol=symbol,
            context=context,
            retryable=True,
            max_retries=1,
            backoff_base=1.0,
            recovery_action="reconcile_cash",
        )

    if isinstance(exc, OrderNotFoundError):
        return ClassifiedError(
            category=ErrorCategory.STATE,
            original=exc,
            symbol=symbol,
            context=context,
            retryable=True,
            max_retries=1,
            backoff_base=1.0,
            recovery_action="sync_positions",
        )

    # 타임아웃 패턴 (ccxt, httpx, aiohttp 공통)
    if any(kw in msg for kw in ("timeout", "timed out", "read timeout", "connect timeout")):
        return ClassifiedError(
            category=ErrorCategory.TRANSIENT,
            original=exc,
            symbol=symbol,
            context=context,
            retryable=True,
            max_retries=3,
            backoff_base=2.0,
            recovery_action=None,
        )

    # 잔고 부족 패턴 (ccxt가 래핑 안 한 경우)
    if any(kw in msg for kw in ("insufficient", "not enough", "balance too low")):
        return ClassifiedError(
            category=ErrorCategory.RESOURCE,
            original=exc,
            symbol=symbol,
            context=context,
            retryable=True,
            max_retries=1,
            backoff_base=1.0,
            recovery_action="reconcile_cash",
        )

    # 일반 ExchangeError → TRANSIENT (보수적 재시도)
    if isinstance(exc, ExchangeError):
        return ClassifiedError(
            category=ErrorCategory.TRANSIENT,
            original=exc,
            symbol=symbol,
            context=context,
            retryable=True,
            max_retries=2,
            backoff_base=3.0,
            recovery_action=None,
        )

    # OrderError (OrderNotFound 제외) → STATE
    if isinstance(exc, OrderError):
        return ClassifiedError(
            category=ErrorCategory.STATE,
            original=exc,
            symbol=symbol,
            context=context,
            retryable=True,
            max_retries=1,
            backoff_base=1.0,
            recovery_action="sync_positions",
        )

    # StrategyError → TRANSIENT (데이터 부족 등, 다음 사이클에 해결)
    if isinstance(exc, StrategyError):
        return ClassifiedError(
            category=ErrorCategory.TRANSIENT,
            original=exc,
            symbol=symbol,
            context=context,
            retryable=False,
            max_retries=0,
            backoff_base=0,
            recovery_action=None,
        )

    # ── 미분류 예외 → TRANSIENT, 재시도 1회 ──
    return ClassifiedError(
        category=ErrorCategory.TRANSIENT,
        original=exc,
        symbol=symbol,
        context=context,
        retryable=True,
        max_retries=1,
        backoff_base=3.0,
        recovery_action=None,
    )
