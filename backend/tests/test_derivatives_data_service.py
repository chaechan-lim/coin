"""Tests for DerivativesDataService in-memory TTL cache (COIN-97).

Covers:
  - update/get for mark price, open interest, long/short ratio
  - TTL expiry (entries return None after TTL)
  - TTL within window (entries still valid before TTL)
  - get_snapshot: combined dict, partial data, missing symbol
  - get_all_snapshots: multi-symbol, empty cache
  - clear()
"""

from datetime import datetime, timezone
from unittest.mock import patch

from exchange.data_models import LongShortRatio, MarkPriceInfo, OpenInterest
from services.derivatives_data import DerivativesDataService


# ── Helpers ──────────────────────────────────────────────────────────


def _make_mark_price(
    symbol: str = "BTC/USDT",
    mark: float = 65000.0,
) -> MarkPriceInfo:
    ts = datetime.now(timezone.utc)
    return MarkPriceInfo(
        symbol=symbol,
        mark_price=mark,
        index_price=mark * 0.999,
        last_funding_rate=0.0001,
        next_funding_time=ts,
        timestamp=ts,
    )


def _make_open_interest(
    symbol: str = "BTC/USDT",
    oi_value: float = 1_000_000_000.0,
) -> OpenInterest:
    return OpenInterest(
        symbol=symbol,
        open_interest_value=oi_value,
        timestamp=datetime.now(timezone.utc),
    )


def _make_long_short_ratio(symbol: str = "BTC/USDT") -> LongShortRatio:
    return LongShortRatio(
        symbol=symbol,
        long_account_ratio=0.55,
        short_account_ratio=0.45,
        long_position_ratio=0.60,
        short_position_ratio=0.40,
        timestamp=datetime.now(timezone.utc),
    )


# ── Mark Price Cache ──────────────────────────────────────────────────


class TestMarkPriceCache:
    def test_update_and_get_mark_price(self):
        """Basic update → get cycle returns the stored value."""
        svc = DerivativesDataService()
        mp = _make_mark_price("BTC/USDT", 65000.0)
        svc.update_mark_price("BTC/USDT", mp)
        result = svc.get_mark_price("BTC/USDT")
        assert result is not None
        assert result.mark_price == 65000.0
        assert result.symbol == "BTC/USDT"

    def test_get_mark_price_missing_symbol(self):
        """Returns None for a symbol that was never updated."""
        svc = DerivativesDataService()
        assert svc.get_mark_price("ETH/USDT") is None

    def test_mark_price_ttl_expiry(self):
        """Entry returns None after TTL has elapsed."""
        svc = DerivativesDataService(mark_price_ttl=60.0)
        mp = _make_mark_price()
        with patch("services.derivatives_data.time") as mock_time:
            mock_time.time.return_value = 1000.0
            svc.update_mark_price("BTC/USDT", mp)
        # 61 seconds later — expired
        with patch("services.derivatives_data.time") as mock_time:
            mock_time.time.return_value = 1061.0
            result = svc.get_mark_price("BTC/USDT")
        assert result is None

    def test_mark_price_within_ttl(self):
        """Entry is still valid before TTL elapses."""
        svc = DerivativesDataService(mark_price_ttl=120.0)
        mp = _make_mark_price()
        with patch("services.derivatives_data.time") as mock_time:
            mock_time.time.return_value = 1000.0
            svc.update_mark_price("BTC/USDT", mp)
        # 100 seconds later — still valid
        with patch("services.derivatives_data.time") as mock_time:
            mock_time.time.return_value = 1100.0
            result = svc.get_mark_price("BTC/USDT")
        assert result is not None
        assert result.mark_price == mp.mark_price

    def test_update_overrides_previous(self):
        """A second update replaces the first cached value."""
        svc = DerivativesDataService()
        svc.update_mark_price("BTC/USDT", _make_mark_price("BTC/USDT", 65000.0))
        svc.update_mark_price("BTC/USDT", _make_mark_price("BTC/USDT", 67000.0))
        result = svc.get_mark_price("BTC/USDT")
        assert result is not None
        assert result.mark_price == 67000.0


# ── Open Interest Cache ───────────────────────────────────────────────


class TestOpenInterestCache:
    def test_update_and_get_open_interest(self):
        """Basic update → get cycle for open interest."""
        svc = DerivativesDataService()
        oi = _make_open_interest("ETH/USDT", 5e8)
        svc.update_open_interest("ETH/USDT", oi)
        result = svc.get_open_interest("ETH/USDT")
        assert result is not None
        assert result.open_interest_value == 5e8

    def test_get_open_interest_missing_symbol(self):
        svc = DerivativesDataService()
        assert svc.get_open_interest("SOL/USDT") is None

    def test_oi_ttl_expiry(self):
        """OI entry expires after its TTL."""
        svc = DerivativesDataService(oi_ttl=300.0)
        oi = _make_open_interest()
        with patch("services.derivatives_data.time") as mock_time:
            mock_time.time.return_value = 2000.0
            svc.update_open_interest("BTC/USDT", oi)
        with patch("services.derivatives_data.time") as mock_time:
            mock_time.time.return_value = 2301.0  # 301s later — expired
            result = svc.get_open_interest("BTC/USDT")
        assert result is None


# ── Long/Short Ratio Cache ────────────────────────────────────────────


class TestLongShortRatioCache:
    def test_update_and_get_long_short_ratio(self):
        """Basic update → get cycle for LS ratio."""
        svc = DerivativesDataService()
        ls = _make_long_short_ratio("BTC/USDT")
        svc.update_long_short_ratio("BTC/USDT", ls)
        result = svc.get_long_short_ratio("BTC/USDT")
        assert result is not None
        assert result.long_account_ratio == 0.55
        assert result.short_account_ratio == 0.45

    def test_get_long_short_ratio_missing_symbol(self):
        svc = DerivativesDataService()
        assert svc.get_long_short_ratio("BTC/USDT") is None

    def test_ls_ratio_ttl_expiry(self):
        """LS ratio entry expires after its TTL."""
        svc = DerivativesDataService(ls_ratio_ttl=300.0)
        ls = _make_long_short_ratio()
        with patch("services.derivatives_data.time") as mock_time:
            mock_time.time.return_value = 3000.0
            svc.update_long_short_ratio("BTC/USDT", ls)
        with patch("services.derivatives_data.time") as mock_time:
            mock_time.time.return_value = 3301.0  # expired
            result = svc.get_long_short_ratio("BTC/USDT")
        assert result is None


# ── Snapshot Methods ──────────────────────────────────────────────────


class TestGetSnapshot:
    def test_get_snapshot_combined(self):
        """Snapshot contains all three data types when all are cached."""
        svc = DerivativesDataService()
        svc.update_mark_price("BTC/USDT", _make_mark_price("BTC/USDT", 65000.0))
        svc.update_open_interest("BTC/USDT", _make_open_interest("BTC/USDT", 1e9))
        svc.update_long_short_ratio("BTC/USDT", _make_long_short_ratio("BTC/USDT"))

        snap = svc.get_snapshot("BTC/USDT")

        assert snap is not None
        assert snap["symbol"] == "BTC/USDT"
        assert snap["mark_price"] == 65000.0
        assert snap["open_interest_value"] == 1e9
        assert snap["long_account_ratio"] == 0.55
        assert snap["short_account_ratio"] == 0.45
        assert snap["long_position_ratio"] == 0.60
        assert snap["short_position_ratio"] == 0.40

    def test_get_snapshot_partial_mark_price_only(self):
        """Snapshot with only mark price has no OI/LS fields."""
        svc = DerivativesDataService()
        svc.update_mark_price("BTC/USDT", _make_mark_price())

        snap = svc.get_snapshot("BTC/USDT")

        assert snap is not None
        assert "mark_price" in snap
        assert "open_interest_value" not in snap
        assert "long_account_ratio" not in snap

    def test_get_snapshot_missing_symbol_returns_none(self):
        """Returns None for a symbol with no cached data at all."""
        svc = DerivativesDataService()
        assert svc.get_snapshot("UNKNOWN/USDT") is None

    def test_get_snapshot_all_expired_returns_none(self):
        """Returns None when all cached data for a symbol has expired."""
        svc = DerivativesDataService(mark_price_ttl=60.0)
        with patch("services.derivatives_data.time") as mock_time:
            mock_time.time.return_value = 5000.0
            svc.update_mark_price("BTC/USDT", _make_mark_price())
        # All data expired
        with patch("services.derivatives_data.time") as mock_time:
            mock_time.time.return_value = 5061.0
            snap = svc.get_snapshot("BTC/USDT")
        assert snap is None


class TestGetAllSnapshots:
    def test_get_all_snapshots_multi_symbol(self):
        """Returns snapshots for all symbols with any live data."""
        svc = DerivativesDataService()
        svc.update_mark_price("BTC/USDT", _make_mark_price("BTC/USDT", 65000.0))
        svc.update_mark_price("ETH/USDT", _make_mark_price("ETH/USDT", 3500.0))
        svc.update_open_interest("BTC/USDT", _make_open_interest("BTC/USDT"))

        all_snaps = svc.get_all_snapshots()

        assert "BTC/USDT" in all_snaps
        assert "ETH/USDT" in all_snaps
        assert all_snaps["BTC/USDT"]["mark_price"] == 65000.0
        assert all_snaps["ETH/USDT"]["mark_price"] == 3500.0
        assert "open_interest_value" in all_snaps["BTC/USDT"]
        assert "open_interest_value" not in all_snaps["ETH/USDT"]

    def test_get_all_snapshots_empty(self):
        """Returns empty dict when no data is cached."""
        svc = DerivativesDataService()
        assert svc.get_all_snapshots() == {}

    def test_get_all_snapshots_excludes_expired(self):
        """Expired symbols do not appear in the result."""
        svc = DerivativesDataService(mark_price_ttl=60.0, oi_ttl=60.0)
        with patch("services.derivatives_data.time") as mock_time:
            mock_time.time.return_value = 1000.0
            svc.update_mark_price("BTC/USDT", _make_mark_price("BTC/USDT"))
            svc.update_mark_price("ETH/USDT", _make_mark_price("ETH/USDT"))
        with patch("services.derivatives_data.time") as mock_time:
            mock_time.time.return_value = 1061.0  # both expired
            all_snaps = svc.get_all_snapshots()
        assert all_snaps == {}


# ── Clear ─────────────────────────────────────────────────────────────


class TestClear:
    def test_clear_removes_all_data(self):
        """clear() evicts all cached data across all three caches."""
        svc = DerivativesDataService()
        svc.update_mark_price("BTC/USDT", _make_mark_price())
        svc.update_open_interest("BTC/USDT", _make_open_interest())
        svc.update_long_short_ratio("BTC/USDT", _make_long_short_ratio())

        svc.clear()

        assert svc.get_mark_price("BTC/USDT") is None
        assert svc.get_open_interest("BTC/USDT") is None
        assert svc.get_long_short_ratio("BTC/USDT") is None
        assert svc.get_all_snapshots() == {}
