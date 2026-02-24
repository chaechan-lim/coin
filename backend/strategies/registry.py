import structlog
from typing import Type
from strategies.base import BaseStrategy

logger = structlog.get_logger(__name__)


class StrategyRegistry:
    """Singleton registry for trading strategies."""

    _strategies: dict[str, Type[BaseStrategy]] = {}

    @classmethod
    def register(cls, strategy_cls: Type[BaseStrategy]):
        """Decorator to register a strategy class."""
        # We need to instantiate briefly to get the name, or use class attribute
        name = strategy_cls.__dict__.get("name", None)
        if name is None:
            # Try instantiating with no args to get name
            try:
                instance = strategy_cls()
                name = instance.name
            except Exception:
                name = strategy_cls.__name__

        cls._strategies[name] = strategy_cls
        logger.info("strategy_registered", name=name)
        return strategy_cls

    @classmethod
    def get(cls, name: str, **kwargs) -> BaseStrategy:
        """Get a strategy instance by name."""
        if name not in cls._strategies:
            raise KeyError(f"Strategy '{name}' not registered. Available: {list(cls._strategies.keys())}")
        return cls._strategies[name](**kwargs)

    @classmethod
    def get_all_names(cls) -> list[str]:
        return list(cls._strategies.keys())

    @classmethod
    def get_all(cls) -> dict[str, Type[BaseStrategy]]:
        return cls._strategies.copy()

    @classmethod
    def create_all(cls, **kwargs) -> dict[str, BaseStrategy]:
        """Create instances of all registered strategies."""
        return {name: strat_cls(**kwargs) for name, strat_cls in cls._strategies.items()}
