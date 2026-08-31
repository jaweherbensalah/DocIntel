from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSONB on Postgres, plain JSON on SQLite.
JsonColumn = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    pass


class Result(Base):
    """The job record: one row per API call that produces a result."""

    __tablename__ = "results"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # "extract" | "match" | "batch_match"
    status: Mapped[str] = mapped_column(String(16), default="done")
    # Legacy blob, still dual-written. Dropped by the contract migration.
    payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    profile: Mapped[Optional["Profile"]] = relationship(
        back_populates="result", cascade="all, delete-orphan", uselist=False
    )
    matches: Mapped[List["Match"]] = relationship(
        back_populates="result", cascade="all, delete-orphan"
    )


class Profile(Base):
    """A candidate profile extracted from a CV; one row per extract result."""

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    result_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("results.id", ondelete="CASCADE"), unique=True
    )
    name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    years_experience: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    result: Mapped["Result"] = relationship(back_populates="profile")
    skills: Mapped[List["ProfileSkill"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_profiles_years_experience", "years_experience"),)


class ProfileSkill(Base):
    """One skill of one profile; a row per skill so it can be indexed."""

    __tablename__ = "profile_skills"

    profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True
    )
    skill: Mapped[str] = mapped_column(String(64), primary_key=True)

    profile: Mapped["Profile"] = relationship(back_populates="skills")

    __table_args__ = (Index("ix_profile_skills_skill", "skill"),)


class Match(Base):
    """One scored candidate. A /match produces one row, /batch-match many."""

    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    result_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("results.id", ondelete="CASCADE"), index=True
    )
    # Position in a batch shortlist; null for a single /match.
    rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    candidate_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    job_description: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    matched_skills: Mapped[Optional[list]] = mapped_column(JsonColumn, nullable=True)
    missing_skills: Mapped[Optional[list]] = mapped_column(JsonColumn, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    result: Mapped["Result"] = relationship(back_populates="matches")

    __table_args__ = (Index("ix_matches_score", "score"),)


class Client(Base):
    """An API consumer, with the budget and rate limit they were sold."""

    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    # SHA-256 of the key. Keys are high-entropy random, so a fast hash is
    # appropriate here; a password KDF would not be.
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    monthly_budget_cents: Mapped[int] = mapped_column(Integer, default=0)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=60)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class BudgetPeriod(Base):
    """Money committed and money spent, per client per calendar month."""

    __tablename__ = "budget_periods"

    client_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True
    )
    period: Mapped[str] = mapped_column(String(7), primary_key=True)  # "YYYY-MM"
    # In flight: reserved before the model runs, cleared when it settles.
    reserved_cents: Mapped[int] = mapped_column(Integer, default=0)
    spent_cents: Mapped[int] = mapped_column(Integer, default=0)


class UsageEvent(Base):
    """One charge. Gives settlement something idempotent to key off."""

    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    client_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    period: Mapped[str] = mapped_column(String(7))
    operation: Mapped[str] = mapped_column(String(16))
    estimated_cents: Mapped[int] = mapped_column(Integer, default=0)
    actual_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="reserved")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
