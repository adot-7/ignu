from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event as ThreadEvent

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db, main, pipeline, profile, rank
from app.config import load_mapping
from app.github_evidence import GitHubResult
from app.models import Evidence, RepoEvidence

ROOT = Path(__file__).parents[1]


def _repo(login: str) -> RepoEvidence:
    return RepoEvidence(
        full_name=f"{login}/ai-project",
        html_url=f"https://github.com/{login}/ai-project",
        is_fork=False,
        stars=1,
        author_commits_90d=12,
        author_commits_total=24,
        primary_language="Python",
        last_push=datetime.now(timezone.utc),
        description="A small RAG service",
        topics=["ai", "rag"],
        readme_excerpt="A retrieval augmented generation service.",
    )


def _offline_adapters(monkeypatch, *, failing_login: str | None = None):
    github_calls: list[str] = []
    llm_calls: list[str] = []

    def fake_fetch(login: str, **_kwargs):
        github_calls.append(login)
        if login == failing_login:
            raise RuntimeError("recorded transport failure")
        return GitHubResult(
            login=login,
            status="ok",
            repos=[_repo(login)],
            fetched_at=datetime.now(timezone.utc),
        )

    def fake_structured(prompt, schema, *, tier, cache_key):
        del tier, cache_key
        llm_calls.append(prompt)
        tag = "[repo]" if '"repositories": [' in prompt else "[registration]"
        return schema(
            skills=[],
            ai_relevance=0.2 if tag == "[repo]" else 0.0,
            summary=f"Recorded evidence is available. {tag}",
            evidence=[
                Evidence(
                    claim="A recorded project was observed.",
                    source_url="https://github.com/example/project",
                    kind="repo",
                    confidence=0.8,
                    observed_at=datetime.now(timezone.utc).date(),
                )
            ]
            if tag == "[repo]"
            else [],
        )

    monkeypatch.setattr(pipeline.github_evidence, "fetch", fake_fetch)
    monkeypatch.setattr(profile.llm, "structured", fake_structured)
    monkeypatch.setenv("GRAPH_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    return github_calls, llm_calls


def test_pipeline_reuses_source_and_profile_cache(tmp_db, monkeypatch):
    github_calls, llm_calls = _offline_adapters(monkeypatch)
    mapping = load_mapping(ROOT / "mapping.sample.yaml")
    source = ROOT / "data" / "sample_registrations.csv"

    first = pipeline.run(source, mapping, "pipeline-cache-test")
    first_verdicts = [
        (item.person_id, item.decision, item.score, item.baseline_rank, item.ignu_rank)
        for item in first["verdicts"]
    ]
    first_github_count = len(github_calls)
    first_llm_count = len(llm_calls)

    second = pipeline.run(source, mapping, "pipeline-cache-test")
    second_verdicts = [
        (item.person_id, item.decision, item.score, item.baseline_rank, item.ignu_rank)
        for item in second["verdicts"]
    ]

    assert first_github_count > 0
    assert first_llm_count > 0
    assert len(github_calls) == first_github_count
    assert len(llm_calls) == first_llm_count
    assert second_verdicts == first_verdicts
    assert second["ingest"].rows == first["ingest"].rows


def test_pipeline_degrades_one_person_and_keeps_ranking(tmp_db, monkeypatch):
    github_calls, _llm_calls = _offline_adapters(monkeypatch, failing_login="adot-7")
    mapping = load_mapping(ROOT / "mapping.sample.yaml")
    source = ROOT / "data" / "sample_registrations.csv"

    result = pipeline.run(source, mapping, "pipeline-degrade-test")

    assert "adot-7" in github_calls
    assert result["verdicts"]
    assert any(item["github_status"] == "error" for item in result["built"].values())
    with db.get_session() as session:
        events = list(session.scalars(select(db.EventRow)).all())
    assert any(
        event.stage == "github"
        and event.status == "error"
        and event.data
        and event.data.get("exception") == "RuntimeError"
        for event in events
    )


def test_reset_demo_removes_post_snapshot_note_and_verdict(tmp_db, monkeypatch):
    _offline_adapters(monkeypatch)
    mapping = load_mapping(ROOT / "mapping.sample.yaml")
    source = ROOT / "data" / "sample_registrations.csv"
    result = pipeline.run(source, mapping, "pipeline-reset-test")
    person_id = next(iter(result["built"]))
    before = next(item for item in result["verdicts"] if item.person_id == person_id)

    with db.get_session() as session:
        session.add(
            db.NoteRow(
                person_id=person_id,
                text="left after lunch",
                author="organizer",
                at=datetime.now(timezone.utc) + timedelta(seconds=1),
                kind="flag",
                source="chat",
            )
        )
    changed = rank.rerank_person(person_id)
    assert changed["new_decision"] == "needs_human"

    reset = pipeline.reset_demo("pipeline-reset-test")
    assert reset["status"] == "ok"
    with db.get_session() as session:
        notes = list(session.scalars(select(db.NoteRow)).all())
        verdicts = list(
            session.scalars(
                select(db.VerdictRow).where(db.VerdictRow.person_id == person_id)
            ).all()
        )
    assert notes == []
    assert verdicts[-1].decision == before.decision


def test_api_run_and_reset_routes_are_minimal_background_wiring(tmp_db, monkeypatch):
    started = ThreadEvent()
    calls: list[dict] = []

    def fake_run(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        started.set()

    monkeypatch.setattr(main.pipeline, "run", fake_run)
    monkeypatch.setattr(
        main.pipeline,
        "reset_demo",
        lambda event=None: {"status": "ok", "event": event or "default"},
    )
    with TestClient(main.app) as client:
        response = client.post("/api/run", json={"n": 15, "slow": 0})
        assert response.status_code == 202
        assert response.json()["status"] == "started"
        assert started.wait(timeout=2)
        reset = client.post("/api/reset", json={"event": "demo"})
        assert reset.status_code == 200
        assert reset.json() == {"status": "ok", "event": "demo"}
    assert calls
    assert calls[0]["kwargs"]["run_id"].startswith("api-")
