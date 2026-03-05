"""에러 분류 시스템 테스트.

classify_error()가 기존 core/exceptions.py 계층 + 메시지 패턴을 올바르게 분류하는지 검증.
"""
import pytest
from core.error_classifier import classify_error, ErrorCategory, ClassifiedError
from core.exceptions import (
    ExchangeConnectionError,
    ExchangeRateLimitError,
    ExchangeError,
    InsufficientBalanceError,
    OrderNotFoundError,
    OrderError,
    StrategyError,
    InsufficientDataError,
)


class TestClassifyExceptionHierarchy:
    """core/exceptions.py 예외 계층 기반 분류."""

    def test_connection_error_is_transient(self):
        exc = ExchangeConnectionError("connection refused")
        result = classify_error(exc, "price_fetch", "BTC/KRW")
        assert result.category == ErrorCategory.TRANSIENT
        assert result.retryable is True
        assert result.max_retries == 3
        assert result.backoff_base == 2.0
        assert result.recovery_action is None

    def test_rate_limit_is_transient_long_backoff(self):
        exc = ExchangeRateLimitError("429 too many requests")
        result = classify_error(exc, "buy_order", "ETH/KRW")
        assert result.category == ErrorCategory.TRANSIENT
        assert result.retryable is True
        assert result.max_retries == 3
        assert result.backoff_base == 5.0

    def test_insufficient_balance_is_resource(self):
        exc = InsufficientBalanceError("not enough USDT")
        result = classify_error(exc, "buy_order", "BTC/USDT")
        assert result.category == ErrorCategory.RESOURCE
        assert result.retryable is True
        assert result.max_retries == 1
        assert result.recovery_action == "reconcile_cash"

    def test_order_not_found_is_state(self):
        exc = OrderNotFoundError("order 12345 not found")
        result = classify_error(exc, "sell_order", "ETH/KRW")
        assert result.category == ErrorCategory.STATE
        assert result.retryable is True
        assert result.recovery_action == "sync_positions"

    def test_generic_exchange_error_is_transient(self):
        exc = ExchangeError("unknown exchange error")
        result = classify_error(exc, "price_fetch", "XRP/KRW")
        assert result.category == ErrorCategory.TRANSIENT
        assert result.retryable is True
        assert result.max_retries == 2
        assert result.backoff_base == 3.0

    def test_generic_order_error_is_state(self):
        exc = OrderError("order processing error")
        result = classify_error(exc, "buy_order", "SOL/USDT")
        assert result.category == ErrorCategory.STATE
        assert result.recovery_action == "sync_positions"

    def test_strategy_error_not_retryable(self):
        exc = StrategyError("strategy computation failed")
        result = classify_error(exc, "evaluation", "BTC/KRW")
        assert result.category == ErrorCategory.TRANSIENT
        assert result.retryable is False
        assert result.max_retries == 0

    def test_insufficient_data_error_not_retryable(self):
        exc = InsufficientDataError("need 200 candles, got 50")
        result = classify_error(exc, "evaluation", "ETH/KRW")
        assert result.category == ErrorCategory.TRANSIENT
        assert result.retryable is False


class TestClassifyMessagePatterns:
    """에러 메시지 패턴 기반 분류."""

    def test_delisted_is_permanent(self):
        exc = ExchangeError("Symbol LUNA/USDT has been delisted")
        result = classify_error(exc, "price_fetch", "LUNA/USDT")
        assert result.category == ErrorCategory.PERMANENT
        assert result.retryable is False
        assert result.recovery_action == "suppress_coin"

    def test_symbol_not_found_is_permanent(self):
        exc = Exception("symbol not found: POWER/USDT")
        result = classify_error(exc, "price_fetch", "POWER/USDT")
        assert result.category == ErrorCategory.PERMANENT
        assert result.recovery_action == "suppress_coin"

    def test_not_trading_is_permanent(self):
        exc = ExchangeError("market is closed for NOT TRADING symbol")
        result = classify_error(exc, "buy_order", "XYZ/KRW")
        assert result.category == ErrorCategory.PERMANENT

    def test_trading_halt_is_permanent(self):
        exc = Exception("trading halt on DOGE/KRW")
        result = classify_error(exc, "buy_order", "DOGE/KRW")
        assert result.category == ErrorCategory.PERMANENT

    def test_timeout_pattern_is_transient(self):
        exc = Exception("read timeout after 30 seconds")
        result = classify_error(exc, "price_fetch", "BTC/KRW")
        assert result.category == ErrorCategory.TRANSIENT
        assert result.retryable is True
        assert result.backoff_base == 2.0

    def test_timed_out_pattern(self):
        exc = Exception("request timed out")
        result = classify_error(exc, "buy_order", "ETH/KRW")
        assert result.category == ErrorCategory.TRANSIENT
        assert result.retryable is True

    def test_insufficient_message_pattern(self):
        exc = Exception("Account has insufficient balance for requested action")
        result = classify_error(exc, "buy_order", "BTC/USDT")
        assert result.category == ErrorCategory.RESOURCE
        assert result.recovery_action == "reconcile_cash"

    def test_balance_too_low_pattern(self):
        exc = Exception("balance too low to execute order")
        result = classify_error(exc, "buy_order", "ETH/USDT")
        assert result.category == ErrorCategory.RESOURCE

    def test_permanent_keyword_overrides_exception_type(self):
        """상폐 키워드는 예외 타입보다 우선."""
        exc = ExchangeConnectionError("symbol not found")
        result = classify_error(exc, "price_fetch", "ABC/KRW")
        assert result.category == ErrorCategory.PERMANENT


class TestClassifyMetadata:
    """ClassifiedError 메타데이터 검증."""

    def test_symbol_preserved(self):
        exc = Exception("some error")
        result = classify_error(exc, "buy_order", "BTC/KRW")
        assert result.symbol == "BTC/KRW"
        assert result.context == "buy_order"
        assert result.original is exc

    def test_no_symbol(self):
        exc = Exception("some error")
        result = classify_error(exc, "system_check")
        assert result.symbol is None

    def test_unknown_exception_is_transient_with_retry(self):
        exc = RuntimeError("unexpected internal error")
        result = classify_error(exc, "evaluation", "BTC/KRW")
        assert result.category == ErrorCategory.TRANSIENT
        assert result.retryable is True
        assert result.max_retries == 1

    def test_classified_error_is_frozen(self):
        exc = Exception("test")
        result = classify_error(exc, "test", "BTC/KRW")
        with pytest.raises(AttributeError):
            result.category = ErrorCategory.PERMANENT
