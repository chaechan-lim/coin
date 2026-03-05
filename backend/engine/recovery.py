"""
복구 매니저 — 분류된 에러에 대해 자동 복구 시도

| 카테고리   | 복구 액션                        |
|-----------|--------------------------------|
| TRANSIENT | 지수 백오프 대기                  |
| RESOURCE  | reconcile_cash → sync_exchange |
| STATE     | sync_positions                 |
| PERMANENT | suppress_coin + critical 알림  |
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from core.error_classifier import ClassifiedError, ErrorCategory
from core.event_bus import emit_event

logger = structlog.get_logger(__name__)


@dataclass
class RecoveryResult:
    resolved: bool
    action_taken: str
    detail: str


class RecoveryManager:
    """에러 분류 기반 자동 복구.

    Parameters
    ----------
    engine : TradingEngine
        pause_buying / suppress_buys 호출용
    portfolio_manager : PortfolioManager
        reconcile_cash / sync_exchange 호출용
    exchange_adapter : ExchangeAdapter
        sync_exchange_positions 에 전달
    exchange_name : str
        이벤트 메타데이터 + 로깅용
    tracked_coins : list[str]
        sync_exchange_positions 에 전달
    """

    MAX_DAILY_RECOVERIES = 10

    def __init__(
        self,
        engine,
        portfolio_manager,
        exchange_adapter,
        exchange_name: str,
        tracked_coins: list[str],
    ):
        self._engine = engine
        self._pm = portfolio_manager
        self._exchange = exchange_adapter
        self._exchange_name = exchange_name
        self._tracked_coins = tracked_coins

        # 일일 쓰로틀: (symbol:context) → count
        self._recovery_counts: dict[str, int] = {}
        self._reset_date = datetime.now(timezone.utc).date()

        # LLM 진단 에이전트 (set_diagnostic_agent로 주입)
        self._diagnostic_agent = None

    # ── public API ───────────────────────────────────────────

    def set_diagnostic_agent(self, agent) -> None:
        """LLM DiagnosticAgent 주입."""
        self._diagnostic_agent = agent

    async def attempt_recovery(self, classified: ClassifiedError) -> RecoveryResult:
        """분류된 에러에 대해 복구 시도. 실패 시 LLM 에스컬레이션."""
        self._maybe_reset_daily()

        key = f"{classified.symbol or 'global'}:{classified.context}"
        count = self._recovery_counts.get(key, 0)
        if count >= self.MAX_DAILY_RECOVERIES:
            logger.warning(
                "recovery_throttled", key=key, count=count,
                exchange=self._exchange_name,
            )
            return RecoveryResult(
                resolved=False,
                action_taken="throttled",
                detail=f"일일 복구 한도 초과 ({count}/{self.MAX_DAILY_RECOVERIES})",
            )
        self._recovery_counts[key] = count + 1

        # 1차: 규칙 기반 복구
        match classified.category:
            case ErrorCategory.TRANSIENT:
                result = await self._recover_transient(classified)
            case ErrorCategory.RESOURCE:
                result = await self._recover_resource(classified)
            case ErrorCategory.STATE:
                result = await self._recover_state(classified)
            case ErrorCategory.PERMANENT:
                result = await self._recover_permanent(classified)
            case _:
                result = RecoveryResult(
                    resolved=False,
                    action_taken="unknown_category",
                    detail=str(classified.category),
                )

        # 2차: 규칙 실패 + LLM 활성 → LLM 진단 에스컬레이션
        if not result.resolved and self._diagnostic_agent:
            try:
                portfolio_state = {
                    "cash": round(self._pm.cash_balance, 2),
                }
                diag = await self._diagnostic_agent.diagnose_and_recover(
                    error=classified.original,
                    context=classified.context,
                    symbol=classified.symbol,
                    rule_based_result=f"{result.action_taken}: {result.detail}",
                    portfolio_state=portfolio_state,
                )
                if diag.action_executed:
                    logger.info(
                        "llm_diagnostic_recovery",
                        action=diag.suggested_action,
                        diagnosis=diag.diagnosis[:100],
                        exchange=self._exchange_name,
                    )
                    return RecoveryResult(
                        resolved=True,
                        action_taken=f"llm:{diag.suggested_action}",
                        detail=f"LLM 진단: {diag.diagnosis}",
                    )
            except Exception as e:
                logger.warning("llm_diagnostic_failed", error=str(e),
                               exchange=self._exchange_name)

        return result

    def reset_daily(self) -> None:
        """일일 카운터 리셋 (엔진 _reset_daily_counter에서 호출)."""
        self._recovery_counts.clear()
        self._reset_date = datetime.now(timezone.utc).date()

    # ── private recovery handlers ────────────────────────────

    async def _recover_transient(self, classified: ClassifiedError) -> RecoveryResult:
        """TRANSIENT: 지수 백오프 대기 후 재시도 허용."""
        # 실제 대기는 _execute_with_retry에서 수행
        # 여기서는 복구 가능 여부만 판단
        logger.info(
            "recovery_transient",
            symbol=classified.symbol,
            context=classified.context,
            backoff=classified.backoff_base,
            exchange=self._exchange_name,
        )
        return RecoveryResult(
            resolved=True,
            action_taken="backoff_wait",
            detail=f"백오프 {classified.backoff_base}초 후 재시도 허용",
        )

    async def _recover_resource(self, classified: ClassifiedError) -> RecoveryResult:
        """RESOURCE: 잔고 재계산 → 거래소 동기화."""
        from db.session import get_session_factory

        sf = get_session_factory()
        try:
            async with sf() as session:
                old_cash = self._pm.cash_balance

                # 1차: DB 기반 reconcile
                await self._pm.reconcile_cash_from_db(session)

                if self._pm.cash_balance > old_cash:
                    logger.info(
                        "recovery_resource_reconciled",
                        old=round(old_cash, 2),
                        new=round(self._pm.cash_balance, 2),
                        exchange=self._exchange_name,
                    )
                    await emit_event(
                        "info", "recovery",
                        f"잔고 복구 완료 [{self._exchange_name}]",
                        detail=f"reconcile: {old_cash:.2f} → {self._pm.cash_balance:.2f}",
                        metadata={
                            "exchange": self._exchange_name,
                            "action": "reconcile_cash",
                            "old_cash": round(old_cash, 2),
                            "new_cash": round(self._pm.cash_balance, 2),
                        },
                    )
                    return RecoveryResult(
                        resolved=True,
                        action_taken="reconcile_cash",
                        detail=f"잔고 {old_cash:.2f} → {self._pm.cash_balance:.2f}",
                    )

                # 2차: 거래소 동기화
                await self._pm.sync_exchange_positions(
                    session, self._exchange, self._tracked_coins,
                )
                new_cash = self._pm.cash_balance

                if new_cash > old_cash:
                    logger.info(
                        "recovery_resource_synced",
                        old=round(old_cash, 2),
                        new=round(new_cash, 2),
                        exchange=self._exchange_name,
                    )
                    await emit_event(
                        "info", "recovery",
                        f"거래소 동기화 복구 [{self._exchange_name}]",
                        detail=f"sync: {old_cash:.2f} → {new_cash:.2f}",
                        metadata={
                            "exchange": self._exchange_name,
                            "action": "sync_exchange",
                            "old_cash": round(old_cash, 2),
                            "new_cash": round(new_cash, 2),
                        },
                    )
                    return RecoveryResult(
                        resolved=True,
                        action_taken="sync_exchange",
                        detail=f"거래소 동기화 잔고 {old_cash:.2f} → {new_cash:.2f}",
                    )

                return RecoveryResult(
                    resolved=False,
                    action_taken="reconcile_cash+sync_exchange",
                    detail=f"잔고 여전히 부족: {new_cash:.2f}",
                )
        except Exception as e:
            logger.error(
                "recovery_resource_failed", error=str(e),
                exchange=self._exchange_name,
            )
            return RecoveryResult(
                resolved=False,
                action_taken="reconcile_failed",
                detail=str(e),
            )

    async def _recover_state(self, classified: ClassifiedError) -> RecoveryResult:
        """STATE: 거래소 포지션 동기화 → DB 수정."""
        from db.session import get_session_factory

        sf = get_session_factory()
        try:
            async with sf() as session:
                await self._pm.sync_exchange_positions(
                    session, self._exchange, self._tracked_coins,
                )
            logger.info(
                "recovery_state_synced",
                symbol=classified.symbol,
                exchange=self._exchange_name,
            )
            await emit_event(
                "info", "recovery",
                f"포지션 동기화 복구 [{self._exchange_name}]",
                detail=f"sync_positions: {classified.symbol}",
                metadata={
                    "exchange": self._exchange_name,
                    "action": "sync_positions",
                    "symbol": classified.symbol,
                },
            )
            return RecoveryResult(
                resolved=True,
                action_taken="sync_positions",
                detail=f"포지션 동기화 완료: {classified.symbol}",
            )
        except Exception as e:
            logger.error(
                "recovery_state_failed", error=str(e),
                symbol=classified.symbol, exchange=self._exchange_name,
            )
            return RecoveryResult(
                resolved=False,
                action_taken="sync_positions_failed",
                detail=str(e),
            )

    async def _recover_permanent(self, classified: ClassifiedError) -> RecoveryResult:
        """PERMANENT: 코인 억제 + critical 알림."""
        symbol = classified.symbol
        if symbol:
            self._engine.suppress_buys([symbol])
            logger.critical(
                "recovery_permanent_suppress",
                symbol=symbol,
                error=str(classified.original),
                exchange=self._exchange_name,
            )
            await emit_event(
                "critical", "health",
                f"코인 영구 억제: {symbol} [{self._exchange_name}]",
                detail=f"사유: {classified.original}",
                metadata={
                    "exchange": self._exchange_name,
                    "action": "suppress_coin",
                    "symbol": symbol,
                    "error": str(classified.original),
                },
            )
            return RecoveryResult(
                resolved=True,
                action_taken="suppress_coin",
                detail=f"{symbol} 매수 억제됨",
            )
        return RecoveryResult(
            resolved=False,
            action_taken="no_symbol",
            detail="심볼 없어 억제 불가",
        )

    # ── helper ───────────────────────────────────────────────

    def _maybe_reset_daily(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._reset_date:
            self.reset_daily()
