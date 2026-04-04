"""선물 파생 데이터 파이프라인 테스트 (COIN-79).

테스트 범위:
1. 데이터 모델 (OpenInterest, MarkPriceInfo, LongShortRatio)
2. BinanceUSDMAdapter 신규 메서드 4개
3. DerivativesDataService 캐시 + 수집 루프
4. RegimeDetector 파생 데이터 통합
5. FuturesEngineV2 파생 서비스 연동
"""

import asyncio
import time
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from exchange.data_models import OpenInterest, MarkPriceInfo, LongShortRatio
from exchange.binance_usdm_adapter import BinanceUSDMAdapter
from services.derivatives_data import DerivativesDataService, DerivativesSnapshot
from engine.regime_detector import RegimeDetector
from core.enums import Regime
import pandas as pd


# ═══════════════════════════════════════════════════════════
# 1. 데이터 모델 테스트
# ═══════════════════════════════════════════════════════════


class TestOpenInterest:
    def test_creation(self):
        oi = OpenInterest(
            symbol="BTC/USDT",
            open_interest=12345.0,
            open_interest_value=1_000_000_000.0,
            timestamp=datetime.now(timezone.utc),
        )
        assert oi.symbol == "BTC/USDT"
        assert oi.open_interest == 12345.0
        assert oi.open_interest_value == 1_000_000_000.0


class TestMarkPriceInfo:
    def test_creation(self):
        mp = MarkPriceInfo(
            symbol="BTC/USDT",
            mark_price=80100.0,
            index_price=80000.0,
            last_funding_rate=0.0001,
            next_funding_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            premium_pct=0.125,
            timestamp=datetime.now(timezone.utc),
        )
        assert mp.mark_price == 80100.0
        assert mp.premium_pct == 0.125

    def test_premium_pct_calculation(self):
        """premium_pct = (mark - index) / index * 100."""
        mark = 80200.0
        index = 80000.0
        expected = (mark - index) / index * 100
        mp = MarkPriceInfo(
            symbol="BTC/USDT",
            mark_price=mark,
            index_price=index,
            last_funding_rate=0.0,
            next_funding_time=None,
            premium_pct=expected,
            timestamp=datetime.now(timezone.utc),
        )
        assert abs(mp.premium_pct - 0.25) < 0.001


class TestLongShortRatio:
    def test_creation(self):
        ls = LongShortRatio(
            symbol="BTC/USDT",
            long_account=0.55,
            short_account=0.45,
            long_short_ratio=1.22,
            timestamp=datetime.now(timezone.utc),
        )
        assert ls.long_account == 0.55
        assert ls.short_account == 0.45
        assert ls.long_short_ratio == 1.22


# ═══════════════════════════════════════════════════════════
# 2. BinanceUSDMAdapter 신규 메서드 테스트
# ═══════════════════════════════════════════════════════════


class TestAdapterFetchOpenInterest:
    @pytest.mark.asyncio
    async def test_fetch_open_interest(self):
        adapter = BinanceUSDMAdapter()
        adapter._exchange = MagicMock()
        adapter._exchange.fetch_open_interest = AsyncMock(
            return_value={
                "openInterestAmount": 12345.67,
                "openInterestValue": 987654321.0,
            }
        )
        adapter._semaphore = asyncio.Semaphore(10)

        result = await adapter.fetch_open_interest("BTC/USDT")
        assert isinstance(result, OpenInterest)
        assert result.symbol == "BTC/USDT"
        assert result.open_interest == 12345.67
        assert result.open_interest_value == 987654321.0

    @pytest.mark.asyncio
    async def test_fetch_open_interest_missing_fields(self):
        adapter = BinanceUSDMAdapter()
        adapter._exchange = MagicMock()
        adapter._exchange.fetch_open_interest = AsyncMock(return_value={})
        adapter._semaphore = asyncio.Semaphore(10)

        result = await adapter.fetch_open_interest("BTC/USDT")
        assert result.open_interest == 0.0
        assert result.open_interest_value == 0.0


class TestAdapterFetchOpenInterestHistory:
    @pytest.mark.asyncio
    async def test_fetch_oi_history(self):
        adapter = BinanceUSDMAdapter()
        adapter._exchange = MagicMock()
        adapter._exchange.fapiPublicGetOpenInterestHist = AsyncMock(
            return_value=[
                {
                    "sumOpenInterest": "100",
                    "sumOpenInterestValue": "8000000",
                    "timestamp": "1700000000000",
                },
                {
                    "sumOpenInterest": "110",
                    "sumOpenInterestValue": "8800000",
                    "timestamp": "1700003600000",
                },
            ]
        )
        adapter._semaphore = asyncio.Semaphore(10)

        result = await adapter.fetch_open_interest_history(
            "BTC/USDT", period="1h", limit=2
        )
        assert len(result) == 2
        assert result[0].open_interest == 100.0
        assert result[1].open_interest == 110.0

    @pytest.mark.asyncio
    async def test_fetch_oi_history_empty(self):
        adapter = BinanceUSDMAdapter()
        adapter._exchange = MagicMock()
        adapter._exchange.fapiPublicGetOpenInterestHist = AsyncMock(return_value=[])
        adapter._semaphore = asyncio.Semaphore(10)

        result = await adapter.fetch_open_interest_history("ETH/USDT")
        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_oi_history_non_list(self):
        adapter = BinanceUSDMAdapter()
        adapter._exchange = MagicMock()
        adapter._exchange.fapiPublicGetOpenInterestHist = AsyncMock(return_value=None)
        adapter._semaphore = asyncio.Semaphore(10)

        result = await adapter.fetch_open_interest_history("ETH/USDT")
        assert result == []

    @pytest.mark.asyncio
    async def test_symbol_normalization(self):
        adapter = BinanceUSDMAdapter()
        adapter._exchange = MagicMock()
        adapter._exchange.fapiPublicGetOpenInterestHist = AsyncMock(return_value=[])
        adapter._semaphore = asyncio.Semaphore(10)

        await adapter.fetch_open_interest_history("BTC/USDT")
        call_args = adapter._exchange.fapiPublicGetOpenInterestHist.call_args
        assert call_args[0][0]["symbol"] == "BTCUSDT"


class TestAdapterFetchMarkPrice:
    @pytest.mark.asyncio
    async def test_fetch_mark_price(self):
        adapter = BinanceUSDMAdapter()
        adapter._exchange = MagicMock()
        adapter._exchange.fapiPublicGetPremiumIndex = AsyncMock(
            return_value={
                "markPrice": "80100.50",
                "indexPrice": "80000.00",
                "lastFundingRate": "0.00010000",
                "nextFundingTime": "1700000000000",
            }
        )
        adapter._semaphore = asyncio.Semaphore(10)

        result = await adapter.fetch_mark_price("BTC/USDT")
        assert isinstance(result, MarkPriceInfo)
        assert result.mark_price == 80100.50
        assert result.index_price == 80000.0
        assert result.last_funding_rate == 0.0001
        assert result.next_funding_time is not None
        # Premium check: (80100.50 - 80000) / 80000 * 100
        expected_premium = (80100.50 - 80000.0) / 80000.0 * 100
        assert abs(result.premium_pct - expected_premium) < 0.0001

    @pytest.mark.asyncio
    async def test_fetch_mark_price_list_response(self):
        """Binance may return list for single symbol."""
        adapter = BinanceUSDMAdapter()
        adapter._exchange = MagicMock()
        adapter._exchange.fapiPublicGetPremiumIndex = AsyncMock(
            return_value=[
                {
                    "markPrice": "3500.00",
                    "indexPrice": "3490.00",
                    "lastFundingRate": "0.00005",
                    "nextFundingTime": "0",
                }
            ]
        )
        adapter._semaphore = asyncio.Semaphore(10)

        result = await adapter.fetch_mark_price("ETH/USDT")
        assert result.mark_price == 3500.0
        assert result.index_price == 3490.0

    @pytest.mark.asyncio
    async def test_fetch_mark_price_zero_index(self):
        """index_price=0 should not cause division by zero."""
        adapter = BinanceUSDMAdapter()
        adapter._exchange = MagicMock()
        adapter._exchange.fapiPublicGetPremiumIndex = AsyncMock(
            return_value={
                "markPrice": "80000",
                "indexPrice": "0",
                "lastFundingRate": "0",
                "nextFundingTime": "0",
            }
        )
        adapter._semaphore = asyncio.Semaphore(10)

        result = await adapter.fetch_mark_price("BTC/USDT")
        assert result.premium_pct == 0.0

    @pytest.mark.asyncio
    async def test_fetch_mark_price_no_next_funding(self):
        adapter = BinanceUSDMAdapter()
        adapter._exchange = MagicMock()
        adapter._exchange.fapiPublicGetPremiumIndex = AsyncMock(
            return_value={
                "markPrice": "80000",
                "indexPrice": "80000",
                "lastFundingRate": "0",
                "nextFundingTime": "0",
            }
        )
        adapter._semaphore = asyncio.Semaphore(10)

        result = await adapter.fetch_mark_price("BTC/USDT")
        assert result.next_funding_time is None


class TestAdapterFetchLongShortRatio:
    @pytest.mark.asyncio
    async def test_fetch_long_short_ratio(self):
        adapter = BinanceUSDMAdapter()
        adapter._exchange = MagicMock()
        adapter._exchange.fapiPublicGetTopLongShortAccountRatio = AsyncMock(
            return_value=[
                {
                    "longAccount": "0.5500",
                    "shortAccount": "0.4500",
                    "longShortRatio": "1.2222",
                    "timestamp": "1700000000000",
                }
            ]
        )
        adapter._semaphore = asyncio.Semaphore(10)

        result = await adapter.fetch_long_short_ratio("BTC/USDT")
        assert isinstance(result, LongShortRatio)
        assert result.long_account == 0.55
        assert result.short_account == 0.45
        assert abs(result.long_short_ratio - 1.2222) < 0.001

    @pytest.mark.asyncio
    async def test_fetch_long_short_ratio_empty(self):
        """Empty response returns defaults."""
        adapter = BinanceUSDMAdapter()
        adapter._exchange = MagicMock()
        adapter._exchange.fapiPublicGetTopLongShortAccountRatio = AsyncMock(
            return_value=[]
        )
        adapter._semaphore = asyncio.Semaphore(10)

        result = await adapter.fetch_long_short_ratio("BTC/USDT")
        assert result.long_account == 0.5
        assert result.short_account == 0.5
        assert result.long_short_ratio == 1.0

    @pytest.mark.asyncio
    async def test_fetch_long_short_ratio_non_list(self):
        adapter = BinanceUSDMAdapter()
        adapter._exchange = MagicMock()
        adapter._exchange.fapiPublicGetTopLongShortAccountRatio = AsyncMock(
            return_value=None
        )
        adapter._semaphore = asyncio.Semaphore(10)

        result = await adapter.fetch_long_short_ratio("BTC/USDT")
        assert result.long_short_ratio == 1.0

    @pytest.mark.asyncio
    async def test_symbol_normalization(self):
        adapter = BinanceUSDMAdapter()
        adapter._exchange = MagicMock()
        adapter._exchange.fapiPublicGetTopLongShortAccountRatio = AsyncMock(
            return_value=[]
        )
        adapter._semaphore = asyncio.Semaphore(10)

        await adapter.fetch_long_short_ratio("ETH/USDT", period="5m")
        call_args = adapter._exchange.fapiPublicGetTopLongShortAccountRatio.call_args
        assert call_args[0][0]["symbol"] == "ETHUSDT"
        assert call_args[0][0]["period"] == "5m"


# ═══════════════════════════════════════════════════════════
# 3. DerivativesDataService 테스트
# ═══════════════════════════════════════════════════════════


def _make_mock_exchange():
    """Mock exchange with all 4 derivative fetch methods."""
    exchange = AsyncMock()
    exchange.fetch_open_interest = AsyncMock(
        return_value=OpenInterest(
            symbol="BTC/USDT",
            open_interest=10000.0,
            open_interest_value=800_000_000.0,
            timestamp=datetime.now(timezone.utc),
        )
    )
    exchange.fetch_mark_price = AsyncMock(
        return_value=MarkPriceInfo(
            symbol="BTC/USDT",
            mark_price=80100.0,
            index_price=80000.0,
            last_funding_rate=0.0001,
            next_funding_time=None,
            premium_pct=0.125,
            timestamp=datetime.now(timezone.utc),
        )
    )
    exchange.fetch_long_short_ratio = AsyncMock(
        return_value=LongShortRatio(
            symbol="BTC/USDT",
            long_account=0.55,
            short_account=0.45,
            long_short_ratio=1.22,
            timestamp=datetime.now(timezone.utc),
        )
    )
    return exchange


class TestDerivativesDataServiceCache:
    def test_empty_cache_returns_none(self):
        svc = DerivativesDataService(exchange=MagicMock())
        assert svc.get_open_interest("BTC/USDT") is None
        assert svc.get_mark_price("BTC/USDT") is None
        assert svc.get_long_short_ratio("BTC/USDT") is None
        assert svc.get_snapshot("BTC/USDT") is None

    def test_is_stale_empty(self):
        svc = DerivativesDataService(exchange=MagicMock())
        assert svc.is_stale("BTC/USDT") is True

    def test_is_stale_fresh(self):
        svc = DerivativesDataService(exchange=MagicMock(), ttl_sec=300)
        svc._snapshots["BTC/USDT"] = DerivativesSnapshot(
            updated_at=time.monotonic(),
        )
        assert svc.is_stale("BTC/USDT") is False

    def test_is_stale_expired(self):
        svc = DerivativesDataService(exchange=MagicMock(), ttl_sec=300)
        svc._snapshots["BTC/USDT"] = DerivativesSnapshot(
            updated_at=time.monotonic() - 400,
        )
        assert svc.is_stale("BTC/USDT") is True

    def test_oi_history_empty(self):
        svc = DerivativesDataService(exchange=MagicMock())
        assert svc.get_open_interest_history("BTC/USDT") == []


class TestDerivativesDataServiceCollection:
    @pytest.mark.asyncio
    async def test_collect_all(self):
        exchange = _make_mock_exchange()
        svc = DerivativesDataService(exchange=exchange, collect_interval=999)
        svc._symbols = ["BTC/USDT"]
        svc._oi_history["BTC/USDT"] = svc._oi_history.get(
            "BTC/USDT", __import__("collections").deque(maxlen=100)
        )
        svc._mark_history["BTC/USDT"] = svc._mark_history.get(
            "BTC/USDT", __import__("collections").deque(maxlen=100)
        )

        await svc._collect_all()

        snap = svc.get_snapshot("BTC/USDT")
        assert snap is not None
        assert snap.open_interest is not None
        assert snap.open_interest.open_interest == 10000.0
        assert snap.mark_price is not None
        assert snap.mark_price.premium_pct == 0.125
        assert snap.long_short_ratio is not None
        assert snap.long_short_ratio.long_short_ratio == 1.22

    @pytest.mark.asyncio
    async def test_collect_graceful_degradation(self):
        """Individual metric failures don't block others."""
        exchange = _make_mock_exchange()
        exchange.fetch_open_interest = AsyncMock(side_effect=Exception("API error"))
        svc = DerivativesDataService(exchange=exchange, collect_interval=999)
        svc._symbols = ["BTC/USDT"]
        svc._oi_history["BTC/USDT"] = __import__("collections").deque(maxlen=100)
        svc._mark_history["BTC/USDT"] = __import__("collections").deque(maxlen=100)

        await svc._collect_all()

        snap = svc.get_snapshot("BTC/USDT")
        assert snap is not None
        assert snap.open_interest is None  # Failed
        assert snap.mark_price is not None  # Succeeded
        assert snap.long_short_ratio is not None  # Succeeded

    @pytest.mark.asyncio
    async def test_collect_all_failures(self):
        """All metrics fail gracefully."""
        exchange = AsyncMock()
        exchange.fetch_open_interest = AsyncMock(side_effect=Exception("fail"))
        exchange.fetch_mark_price = AsyncMock(side_effect=Exception("fail"))
        exchange.fetch_long_short_ratio = AsyncMock(side_effect=Exception("fail"))
        svc = DerivativesDataService(exchange=exchange, collect_interval=999)
        svc._symbols = ["BTC/USDT"]
        svc._oi_history["BTC/USDT"] = __import__("collections").deque(maxlen=100)
        svc._mark_history["BTC/USDT"] = __import__("collections").deque(maxlen=100)

        await svc._collect_all()

        snap = svc.get_snapshot("BTC/USDT")
        assert snap is not None
        assert snap.open_interest is None
        assert snap.mark_price is None
        assert snap.long_short_ratio is None
        assert snap.updated_at > 0  # Still updated

    @pytest.mark.asyncio
    async def test_oi_history_accumulates(self):
        exchange = _make_mock_exchange()
        svc = DerivativesDataService(exchange=exchange, collect_interval=999)
        svc._symbols = ["BTC/USDT"]
        svc._oi_history["BTC/USDT"] = __import__("collections").deque(maxlen=100)
        svc._mark_history["BTC/USDT"] = __import__("collections").deque(maxlen=100)

        await svc._collect_all()
        await svc._collect_all()
        await svc._collect_all()

        history = svc.get_open_interest_history("BTC/USDT")
        assert len(history) == 3

    @pytest.mark.asyncio
    async def test_history_maxlen_enforced(self):
        exchange = _make_mock_exchange()
        svc = DerivativesDataService(
            exchange=exchange,
            collect_interval=999,
            history_hours=1,  # 1 hour
        )
        # max_history = 3600 / 999 ≈ 3
        svc._symbols = ["BTC/USDT"]
        svc._oi_history["BTC/USDT"] = __import__("collections").deque(
            maxlen=svc._max_history
        )
        svc._mark_history["BTC/USDT"] = __import__("collections").deque(
            maxlen=svc._max_history
        )

        for _ in range(20):
            await svc._collect_all()

        history = svc.get_open_interest_history("BTC/USDT")
        assert len(history) <= svc._max_history

    @pytest.mark.asyncio
    async def test_multiple_symbols(self):
        exchange = AsyncMock()
        exchange.fetch_open_interest = AsyncMock(
            side_effect=lambda s: OpenInterest(
                symbol=s,
                open_interest=100.0,
                open_interest_value=1000.0,
                timestamp=datetime.now(timezone.utc),
            )
        )
        exchange.fetch_mark_price = AsyncMock(
            side_effect=lambda s: MarkPriceInfo(
                symbol=s,
                mark_price=100.0,
                index_price=100.0,
                last_funding_rate=0,
                next_funding_time=None,
                premium_pct=0,
                timestamp=datetime.now(timezone.utc),
            )
        )
        exchange.fetch_long_short_ratio = AsyncMock(
            side_effect=lambda s, **kw: LongShortRatio(
                symbol=s,
                long_account=0.5,
                short_account=0.5,
                long_short_ratio=1.0,
                timestamp=datetime.now(timezone.utc),
            )
        )

        svc = DerivativesDataService(exchange=exchange, collect_interval=999)
        symbols = ["BTC/USDT", "ETH/USDT"]
        svc._symbols = symbols
        for s in symbols:
            svc._oi_history[s] = __import__("collections").deque(maxlen=100)
            svc._mark_history[s] = __import__("collections").deque(maxlen=100)

        await svc._collect_all()

        assert svc.get_open_interest("BTC/USDT") is not None
        assert svc.get_open_interest("ETH/USDT") is not None


class TestDerivativesDataServiceStartStop:
    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        exchange = _make_mock_exchange()
        svc = DerivativesDataService(exchange=exchange, collect_interval=999)
        await svc.start(["BTC/USDT"])
        assert svc._is_running is True
        assert svc._task is not None
        assert "BTC/USDT" in svc._oi_history
        await svc.stop()
        assert svc._is_running is False
        assert svc._task is None

    @pytest.mark.asyncio
    async def test_stop_without_start(self):
        svc = DerivativesDataService(exchange=MagicMock())
        await svc.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_double_start_idempotent(self):
        exchange = _make_mock_exchange()
        svc = DerivativesDataService(exchange=exchange, collect_interval=999)
        await svc.start(["BTC/USDT"])
        task1 = svc._task
        await svc.start(["BTC/USDT", "ETH/USDT"])  # Should not create second task
        assert svc._task is task1
        await svc.stop()

    @pytest.mark.asyncio
    async def test_get_methods_after_collection(self):
        exchange = _make_mock_exchange()
        svc = DerivativesDataService(exchange=exchange, collect_interval=999)
        await svc.start(["BTC/USDT"])
        # Wait for initial collection
        await asyncio.sleep(0.1)

        oi = svc.get_open_interest("BTC/USDT")
        assert oi is not None
        assert oi.open_interest == 10000.0

        mp = svc.get_mark_price("BTC/USDT")
        assert mp is not None
        assert mp.premium_pct == 0.125

        ls = svc.get_long_short_ratio("BTC/USDT")
        assert ls is not None
        assert ls.long_short_ratio == 1.22

        await svc.stop()


# ═══════════════════════════════════════════════════════════
# 4. RegimeDetector 파생 데이터 통합 테스트
# ═══════════════════════════════════════════════════════════


def _make_df(
    n=100,
    close=80000.0,
    adx=30.0,
    atr=1000.0,
    ema_20=80000.0,
    ema_50=79000.0,
    bb_upper=82000.0,
    bb_lower=78000.0,
    bb_mid=80000.0,
    volume=1000.0,
    ema_slope_dir=1,
) -> pd.DataFrame:
    ema_values = []
    for i in range(n):
        pct = ema_slope_dir * 0.002 * (i - (n - 1))
        ema_values.append(ema_20 * (1 + pct))
    return pd.DataFrame(
        {
            "close": [close] * n,
            "adx_14": [adx] * n,
            "atr_14": [atr] * n,
            "ema_20": ema_values,
            "ema_50": [ema_50] * n,
            "bb_upper_20": [bb_upper] * n,
            "bb_lower_20": [bb_lower] * n,
            "bb_mid_20": [bb_mid] * n,
            "volume": [volume] * n,
        }
    )


class TestRegimeDetectorWithDerivatives:
    def test_detect_without_derivatives(self):
        """derivatives_data=None preserves existing behavior."""
        detector = RegimeDetector()
        df = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state = detector.detect(df)
        assert state.regime == Regime.TRENDING_UP
        assert state.derivatives_snapshot is None

    def test_detect_with_derivatives(self):
        """derivatives_data available adds snapshot to state."""
        deriv_svc = MagicMock(spec=DerivativesDataService)
        snap = DerivativesSnapshot(
            open_interest=OpenInterest(
                symbol="BTC/USDT",
                open_interest=10000.0,
                open_interest_value=800_000_000.0,
                timestamp=datetime.now(timezone.utc),
            ),
            mark_price=MarkPriceInfo(
                symbol="BTC/USDT",
                mark_price=80100.0,
                index_price=80000.0,
                last_funding_rate=0.0001,
                next_funding_time=None,
                premium_pct=0.125,
                timestamp=datetime.now(timezone.utc),
            ),
            long_short_ratio=LongShortRatio(
                symbol="BTC/USDT",
                long_account=0.55,
                short_account=0.45,
                long_short_ratio=1.22,
                timestamp=datetime.now(timezone.utc),
            ),
            updated_at=time.monotonic(),
        )
        deriv_svc.get_snapshot.return_value = snap
        deriv_svc.is_stale.return_value = False

        detector = RegimeDetector(derivatives_data=deriv_svc)
        df = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state = detector.detect(df, symbol="BTC/USDT")
        assert state.regime == Regime.TRENDING_UP
        assert state.derivatives_snapshot is not None
        assert state.derivatives_snapshot["oi_value"] == 800_000_000.0
        assert state.derivatives_snapshot["premium_pct"] == 0.125
        assert state.derivatives_snapshot["long_short_ratio"] == 1.22
        assert state.derivatives_snapshot["is_stale"] is False

    def test_detect_with_derivatives_stale(self):
        deriv_svc = MagicMock(spec=DerivativesDataService)
        snap = DerivativesSnapshot(
            open_interest=OpenInterest(
                symbol="BTC/USDT",
                open_interest=10000.0,
                open_interest_value=800_000_000.0,
                timestamp=datetime.now(timezone.utc),
            ),
            updated_at=time.monotonic() - 600,
        )
        deriv_svc.get_snapshot.return_value = snap
        deriv_svc.is_stale.return_value = True

        detector = RegimeDetector(derivatives_data=deriv_svc)
        df = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state = detector.detect(df, symbol="BTC/USDT")
        assert state.derivatives_snapshot is not None
        assert state.derivatives_snapshot["is_stale"] is True

    def test_detect_with_derivatives_no_snapshot(self):
        """No snapshot available for symbol returns None."""
        deriv_svc = MagicMock(spec=DerivativesDataService)
        deriv_svc.get_snapshot.return_value = None

        detector = RegimeDetector(derivatives_data=deriv_svc)
        df = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state = detector.detect(df, symbol="BTC/USDT")
        assert state.derivatives_snapshot is None

    def test_detect_with_partial_derivatives(self):
        """Only some metrics available."""
        deriv_svc = MagicMock(spec=DerivativesDataService)
        snap = DerivativesSnapshot(
            mark_price=MarkPriceInfo(
                symbol="BTC/USDT",
                mark_price=80100.0,
                index_price=80000.0,
                last_funding_rate=0.0001,
                next_funding_time=None,
                premium_pct=0.125,
                timestamp=datetime.now(timezone.utc),
            ),
            updated_at=time.monotonic(),
        )
        deriv_svc.get_snapshot.return_value = snap
        deriv_svc.is_stale.return_value = False

        detector = RegimeDetector(derivatives_data=deriv_svc)
        df = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        state = detector.detect(df, symbol="BTC/USDT")
        assert state.derivatives_snapshot is not None
        assert "premium_pct" in state.derivatives_snapshot
        assert "oi_value" not in state.derivatives_snapshot  # Not available

    @pytest.mark.asyncio
    async def test_update_passes_symbol_to_detect(self):
        """update() passes symbol to detect() for derivatives lookup."""
        deriv_svc = MagicMock(spec=DerivativesDataService)
        deriv_svc.get_snapshot.return_value = None

        detector = RegimeDetector(derivatives_data=deriv_svc)
        df = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)
        with patch.object(
            detector, "_build_derivatives_snapshot", return_value=None
        ) as mock_build:
            await detector.update(df, symbol="ETH/USDT")
            mock_build.assert_called_with("ETH/USDT")

    def test_hysteresis_preserves_derivatives(self):
        """When regime stays same, derivatives_snapshot is updated."""
        deriv_svc = MagicMock(spec=DerivativesDataService)
        snap = DerivativesSnapshot(
            mark_price=MarkPriceInfo(
                symbol="BTC/USDT",
                mark_price=80100.0,
                index_price=80000.0,
                last_funding_rate=0.0001,
                next_funding_time=None,
                premium_pct=0.125,
                timestamp=datetime.now(timezone.utc),
            ),
            updated_at=time.monotonic(),
        )
        deriv_svc.get_snapshot.return_value = snap
        deriv_svc.is_stale.return_value = False

        detector = RegimeDetector(derivatives_data=deriv_svc)
        df = _make_df(adx=30, ema_20=81000, ema_50=79000, ema_slope_dir=1)

        # First detect: establishes regime
        state1 = detector.detect(df, symbol="BTC/USDT")
        detector._current = state1
        detector._last_transition = state1.timestamp

        # Second detect: same regime, derivatives preserved
        state2 = detector.detect(df, symbol="BTC/USDT")
        hysteresis_result = detector._apply_hysteresis(state2)
        assert hysteresis_result.derivatives_snapshot is not None


# ═══════════════════════════════════════════════════════════
# 5. FuturesEngineV2 파생 서비스 연동 테스트
# ═══════════════════════════════════════════════════════════


class TestFuturesEngineV2Derivatives:
    @pytest.fixture
    def mock_config(self):
        """Minimal AppConfig mock for engine creation."""
        config = MagicMock()
        v2_cfg = MagicMock()
        v2_cfg.regime_adx_enter = 27.0
        v2_cfg.regime_adx_exit = 23.0
        v2_cfg.regime_confirm_count = 2
        v2_cfg.regime_min_duration_h = 3
        v2_cfg.leverage = 3
        v2_cfg.tier1_coins = ["BTC/USDT"]
        v2_cfg.tier1_max_position_pct = 0.4
        v2_cfg.tier1_min_confidence = 0.4
        v2_cfg.tier1_cooldown_seconds = 86400
        v2_cfg.tier1_sl_long_cooldown_hours = 12
        v2_cfg.tier1_sl_short_cooldown_hours = 26
        v2_cfg.tier1_daily_buy_limit = 20
        v2_cfg.tier1_max_daily_coin_buys = 3
        v2_cfg.tier1_max_eval_errors = 3
        v2_cfg.tier1_max_hold_hours = 0
        v2_cfg.tier1_regime_eval_interval_sec = 14400
        v2_cfg.tier1_regime_cooldown_hours = 26
        v2_cfg.balance_divergence_warn_pct = 2.0
        v2_cfg.balance_divergence_pause_pct = 5.0
        v2_cfg.asymmetric_mode = False
        v2_cfg.dynamic_sl = False
        v2_cfg.atr_leverage_scaling = False
        v2_cfg.min_sell_active_weight = 0.0
        v2_cfg.strategy_mode = "regime"
        # Tier2 config
        v2_cfg.tier2_max_concurrent = 3
        v2_cfg.tier2_max_position_pct = 0.1
        v2_cfg.tier2_max_hold_minutes = 120
        v2_cfg.tier2_vol_threshold = 2.0
        v2_cfg.tier2_price_threshold = 1.0
        v2_cfg.tier2_sl_pct = 3.5
        v2_cfg.tier2_tp_pct = 4.5
        v2_cfg.tier2_trail_activation_pct = 1.5
        v2_cfg.tier2_trail_stop_pct = 1.0
        v2_cfg.tier2_daily_trade_limit = 20
        v2_cfg.tier2_cooldown_per_symbol_sec = 3600
        v2_cfg.tier2_rsi_overbought = 75
        v2_cfg.tier2_rsi_oversold = 25
        v2_cfg.tier2_min_atr_pct = 0.5
        v2_cfg.tier2_exhaustion_pct = 8.0
        v2_cfg.tier2_min_score = 0.55
        v2_cfg.tier2_consecutive_sl_cooldown_sec = 10800
        # Spot evaluator fields
        v2_cfg.tier1_long_min_confidence = 0.5
        v2_cfg.tier1_long_eval_interval_sec = 300
        v2_cfg.tier1_long_cooldown_hours = 60
        v2_cfg.tier1_long_sl_atr_mult = 5.0
        v2_cfg.tier1_long_tp_atr_mult = 14.0
        v2_cfg.tier1_long_trail_activation_atr_mult = 3.0
        v2_cfg.tier1_long_trail_stop_atr_mult = 1.5
        config.futures_v2 = v2_cfg
        return config

    def test_derivatives_service_created(self, mock_config):
        exchange = AsyncMock()
        md = AsyncMock()
        om = MagicMock()
        pm = MagicMock()
        pm.cash_balance = 500.0
        pm._is_paper = False
        pm._exchange_name = "binance_futures"

        engine = _create_engine(mock_config, exchange, md, om, pm)
        assert engine._derivatives is not None
        assert isinstance(engine._derivatives, DerivativesDataService)

    def test_regime_detector_has_derivatives(self, mock_config):
        exchange = AsyncMock()
        md = AsyncMock()
        om = MagicMock()
        pm = MagicMock()
        pm.cash_balance = 500.0
        pm._is_paper = False
        pm._exchange_name = "binance_futures"

        engine = _create_engine(mock_config, exchange, md, om, pm)
        assert engine._regime._derivatives_data is engine._derivatives

    def test_get_status_includes_derivatives(self, mock_config):
        exchange = AsyncMock()
        md = AsyncMock()
        om = MagicMock()
        pm = MagicMock()
        pm.cash_balance = 500.0
        pm._is_paper = False
        pm._exchange_name = "binance_futures"

        engine = _create_engine(mock_config, exchange, md, om, pm)
        status = engine.get_status()
        assert "derivatives_collecting" in status
        assert "derivatives_symbols" in status

    def test_derivatives_property(self, mock_config):
        exchange = AsyncMock()
        md = AsyncMock()
        om = MagicMock()
        pm = MagicMock()
        pm.cash_balance = 500.0
        pm._is_paper = False
        pm._exchange_name = "binance_futures"

        engine = _create_engine(mock_config, exchange, md, om, pm)
        assert engine.derivatives_data is engine._derivatives


def _create_engine(config, exchange, md, om, pm):
    """Helper to create engine with mocked strategy dependencies."""
    with (
        patch("engine.futures_engine_v2.StrategySelector"),
        patch("engine.futures_engine_v2.PositionStateTracker"),
        patch("engine.futures_engine_v2.BalanceGuard"),
        patch("engine.futures_engine_v2.SafeOrderPipeline"),
        patch("engine.futures_engine_v2.LiquidationGuard"),
        patch("engine.futures_engine_v2.Tier1Manager"),
        patch("engine.futures_engine_v2.Tier2Scanner"),
        patch("engine.futures_engine_v2.RegimeLongEvaluator"),
        patch("engine.futures_engine_v2.RegimeShortEvaluator"),
    ):
        from engine.futures_engine_v2 import FuturesEngineV2

        return FuturesEngineV2(
            config=config,
            exchange=exchange,
            market_data=md,
            order_manager=om,
            portfolio_manager=pm,
        )


# ═══════════════════════════════════════════════════════════
# 6. Base Adapter 선물 메서드 테스트
# ═══════════════════════════════════════════════════════════


class TestBaseAdapterFuturesMethods:
    @pytest.mark.asyncio
    async def test_fetch_open_interest_raises(self):
        """Base adapter raises NotImplementedError for futures methods."""
        from exchange.binance_spot_adapter import BinanceSpotAdapter

        adapter = BinanceSpotAdapter()
        with pytest.raises(NotImplementedError):
            await adapter.fetch_open_interest("BTC/USDT")

    @pytest.mark.asyncio
    async def test_fetch_mark_price_raises(self):
        from exchange.binance_spot_adapter import BinanceSpotAdapter

        adapter = BinanceSpotAdapter()
        with pytest.raises(NotImplementedError):
            await adapter.fetch_mark_price("BTC/USDT")

    @pytest.mark.asyncio
    async def test_fetch_long_short_ratio_raises(self):
        from exchange.binance_spot_adapter import BinanceSpotAdapter

        adapter = BinanceSpotAdapter()
        with pytest.raises(NotImplementedError):
            await adapter.fetch_long_short_ratio("BTC/USDT")

    @pytest.mark.asyncio
    async def test_fetch_oi_history_raises(self):
        from exchange.binance_spot_adapter import BinanceSpotAdapter

        adapter = BinanceSpotAdapter()
        with pytest.raises(NotImplementedError):
            await adapter.fetch_open_interest_history("BTC/USDT")
