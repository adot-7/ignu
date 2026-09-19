from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import profile
from app.models import Evidence, Participation, Person, Profile, RepoEvidence, SkillScore


def _person(
    *,
    person_id: str = "person-1",
    github_login: str | None = "quiet-builder",
    answers: dict[str, str] | None = None,
) -> Person:
    return Person(
        id=person_id,
        name="Quiet Builder",
        email="",
        email_domain="",
        github_login=github_login,
        linkedin_url=None,
        org="Quiet Labs",
        role="Engineer",
        is_student=False,
        answers=answers or {},
        registered_at=None,
        alias_of=None,
        source_file="sample.csv",
        source_row=7,
    )


def _repo(
    *,
    name: str = "quiet-builder/service",
    total: int = 35,
    recent: int = 15,
    fork: bool = False,
    readme: str | None = "A maintained service.",
) -> RepoEvidence:
    return RepoEvidence(
        full_name=name,
        html_url=f"https://github.com/{name}",
        is_fork=fork,
        stars=1,
        author_commits_90d=recent,
        author_commits_total=total,
        primary_language="Python",
        last_push=datetime.now(timezone.utc) - timedelta(days=4),
        description="A useful project",
        topics=["automation"],
        readme_excerpt=readme,
    )


def test_original_work_score_distinguishes_fork_farmer_and_quiet_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(profile.events, "emit", lambda event: event)
    monkeypatch.setattr(
        profile.llm,
        "structured",
        lambda prompt, schema, *, tier, cache_key: schema(
            skills=[],
            ai_relevance=0.0,
            summary="The repository evidence is available. [repo]",
            evidence=[],
        ),
    )

    fork_repo = _repo(
        name="fork-farmer-one/copied", total=2, recent=0, fork=True
    )
    fork_repo.last_push = None
    fork_farmer = profile.build_profile(
        _person(person_id="fork", github_login="fork-farmer-one"),
        [fork_repo],
        [],
        None,
    )
    quiet_builder = profile.build_profile(
        _person(),
        [_repo()],
        [],
        None,
    )

    assert fork_farmer.evidence_level == "thin"
    assert fork_farmer.original_work_score == pytest.approx(0.0)
    assert quiet_builder.evidence_level == "solid"
    assert quiet_builder.original_work_score > 0.6


def test_no_github_and_empty_answers_skips_structured_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    emitted: list[object] = []

    def fake_structured(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("no-GitHub empty-answer profiles must not call the LLM")

    monkeypatch.setattr(profile.llm, "structured", fake_structured)
    monkeypatch.setattr(profile.events, "emit", emitted.append)

    result = profile.build_profile(_person(github_login=None), [], [], None)

    assert calls == []
    assert result.evidence_level == "none"
    assert result.original_work_score == 0.0
    assert result.summary == "No external evidence; registration answers empty."
    assert result.model_used == "deterministic"
    assert [event.status for event in emitted] == ["start", "skip"]


def test_reserved_github_path_is_treated_as_missing_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        profile.llm,
        "structured",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(profile.events, "emit", lambda event: event)

    result = profile.build_profile(_person(github_login="in"), [_repo()], [], None)

    assert calls == []
    assert result.evidence_level == "none"
    assert result.summary == "No external evidence; registration answers empty."


def test_structured_call_is_bounded_cached_and_versioned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[object] = []
    calls: list[tuple[str, type, str, str]] = []
    source_evidence = Evidence(
        claim="Repository shows an automation service. [repo]",
        source_url="https://github.com/quiet-builder/service",
        kind="repo",
        confidence=0.8,
        observed_at=datetime.now(timezone.utc).date(),
    )

    def fake_structured(prompt, schema, *, tier, cache_key):
        calls.append((prompt, schema, tier, cache_key))
        return schema(
            skills=[SkillScore(skill="backend", confidence=0.9)],
            ai_relevance=0.2,
            summary="Repository shows an automation service. [repo]",
            evidence=[source_evidence],
        )

    monkeypatch.setattr(profile.llm, "structured", fake_structured)
    monkeypatch.setattr(profile.events, "emit", emitted.append)
    long_readme = "R" * 20_000
    person = _person(answers={"Why": "Build reliable tools"})
    repo = _repo(readme=long_readme)
    first = profile.build_profile(person, [repo], [], None)
    previous = Profile.model_validate(first.model_dump())
    second = profile.build_profile(
        person,
        [repo],
        [
            Participation(
                person_id=person.id,
                event="past",
                registered=True,
                approved=True,
                checked_in=True,
                submitted=True,
                placed=1,
                team_id=None,
                at=None,
            )
        ],
        previous,
    )

    assert len(calls) == 2
    assert calls[0][1] is profile.ProfileLLM
    assert calls[0][2] == "batch"
    assert long_readme not in calls[0][0]
    assert ("R" * 1_197 + "...") in calls[0][0]
    assert calls[0][3] == profile.profile_cache_key(person, [repo])
    assert first.version == 1
    assert second.version == 2
    assert second.reliability == pytest.approx(2 / 3)
    assert second.evidence == [source_evidence]
    assert second.input_tokens == 0
    assert second.output_tokens == 0
    assert [event.status for event in emitted] == ["start", "ok", "start", "ok"]


def test_budget_exhaustion_keeps_deterministic_sourced_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[object] = []

    def fake_structured(*args, **kwargs):
        raise profile.llm.BudgetExceeded("exhausted")

    monkeypatch.setattr(profile.llm, "structured", fake_structured)
    monkeypatch.setattr(profile.events, "emit", emitted.append)

    result = profile.build_profile(_person(), [_repo()], [], None)

    assert result.summary == "LLM budget exhausted; deterministic evidence only."
    assert result.evidence_level == "solid"
    assert result.evidence[0].source_url == "https://github.com/quiet-builder/service"
    assert [event.status for event in emitted] == ["start", "error"]


def test_registration_only_answers_call_has_low_confidence_skills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(profile.events, "emit", lambda event: event)

    def fake_structured(prompt, schema, *, tier, cache_key):
        assert '"repositories": []' in prompt
        return schema(
            skills=[SkillScore(skill="backend", confidence=0.95)],
            ai_relevance=0.0,
            summary="The registration lists an engineering role. [registration]",
            evidence=[],
        )

    monkeypatch.setattr(profile.llm, "structured", fake_structured)
    result = profile.build_profile(
        _person(github_login=None, answers={"Why": "Learn with builders"}),
        [],
        [],
        None,
    )

    assert result.evidence_level == "none"
    assert result.skills[0].confidence == pytest.approx(0.4)
    assert result.ai_relevance == 0.0


def test_prompt_contains_skill_enum_and_traceability_rule() -> None:
    prompt = profile.build_profile_prompt(_person(), [_repo(readme="X" * 5000)])

    for skill in (
        "frontend",
        "backend",
        "ml_ai",
        "data",
        "mobile",
        "devops_cloud",
        "design_product",
        "pitch_comms",
    ):
        assert skill in prompt
    assert "traceable" in prompt
    assert "80 words" in prompt
