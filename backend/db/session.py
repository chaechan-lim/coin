from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from config import get_config

_engine = None
_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        config = get_config()
        kwargs = {"echo": config.database.echo}
        # SQLite는 connection pool 미지원
        if "sqlite" not in config.database.url:
            kwargs.update(pool_size=10, max_overflow=20, pool_pre_ping=True)
        _engine = create_async_engine(config.database.url, **kwargs)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def get_db() -> AsyncSession:
    """FastAPI dependency for database sessions."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
