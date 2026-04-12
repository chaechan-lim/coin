"""
거래소별 엔진/PM/콤바이너/코디네이터 중앙 레지스트리.
"""
import structlog
from typing import Any, Literal

logger = structlog.get_logger(__name__)

# Valid exchange names for API parameter validation
VALID_EXCHANGES = {
    "bithumb",
    "binance_futures",
    "binance_spot",
    "binance_surge",
    "binance_donchian",
    "binance_donchian_futures",
    "binance_pairs",
    "binance_momentum",
    "binance_hmm",
    "binance_fgdca",
}
ExchangeNameType = Literal[
    "bithumb",
    "binance_futures",
    "binance_spot",
    "binance_surge",
    "binance_donchian",
    "binance_donchian_futures",
    "binance_pairs",
    "binance_momentum",
    "binance_hmm",
    "binance_fgdca",
]


def validate_exchange(exchange: str) -> str:
    """Validate exchange name, raise ValueError if invalid."""
    if exchange not in VALID_EXCHANGES:
        raise ValueError(f"Invalid exchange: '{exchange}'. Valid: {sorted(VALID_EXCHANGES)}")
    return exchange


class EngineRegistry:
    """API 레이어에서 거래소별 엔진/PM에 접근하는 중앙 레지스트리."""

    def __init__(self):
        self._engines: dict[str, Any] = {}
        self._portfolio_managers: dict[str, Any] = {}
        self._combiners: dict[str, Any] = {}
        self._coordinators: dict[str, Any] = {}
        self._shared: dict[str, Any] = {}

    def register(
        self,
        exchange_name: str,
        engine,
        portfolio_manager,
        combiner,
        coordinator,
    ) -> None:
        self._engines[exchange_name] = engine
        self._portfolio_managers[exchange_name] = portfolio_manager
        self._combiners[exchange_name] = combiner
        self._coordinators[exchange_name] = coordinator
        if engine is not None and hasattr(engine, "set_engine_registry"):
            engine.set_engine_registry(self)
        logger.info("engine_registered", exchange=exchange_name)

    def get_engine(self, exchange: str = "bithumb"):
        return self._engines.get(exchange)

    def get_portfolio_manager(self, exchange: str = "bithumb"):
        return self._portfolio_managers.get(exchange)

    def get_combiner(self, exchange: str = "bithumb"):
        return self._combiners.get(exchange)

    def get_coordinator(self, exchange: str = "bithumb"):
        return self._coordinators.get(exchange)

    def set_shared(self, key: str, value: Any) -> None:
        self._shared[key] = value

    def get_shared(self, key: str, default: Any = None):
        return self._shared.get(key, default)

    @property
    def available_exchanges(self) -> list[str]:
        return list(self._engines.keys())


# Singleton
engine_registry = EngineRegistry()
