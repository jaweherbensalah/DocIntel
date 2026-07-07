"""Synchronous database access for the Celery worker.

FastAPI uses an async engine (asyncpg / aiosqlite). Celery tasks run in plain
synchronous worker processes, so we give them a matching *sync* engine derived
from the same ``DATABASE_URL``.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings


def _sync_url(url: str) -> str:
    # Translate the async driver URL into its synchronous equivalent.
    return url.replace("+asyncpg", "+psycopg2").replace("+aiosqlite", "")


_url = _sync_url(settings.database_url)
# SQLite (used in tests) benefits from a busy timeout so a concurrent writer
# waits briefly for a lock instead of failing immediately.
_connect_args = {"timeout": 30} if _url.startswith("sqlite") else {}

sync_engine = create_engine(_url, future=True, connect_args=_connect_args)
SyncSessionLocal = sessionmaker(sync_engine, expire_on_commit=False)
