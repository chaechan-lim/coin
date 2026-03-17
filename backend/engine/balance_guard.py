"""
BalanceGuard — 잔고 무결성 감시.

거래소 실제 잔고와 내부 장부를 교차 검증하여
괴리 발생 시 경고 → 일시 정지 → 복구를 자동으로 수행한다.

선물 잔고 계산:
  USDT.free에는 unrealizedPnL이 포함되어 내부 장부와 불일치 발생.
  올바른 계산: wallet = total - unrealizedPnL, cash = wallet - totalMargin.
"""
import structlog
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional

from exchange.base import ExchangeAdapter
from core.event_bus import emit_event

logger = structlog.get_logger(__name__)

# 자동 재동기화 콜백 타입: 새로운 cash 값을 받아 내부 장부를 갱신
ResyncCallback = Callable[[float], Awaitable[None]]


@dataclass
class BalanceCheckResult:
    """잔고 교차 검증 결과."""
    exchange_balance: float  # 거래소 실제 잔고
    internal_balance: float  # 내부 장부 잔고
    divergence_pct: float    # 괴리율 (%)
    is_warning: bool         # 경고 수준 (> warn_threshold)
    is_critical: bool        # 위험 수준 (> pause_threshold)
    checked_at: datetime


class BalanceGuard:
    """잔고 무결성 감시자.

    - 주기적으로 거래소 실제 잔고와 내부 장부를 비교.
    - 경고 임계: warn_pct (기본 3%) → 로그 + 이벤트.
    - 위험 임계: pause_pct (기본 5%) → 엔진 일시 정지 요청.
    - 스냅샷 스파이크: snapshot_spike_pct (기본 10%) → 거부.
    - 자동 복구: 일시 정지 후 warn_pct 미만이 연속 N회 → 자동 재개.
    """

    # 연속 N회 critical 시 자동 재동기화 트리거
    DEFAULT_AUTO_RESYNC_COUNT = 5

    def __init__(
        self,
        exchange: ExchangeAdapter,
        exchange_name: str = "binance_futures",
        warn_pct: float = 3.0,
        pause_pct: float = 5.0,
        snapshot_spike_pct: float = 10.0,
        auto_resume_stable_count: int = 3,
        resync_callback: Optional[ResyncCallback] = None,
        auto_resync_count: int = DEFAULT_AUTO_RESYNC_COUNT,
    ):
        self._exchange = exchange
        self._exchange_name = exchange_name
        self._is_futures = "futures" in exchange_name
        self._warn_pct = warn_pct
        self._pause_pct = pause_pct
        self._snapshot_spike_pct = snapshot_spike_pct
        self._auto_resume_stable_count = auto_resume_stable_count
        self._resync_callback = resync_callback
        self._auto_resync_count = auto_resync_count
        self._paused = False
        self._last_check: BalanceCheckResult | None = None
        self._consecutive_warnings = 0
        self._consecutive_stable = 0
        self._consecutive_critical = 0
        self._resync_count = 0

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def last_check(self) -> BalanceCheckResult | None:
        return self._last_check

    def resume(self, reason: str = "manual") -> None:
        """재개 (수동 또는 자동 복구).

        Args:
            reason: 재개 사유 ("manual", "auto_recovery", "resync").
        """
        self._paused = False
        self._consecutive_warnings = 0
        self._consecutive_stable = 0
        self._consecutive_critical = 0
        logger.info(
            "balance_guard_resumed",
            exchange=self._exchange_name,
            reason=reason,
        )

    async def check_balance(self, internal_balance: float) -> BalanceCheckResult:
        """거래소 잔고와 내부 장부를 비교한다.

        Args:
            internal_balance: PortfolioManager의 cash_balance.

        Returns:
            BalanceCheckResult with divergence info.
        """
        exchange_balance = await self._fetch_exchange_balance()

        # 괴리율 계산 — 0으로 나누기 방지
        base = max(abs(exchange_balance), abs(internal_balance), 1.0)
        divergence = abs(exchange_balance - internal_balance)
        divergence_pct = (divergence / base) * 100

        is_warning = divergence_pct >= self._warn_pct
        is_critical = divergence_pct >= self._pause_pct

        result = BalanceCheckResult(
            exchange_balance=exchange_balance,
            internal_balance=internal_balance,
            divergence_pct=round(divergence_pct, 2),
            is_warning=is_warning,
            is_critical=is_critical,
            checked_at=datetime.now(timezone.utc),
        )
        self._last_check = result

        if is_critical:
            self._consecutive_warnings += 1
            self._consecutive_stable = 0
            self._consecutive_critical += 1
            self._paused = True
            logger.error(
                "balance_guard_CRITICAL",
                exchange=self._exchange_name,
                exchange_bal=round(exchange_balance, 4),
                internal_bal=round(internal_balance, 4),
                divergence_pct=result.divergence_pct,
                consecutive_critical=self._consecutive_critical,
            )
            await emit_event(
                "critical", "balance_guard",
                f"잔고 괴리 {result.divergence_pct}% — 엔진 일시 정지",
                detail=f"거래소: {exchange_balance:.4f}, 내부: {internal_balance:.4f}",
                metadata={"divergence_pct": result.divergence_pct},
            )

            # 자동 재동기화: N회 연속 critical → 내부 장부를 거래소 잔고로 강제 재초기화
            if (
                self._resync_callback
                and self._consecutive_critical >= self._auto_resync_count
                and exchange_balance > 0
            ):
                await self._trigger_resync(exchange_balance)
        elif is_warning:
            self._consecutive_warnings += 1
            self._consecutive_stable = 0
            self._consecutive_critical = 0
            logger.warning(
                "balance_guard_warning",
                exchange=self._exchange_name,
                exchange_bal=round(exchange_balance, 4),
                internal_bal=round(internal_balance, 4),
                divergence_pct=result.divergence_pct,
            )
            if self._consecutive_warnings >= 3:
                # 3회 연속 경고 → 위험 수준으로 격상
                self._paused = True
                await emit_event(
                    "critical", "balance_guard",
                    "잔고 괴리 3회 연속 — 엔진 일시 정지",
                    detail=f"괴리율: {result.divergence_pct}%",
                )
        else:
            self._consecutive_warnings = 0
            self._consecutive_critical = 0
            # 자동 복구: 일시 정지 상태에서 안정 체크 연속 N회 시 자동 재개
            if self._paused:
                self._consecutive_stable += 1
                if self._consecutive_stable >= self._auto_resume_stable_count:
                    self.resume(reason="auto_recovery")
                    await emit_event(
                        "info", "balance_guard",
                        f"잔고 안정 {self._auto_resume_stable_count}회 연속 — 자동 재개",
                        detail=f"괴리율: {result.divergence_pct}%",
                        metadata={"divergence_pct": result.divergence_pct},
                    )
                else:
                    logger.info(
                        "balance_guard_recovering",
                        exchange=self._exchange_name,
                        stable_count=self._consecutive_stable,
                        required=self._auto_resume_stable_count,
                        divergence_pct=result.divergence_pct,
                    )

        return result

    def validate_snapshot(
        self, new_total: float, last_total: float | None,
    ) -> bool:
        """스냅샷 스파이크 검증.

        Returns:
            True if valid, False if spike detected (should reject).
        """
        if last_total is None or last_total <= 0:
            return True

        change_pct = abs(new_total - last_total) / last_total * 100
        if change_pct > self._snapshot_spike_pct:
            logger.error(
                "snapshot_spike_rejected",
                exchange=self._exchange_name,
                new_total=round(new_total, 4),
                last_total=round(last_total, 4),
                change_pct=round(change_pct, 2),
            )
            return False
        return True

    def validate_order_pre(
        self,
        cash_balance: float,
        order_cost: float,
    ) -> tuple[bool, str]:
        """주문 전 잔고 검증.

        Returns:
            (is_valid, reason)
        """
        if self._paused:
            return False, "balance_guard_paused"

        if order_cost <= 0:
            return False, "invalid_order_cost"

        if order_cost > cash_balance:
            return False, f"insufficient_cash: need {order_cost:.4f}, have {cash_balance:.4f}"

        return True, "ok"

    def validate_order_post(
        self,
        cash_before: float,
        cash_after: float,
        expected_change: float,
    ) -> tuple[bool, str]:
        """주문 후 잔고 변화 검증.

        Returns:
            (is_valid, reason)
        """
        actual_change = cash_before - cash_after
        if expected_change <= 0:
            return True, "ok"

        slippage_pct = abs(actual_change - expected_change) / expected_change * 100
        if slippage_pct > 5.0:
            logger.warning(
                "order_cash_slippage",
                expected=round(expected_change, 4),
                actual=round(actual_change, 4),
                slippage_pct=round(slippage_pct, 2),
            )
            return False, f"cash_slippage_{slippage_pct:.1f}%"

        return True, "ok"

    async def _fetch_exchange_balance(self) -> float:
        """거래소에서 실제 가용 잔고를 가져온다.

        선물: walletBalance - totalMargin (USDT.free는 unrealizedPnL 포함으로 부정확).
        현물: USDT.free 또는 KRW.free.
        """
        try:
            balance = await self._exchange.fetch_balance()

            if self._is_futures:
                return await self._calc_futures_available_cash(balance)

            # 현물: free 잔고 사용
            usdt = balance.get("USDT")
            if usdt:
                return usdt.free
            krw = balance.get("KRW")
            if krw:
                return krw.free
            return 0.0
        except Exception as e:
            logger.warning("balance_fetch_failed", error=str(e))
            return 0.0

    async def _calc_futures_available_cash(self, balance: dict) -> float:
        """선물 가용 현금 계산.

        USDT.free에는 unrealizedPnL이 포함되어 이중계산 위험.
        올바른 계산:
          wallet = USDT.total - sum(unrealizedPnL)
          available_cash = wallet - sum(initialMargin)
        """
        cash_bal = balance.get("USDT")
        if not cash_bal:
            return 0.0

        try:
            raw_positions = await self._exchange._exchange.fetch_positions()
            total_margin = 0.0
            total_unrealized = 0.0
            for fp in raw_positions:
                if float(fp.get("contracts", 0) or 0) > 0:
                    total_margin += float(fp.get("initialMargin", 0) or 0)
                    total_unrealized += float(fp.get("unrealizedPnl", 0) or 0)

            wallet = cash_bal.total - total_unrealized
            available_cash = wallet - total_margin
            return available_cash
        except Exception as e:
            logger.warning(
                "futures_balance_calc_failed",
                error=str(e),
                fallback="usdt_free",
            )
            # 폴백: 부정확하지만 0보다 나음
            return cash_bal.free

    async def _trigger_resync(self, exchange_balance: float) -> None:
        """내부 장부를 거래소 잔고로 강제 재동기화 + 자동 재개."""
        try:
            await self._resync_callback(exchange_balance)
            self._resync_count += 1
            self.resume(reason="resync")
            logger.warning(
                "balance_guard_resync_triggered",
                exchange=self._exchange_name,
                new_cash=round(exchange_balance, 4),
                resync_count=self._resync_count,
            )
            await emit_event(
                "warning", "balance_guard",
                f"내부 장부 재동기화 #{self._resync_count} — 자동 재개",
                detail=f"새 잔고: {exchange_balance:.4f} USDT",
                metadata={
                    "exchange_balance": exchange_balance,
                    "resync_count": self._resync_count,
                },
            )
        except Exception as e:
            logger.error(
                "balance_guard_resync_failed",
                exchange=self._exchange_name,
                error=str(e),
            )

    def get_status(self) -> dict:
        """BalanceGuard 상태 정보 반환 (API용)."""
        last = self._last_check
        return {
            "is_paused": self._paused,
            "consecutive_warnings": self._consecutive_warnings,
            "consecutive_stable": self._consecutive_stable,
            "consecutive_critical": self._consecutive_critical,
            "auto_resume_stable_count": self._auto_resume_stable_count,
            "auto_resync_count": self._auto_resync_count,
            "resync_count": self._resync_count,
            "is_futures": self._is_futures,
            "warn_pct": self._warn_pct,
            "pause_pct": self._pause_pct,
            "last_check": {
                "exchange_balance": last.exchange_balance,
                "internal_balance": last.internal_balance,
                "divergence_pct": last.divergence_pct,
                "is_warning": last.is_warning,
                "is_critical": last.is_critical,
                "checked_at": last.checked_at.isoformat(),
            } if last else None,
        }

    async def periodic_reconcile(self, internal_balance: float) -> BalanceCheckResult:
        """주기적 교차 검증 (루프에서 호출)."""
        return await self.check_balance(internal_balance)
