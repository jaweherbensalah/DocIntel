from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    debug: bool = False
    # No cross-origin browser access unless explicitly configured per environment.
    cors_origins: List[str] = []

    # Auth — REQUIRED. There is deliberately no default so the app refuses to
    # start unless an API key is supplied via the environment (fail closed).
    api_key: str

    # LLM
    llm_provider: str = "fake"  # "fake" | "openai"
    llm_latency: float = 2.0  # seconds the fake model "thinks" for
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # Storage
    database_url: str = "postgresql+asyncpg://docintel:docintel@localhost:5432/docintel"

    # Task queue (Celery broker + result backend)
    redis_url: str = "redis://localhost:6379/0"


settings = Settings()
