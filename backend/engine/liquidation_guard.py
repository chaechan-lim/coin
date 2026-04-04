"""
LiquidationGuard — 선물 진입 전 청산 거리 검증.

진입 전 청산가까지 거리가 SL 거리의 2배 이상인지 확인.
실패 시 레버리지 자동 하향 또는 진입 거부.

COIN-76: leverageBracket + positionRisk 사전 검증.
"""

import time
import structlog
from dataclasses import dataclass

logger = structlog.get_logger(__name__)


@dataclass
class LiquidationCheckResult:
    """청산 거리 검증 결과."""

    safe: bool
    liquidation_price: float = 0.0
    sl_price: float = 0.0
    liq_distance_pct: float = 0.0
    sl_distance_pct: float = 0.0
    buffer_ratio: float = 0.0               # liq_distance / sl_distance (목표: ≥ 2.0)
    suggested_leverage: int | None = None   # 레버리지 자동 하향 제안
    reason: str = ""


class LiquidationGuard:
    """진입 전 청산 거리 검증 + 마진비율 확인.

    Binance USDM isolated margin 기준 청산가 계산.

    Usage::

        guard = LiquidationGuard(exchange)
        result = await guard.check_entry(
            symbol="BTC/USDT",
            direction="long",
            entry_price=80000.0,
            sl_atr_mult=1.5,
            atr=1000.0,
            leverage=3,
        )
        if not result.safe:
            return  # 진입 거부
        if result.suggested_leverage:
            leverage = result.suggested_leverage  # 레버리지 자동 하향
    """

    LIQUIDATION_BUFFER_RATIO = 2.0  # 청산거리 > SL거리 × 2 조건
    MAX_MARGIN_RATIO = 0.80         # 마진비율 80% 이하 유지
    _DEFAULT_MMR = 0.025            # API 실패 시 보수적 기본값 (2.5%)

    def __init__(self, exchange, cache_ttl: int = 300):
        """
        Args:
            exchange: ExchangeAdapter (BinanceUSDMAdapter 등)
            cache_ttl: 브라켓 데이터 캐시 TTL (초, 기본 5분)
        """
        self._exchange = exchange
        self._bracket_cache: dict[str, tuple[list[dict], float]] = {}
        self._cache_ttl = cache_ttl

    # ── 청산가 계산 (정적 메서드) ────────────────────────────────

    @staticmethod
    def calc_liquidation_price(
        direction: str,
        entry_price: float,
        leverage: int,
        maint_margin_ratio: float,
    ) -> float:
        """Binance USDM isolated margin 청산가 계산 (Mark Price 기준).

        공식 (simplified isolated margin):
          LONG:  liq = entry * (1 - 1/leverage + mmr)
          SHORT: liq = entry * (1 + 1/leverage - mmr)

        Note:
            - 실제 청산가에는 추가 수수료(taker fee)가 반영되지만
              안전 마진을 위해 단순화 공식 사용.
            - Binance는 Mark Price 기준 청산 (Last Price와 괴리 가능).

        Args:
            direction: "long" 또는 "short"
            entry_price: 진입가
            leverage: 레버리지 배수
            maint_margin_ratio: 유지증거금 비율 (예: 0.01 = 1%)

        Returns:
            청산 예상가. 0.0 = 계산 불가.
        """
        if leverage <= 0 or entry_price <= 0:
            return 0.0
        mmr = maint_margin_ratio
        if direction == "long":
            return entry_price * (1 - 1.0 / leverage + mmr)
        else:  # short
            return entry_price * (1 + 1.0 / leverage - mmr)

    # ── 브라켓 조회 (캐시 포함) ───────────────────────────────────

    async def _get_maint_margin_ratio(
        self, symbol: str, notional: float
    ) -> float:
        """notional 규모에 맞는 maintMarginRatio 반환.

        leverageBracket 캐시(5분)를 활용.
        API 오류 시 보수적 기본값 반환.

        Args:
            symbol: 심볼 (예: "BTC/USDT")
            notional: 명목 포지션 크기 (entry_price × leverage)

        Returns:
            유지증거금 비율 (0.0~1.0)
        """
        now = time.monotonic()
        cached = self._bracket_cache.get(symbol)
        if cached:
            brackets, fetched_at = cached
            if now - fetched_at < self._cache_ttl:
                return self._find_mmr(brackets, notional)

        try:
            brackets = await self._exchange.fetch_leverage_brackets(symbol)
            self._bracket_cache[symbol] = (brackets, now)
            return self._find_mmr(brackets, notional)
        except Exception as e:
            logger.warning(
                "leverage_bracket_fetch_failed",
                symbol=symbol,
                error=str(e),
            )
            # 캐시된 오래된 데이터라도 사용
            if cached:
                brackets, _ = cached
                return self._find_mmr(brackets, notional)
            # 보수적 기본값: MMR 2.5%
            return self._DEFAULT_MMR

    @staticmethod
    def _find_mmr(brackets: list[dict], notional: float) -> float:
        """notional 금액에 맞는 브라켓의 maintMarginRatio 반환.

        Args:
            brackets: leverageBracket API 응답 브라켓 리스트
            notional: 명목 포지션 크기

        Returns:
            유지증거금 비율. 브라켓 없으면 기본값 2.5%.
        """
        if not brackets:
            return 0.025
        for bracket in brackets:
            floor = float(bracket.get("notionalFloor", 0))
            cap = float(bracket.get("notionalCap", float("inf")))
            if floor <= notional < cap:
                return float(bracket.get("maintMarginRatio", 0.025))
        # 마지막 브라켓 fallback
        last = brackets[-1]
        return float(last.get("maintMarginRatio", 0.025))

    # ── 진입 전 검증 ──────────────────────────────────────────────

    async def check_entry(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        sl_atr_mult: float,
        atr: float,
        leverage: int,
    ) -> LiquidationCheckResult:
        """진입 전 청산 거리 검증.

        1. leverageBracket에서 maintMarginRatio 조회 (5분 캐시)
        2. 청산가 계산 (isolated margin 공식)
        3. SL 가격 계산 (ATR 기반)
        4. 청산거리 > SL거리 × 2 확인
        5. 실패 시: 레버리지 자동 하향 (leverage-1 → 1)
        6. 모두 실패 시: 진입 거부 (safe=False)

        Note:
            API 오류 시 safe=True 반환 — 거래를 차단하지 않음 (graceful degradation).

        Args:
            symbol: 심볼 (예: "BTC/USDT")
            direction: "long" 또는 "short"
            entry_price: 진입가
            sl_atr_mult: SL ATR 배수
            atr: 현재 ATR 값
            leverage: 현재 레버리지

        Returns:
            LiquidationCheckResult. safe=False 시 진입 거부.
            suggested_leverage가 있으면 해당 레버리지로 재시도.
        """
        if entry_price <= 0 or atr <= 0 or leverage <= 0:
            return LiquidationCheckResult(safe=True, reason="invalid_params")

        # SL 가격 + 거리
        sl_distance = sl_atr_mult * atr
        if direction == "long":
            sl_price = entry_price - sl_distance
        else:
            sl_price = entry_price + sl_distance

        sl_distance_pct = (sl_distance / entry_price) * 100

        # notional 추정 (진입가 × 레버리지)
        notional = entry_price * leverage

        try:
            mmr = await self._get_maint_margin_ratio(symbol, notional)
        except Exception as e:
            logger.warning(
                "liq_guard_mmr_fetch_failed",
                symbol=symbol,
                error=str(e),
            )
            return LiquidationCheckResult(safe=True, reason=f"api_error:{e}")

        # 현재 레버리지부터 1x까지 순차 시도
        for try_leverage in range(leverage, 0, -1):
            liq_price = self.calc_liquidation_price(
                direction, entry_price, try_leverage, mmr
            )
            liq_distance = abs(entry_price - liq_price)
            liq_distance_pct = (liq_distance / entry_price) * 100

            buffer_ratio = liq_distance / sl_distance if sl_distance > 0 else 0.0
            is_safe = buffer_ratio >= self.LIQUIDATION_BUFFER_RATIO

            if is_safe:
                suggested = try_leverage if try_leverage < leverage else None
                if try_leverage < leverage:
                    logger.info(
                        "liq_guard_leverage_reduced",
                        symbol=symbol,
                        original_leverage=leverage,
                        suggested_leverage=try_leverage,
                        buffer_ratio=round(buffer_ratio, 3),
                        sl_distance_pct=round(sl_distance_pct, 2),
                    )
                return LiquidationCheckResult(
                    safe=True,
                    liquidation_price=liq_price,
                    sl_price=sl_price,
                    liq_distance_pct=liq_distance_pct,
                    sl_distance_pct=sl_distance_pct,
                    buffer_ratio=buffer_ratio,
                    suggested_leverage=suggested,
                    reason="ok" if try_leverage == leverage else f"reduced_to_{try_leverage}x",
                )

        # 1x에서도 실패 → 진입 거부
        liq_price_1x = self.calc_liquidation_price(direction, entry_price, 1, mmr)
        liq_distance_1x = abs(entry_price - liq_price_1x)
        buffer_1x = liq_distance_1x / sl_distance if sl_distance > 0 else 0.0

        logger.warning(
            "liq_guard_entry_rejected",
            symbol=symbol,
            direction=direction,
            leverage=leverage,
            buffer_ratio_at_1x=round(buffer_1x, 3),
            sl_distance_pct=round(sl_distance_pct, 2),
            liq_distance_1x_pct=round(liq_distance_1x / entry_price * 100, 2),
        )
        return LiquidationCheckResult(
            safe=False,
            liquidation_price=liq_price_1x,
            sl_price=sl_price,
            liq_distance_pct=(liq_distance_1x / entry_price) * 100,
            sl_distance_pct=sl_distance_pct,
            buffer_ratio=buffer_1x,
            reason="liq_too_close",
        )

    # ── 마진비율 확인 ──────────────────────────────────────────────

    async def check_margin_ratio(self, symbol: str | None = None) -> float:
        """현재 마진비율 확인 (positionRisk API).

        마진비율 = 유지증거금 / 마진잔액.
        80% 이상 → 위험 (강제 청산 임박).

        Args:
            symbol: 특정 심볼 필터 (None=전체 포지션).

        Returns:
            최대 마진비율 (0.0~1.0). API 오류 시 0.0 반환.
        """
        try:
            risks = await self._exchange.fetch_position_risk(symbol)
            if not risks:
                return 0.0
            ratios = [
                float(r.get("marginRatio", 0) or 0)
                for r in risks
                if float(r.get("marginRatio", 0) or 0) > 0
            ]
            return max(ratios) if ratios else 0.0
        except Exception as e:
            logger.warning("liq_guard_margin_ratio_failed", error=str(e))
            return 0.0
