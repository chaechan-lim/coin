import asyncio
import structlog
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
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

    def start(self) -> None:
        self._scheduler.start()
        logger.info("scheduler_started", jobs=list(self._jobs.keys()))

    def shutdown(self, wait: bool = True) -> None:
        self._scheduler.shutdown(wait=wait)
        logger.info("scheduler_stopped")

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

    # Trade review agent (매시간)
    scheduler.add_job(
        _wrap(coordinator.run_trade_review),
        name="trade_review",
        seconds=3600,  # 1 hour
    )

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

    return scheduler


def _wrap(coro_func):
    """Wrap async function for APScheduler (handles exceptions gracefully)."""
    async def wrapped():
        try:
            await coro_func()
        except Exception as e:
            logger.error("scheduled_job_error", func=coro_func.__name__, error=str(e), exc_info=True)
    return wrapped
