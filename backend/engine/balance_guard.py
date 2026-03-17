"""
BalanceGuard — 잔고 무결성 감시.

거래소 실제 잔고와 내부 장부를 교차 검증하여
괴리 발생 시 경고 → 일시 정지 → 자동 재동기화를 수행한다.
"""
import structlog
from dataclasses import dataclass
from datetime import datetime, timezone

from exchange.base import ExchangeAdapter
from core.event_bus import emit_event

logger = structlog.get_logger(__name__)

# 선물 거래소 이름 패턴
_FUTURES_EXCHANGES = {"binance_futures"}


@dataclass
class BalanceCheckResult:
    """잔고 교차 검증 결과."""
    exchange_balance: float  # 거래소 실제 잔고
    internal_balance: float  # 내부 장부 잔고
    divergence_pct: float    # 괴리율 (%)
    is_warning: bool         # 경고 수준 (> warn_threshold)
    is_critical: bool        # 위험 수준 (> pause_threshold)
    checked_at: datetime
    resynced: bool = False   # 자동 재동기화 수행 여부


class BalanceGuard:
    """잔고 무결성 감시자.

    - 주기적으로 거래소 실제 잔고와 내부 장부를 비교.
    - 경고 임계: warn_pct (기본 3%) → 로그 + 이벤트.
    - 위험 임계: pause_pct (기본 5%) → 엔진 일시 정지 요청.
    - 스냅샷 스파이크: snapshot_spike_pct (기본 10%) → 거부.
    - 자동 재동기화: auto_resync_count (기본 5) 연속 critical → 내부 장부 재동기화 + 자동 재개.
    """

    def __init__(
        self,
        exchange: ExchangeAdapter,
        exchange_name: str = "binance_futures",
        warn_pct: float = 3.0,
        pause_pct: float = 5.0,
        snapshot_spike_pct: float = 10.0,
        auto_resync_count: int = 5,
        portfolio_manager=None,
    ):
        self._exchange = exchange
        self._exchange_name = exchange_name
        self._warn_pct = warn_pct
        self._pause_pct = pause_pct
        self._snapshot_spike_pct = snapshot_spike_pct
        self._auto_resync_count = auto_resync_count
        self._portfolio_manager = portfolio_manager
        self._paused = False
        self._last_check: BalanceCheckResult | None = None
        self._consecutive_warnings = 0
        self._consecutive_criticals = 0

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def last_check(self) -> BalanceCheckResult | None:
        return self._last_check

    @property
    def is_futures(self) -> bool:
        """선물 거래소 여부."""
        return self._exchange_name in _FUTURES_EXCHANGES

    def set_portfolio_manager(self, pm) -> None:
        """포트폴리오 매니저 참조 설정 (재동기화용)."""
        self._portfolio_manager = pm

    def resume(self) -> None:
        """수동 재개 (관리자 확인 후)."""
        self._paused = False
        self._consecutive_warnings = 0
        self._consecutive_criticals = 0
        logger.info("balance_guard_resumed", exchange=self._exchange_name)

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

        resynced = False

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
            self._consecutive_criticals += 1
            self._paused = True
            logger.error(
                "balance_guard_CRITICAL",
                exchange=self._exchange_name,
                exchange_bal=round(exchange_balance, 4),
                internal_bal=round(internal_balance, 4),
                divergence_pct=result.divergence_pct,
                consecutive_criticals=self._consecutive_criticals,
            )

            # 자동 재동기화: N회 연속 critical이면 내부 장부를 거래소 잔고로 재초기화
            if (
                self._auto_resync_count > 0
                and self._consecutive_criticals >= self._auto_resync_count
                and self._portfolio_manager is not None
            ):
                resynced = await self._auto_resync(exchange_balance)
                if resynced:
                    result.resynced = True
                    # resync 후 divergence 재계산
                    new_internal = self._portfolio_manager.cash_balance
                    new_base = max(abs(exchange_balance), abs(new_internal), 1.0)
                    new_divergence = abs(exchange_balance - new_internal)
                    result.divergence_pct = round(
                        (new_divergence / new_base) * 100, 2,
                    )
                    result.internal_balance = new_internal
                    result.is_critical = result.divergence_pct >= self._pause_pct
                    result.is_warning = result.divergence_pct >= self._warn_pct
            else:
                await emit_event(
                    "critical", "balance_guard",
                    f"잔고 괴리 {result.divergence_pct}% — 엔진 일시 정지",
                    detail=f"거래소: {exchange_balance:.4f}, 내부: {internal_balance:.4f}",
                    metadata={"divergence_pct": result.divergence_pct},
                )
        elif is_warning:
            self._consecutive_warnings += 1
            self._consecutive_criticals = 0
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
            self._consecutive_criticals = 0

        return result

    async def _auto_resync(self, exchange_balance: float) -> bool:
        """내부 장부를 거래소 실제 잔고로 재동기화.

        선물의 경우 initialize_cash_from_exchange()를 재호출하고,
        현물의 경우 exchange_balance로 직접 설정한다.

        Returns:
            True if resync 성공.
        """
        try:
            pm = self._portfolio_manager
            old_cash = pm.cash_balance

            if self.is_futures:
                # 선물: PM의 initialize_cash_from_exchange 재호출
                await pm.initialize_cash_from_exchange(self._exchange)
            else:
                # 현물: exchange_balance로 직접 설정
                pm._cash_balance = exchange_balance

            new_cash = pm.cash_balance

            self._paused = False
            self._consecutive_warnings = 0
            self._consecutive_criticals = 0

            logger.info(
                "balance_guard_auto_resynced",
                exchange=self._exchange_name,
                old_cash=round(old_cash, 4),
                new_cash=round(new_cash, 4),
                exchange_bal=round(exchange_balance, 4),
            )
            await emit_event(
                "warning", "balance_guard",
                "잔고 자동 재동기화 완료 — 엔진 재개",
                detail=f"이전: {old_cash:.4f}, 이후: {new_cash:.4f}, 거래소: {exchange_balance:.4f}",
                metadata={
                    "old_cash": round(old_cash, 4),
                    "new_cash": round(new_cash, 4),
                },
            )
            return True
        except Exception as e:
            logger.error(
                "balance_guard_resync_failed",
                exchange=self._exchange_name,
                error=str(e),
            )
            return False

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

        선물: walletBalance - unrealizedPnL - totalMargin
          → PortfolioManager.initialize_cash_from_exchange()와 동일한 계산.
          → USDT.free는 unrealizedPnL이 포함되어 부정확.

        현물: USDT.free 또는 KRW.free.
        """
        try:
            balance = await self._exchange.fetch_balance()
            usdt = balance.get("USDT")

            if usdt and self.is_futures:
                # 선물: walletBalance 기반 계산
                # total = walletBalance (unrealizedPnL 포함)
                # free에 unrealizedPnL이 포함되어 이중계산됨 → 사용 금지
                return await self._calc_futures_cash(usdt)

            if usdt:
                return usdt.free
            # KRW fallback (현물)
            krw = balance.get("KRW")
            if krw:
                return krw.free
            return 0.0
        except Exception as e:
            logger.warning("balance_fetch_failed", error=str(e))
            return 0.0

    async def _calc_futures_cash(self, usdt_balance) -> float:
        """선물 가용 잔고 계산.

        wallet = total - unrealizedPnL
        cash = wallet - totalMargin

        PortfolioManager.initialize_cash_from_exchange()와 동일 로직.
        """
        try:
            raw_positions = await self._exchange._exchange.fetch_positions()
            total_margin = 0.0
            total_unrealized = 0.0
            for fp in raw_positions:
                if float(fp.get("contracts", 0) or 0) > 0:
                    total_margin += float(fp.get("initialMargin", 0) or 0)
                    total_unrealized += float(fp.get("unrealizedPnl", 0) or 0)

            wallet = usdt_balance.total - total_unrealized
            cash = wallet - total_margin
            return cash
        except Exception as e:
            logger.warning(
                "futures_cash_calc_fallback",
                error=str(e),
                fallback="usdt.free",
            )
            # 포지션 조회 실패 시 free로 폴백 (기존 동작)
            return usdt_balance.free

    async def periodic_reconcile(self, internal_balance: float) -> BalanceCheckResult:
        """주기적 교차 검증 (루프에서 호출)."""
        return await self.check_balance(internal_balance)
