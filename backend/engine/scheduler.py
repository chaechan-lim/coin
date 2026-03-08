import asyncio
import structlog
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import delete

logger = structlog.get_logger(__name__)


class TradingScheduler:
    """APScheduler-based task scheduler for periodic trading operations."""

    def __init__(self):
        self._scheduler = AsyncIOScheduler()
        self._jobs: dict[str, str] = {}  # name -> job_id

    def add_job(self, func, name: str, seconds: int, **kwargs) -> None:
        """Add a periodic job."""
        job = self._scheduler.add_job(
            func,
            trigger=IntervalTrigger(seconds=seconds),
            id=name,
            name=name,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            **kwargs,
        )
        self._jobs[name] = job.id
        logger.info("scheduler_job_added", name=name, interval_sec=seconds)

    def add_cron_job(self, func, name: str, hour: int, minute: int = 0, **kwargs) -> None:
        """Add a daily cron job (UTC)."""
        job = self._scheduler.add_job(
            func,
            trigger=CronTrigger(hour=hour, minute=minute, timezone="UTC"),
            id=name,
            name=name,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            **kwargs,
        )
        self._jobs[name] = job.id
        logger.info("scheduler_cron_job_added", name=name, hour=hour, minute=minute)

    def start(self) -> None:
        self._scheduler.start()
        logger.info("scheduler_started", jobs=list(self._jobs.keys()))

    def shutdown(self, wait: bool = True) -> None:
        self._scheduler.shutdown(wait=wait)
        logger.info("scheduler_stopped")

    def add_weekly_cron_job(self, func, name: str, day_of_week: str, hour: int, minute: int = 0, **kwargs) -> None:
        """Add a weekly cron job (UTC)."""
        job = self._scheduler.add_job(
            func,
            trigger=CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute, timezone="UTC"),
            id=name,
            name=name,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            **kwargs,
        )
        self._jobs[name] = job.id
        logger.info("scheduler_weekly_cron_added", name=name, day=day_of_week, hour=hour, minute=minute)

    def pause_job(self, name: str) -> None:
        if name in self._jobs:
            self._scheduler.pause_job(name)

    def resume_job(self, name: str) -> None:
        if name in self._jobs:
            self._scheduler.resume_job(name)

    @property
    def running(self) -> bool:
        return self._scheduler.running


def setup_scheduler(
    config,
    session_factory,
    coordinator=None,
    portfolio_manager=None,
) -> TradingScheduler:
    """
    Create and configure the trading scheduler.

    coordinator/portfolio_manager가 None이면 빗썸 관련 잡 생략.
    """
    scheduler = TradingScheduler()

    # 빗썸 관련 잡 (coordinator가 있을 때만)
    if coordinator:
        scheduler.add_job(
            _wrap(coordinator.run_market_analysis),
            name="market_analysis",
            seconds=900,
        )

        if portfolio_manager:
            async def risk_check():
                await coordinator.run_risk_evaluation(portfolio_manager.cash_balance)
            scheduler.add_job(
                _wrap(risk_check),
                name="risk_check",
                seconds=300,
            )

        from config import get_config
        llm_config = get_config().llm
        if llm_config.enabled and llm_config.daily_review_enabled and llm_config.api_key:
            scheduler.add_job(
                _wrap(coordinator.run_trade_review),
                name="daily_llm_review",
                seconds=86400,
            )
            logger.info("daily_llm_review_scheduled")

        scheduler.add_cron_job(
            _wrap(coordinator.run_performance_analysis),
            name="performance_analytics",
            hour=12, minute=30,
        )

        scheduler.add_weekly_cron_job(
            _wrap(coordinator.run_strategy_advice),
            name="strategy_advice",
            day_of_week="sun", hour=13, minute=0,
        )

    # 서버 이벤트 7일 자동 정리 (24시간마다) — 공통
    async def cleanup_old_events():
        from core.models import ServerEvent
        from db.session import get_session_factory
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        sf = get_session_factory()
        async with sf() as session:
            await session.execute(delete(ServerEvent).where(ServerEvent.created_at < cutoff))
            await session.commit()
        logger.info("old_events_cleaned", cutoff=cutoff.isoformat())

    scheduler.add_job(
        _wrap(cleanup_old_events),
        name="event_cleanup",
        seconds=86400,
    )

    # DB 테이블 정리 (매일 04:00 UTC = 13:00 KST) — 디스크 고갈 방지
    async def cleanup_old_data():
        from core.models import (
            Order, StrategyLog, PortfolioSnapshot, AgentAnalysisLog,
        )
        from db.session import get_session_factory
        sf = get_session_factory()
        retention = [
            (StrategyLog, "logged_at", 30),         # 전략 로그 30일
            (PortfolioSnapshot, "snapshot_at", 60),  # 포트폴리오 스냅샷 60일
            (AgentAnalysisLog, "analyzed_at", 60),   # 에이전트 분석 60일
            (Order, "created_at", 90),               # 주문 기록 90일
        ]
        total_deleted = 0
        for model, ts_field, days in retention:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            ts_col = getattr(model, ts_field, None)
            if ts_col is None:
                continue
            try:
                async with sf() as session:
                    result = await session.execute(
                        delete(model).where(ts_col < cutoff)
                    )
                    deleted = result.rowcount
                    total_deleted += deleted
                    await session.commit()
                    if deleted:
                        logger.info("db_cleanup", table=model.__tablename__, deleted=deleted, retention_days=days)
            except Exception as e:
                logger.warning("db_cleanup_failed", table=model.__tablename__, error=str(e))
        if total_deleted:
            # VACUUM은 별도 커넥션 (autocommit) 필요 — 주간 수동 권장
            logger.info("db_cleanup_complete", total_deleted=total_deleted)

    scheduler.add_cron_job(
        _wrap(cleanup_old_data),
        name="db_data_cleanup",
        hour=4, minute=0,  # 04:00 UTC = 13:00 KST
    )

    return scheduler


def _wrap(coro_func):
    """Wrap async function for APScheduler (handles exceptions gracefully)."""
    async def wrapped():
        try:
            await coro_func()
        except Exception as e:
            logger.error("scheduled_job_error", func=coro_func.__name__, error=str(e), exc_info=True)
    return wrapped
