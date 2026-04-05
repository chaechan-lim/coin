"""
DerivativesDataService — 파생상품 데이터 인메모리 TTL 캐시.

MarkPriceInfo, OpenInterest, LongShortRatio를 심볼별로 캐시하고
TTL 만료 시 None을 반환한다. RegimeDetector / FuturesEngineV2에서 참조.
"""

import time
import structlog
from collections import OrderedDict
from dataclasses import dataclass

from exchange.data_models import LongShortRatio, MarkPriceInfo, OpenInterest

logger = structlog.get_logger(__name__)

# 기본 TTL (초)
_DEFAULT_MARK_PRICE_TTL = 120  # 마크프라이스: 2분
_DEFAULT_OI_TTL = 300  # 미결제약정: 5분
_DEFAULT_LS_RATIO_TTL = 300  # 롱숏비율: 5분
_MAX_SYMBOLS = 100  # 최대 추적 심볼 수


@dataclass
class _CacheEntry:
    """타임스탬프 포함 캐시 엔트리."""

    value: object
    stored_at: float  # time.monotonic()


class DerivativesDataService:
    """파생상품 데이터 인메모리 TTL 캐시.

    - update_mark_price / get_mark_price: 마크프라이스 + 프리미엄 + 펀딩비율
    - update_open_interest / get_open_interest: 미결제약정
    - update_long_short_ratio / get_long_short_ratio: 롱숏비율
    - get_snapshot: 심볼별 전체 데이터 스냅샷 (RegimeDetector용)
    """

    def __init__(
        self,
        mark_price_ttl: float = _DEFAULT_MARK_PRICE_TTL,
        oi_ttl: float = _DEFAULT_OI_TTL,
        ls_ratio_ttl: float = _DEFAULT_LS_RATIO_TTL,
        max_symbols: int = _MAX_SYMBOLS,
    ):
        self._mark_price_ttl = mark_price_ttl
        self._oi_ttl = oi_ttl
        self._ls_ratio_ttl = ls_ratio_ttl
        self._max_symbols = max_symbols

        self._mark_prices: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._open_interests: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._ls_ratios: OrderedDict[str, _CacheEntry] = OrderedDict()

    # ── Mark Price ────────────────────────────────────

    def update_mark_price(self, symbol: str, info: MarkPriceInfo) -> None:
        """마크프라이스 캐시 업데이트."""
        self._put(self._mark_prices, symbol, info)

    def get_mark_price(self, symbol: str) -> MarkPriceInfo | None:
        """마크프라이스 조회 (TTL 만료 시 None)."""
        return self._get(self._mark_prices, symbol, self._mark_price_ttl)

    # ── Open Interest ─────────────────────────────────

    def update_open_interest(self, symbol: str, oi: OpenInterest) -> None:
        """미결제약정 캐시 업데이트."""
        self._put(self._open_interests, symbol, oi)

    def get_open_interest(self, symbol: str) -> OpenInterest | None:
        """미결제약정 조회 (TTL 만료 시 None)."""
        return self._get(self._open_interests, symbol, self._oi_ttl)

    # ── Long/Short Ratio ──────────────────────────────

    def update_long_short_ratio(self, symbol: str, ratio: LongShortRatio) -> None:
        """롱숏비율 캐시 업데이트."""
        self._put(self._ls_ratios, symbol, ratio)

    def get_long_short_ratio(self, symbol: str) -> LongShortRatio | None:
        """롱숏비율 조회 (TTL 만료 시 None)."""
        return self._get(self._ls_ratios, symbol, self._ls_ratio_ttl)

    # ── Snapshot (RegimeDetector용) ───────────────────

    def get_snapshot(self, symbol: str) -> dict | None:
        """심볼의 전체 파생상품 데이터 스냅샷 반환.

        Returns:
            dict with keys: mark_price, open_interest, long_short_ratio
            모든 데이터가 없으면 None.
        """
        mp = self.get_mark_price(symbol)
        oi = self.get_open_interest(symbol)
        ls = self.get_long_short_ratio(symbol)

        if mp is None and oi is None and ls is None:
            return None

        snapshot: dict = {}
        if mp is not None:
            snapshot["mark_price"] = mp.mark_price
            snapshot["index_price"] = mp.index_price
            snapshot["premium_pct"] = mp.premium_pct
            snapshot["funding_rate"] = mp.last_funding_rate
        if oi is not None:
            snapshot["open_interest_value"] = oi.open_interest_value
        if ls is not None:
            snapshot["long_account_ratio"] = ls.long_account_ratio
            snapshot["short_account_ratio"] = ls.short_account_ratio
            snapshot["long_position_ratio"] = ls.long_position_ratio
            snapshot["short_position_ratio"] = ls.short_position_ratio

        return snapshot

    def get_all_snapshots(self) -> dict[str, dict]:
        """모든 심볼의 스냅샷 반환."""
        symbols: set[str] = set()
        symbols.update(self._mark_prices.keys())
        symbols.update(self._open_interests.keys())
        symbols.update(self._ls_ratios.keys())

        result: dict[str, dict] = {}
        for symbol in symbols:
            snap = self.get_snapshot(symbol)
            if snap is not None:
                result[symbol] = snap
        return result

    # ── Internal ──────────────────────────────────────

    def _get(
        self, store: OrderedDict[str, _CacheEntry], key: str, ttl: float
    ) -> object | None:
        """TTL 기반 캐시 조회."""
        entry = store.get(key)
        if entry is None:
            return None
        if (time.monotonic() - entry.stored_at) > ttl:
            # TTL 만료 — 삭제하지 않고 None 반환 (lazy expiry)
            return None
        return entry.value

    def _put(self, store: OrderedDict[str, _CacheEntry], key: str, value: object) -> None:
        """캐시에 저장 (LRU 방식 eviction)."""
        store[key] = _CacheEntry(value=value, stored_at=time.monotonic())
        store.move_to_end(key)
        while len(store) > self._max_symbols:
            store.popitem(last=False)
