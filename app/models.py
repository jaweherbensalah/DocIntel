from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    JSON,
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
