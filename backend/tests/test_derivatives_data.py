"""DerivativesDataService 테스트."""

import time
import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from services.derivatives_data import DerivativesDataService
from exchange.data_models import MarkPriceInfo, OpenInterest, LongShortRatio


def _make_mark_price(
    symbol: str = "BTC/USDT",
    mark: float = 65100.0,
    index: float = 65000.0,
    funding: float = 0.0005,
) -> MarkPriceInfo:
    return MarkPriceInfo(
        symbol=symbol,
        mark_price=mark,
        index_price=index,
        last_funding_rate=funding,
        next_funding_time=datetime(2024, 1, 1, 8, 0, tzinfo=timezone.utc),
        premium_pct=((mark - index) / index * 100) if index else 0.0,
        timestamp=datetime.now(timezone.utc),
    )


def _make_open_interest(
    symbol: str = "BTC/USDT",
    value: float = 100_000_000.0,
) -> OpenInterest:
    return OpenInterest(
        symbol=symbol,
        open_interest_value=value,
        timestamp=datetime.now(timezone.utc),
    )


def _make_long_short_ratio(
    symbol: str = "BTC/USDT",
    long_account: float = 0.65,
    short_account: float = 0.35,
    long_position: float = 0.60,
    short_position: float = 0.40,
) -> LongShortRatio:
    return LongShortRatio(
        symbol=symbol,
        long_account_ratio=long_account,
        short_account_ratio=short_account,
        long_position_ratio=long_position,
        short_position_ratio=short_position,
        timestamp=datetime.now(timezone.utc),
    )


class TestMarkPriceCache:
    def test_update_and_get(self):
        svc = DerivativesDataService()
        mp = _make_mark_price()
        svc.update_mark_price("BTC/USDT", mp)
        result = svc.get_mark_price("BTC/USDT")
        assert result is not None
        assert result.mark_price == 65100.0
        assert result.index_price == 65000.0

    def test_missing_symbol_returns_none(self):
        svc = DerivativesDataService()
        assert svc.get_mark_price("ETH/USDT") is None

    def test_ttl_expiry(self):
        svc = DerivativesDataService(mark_price_ttl=1.0)
        mp = _make_mark_price()
        svc.update_mark_price("BTC/USDT", mp)

        # 즉시 조회: 유효
        assert svc.get_mark_price("BTC/USDT") is not None

        # TTL 만료 시뮬레이션
        entry = svc._mark_prices["BTC/USDT"]
        # stored_at을 2초 전으로 변경
        svc._mark_prices["BTC/USDT"] = type(entry)(
            value=entry.value, stored_at=time.monotonic() - 2.0
        )
        assert svc.get_mark_price("BTC/USDT") is None

    def test_overwrite_updates_value(self):
        svc = DerivativesDataService()
        mp1 = _make_mark_price(mark=65100.0)
        mp2 = _make_mark_price(mark=66000.0)
        svc.update_mark_price("BTC/USDT", mp1)
        svc.update_mark_price("BTC/USDT", mp2)
        result = svc.get_mark_price("BTC/USDT")
        assert result.mark_price == 66000.0


class TestOpenInterestCache:
    def test_update_and_get(self):
        svc = DerivativesDataService()
        oi = _make_open_interest()
        svc.update_open_interest("BTC/USDT", oi)
        result = svc.get_open_interest("BTC/USDT")
        assert result is not None
        assert result.open_interest_value == 100_000_000.0

    def test_missing_returns_none(self):
        svc = DerivativesDataService()
        assert svc.get_open_interest("ETH/USDT") is None

    def test_ttl_expiry(self):
        svc = DerivativesDataService(oi_ttl=1.0)
        oi = _make_open_interest()
        svc.update_open_interest("BTC/USDT", oi)
        assert svc.get_open_interest("BTC/USDT") is not None

        entry = svc._open_interests["BTC/USDT"]
        svc._open_interests["BTC/USDT"] = type(entry)(
            value=entry.value, stored_at=time.monotonic() - 2.0
        )
        assert svc.get_open_interest("BTC/USDT") is None


class TestLongShortRatioCache:
    def test_update_and_get(self):
        svc = DerivativesDataService()
        ls = _make_long_short_ratio()
        svc.update_long_short_ratio("BTC/USDT", ls)
        result = svc.get_long_short_ratio("BTC/USDT")
        assert result is not None
        assert result.long_account_ratio == 0.65

    def test_missing_returns_none(self):
        svc = DerivativesDataService()
        assert svc.get_long_short_ratio("ETH/USDT") is None


class TestSnapshot:
    def test_full_snapshot(self):
        svc = DerivativesDataService()
        svc.update_mark_price("BTC/USDT", _make_mark_price())
        svc.update_open_interest("BTC/USDT", _make_open_interest())
        svc.update_long_short_ratio("BTC/USDT", _make_long_short_ratio())

        snap = svc.get_snapshot("BTC/USDT")
        assert snap is not None
        assert "mark_price" in snap
        assert "premium_pct" in snap
        assert "funding_rate" in snap
        assert "open_interest_value" in snap
        assert "long_account_ratio" in snap
        assert "short_account_ratio" in snap

    def test_partial_snapshot_mark_price_only(self):
        svc = DerivativesDataService()
        svc.update_mark_price("BTC/USDT", _make_mark_price())
        snap = svc.get_snapshot("BTC/USDT")
        assert snap is not None
        assert "mark_price" in snap
        assert "open_interest_value" not in snap

    def test_missing_symbol_returns_none(self):
        svc = DerivativesDataService()
        assert svc.get_snapshot("ETH/USDT") is None

    def test_all_expired_returns_none(self):
        svc = DerivativesDataService(mark_price_ttl=0.0, oi_ttl=0.0, ls_ratio_ttl=0.0)
        svc.update_mark_price("BTC/USDT", _make_mark_price())
        # TTL=0 이므로 즉시 만료
        entry = svc._mark_prices["BTC/USDT"]
        svc._mark_prices["BTC/USDT"] = type(entry)(
            value=entry.value, stored_at=time.monotonic() - 1.0
        )
        assert svc.get_snapshot("BTC/USDT") is None

    def test_get_all_snapshots(self):
        svc = DerivativesDataService()
        svc.update_mark_price("BTC/USDT", _make_mark_price("BTC/USDT"))
        svc.update_mark_price("ETH/USDT", _make_mark_price("ETH/USDT"))
        all_snaps = svc.get_all_snapshots()
        assert len(all_snaps) == 2
        assert "BTC/USDT" in all_snaps
        assert "ETH/USDT" in all_snaps


class TestLRUEviction:
    def test_max_symbols_eviction(self):
        svc = DerivativesDataService(max_symbols=3)
        for i in range(5):
            svc.update_mark_price(f"COIN{i}/USDT", _make_mark_price(f"COIN{i}/USDT"))

        # 최신 3개만 남아야 함
        assert len(svc._mark_prices) == 3
        assert svc.get_mark_price("COIN0/USDT") is None
        assert svc.get_mark_price("COIN1/USDT") is None
        assert svc.get_mark_price("COIN4/USDT") is not None
