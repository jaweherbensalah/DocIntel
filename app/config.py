from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    debug: bool = True
    cors_origins: List[str] = ["*"]

    # Auth
    api_key: str = "dev-secret-key-change-me"

    # LLM
    llm_provider: str = "fake"  # "fake" | "openai"
    llm_latency: float = 2.0  # seconds the fake model "thinks" for
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # Storage
    database_url: str = "postgresql+asyncpg://docintel:docintel@localhost:5432/docintel"


settings = Settings()
