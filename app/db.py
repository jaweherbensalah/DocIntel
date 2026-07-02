from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(settings.database_url, echo=settings.debug)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# One session for the whole app. Simple and it works fine in dev.
session: AsyncSession = SessionLocal()
