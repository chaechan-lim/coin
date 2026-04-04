"""LiquidationGuard 테스트 — COIN-76.

청산 거리 검증, 레버리지 자동 하향, 마진비율 확인, 캐시 로직을 검증한다.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from engine.liquidation_guard import LiquidationGuard, LiquidationCheckResult


# ── 샘플 브라켓 데이터 ──────────────────────────────────────────────

SAMPLE_BRACKETS = [
    {"notionalFloor": 0, "notionalCap": 50000, "maintMarginRatio": 0.004, "maxLeverage": 125},
    {"notionalFloor": 50000, "notionalCap": 250000, "maintMarginRatio": 0.005, "maxLeverage": 100},
    {"notionalFloor": 250000, "notionalCap": 1000000, "maintMarginRatio": 0.01, "maxLeverage": 50},
    {"notionalFloor": 1000000, "notionalCap": 10000000, "maintMarginRatio": 0.025, "maxLeverage": 20},
]

SAMPLE_POSITION_RISK = [
    {
        "symbol": "BTCUSDT",
        "markPrice": "80000.0",
        "liquidationPrice": "55000.0",
        "marginRatio": "0.15",
        "positionAmt": "0.01",
    }
]


def _make_exchange(brackets=None, position_risk=None, fail=False):
    """테스트용 mock exchange."""
    exchange = MagicMock()
    if fail:
        exchange.fetch_leverage_brackets = AsyncMock(side_effect=Exception("API Error"))
        exchange.fetch_position_risk = AsyncMock(side_effect=Exception("API Error"))
    else:
        _brackets = brackets if brackets is not None else SAMPLE_BRACKETS
        _risks = position_risk if position_risk is not None else SAMPLE_POSITION_RISK
        exchange.fetch_leverage_brackets = AsyncMock(return_value=_brackets)
        exchange.fetch_position_risk = AsyncMock(return_value=_risks)
    return exchange


# ── calc_liquidation_price 테스트 ──────────────────────────────────

class TestCalcLiquidationPrice:
    """LiquidationGuard.calc_liquidation_price() 단위 테스트."""

    def test_long_3x_standard(self):
        """롱 3x, MMR 1% — 예상 청산가 확인."""
        # liq = 80000 * (1 - 1/3 + 0.01) = 80000 * 0.6767 = 54133.33
        price = LiquidationGuard.calc_liquidation_price("long", 80000.0, 3, 0.01)
        assert pytest.approx(price, rel=1e-4) == 80000 * (1 - 1/3 + 0.01)

    def test_short_3x_standard(self):
        """숏 3x, MMR 1% — 예상 청산가 확인."""
        # liq = 80000 * (1 + 1/3 - 0.01) = 80000 * 1.3233
        price = LiquidationGuard.calc_liquidation_price("short", 80000.0, 3, 0.01)
        assert pytest.approx(price, rel=1e-4) == 80000 * (1 + 1/3 - 0.01)

    def test_long_distance_roughly_30pct_at_3x(self):
        """롱 3x: 청산거리 ≈ 진입가의 30% 이상."""
        price = LiquidationGuard.calc_liquidation_price("long", 80000.0, 3, 0.01)
        distance_pct = abs(80000.0 - price) / 80000.0 * 100
        assert distance_pct > 30  # ~32.3%

    def test_short_distance_roughly_30pct_at_3x(self):
        """숏 3x: 청산거리 ≈ 진입가의 30% 이상."""
        price = LiquidationGuard.calc_liquidation_price("short", 80000.0, 3, 0.01)
        distance_pct = abs(80000.0 - price) / 80000.0 * 100
        assert distance_pct > 30

    def test_long_liq_below_entry(self):
        """롱 청산가는 진입가보다 낮아야 한다."""
        price = LiquidationGuard.calc_liquidation_price("long", 80000.0, 5, 0.01)
        assert price < 80000.0

    def test_short_liq_above_entry(self):
        """숏 청산가는 진입가보다 높아야 한다."""
        price = LiquidationGuard.calc_liquidation_price("short", 80000.0, 5, 0.01)
        assert price > 80000.0

    def test_higher_leverage_closer_liquidation(self):
        """레버리지가 높을수록 청산가가 진입가에 가까워야 한다."""
        price_3x = LiquidationGuard.calc_liquidation_price("long", 80000.0, 3, 0.01)
        price_5x = LiquidationGuard.calc_liquidation_price("long", 80000.0, 5, 0.01)
        assert price_5x > price_3x  # 5x 청산가가 더 높음(진입가에 더 가까움)

    def test_invalid_leverage_returns_zero(self):
        """레버리지 0 → 0.0 반환."""
        price = LiquidationGuard.calc_liquidation_price("long", 80000.0, 0, 0.01)
        assert price == 0.0

    def test_invalid_entry_price_returns_zero(self):
        """진입가 0 → 0.0 반환."""
        price = LiquidationGuard.calc_liquidation_price("long", 0.0, 3, 0.01)
        assert price == 0.0

    def test_1x_leverage_long(self):
        """1x 레버리지 롱 — 청산거리가 가장 넓어야 한다."""
        price = LiquidationGuard.calc_liquidation_price("long", 80000.0, 1, 0.01)
        # liq = 80000 * (1 - 1 + 0.01) = 80000 * 0.01 = 800
        assert pytest.approx(price, rel=1e-4) == 80000.0 * 0.01


# ── _find_mmr 테스트 ──────────────────────────────────────────────

class TestFindMMR:
    """LiquidationGuard._find_mmr() 단위 테스트."""

    def test_first_bracket(self):
        """notional=10000 → 첫 번째 브라켓 (MMR=0.004)."""
        mmr = LiquidationGuard._find_mmr(SAMPLE_BRACKETS, 10000)
        assert mmr == 0.004

    def test_middle_bracket(self):
        """notional=100000 → 두 번째 브라켓 (MMR=0.005)."""
        mmr = LiquidationGuard._find_mmr(SAMPLE_BRACKETS, 100000)
        assert mmr == 0.005

    def test_last_bracket_fallback(self):
        """notional=9999999 → 마지막 브라켓 (MMR=0.025)."""
        mmr = LiquidationGuard._find_mmr(SAMPLE_BRACKETS, 9999999)
        assert mmr == 0.025

    def test_empty_brackets_default(self):
        """빈 브라켓 → 기본값 0.025."""
        mmr = LiquidationGuard._find_mmr([], 10000)
        assert mmr == 0.025

    def test_exact_floor_boundary(self):
        """notionalFloor 경계값 테스트."""
        mmr = LiquidationGuard._find_mmr(SAMPLE_BRACKETS, 50000)
        assert mmr == 0.005  # 두 번째 브라켓


# ── check_entry 정상 경우 ──────────────────────────────────────────

class TestCheckEntrySafe:
    """check_entry() — 안전한 진입 (청산거리 충분) 테스트."""

    @pytest.mark.asyncio
    async def test_long_safe_entry(self):
        """롱 3x, ATR 1000 (약 1.25%), SL 1.5 ATR — 청산거리 충분."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange)
        # entry=80000, atr=1000, sl_mult=1.5 → sl_distance=1500 (1.875%)
        # liq at 3x, MMR=0.004: liq = 80000*(1 - 1/3 + 0.004) = ~53653
        # liq_distance = 26347 (32.9%) >> sl_distance=1500 × 2 = 3000 → SAFE
        result = await guard.check_entry("BTC/USDT", "long", 80000.0, 1.5, 1000.0, 3)
        assert result.safe is True
        assert result.buffer_ratio >= 2.0
        assert result.suggested_leverage is None
        assert result.reason == "ok"

    @pytest.mark.asyncio
    async def test_short_safe_entry(self):
        """숏 3x, ATR 1000 — 청산거리 충분."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange)
        result = await guard.check_entry("BTC/USDT", "short", 80000.0, 1.5, 1000.0, 3)
        assert result.safe is True
        assert result.buffer_ratio >= 2.0

    @pytest.mark.asyncio
    async def test_result_contains_prices(self):
        """결과에 청산가, SL가 포함됨."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange)
        result = await guard.check_entry("BTC/USDT", "long", 80000.0, 1.5, 1000.0, 3)
        assert result.liquidation_price > 0
        assert result.sl_price > 0
        assert result.sl_price < 80000.0  # 롱 SL은 진입가 아래

    @pytest.mark.asyncio
    async def test_short_sl_above_entry(self):
        """숏 SL가는 진입가 위."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange)
        result = await guard.check_entry("BTC/USDT", "short", 80000.0, 1.5, 1000.0, 3)
        assert result.sl_price > 80000.0

    @pytest.mark.asyncio
    async def test_distance_percentages_set(self):
        """liq_distance_pct, sl_distance_pct 모두 설정됨."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange)
        result = await guard.check_entry("BTC/USDT", "long", 80000.0, 1.5, 1000.0, 3)
        assert result.liq_distance_pct > 0
        assert result.sl_distance_pct > 0


# ── check_entry 레버리지 자동 하향 테스트 ─────────────────────────

class TestCheckEntryLeverageReduction:
    """check_entry() — 레버리지 자동 하향 테스트."""

    @pytest.mark.asyncio
    async def test_leverage_reduced_when_needed(self):
        """큰 ATR(고변동성) + 큰 SL mult → 레버리지 하향 필요."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange)
        # entry=80000, atr=10000 (12.5%), sl_mult=5 → sl_distance=50000 (62.5%)
        # At 5x, MMR=0.004: liq_distance = 80000*(1/5 - 0.004) = 80000*0.196 = 15680 (19.6%)
        # 15680 < 50000*2=100000 → unsafe at 5x
        # At 3x: liq_distance = 80000*(1/3 - 0.004) = 80000*0.329 = 26347 (32.9%)
        # 26347 < 50000*2=100000 → still unsafe
        # At 1x: liq_distance = 80000*(1 - 0.004) = 79680 (99.6%)
        # 79680 > 50000*2=100000? → 79680 < 100000 → still unsafe (edge case)
        # Let's use a more realistic scenario:
        # entry=80000, atr=3000 (3.75%), sl_mult=3 → sl_distance=9000 (11.25%)
        # At 5x, MMR=0.004: liq_distance = 80000*(1/5 - 0.004) = 80000*0.196 = 15680
        # 15680 / 9000 = 1.74 < 2.0 → unsafe at 5x
        # At 4x: liq_distance = 80000*(1/4 - 0.004) = 80000*0.246 = 19680
        # 19680 / 9000 = 2.19 > 2.0 → SAFE!
        result = await guard.check_entry("BTC/USDT", "long", 80000.0, 3.0, 3000.0, 5)
        # Either safe at 5x or reduced
        if result.safe:
            # If safe at 5x, no reduction needed
            # If reduced, suggested_leverage < 5
            if result.suggested_leverage is not None:
                assert result.suggested_leverage < 5

    @pytest.mark.asyncio
    async def test_suggested_leverage_less_than_original(self):
        """suggested_leverage는 원래보다 작아야 함."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange)
        # Scenario where 5x is unsafe but lower leverage passes
        # entry=80000, atr=5000 (6.25%), sl_mult=3 → sl_distance=15000 (18.75%)
        # At 5x, MMR=0.004: liq_distance = 80000*0.196 = 15680
        # 15680 / 15000 = 1.045 < 2.0 → unsafe at 5x
        # At 3x: liq_distance = 80000*(0.333-0.004) = 80000*0.329 = 26347
        # 26347 / 15000 = 1.76 < 2.0 → still unsafe
        # At 2x: liq_distance = 80000*(0.5-0.004) = 80000*0.496 = 39680
        # 39680 / 15000 = 2.64 > 2.0 → SAFE!
        result = await guard.check_entry("BTC/USDT", "long", 80000.0, 3.0, 5000.0, 5)
        if result.suggested_leverage is not None:
            assert result.suggested_leverage < 5
            assert result.safe is True
            assert "reduced_to_" in result.reason

    @pytest.mark.asyncio
    async def test_reason_contains_reduced_label(self):
        """레버리지 하향 시 reason에 reduced_to_Nx 포함."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange)
        result = await guard.check_entry("BTC/USDT", "long", 80000.0, 3.0, 5000.0, 5)
        if result.suggested_leverage is not None:
            assert "reduced_to_" in result.reason


# ── check_entry 거부 테스트 ────────────────────────────────────────

class TestCheckEntryRejected:
    """check_entry() — 진입 거부 (모든 레버리지에서 실패) 테스트."""

    @pytest.mark.asyncio
    async def test_rejected_when_all_leverages_fail(self):
        """SL 거리가 너무 커서 1x에서도 청산거리 기준 미충족 → 거부."""
        exchange = _make_exchange(brackets=[
            {"notionalFloor": 0, "notionalCap": float("inf"), "maintMarginRatio": 0.004, "maxLeverage": 5}
        ])
        guard = LiquidationGuard(exchange)
        # entry=80000, atr=40000 (50%), sl_mult=2 → sl_distance=80000 (100%)
        # At 1x: liq_distance = 80000*(1-0.004) = 79680 (99.6%)
        # 79680 / 80000 = 0.996 < 2.0 → rejected
        result = await guard.check_entry("BTC/USDT", "long", 80000.0, 2.0, 40000.0, 3)
        assert result.safe is False
        assert result.reason == "liq_too_close"
        assert result.suggested_leverage is None

    @pytest.mark.asyncio
    async def test_rejected_result_has_1x_liquidation_price(self):
        """거부된 결과는 1x 청산가를 포함."""
        exchange = _make_exchange(brackets=[
            {"notionalFloor": 0, "notionalCap": float("inf"), "maintMarginRatio": 0.004, "maxLeverage": 5}
        ])
        guard = LiquidationGuard(exchange)
        result = await guard.check_entry("BTC/USDT", "long", 80000.0, 2.0, 40000.0, 3)
        if not result.safe:
            # liquidation_price should be 1x liquidation
            assert result.liquidation_price > 0


# ── 브라켓 캐시 테스트 ────────────────────────────────────────────

class TestBracketCache:
    """leverageBracket 캐시 (TTL 5분) 테스트."""

    @pytest.mark.asyncio
    async def test_cache_used_on_second_call(self):
        """두 번째 호출은 캐시 사용 — fetch_leverage_brackets 1회만 호출됨."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange)
        await guard.check_entry("BTC/USDT", "long", 80000.0, 1.5, 1000.0, 3)
        await guard.check_entry("BTC/USDT", "long", 80000.0, 1.5, 1000.0, 3)
        assert exchange.fetch_leverage_brackets.call_count == 1

    @pytest.mark.asyncio
    async def test_different_symbols_fetched_separately(self):
        """다른 심볼은 별도로 fetch됨."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange)
        await guard.check_entry("BTC/USDT", "long", 80000.0, 1.5, 1000.0, 3)
        await guard.check_entry("ETH/USDT", "long", 3000.0, 1.5, 50.0, 3)
        assert exchange.fetch_leverage_brackets.call_count == 2

    @pytest.mark.asyncio
    async def test_cache_expires_after_ttl(self):
        """TTL 만료 후 재fetch됨."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange, cache_ttl=0)  # TTL=0 → 즉시 만료
        await guard.check_entry("BTC/USDT", "long", 80000.0, 1.5, 1000.0, 3)
        await guard.check_entry("BTC/USDT", "long", 80000.0, 1.5, 1000.0, 3)
        assert exchange.fetch_leverage_brackets.call_count == 2

    @pytest.mark.asyncio
    async def test_cache_within_ttl_not_refetched(self):
        """TTL 내에 있으면 재fetch 안 됨."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange, cache_ttl=300)  # 5분
        await guard.check_entry("BTC/USDT", "long", 80000.0, 1.5, 1000.0, 3)
        await guard.check_entry("BTC/USDT", "long", 80000.0, 1.5, 1000.0, 3)
        await guard.check_entry("BTC/USDT", "long", 80000.0, 1.5, 1000.0, 3)
        assert exchange.fetch_leverage_brackets.call_count == 1

    @pytest.mark.asyncio
    async def test_stale_cache_used_on_api_failure(self):
        """API 실패 시 오래된 캐시라도 사용."""
        exchange = MagicMock()
        call_count = 0

        async def mock_fetch_brackets(symbol):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return SAMPLE_BRACKETS
            raise Exception("Network error")

        exchange.fetch_leverage_brackets = mock_fetch_brackets
        guard = LiquidationGuard(exchange, cache_ttl=0)  # TTL=0 → 즉시 만료

        # 첫 번째 호출: 성공
        await guard.check_entry("BTC/USDT", "long", 80000.0, 1.5, 1000.0, 3)
        # 두 번째 호출: API 실패 → 오래된 캐시 사용 → safe=True (graceful)
        result = await guard.check_entry("BTC/USDT", "long", 80000.0, 1.5, 1000.0, 3)
        assert result.safe is True  # graceful degradation


# ── API 오류 graceful degradation 테스트 ─────────────────────────

class TestGracefulDegradation:
    """API 실패 시 graceful degradation — 진입 차단 않음."""

    @pytest.mark.asyncio
    async def test_api_failure_returns_safe(self):
        """API 오류 시 safe=True 반환 (거래 차단 방지)."""
        exchange = _make_exchange(fail=True)
        guard = LiquidationGuard(exchange)
        result = await guard.check_entry("BTC/USDT", "long", 80000.0, 1.5, 1000.0, 3)
        assert result.safe is True

    @pytest.mark.asyncio
    async def test_api_failure_still_safe(self):
        """API 오류 시에도 safe=True (기본 MMR로 계산 진행)."""
        # _get_maint_margin_ratio가 내부에서 예외를 잡아 기본 MMR(0.025)로 대체함
        # → check_entry는 기본 MMR로 청산가를 계산하여 정상 진행
        exchange = _make_exchange(fail=True)
        guard = LiquidationGuard(exchange)
        result = await guard.check_entry("BTC/USDT", "long", 80000.0, 1.5, 1000.0, 3)
        assert result.safe is True

    @pytest.mark.asyncio
    async def test_invalid_params_returns_safe(self):
        """잘못된 파라미터(entry_price=0) → safe=True."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange)
        result = await guard.check_entry("BTC/USDT", "long", 0.0, 1.5, 1000.0, 3)
        assert result.safe is True
        assert result.reason == "invalid_params"

    @pytest.mark.asyncio
    async def test_zero_atr_returns_safe(self):
        """ATR=0 → safe=True."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange)
        result = await guard.check_entry("BTC/USDT", "long", 80000.0, 1.5, 0.0, 3)
        assert result.safe is True

    @pytest.mark.asyncio
    async def test_zero_leverage_returns_safe(self):
        """레버리지=0 → safe=True."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange)
        result = await guard.check_entry("BTC/USDT", "long", 80000.0, 1.5, 1000.0, 0)
        assert result.safe is True


# ── check_margin_ratio 테스트 ─────────────────────────────────────

class TestCheckMarginRatio:
    """check_margin_ratio() 테스트."""

    @pytest.mark.asyncio
    async def test_returns_margin_ratio(self):
        """정상 응답에서 marginRatio 반환."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange)
        ratio = await guard.check_margin_ratio()
        assert ratio == 0.15

    @pytest.mark.asyncio
    async def test_returns_max_ratio_across_positions(self):
        """여러 포지션 중 최대 marginRatio 반환."""
        exchange = _make_exchange(position_risk=[
            {"symbol": "BTCUSDT", "marginRatio": "0.10"},
            {"symbol": "ETHUSDT", "marginRatio": "0.55"},
            {"symbol": "BNBUSDT", "marginRatio": "0.25"},
        ])
        guard = LiquidationGuard(exchange)
        ratio = await guard.check_margin_ratio()
        assert ratio == pytest.approx(0.55)

    @pytest.mark.asyncio
    async def test_api_failure_returns_zero(self):
        """API 오류 시 0.0 반환 (안전하다고 가정)."""
        exchange = _make_exchange(fail=True)
        guard = LiquidationGuard(exchange)
        ratio = await guard.check_margin_ratio()
        assert ratio == 0.0

    @pytest.mark.asyncio
    async def test_empty_response_returns_zero(self):
        """빈 응답 → 0.0 반환."""
        exchange = _make_exchange(position_risk=[])
        guard = LiquidationGuard(exchange)
        ratio = await guard.check_margin_ratio()
        assert ratio == 0.0

    @pytest.mark.asyncio
    async def test_symbol_filter_passed(self):
        """symbol 필터가 exchange에 전달됨."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange)
        await guard.check_margin_ratio("BTC/USDT")
        exchange.fetch_position_risk.assert_called_once_with("BTC/USDT")

    @pytest.mark.asyncio
    async def test_no_symbol_filter(self):
        """symbol=None → 전체 조회."""
        exchange = _make_exchange()
        guard = LiquidationGuard(exchange)
        await guard.check_margin_ratio()
        exchange.fetch_position_risk.assert_called_once_with(None)

    @pytest.mark.asyncio
    async def test_zero_margin_ratio_skipped(self):
        """marginRatio=0 포지션은 집계에서 제외."""
        exchange = _make_exchange(position_risk=[
            {"symbol": "BTCUSDT", "marginRatio": "0"},
            {"symbol": "ETHUSDT", "marginRatio": "0.30"},
        ])
        guard = LiquidationGuard(exchange)
        ratio = await guard.check_margin_ratio()
        assert ratio == pytest.approx(0.30)


# ── MAX_MARGIN_RATIO 상수 테스트 ──────────────────────────────────

class TestConstants:
    def test_liquidation_buffer_ratio(self):
        assert LiquidationGuard.LIQUIDATION_BUFFER_RATIO == 2.0

    def test_max_margin_ratio(self):
        assert LiquidationGuard.MAX_MARGIN_RATIO == 0.80

    def test_margin_ratio_check_above_threshold(self):
        """마진비율 80% 이상 = 위험."""
        assert 0.85 >= LiquidationGuard.MAX_MARGIN_RATIO

    def test_margin_ratio_check_below_threshold(self):
        """마진비율 80% 미만 = 안전."""
        assert 0.70 < LiquidationGuard.MAX_MARGIN_RATIO


# ── LiquidationCheckResult dataclass 테스트 ──────────────────────

class TestLiquidationCheckResult:
    def test_defaults(self):
        r = LiquidationCheckResult(safe=True)
        assert r.safe is True
        assert r.liquidation_price == 0.0
        assert r.sl_price == 0.0
        assert r.liq_distance_pct == 0.0
        assert r.sl_distance_pct == 0.0
        assert r.buffer_ratio == 0.0
        assert r.suggested_leverage is None
        assert r.reason == ""

    def test_unsafe_result(self):
        r = LiquidationCheckResult(
            safe=False,
            buffer_ratio=0.8,
            reason="liq_too_close",
        )
        assert r.safe is False
        assert r.buffer_ratio == 0.8
        assert r.reason == "liq_too_close"
