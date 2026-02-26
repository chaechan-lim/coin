"""
거래소별 엔진/PM/콤바이너/코디네이터 중앙 레지스트리.
"""
import structlog
from typing import Any

logger = structlog.get_logger(__name__)


class EngineRegistry:
    """API 레이어에서 거래소별 엔진/PM에 접근하는 중앙 레지스트리."""

    def __init__(self):
        self._engines: dict[str, Any] = {}
        self._portfolio_managers: dict[str, Any] = {}
        self._combiners: dict[str, Any] = {}
        self._coordinators: dict[str, Any] = {}

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
        logger.info("engine_registered", exchange=exchange_name)

    def get_engine(self, exchange: str = "bithumb"):
        return self._engines.get(exchange)

    def get_portfolio_manager(self, exchange: str = "bithumb"):
        return self._portfolio_managers.get(exchange)

    def get_combiner(self, exchange: str = "bithumb"):
        return self._combiners.get(exchange)

    def get_coordinator(self, exchange: str = "bithumb"):
        return self._coordinators.get(exchange)

    @property
    def available_exchanges(self) -> list[str]:
        return list(self._engines.keys())


# Singleton
engine_registry = EngineRegistry()
