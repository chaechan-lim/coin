class TradingBotError(Exception):
    """Base exception for the trading bot."""
    pass


class ExchangeError(TradingBotError):
    """Exchange-related errors."""
    pass


class ExchangeConnectionError(ExchangeError):
    """Failed to connect to exchange."""
    pass


class ExchangeRateLimitError(ExchangeError):
    """Rate limit exceeded."""
    pass


class InsufficientBalanceError(ExchangeError):
    """Insufficient balance for order."""
    pass


class OrderError(TradingBotError):
    """Order-related errors."""
    pass


class OrderNotFoundError(OrderError):
    """Order not found."""
    pass


class StrategyError(TradingBotError):
    """Strategy-related errors."""
    pass


class InsufficientDataError(StrategyError):
    """Not enough data to compute strategy signal."""
    pass


class RiskLimitError(TradingBotError):
    """Risk limit exceeded."""
    pass


class ConfigurationError(TradingBotError):
    """Configuration error."""
    pass
