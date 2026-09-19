from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest

from app import baseline
from app.config import load_scoring
from app.models import RepoEvidence


@pytest.fixture
def repo() -> Callable[..., RepoEvidence]:
    def build(
        name: str,
        *,
        is_fork: bool = False,
        description: str | None = None,
        topics: list[str] | None = None,
    ) -> RepoEvidence:
        return RepoEvidence(
            full_name=f"builder/{name}",
            html_url=f"https://github.com/builder/{name}",
            is_fork=is_fork,
            stars=0,
            author_commits_90d=0,
            author_commits_total=0,
            primary_language="Python",
            last_push=None,
            description=description,
            topics=topics or [],
            readme_excerpt=None,
        )

    return build


@pytest.fixture
def quiet_events(monkeypatch: pytest.MonkeyPatch) -> list:
    emitted = []
    monkeypatch.setattr(baseline.events, "emit", emitted.append)
    return emitted


def test_baseline_counts_forks_from_active_config(repo, quiet_events) -> None:
    repos = [repo(f"ai-fork-{number}", is_fork=True) for number in range(6)]
    count_forks = SimpleNamespace(
        baseline=SimpleNamespace(
            keywords=["ai"], fields=["name"], count_forks=True
        )
    )
    ignore_forks = SimpleNamespace(
        baseline=SimpleNamespace(
            keywords=["ai"], fields=["name"], count_forks=False
        )
    )

    assert baseline.baseline_score(repos, count_forks) == 6
    assert baseline.baseline_score(repos, ignore_forks) == 0
    assert len(quiet_events) == 2


def test_baseline_uses_configured_fields_and_real_scoring_keywords(repo, quiet_events) -> None:
    scoring = load_scoring("scoring.yaml")
    quiet_builder = repo("quiet-builder", description="A RAG pipeline")
    topic_only = repo("unrelated", description="ordinary tooling", topics=["GenAI"])

    assert baseline.baseline_score([quiet_builder], scoring) == 1
    assert baseline.baseline_score([topic_only], scoring) == 1


def test_baseline_rank_breaks_ties_by_person_id(quiet_events) -> None:
    scores = {"z-person": 2.0, "a-person": 2.0, "m-person": 3.0}

    assert baseline.baseline_rank(scores) == {
        "m-person": 1,
        "a-person": 2,
        "z-person": 3,
    }


def test_baseline_docstring_identifies_comparison_reimplementation() -> None:
    assert "organizer's keyword-count script" in (baseline.__doc__ or "")
    assert "comparison" in (baseline.__doc__ or "")
