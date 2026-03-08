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
    engine,
    coordinator,
    portfolio_manager,
    config,
    session_factory,
) -> TradingScheduler:
    """
    Create and configure the trading scheduler.

    Scheduled jobs:
    - market_analysis:  every 15 min (AI agent)
    - risk_check:       every 5 min (risk agent)
    - trade_review:     every 1 hour

    NOTE: evaluation_cycle은 engine.start() 루프에서 직접 실행.
    스케줄러에서 중복 등록하지 않음.
    """
    scheduler = TradingScheduler()

    # Market analysis agent
    scheduler.add_job(
        _wrap(coordinator.run_market_analysis),
        name="market_analysis",
        seconds=900,  # 15 minutes
    )

    # Risk management agent
    async def risk_check():
        await coordinator.run_risk_evaluation(portfolio_manager.cash_balance)

    scheduler.add_job(
        _wrap(risk_check),
        name="risk_check",
        seconds=300,  # 5 minutes
    )

    # Trade review: 매도 5회마다 엔진에서 직접 트리거 (engine._on_sell_completed)
    # 시간 기반 스케줄러 제거 → 매매 없으면 안 돌고, 매매 많으면 자주 돔

    # 서버 이벤트 7일 자동 정리 (24시간마다)
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
        seconds=86400,  # 24 hours
    )

    # LLM 기반 일일 매매 회고 (24시간 간격)
    from config import get_config
    llm_config = get_config().llm
    if llm_config.enabled and llm_config.daily_review_enabled and llm_config.api_key:
        scheduler.add_job(
            _wrap(coordinator.run_trade_review),
            name="daily_llm_review",
            seconds=86400,  # 24 hours
        )
        logger.info("daily_llm_review_scheduled")

    # 성과 분석 에이전트 (매일 12:30 UTC = 21:30 KST)
    scheduler.add_cron_job(
        _wrap(coordinator.run_performance_analysis),
        name="performance_analytics",
        hour=12, minute=30,
    )

    # 전략 어드바이저 (매주 일요일 13:00 UTC = 22:00 KST)
    scheduler.add_weekly_cron_job(
        _wrap(coordinator.run_strategy_advice),
        name="strategy_advice",
        day_of_week="sun", hour=13, minute=0,
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
