"""SQLAlchemy persistence for the frozen ignu contracts.

The application deliberately uses a small set of ordinary tables rather than
an ORM domain layer.  Pipeline modules can persist their Pydantic models as
JSON-backed records while retaining queryable identity, version, and timestamp
columns for the dashboard and later ranking stages.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .config import get_settings


class Base(DeclarativeBase):
    pass


class PersonRow(Base):
    __tablename__ = "persons"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    github_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    org: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_student: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    answers: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    alias_of: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_file: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    source_row: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ParticipationRow(Base):
    __tablename__ = "participations"

    person_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event: Mapped[str] = mapped_column(String(255), primary_key=True)
    registered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    approved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    checked_in: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    submitted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    placed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    team_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    at: Mapped[date | None] = mapped_column(Date, nullable=True)


class RepoRow(Base):
    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    person_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(500), nullable=False)
    html_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    is_fork: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    author_commits_90d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    author_commits_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    primary_language: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_push: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    topics: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    readme_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_relevance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class ProfileRow(Base):
    __tablename__ = "profiles"
    __table_args__ = (UniqueConstraint("person_id", "version", name="uq_profiles_person_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    person_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    skills: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    evidence_level: Mapped[str] = mapped_column(String(32), nullable=False)
    original_work_score: Mapped[float] = mapped_column(Float, nullable=False)
    ai_relevance: Mapped[float] = mapped_column(Float, nullable=False)
    reliability: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    model_used: Mapped[str] = mapped_column(String(255), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class VerdictRow(Base):
    __tablename__ = "verdicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    person_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    eligibility: Mapped[str] = mapped_column(String(32), nullable=False)
    baseline_score: Mapped[float] = mapped_column(Float, nullable=False)
    baseline_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    ignu_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    evidence_ids: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TeamRow(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event: Mapped[str] = mapped_column(String(255), nullable=False)
    member_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    coverage: Mapped[dict[str, float]] = mapped_column(JSON, default=dict, nullable=False)
    balance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    why: Mapped[str] = mapped_column(Text, nullable=False, default="")


class NoteRow(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    person_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(String(255), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    person_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    msg: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class LLMUsageRow(Base):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    tier: Mapped[str] = mapped_column(String(32), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cache_key: Mapped[str | None] = mapped_column(String(255), nullable=True)


_engine: Engine | None = None
_engine_url: str | None = None


def _database_url() -> str:
    """Read the URL lazily so test settings can be installed before first use."""

    return os.environ.get("DATABASE_URL") or get_settings().database_url


def _ensure_sqlite_parent(url: str) -> None:
    if not url.startswith("sqlite:///") or url.startswith("sqlite:///:memory:"):
        return
    raw_path = url.removeprefix("sqlite:///")
    if raw_path.startswith("/"):
        path = Path(raw_path)
    else:
        path = Path(raw_path)
    if str(path) and str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)


def get_engine() -> Engine:
    global _engine, _engine_url

    url = _database_url()
    if _engine is None or _engine_url != url:
        if _engine is not None:
            _engine.dispose()
        _ensure_sqlite_parent(url)
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
        _engine_url = url
    return _engine


def init_db() -> None:
    """Create all foundation tables, idempotently."""

    Base.metadata.create_all(bind=get_engine())


@contextmanager
def get_session() -> Iterator[Session]:
    """Yield a transaction-scoped SQLAlchemy session and commit on success."""

    session = Session(get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_engine() -> None:
    """Dispose the cached engine (primarily useful for isolated test databases)."""

    global _engine, _engine_url
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_url = None


# Named exports make the table layer easy to use from later lanes and from
# small scripts without requiring callers to know the concrete ORM class names.
__all__ = [
    "Base",
    "EventRow",
    "LLMUsageRow",
    "NoteRow",
    "ParticipationRow",
    "PersonRow",
    "ProfileRow",
    "RepoRow",
    "TeamRow",
    "VerdictRow",
    "dispose_engine",
    "get_engine",
    "get_session",
    "init_db",
]
