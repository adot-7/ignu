"""Shared offline fixtures for the foundation and later lane tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The repository intentionally has no packaging metadata yet.  Make the
# checkout importable both via ``python -m pytest`` and the standalone pytest
# executable used by the hackathon environment.
sys.path.insert(0, str(Path(__file__).parents[1]))

from app import db, llm
from app.config import get_settings


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use an isolated SQLite file for each test that needs persistence."""

    database_path = tmp_path / "ignu-test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    llm.reset_client()
    db.dispose_engine()
    yield database_path
    db.dispose_engine()
    get_settings.cache_clear()
    llm.reset_client()


@pytest.fixture
def settings(tmp_db: Path):
    return get_settings()


@pytest.fixture
def sample_registrations_path() -> Path:
    return Path(__file__).parents[1] / "data" / "sample_registrations.csv"


@pytest.fixture
def sample_prev_event_path() -> Path:
    return Path(__file__).parents[1] / "data" / "sample_prev_event.csv"
