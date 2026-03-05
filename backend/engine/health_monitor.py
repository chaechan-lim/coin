"""
헬스 모니터 — 2분마다 프로액티브 건강 검진

| 검진           | 감지                        | 자동 수정                    |
|---------------|----------------------------|-----------------------------|
| cash 정합성    | cash<0, cash=0+포지션없음    | reconcile_cash, sync_exchange|
| 포지션 정합성  | entry_price=0, stale tracker| 현재가 대입, tracker 제거      |
| API 건강      | BTC ticker fetch 실패       | 3회 연속 실패 → 매수 일시중지  |
| 에러 추세     | eval_error_counts 증가       | 경고 이벤트 발행              |
| 멈춘 포지션   | 30분+ updated_at 미갱신      | 경고 이벤트 발행              |
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Position
from core.event_bus import emit_event

logger = structlog.get_logger(__name__)


@dataclass
class HealthCheckResult:
    name: str
    healthy: bool
    detail: str
    auto_fixed: bool = False


class HealthMonitor:
    """거래소별 건강 검진기.

    Parameters
    ----------
    engine : TradingEngine
        pause_buying, _eval_error_counts, _position_trackers 접근
    portfolio_manager : PortfolioManager
        cash_balance, reconcile_cash, sync_exchange_positions 접근
    exchange_adapter : ExchangeAdapter
        ticker fetch, balance fetch
    market_data : MarketDataService
        get_ticker (API 헬스 체크)
    exchange_name : str
        DB 필터 + 이벤트 메타데이터
    tracked_coins : list[str]
        sync 대상 코인
    """

    def __init__(
        self,
        engine,
        portfolio_manager,
        exchange_adapter,
        market_data,
        exchange_name: str,
        tracked_coins: list[str],
    ):
        self._engine = engine
        self._pm = portfolio_manager
        self._exchange = exchange_adapter
        self._market_data = market_data
        self._exchange_name = exchange_name
        self._tracked_coins = tracked_coins
        self._api_fail_streak = 0
        self._api_paused = False

    async def run_health_checks(self) -> list[HealthCheckResult]:
        """모든 건강 검진 실행. 스케줄러에서 120초 간격 호출."""
        from db.session import get_session_factory

        results: list[HealthCheckResult] = []
        sf = get_session_factory()

        try:
            async with sf() as session:
                results.append(await self._check_cash_consistency(session))
                results.append(await self._check_position_consistency(session))
        except Exception as e:
            logger.error("health_check_db_error", error=str(e),
                         exchange=self._exchange_name)
            results.append(HealthCheckResult(
                name="db_access", healthy=False, detail=str(e)))

        results.append(await self._check_api_health())
        results.append(self._check_error_rate_trend())
        try:
            async with sf() as session:
                results.append(await self._check_stuck_positions(session))
        except Exception as e:
            logger.error("health_check_stuck_error", error=str(e),
                         exchange=self._exchange_name)

        # 이상 발견 시 이벤트 발행
        unhealthy = [r for r in results if not r.healthy]
        if unhealthy:
            names = [r.name for r in unhealthy]
            details = "; ".join(f"{r.name}: {r.detail}" for r in unhealthy)
            fixed = [r.name for r in unhealthy if r.auto_fixed]

            logger.warning(
                "health_check_issues",
                exchange=self._exchange_name,
                issues=names,
                auto_fixed=fixed,
            )
            await emit_event(
                "warning", "health",
                f"헬스체크 이상 [{self._exchange_name}] {len(unhealthy)}건",
                detail=details[:500],
                metadata={
                    "exchange": self._exchange_name,
                    "issues": names,
                    "auto_fixed": fixed,
                    "total_checks": len(results),
                },
            )

        return results

    # ── 1. Cash 정합성 ───────────────────────────────────────

    async def _check_cash_consistency(self, session: AsyncSession) -> HealthCheckResult:
        """cash < 0 또는 cash=0+포지션없음 감지."""
        cash = self._pm.cash_balance

        if cash < 0:
            # 음수 현금 — reconcile 시도
            old = cash
            await self._pm.reconcile_cash_from_db(session)
            new = self._pm.cash_balance
            if new >= 0:
                return HealthCheckResult(
                    name="cash_negative",
                    healthy=False,
                    detail=f"음수 현금 수정: {old:.2f} → {new:.2f}",
                    auto_fixed=True,
                )
            # reconcile 후에도 음수면 sync 시도
            await self._pm.sync_exchange_positions(
                session, self._exchange, self._tracked_coins,
            )
            final = self._pm.cash_balance
            return HealthCheckResult(
                name="cash_negative",
                healthy=final >= 0,
                detail=f"음수 현금: {old:.2f} → reconcile {new:.2f} → sync {final:.2f}",
                auto_fixed=final >= 0,
            )

        if cash == 0:
            # cash=0 + 포지션 없음 → 의심
            result = await session.execute(
                select(Position).where(
                    Position.quantity > 0,
                    Position.exchange == self._exchange_name,
                )
            )
            positions = result.scalars().all()
            if not positions:
                # 포지션 없는데 현금도 0 → reconcile
                await self._pm.reconcile_cash_from_db(session)
                new = self._pm.cash_balance
                if new > 0:
                    return HealthCheckResult(
                        name="cash_zero_no_positions",
                        healthy=False,
                        detail=f"현금=0 포지션=0 수정: → {new:.2f}",
                        auto_fixed=True,
                    )
                return HealthCheckResult(
                    name="cash_zero_no_positions",
                    healthy=False,
                    detail="현금=0, 포지션=0, reconcile 후에도 0",
                )

        return HealthCheckResult(
            name="cash_consistency", healthy=True,
            detail=f"cash={cash:.2f}",
        )

    # ── 2. 포지션 정합성 ─────────────────────────────────────

    async def _check_position_consistency(self, session: AsyncSession) -> HealthCheckResult:
        """entry_price=0, stale tracker 감지."""
        result = await session.execute(
            select(Position).where(
                Position.quantity > 0,
                Position.exchange == self._exchange_name,
            )
        )
        positions = list(result.scalars().all())
        issues = []

        for pos in positions:
            # entry_price = 0 감지
            if (pos.average_buy_price or 0) <= 0:
                # 현재가로 대입 시도
                try:
                    ticker = await self._market_data.get_ticker(pos.symbol)
                    if ticker and ticker.last > 0:
                        pos.average_buy_price = ticker.last
                        issues.append(f"{pos.symbol}: entry=0 → {ticker.last:.4f}")
                except Exception:
                    issues.append(f"{pos.symbol}: entry=0 (가격 조회 실패)")

            # stale tracker: 엔진 trackers에 있는데 포지션이 0인 경우는 위에서 필터됨
            # 포지션이 있는데 tracker가 없으면 로그
            if pos.symbol not in self._engine._position_trackers:
                if pos.stop_loss_pct:
                    # DB에 tracker 정보가 있으면 무시 (stop_check에서 복원됨)
                    pass
                else:
                    issues.append(f"{pos.symbol}: tracker 없음")

        if issues:
            await session.commit()
            return HealthCheckResult(
                name="position_consistency",
                healthy=False,
                detail="; ".join(issues),
                auto_fixed=any("→" in i for i in issues),
            )

        return HealthCheckResult(
            name="position_consistency", healthy=True,
            detail=f"포지션 {len(positions)}개 정상",
        )

    # ── 3. API 건강 ──────────────────────────────────────────

    async def _check_api_health(self) -> HealthCheckResult:
        """BTC ticker fetch로 API 건강 확인. 3회 연속 실패 → 매수 일시중지."""
        # 거래소에 맞는 BTC 심볼
        btc_sym = "BTC/USDT" if "binance" in self._exchange_name else "BTC/KRW"

        try:
            ticker = await self._market_data.get_ticker(btc_sym)
            if ticker and ticker.last > 0:
                self._api_fail_streak = 0
                if self._api_paused:
                    # API 복구 → 매수 재개
                    self._engine.resume_buying()
                    self._api_paused = False
                    logger.info("api_health_restored", exchange=self._exchange_name)
                    await emit_event(
                        "info", "health",
                        f"API 복구 — 매수 재개 [{self._exchange_name}]",
                        metadata={"exchange": self._exchange_name},
                    )
                return HealthCheckResult(
                    name="api_health", healthy=True,
                    detail=f"BTC={ticker.last}",
                )
        except Exception as e:
            self._api_fail_streak += 1
            logger.warning(
                "api_health_fail",
                streak=self._api_fail_streak,
                error=str(e),
                exchange=self._exchange_name,
            )

        if self._api_fail_streak >= 3 and not self._api_paused:
            self._engine.pause_buying(self._tracked_coins)
            self._api_paused = True
            logger.critical(
                "api_health_pause_buying",
                streak=self._api_fail_streak,
                exchange=self._exchange_name,
            )
            await emit_event(
                "critical", "health",
                f"API 3회 연속 실패 — 매수 일시중지 [{self._exchange_name}]",
                metadata={
                    "exchange": self._exchange_name,
                    "fail_streak": self._api_fail_streak,
                },
            )

        return HealthCheckResult(
            name="api_health",
            healthy=False,
            detail=f"연속 실패 {self._api_fail_streak}회",
        )

    # ── 4. 에러 추세 ─────────────────────────────────────────

    def _check_error_rate_trend(self) -> HealthCheckResult:
        """eval_error_counts가 증가 추세인지 확인."""
        counts = self._engine._eval_error_counts
        if not counts:
            return HealthCheckResult(
                name="error_rate", healthy=True, detail="에러 없음",
            )

        total = sum(counts.values())
        high_error_coins = [sym for sym, c in counts.items() if c >= 2]

        if high_error_coins:
            return HealthCheckResult(
                name="error_rate",
                healthy=False,
                detail=f"연속 에러 코인: {', '.join(high_error_coins)} (합계 {total})",
            )

        return HealthCheckResult(
            name="error_rate", healthy=True,
            detail=f"에러 합계 {total} (경미)",
        )

    # ── 5. 멈춘 포지션 ──────────────────────────────────────

    async def _check_stuck_positions(self, session: AsyncSession) -> HealthCheckResult:
        """30분+ updated_at 미갱신 포지션 감지."""
        threshold = datetime.now(timezone.utc) - timedelta(minutes=30)

        result = await session.execute(
            select(Position).where(
                Position.quantity > 0,
                Position.exchange == self._exchange_name,
                Position.updated_at < threshold,
            )
        )
        stuck = list(result.scalars().all())

        if stuck:
            symbols = [p.symbol for p in stuck]
            ages = []
            now = datetime.now(timezone.utc)
            for p in stuck:
                if p.updated_at:
                    mins = (now - p.updated_at).total_seconds() / 60
                    ages.append(f"{p.symbol}({mins:.0f}분)")
                else:
                    ages.append(f"{p.symbol}(updated_at없음)")

            return HealthCheckResult(
                name="stuck_positions",
                healthy=False,
                detail=f"30분+ 미갱신: {', '.join(ages)}",
            )

        return HealthCheckResult(
            name="stuck_positions", healthy=True,
            detail="멈춘 포지션 없음",
        )
