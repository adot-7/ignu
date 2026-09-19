from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import inspect

from app import db, llm
from app.config import load_mapping, load_scoring
from app.main import app
from app.models import (
    Evidence,
    Note,
    Participation,
    Person,
    PipelineEvent,
    Profile,
    RepoEvidence,
    SkillScore,
    Team,
    Verdict,
)


def test_models_round_trip() -> None:
    built_at = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
    person = Person(
        id="abc123",
        name="Ada Example",
        email="ada@example.org",
        email_domain="example.org",
        github_login="ada-example",
        linkedin_url=None,
        org="Example Org",
        role="Engineer",
        is_student=False,
        answers={"why": "Build useful tools"},
        registered_at=built_at,
        alias_of=None,
        source_file="sample.csv",
        source_row=1,
    )
    participation = Participation(
        person_id=person.id,
        event="demo",
        registered=True,
        approved=True,
        checked_in=True,
        submitted=True,
        placed=1,
        team_id="team-1",
        at=date(2026, 9, 19),
    )
    repo = RepoEvidence(
        full_name="ada-example/demo",
        html_url="https://github.com/ada-example/demo",
        is_fork=False,
        stars=2,
        author_commits_90d=4,
        author_commits_total=12,
        primary_language="Python",
        last_push=built_at,
        description="A demo",
        topics=["ai"],
        readme_excerpt="A useful project.",
    )
    evidence = Evidence(
        claim="Built a demo",
        source_url=repo.html_url,
        kind="repo",
        confidence=0.9,
        observed_at=date(2026, 9, 19),
    )
    profile = Profile(
        person_id=person.id,
        version=1,
        built_at=built_at,
        skills=[SkillScore(skill="backend", confidence=0.8)],
        evidence_level="solid",
        original_work_score=0.7,
        ai_relevance=0.5,
        reliability=1.0,
        summary="Built a demo.",
        evidence=[evidence],
        model_used="test",
        input_tokens=10,
        output_tokens=5,
    )
    verdict = Verdict(
        person_id=person.id,
        decision="admit",
        score=0.8,
        eligibility="pass",
        baseline_score=1.0,
        baseline_rank=1,
        ignu_rank=1,
        reasons=["original work"],
        evidence_ids=[1],
        profile_version=1,
        at=built_at,
    )
    team = Team(
        id="team-1",
        event="demo",
        member_ids=[person.id],
        coverage={"backend": 0.8},
        balance=0.8,
        why="Backend coverage.",
    )
    note = Note(
        id=None,
        person_id=person.id,
        text="Strong demo.",
        author="organizer",
        at=built_at,
        kind="praise",
        source="import",
    )
    event = PipelineEvent(
        ts=built_at,
        run_id="run-1",
        stage="profile",
        person_id=person.id,
        status="ok",
        msg="profile built",
        data={"version": 1},
    )

    for model in (person, participation, repo, evidence, profile, verdict, team, note, event):
        assert type(model).model_validate(model.model_dump()) == model


def test_init_db_creates_all_contract_tables(tmp_db) -> None:
    db.init_db()
    table_names = set(inspect(db.get_engine()).get_table_names())
    assert table_names == {
        "persons",
        "participations",
        "repos",
        "profiles",
        "verdicts",
        "teams",
        "notes",
        "events",
        "llm_usage",
    }


def test_yaml_loaders_are_typed() -> None:
    mapping = load_mapping("mapping.sample.yaml")
    scoring = load_scoring("scoring.yaml")
    assert mapping.columns.github.column == "GitHub"
    assert mapping.columns.student_flag.truthy == ["yes", "y", "true", "student"]
    assert scoring.baseline.count_forks is True
    assert scoring.free_mail_domains[0] == "gmail.com"
    assert scoring.weights.claim_consistency == pytest.approx(0.15)
    assert scoring.eligibility.professional_titles[0] == "engineer"
    assert scoring.eligibility.intern_titles == ["intern", "internship", "trainee"]
    assert scoring.eligibility.student_titles[0] == "student"
    assert scoring.eligibility.unknown_titles[-1] == ""
    assert scoring.eligibility.rules[0].fail_if_all == [
        "role matches student_titles",
        "org matches student_titles or org is a college",
    ]
    assert scoring.note_adjustments["flag"] == -0.25
    assert scoring.note_adjustments["praise"] == pytest.approx(0.15)
    assert scoring.note_adjustments["override"] == "force_human"
    assert scoring.llm_gating["max_profiles"] == 300
    assert scoring.llm_gating["always_profile_top_n"] == 220
    assert scoring.llm_gating["always_profile_decisions_near_threshold"] == pytest.approx(
        0.08
    )
    assert scoring.llm_gating["skip_profile_when"] == [
        "evidence_level == none and org is blank"
    ]


def test_dataset_scoring_fields_keep_legacy_defaults() -> None:
    from app.config import Scoring

    legacy = Scoring.model_validate(
        {
            "weights": {
                "original_work": 0.45,
                "reliability": 0.25,
                "ai_relevance": 0.15,
                "trajectory": 0.15,
            },
            "eligibility": {
                "rules": [{"unknown_if": "student flag is missing"}],
            },
        }
    )

    assert legacy.weights.claim_consistency == 0.0
    assert legacy.eligibility.professional_titles == []
    assert legacy.eligibility.intern_titles == []
    assert legacy.eligibility.student_titles == []
    assert legacy.eligibility.unknown_titles == []
    assert legacy.eligibility.rules[0].fail_if_all == []
    assert legacy.note_adjustments == {}
    assert legacy.llm_gating == {}


def test_scoring_accepts_scalar_or_list_unknown_predicates() -> None:
    from app.config import Scoring

    scalar = Scoring.model_validate(
        {"eligibility": {"rules": [{"unknown_if": "student flag is missing"}]}}
    )
    listed = Scoring.model_validate(
        {
            "eligibility": {
                "rules": [
                    {"unknown_if": ["student flag is missing", "free email domain"]}
                ]
            }
        }
    )
    assert scalar.eligibility.rules[0].unknown_if == "student flag is missing"
    assert listed.eligibility.rules[0].unknown_if == [
        "student flag is missing",
        "free email domain",
    ]


class _Result(BaseModel):
    answer: str


class _FakeMessages:
    def __init__(self, payload: dict, input_tokens: int = 12, output_tokens: int = 3):
        self.payload = payload
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", input=self.payload)],
            usage=SimpleNamespace(
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
            ),
        )


class _FakeClient:
    def __init__(self, messages: _FakeMessages):
        self.messages = messages


def test_structured_uses_forced_tool_and_cache(tmp_db, monkeypatch) -> None:
    messages = _FakeMessages({"answer": "cached after first call"})
    monkeypatch.setattr(llm, "_client", _FakeClient(messages))
    result = llm.structured(
        "Return one answer.", _Result, tier="batch", cache_key="foundation-cache"
    )
    assert result.answer == "cached after first call"
    assert len(messages.calls) == 1
    assert messages.calls[0]["tool_choice"] == {"type": "tool", "name": "structured_output"}
    assert messages.calls[0]["tools"][0]["input_schema"]["title"] == "_Result"

    messages.payload = {"answer": "should not be used"}
    cached = llm.structured(
        "Return one answer.", _Result, tier="batch", cache_key="foundation-cache"
    )
    assert cached.answer == "cached after first call"
    assert len(messages.calls) == 1


def test_structured_refuses_after_budget_is_exhausted(tmp_db, monkeypatch) -> None:
    db.init_db()
    with db.get_session() as session:
        session.add(
            db.LLMUsageRow(
                ts=datetime.now(timezone.utc),
                model="test",
                tier="batch",
                input_tokens=1,
                output_tokens=1,
                cost_usd=6.0,
            )
        )
    messages = _FakeMessages({"answer": "must not run"})
    monkeypatch.setattr(llm, "_client", _FakeClient(messages))
    with pytest.raises(llm.BudgetExceeded):
        llm.structured("Return one answer.", _Result, tier="batch", cache_key="budget-test")
    assert messages.calls == []


def test_health_and_events_heartbeat(tmp_db) -> None:
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health").json() == {"status": "ok"}
        with client.stream("GET", "/events") as response:
            assert response.status_code == 200
            first_line = next(line for line in response.iter_lines() if line)
            assert first_line == "event: heartbeat"
