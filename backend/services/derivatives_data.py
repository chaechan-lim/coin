"""
DerivativesDataService
======================
In-memory TTL cache for derivatives market data:
  - Mark price (MarkPriceInfo)
  - Open interest (OpenInterest)
  - Long/short ratio (LongShortRatio)

Provides get_snapshot() / get_all_snapshots() for RegimeDetector consumption.
Thread-safe for asyncio (single-threaded event loop, no locks needed).
"""

import time
import structlog
from collections import OrderedDict

from exchange.data_models import LongShortRatio, MarkPriceInfo, OpenInterest

logger = structlog.get_logger(__name__)

# Default TTL values (seconds)
_MARK_PRICE_TTL: float = 120.0
_OI_TTL: float = 300.0
_LS_RATIO_TTL: float = 300.0

# Max number of symbols to cache per data type (LRU eviction beyond this)
_MAX_SYMBOLS: int = 200


class _TTLCache:
    """Simple LRU cache with TTL and configurable max size.

    Follows the _LRUCache pattern from services/market_data.py.
    Stores (timestamp, value) tuples keyed by symbol string.
    """

    def __init__(self, ttl_sec: float, max_size: int = _MAX_SYMBOLS) -> None:
        self._ttl = ttl_sec
        self._max_size = max_size
        self._data: OrderedDict[str, tuple[float, object]] = OrderedDict()

    def get(self, key: str):
        """Return value if present and not expired; None otherwise."""
        if key in self._data:
            ts, val = self._data[key]
            if time.time() - ts < self._ttl:
                self._data.move_to_end(key)
                return val
            del self._data[key]
        return None

    def put(self, key: str, value) -> None:
        """Insert or update a value, evicting LRU entry if over max_size."""
        self._data[key] = (time.time(), value)
        self._data.move_to_end(key)
        while len(self._data) > self._max_size:
            self._data.popitem(last=False)

    def keys_not_expired(self) -> list[str]:
        """Return list of keys whose entries have not yet expired."""
        now = time.time()
        expired = [k for k, (ts, _) in self._data.items() if now - ts >= self._ttl]
        for k in expired:
            del self._data[k]
        return list(self._data.keys())

    def clear(self) -> None:
        """Remove all cached entries."""
        self._data.clear()


class DerivativesDataService:
    """In-memory TTL cache for derivatives market data.

    Provides per-symbol caching for mark price, open interest, and long/short
    ratio with configurable TTLs. Exposes snapshot methods for RegimeDetector.

    Usage::

        svc = DerivativesDataService()
        svc.update_mark_price("BTC/USDT", mark_price_info)
        snap = svc.get_snapshot("BTC/USDT")   # -> dict | None
    """

    def __init__(
        self,
        mark_price_ttl: float = _MARK_PRICE_TTL,
        oi_ttl: float = _OI_TTL,
        ls_ratio_ttl: float = _LS_RATIO_TTL,
        max_symbols: int = _MAX_SYMBOLS,
    ) -> None:
        self._mark_price_cache: _TTLCache = _TTLCache(mark_price_ttl, max_symbols)
        self._oi_cache: _TTLCache = _TTLCache(oi_ttl, max_symbols)
        self._ls_ratio_cache: _TTLCache = _TTLCache(ls_ratio_ttl, max_symbols)

    # ── Update methods ──────────────────────────────────────────────

    def update_mark_price(self, symbol: str, info: MarkPriceInfo) -> None:
        """Cache mark price data for *symbol*."""
        self._mark_price_cache.put(symbol, info)
        logger.debug(
            "derivatives_mark_price_updated",
            symbol=symbol,
            mark_price=info.mark_price,
        )

    def update_open_interest(self, symbol: str, oi: OpenInterest) -> None:
        """Cache open interest data for *symbol*."""
        self._oi_cache.put(symbol, oi)
        logger.debug(
            "derivatives_oi_updated",
            symbol=symbol,
            oi_value=oi.open_interest_value,
        )

    def update_long_short_ratio(self, symbol: str, ratio: LongShortRatio) -> None:
        """Cache long/short ratio data for *symbol*."""
        self._ls_ratio_cache.put(symbol, ratio)
        logger.debug(
            "derivatives_ls_ratio_updated",
            symbol=symbol,
            long_account_ratio=ratio.long_account_ratio,
        )

    # ── Get methods ─────────────────────────────────────────────────

    def get_mark_price(self, symbol: str) -> MarkPriceInfo | None:
        """Retrieve cached mark price (TTL-checked). Returns None if absent/expired."""
        return self._mark_price_cache.get(symbol)  # type: ignore[return-value]

    def get_open_interest(self, symbol: str) -> OpenInterest | None:
        """Retrieve cached open interest (TTL-checked). Returns None if absent/expired."""
        return self._oi_cache.get(symbol)  # type: ignore[return-value]

    def get_long_short_ratio(self, symbol: str) -> LongShortRatio | None:
        """Retrieve cached long/short ratio (TTL-checked). Returns None if absent/expired."""
        return self._ls_ratio_cache.get(symbol)  # type: ignore[return-value]

    # ── Snapshot methods ────────────────────────────────────────────

    def get_snapshot(self, symbol: str) -> dict | None:
        """Return combined dict of all cached data for *symbol*.

        Used by RegimeDetector. Returns None only if *no* data is cached for
        the symbol. Individual fields are omitted if their cache has expired.
        """
        mark = self.get_mark_price(symbol)
        oi = self.get_open_interest(symbol)
        ls = self.get_long_short_ratio(symbol)

        if mark is None and oi is None and ls is None:
            return None

        result: dict = {"symbol": symbol}

        if mark is not None:
            result["mark_price"] = mark.mark_price
            result["index_price"] = mark.index_price
            result["last_funding_rate"] = mark.last_funding_rate
            result["next_funding_time"] = mark.next_funding_time
            result["premium_pct"] = mark.premium_pct
            result["mark_price_timestamp"] = mark.timestamp

        if oi is not None:
            result["open_interest_value"] = oi.open_interest_value
            result["oi_timestamp"] = oi.timestamp

        if ls is not None:
            result["long_account_ratio"] = ls.long_account_ratio
            result["short_account_ratio"] = ls.short_account_ratio
            result["long_position_ratio"] = ls.long_position_ratio
            result["short_position_ratio"] = ls.short_position_ratio
            result["ls_timestamp"] = ls.timestamp

        return result

    def get_all_snapshots(self) -> dict[str, dict]:
        """Return combined snapshots for all symbols with any cached data.

        Iterates across all three caches, collects live (non-expired) symbol
        keys, and assembles per-symbol snapshots.
        """
        symbols: set[str] = set()
        symbols.update(self._mark_price_cache.keys_not_expired())
        symbols.update(self._oi_cache.keys_not_expired())
        symbols.update(self._ls_ratio_cache.keys_not_expired())

        snapshots: dict[str, dict] = {}
        for symbol in symbols:
            snap = self.get_snapshot(symbol)
            if snap is not None:
                snapshots[symbol] = snap

        return snapshots

    def clear(self) -> None:
        """Clear all derivative data caches."""
        self._mark_price_cache.clear()
        self._oi_cache.clear()
        self._ls_ratio_cache.clear()
        logger.debug("derivatives_cache_cleared")
