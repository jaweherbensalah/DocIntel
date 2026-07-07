from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(settings.database_url, echo=settings.debug)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a fresh session per request, closed when the request finishes.

    A single shared session (the previous approach) returns stale, cached rows
    and is not safe under concurrency — which also made background status
    updates invisible to later reads.
    """
    async with SessionLocal() as session:
        yield session
