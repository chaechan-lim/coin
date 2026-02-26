from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from config import get_config

_engine = None
_session_factory = None


def _set_sqlite_pragmas(dbapi_conn, connection_record):
    """SQLite 연결 시 WAL 모드 + busy_timeout 설정."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def get_engine():
    global _engine
    if _engine is None:
        config = get_config()
        kwargs = {"echo": config.database.echo}
        if "sqlite" in config.database.url:
            # SQLite: WAL 모드로 동시 읽기/쓰기 지원
            pass
        else:
            kwargs.update(pool_size=10, max_overflow=20, pool_pre_ping=True)
        _engine = create_async_engine(config.database.url, **kwargs)
        # SQLite인 경우 WAL pragma 적용
        if "sqlite" in config.database.url:
            event.listen(_engine.sync_engine, "connect", _set_sqlite_pragmas)
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
