from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Result(Base):
    __tablename__ = "results"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # "extract" | "match"
    payload: Mapped[str] = mapped_column(Text)  # JSON blob
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
