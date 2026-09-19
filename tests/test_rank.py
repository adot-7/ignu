from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app import db, rank
from app.config import load_scoring
from app.models import (
    Evidence,
    Note,
    Participation,
    Person,
    Profile,
    RepoEvidence,
    SkillScore,
)


def _person(
    person_id: str,
    name: str,
    *,
    role: str,
    org: str,
    github: str | None,
    student: bool | None = False,
    email: str = "person@example.org",
) -> Person:
    return Person(
        id=person_id,
        name=name,
        email=email,
        email_domain=email.rsplit("@", 1)[-1] if "@" in email else "",
        github_login=github,
        linkedin_url=None,
        org=org,
        role=role,
        is_student=student,
        answers={},
        registered_at=None,
        alias_of=None,
        source_file="tests/rank.csv",
        source_row=2,
    )


def _repo(name: str, *, fork: bool, commits: int, description: str) -> RepoEvidence:
    return RepoEvidence(
        full_name=f"builder/{name}",
        html_url=f"https://github.com/builder/{name}",
        is_fork=fork,
        stars=0,
        author_commits_90d=min(commits, 20),
        author_commits_total=commits,
        primary_language="Python",
        last_push=datetime(2026, 9, 1, tzinfo=timezone.utc),
        description=description,
        topics=["ai"] if "ai" in description.casefold() else [],
        readme_excerpt="",
    )


def _profile(person: Person, *, original: float, ai: float, level: str = "solid") -> Profile:
    evidence = Evidence(
        claim="Recorded repository evidence",
        source_url=f"https://github.com/{person.github_login or 'none'}",
        kind="repo",
        confidence=0.9,
        observed_at=date(2026, 9, 1),
    )
    return Profile(
        person_id=person.id,
        version=1,
        built_at=datetime(2026, 9, 19, tzinfo=timezone.utc),
        skills=[SkillScore(skill="backend", confidence=0.8)],
        evidence_level=level,  # type: ignore[arg-type]
        original_work_score=original,
        ai_relevance=ai,
        reliability=None,
        summary="Recorded evidence.",
        evidence=[evidence] if level != "none" else [],
        model_used="stub",
        input_tokens=0,
        output_tokens=0,
    )


def _persist_context(
    people: list[Person],
    profiles: list[Profile],
    repos: dict[str, list[RepoEvidence]],
    notes: list[Note] | None = None,
) -> None:
    db.init_db()
    with db.get_session() as session:
        for person in people:
            session.add(db.PersonRow(**person.model_dump()))
        for profile in profiles:
            payload = profile.model_dump(mode="json")
            session.add(
                db.ProfileRow(
                    person_id=profile.person_id,
                    version=profile.version,
                    built_at=profile.built_at,
                    skills=payload["skills"],
                    evidence_level=profile.evidence_level,
                    original_work_score=profile.original_work_score,
                    ai_relevance=profile.ai_relevance,
                    reliability=profile.reliability,
                    summary=profile.summary,
                    evidence=payload["evidence"],
                    model_used=profile.model_used,
                    input_tokens=profile.input_tokens,
                    output_tokens=profile.output_tokens,
                )
            )
        for person_id, person_repos in repos.items():
            for repo in person_repos:
                session.add(db.RepoRow(person_id=person_id, **repo.model_dump()))
        for note in notes or []:
            session.add(db.NoteRow(**note.model_dump()))


def test_eligibility_uses_role_and_org_not_email_domain() -> None:
    scoring = load_scoring("scoring.yaml")
    professional = _person(
        "professional",
        "Professional",
        role="Software Engineer",
        org="Acme",
        github="professional",
        email="professional@gmail.com",
    )
    intern = _person(
        "intern",
        "Intern",
        role="Technical Intern",
        org="Example College",
        github="intern",
        student=True,
        email="intern@gmail.com",
    )
    student = _person(
        "student",
        "Student",
        role="Student",
        org="Example College",
        github=None,
        student=True,
        email="student@example.org",
    )
    unknown = _person(
        "unknown",
        "Unknown",
        role="",
        org="Example",
        github=None,
    )

    assert rank.eligibility(professional, [], scoring) == "pass"
    assert rank.eligibility(intern, [], scoring) == "pass"
    assert rank.eligibility(student, [], scoring) == "fail"
    assert rank.eligibility(unknown, [], scoring) == "unknown"


def test_components_keep_unsupported_reliability_and_trajectory_neutral() -> None:
    person = _person(
        "p1",
        "Builder",
        role="Engineer",
        org="Acme",
        github="builder",
    )
    current = _profile(person, original=0.9, ai=0.8)
    previous = current.model_copy(update={"version": 0, "original_work_score": 0.1})
    participation = Participation(
        person_id=person.id,
        event="old",
        registered=True,
        approved=True,
        checked_in=False,
        submitted=False,
        placed=None,
        team_id=None,
        at=None,
    )

    values = rank.components(current, [participation], previous, person=person)

    assert values["reliability"] == 0.5
    assert values["trajectory"] == 0.5
    assert values["claim_consistency"] == 0.75


def test_rank_persists_sourced_verdicts_and_rerank_is_append_only(
    tmp_db, monkeypatch
) -> None:
    emitted = []
    monkeypatch.setattr(rank.events, "emit", emitted.append)
    builder = _person(
        "builder",
        "Quiet Builder",
        role="Engineer",
        org="Quiet Labs",
        github="quiet-builder",
    )
    profile = _profile(builder, original=0.9, ai=0.8)
    repos = {builder.id: [_repo("quiet", fork=False, commits=30, description="data service")]}
    _persist_context([builder], [profile], repos)

    first = rank.rank_all(SimpleNamespace(event="ignite", run_id="run-1"))

    assert len(first) == 1
    assert first[0].decision == "admit"
    assert first[0].reasons
    assert first[0].evidence_ids
    assert any(event.stage == "rank" for event in emitted)
    with db.get_session() as session:
        assert len(session.scalars(select(db.VerdictRow)).all()) == 1
        session.add(
            db.NoteRow(
                person_id=builder.id,
                text="left after lunch",
                author="organizer",
                at=datetime(2026, 9, 19, tzinfo=timezone.utc),
                kind="flag",
                source="import",
            )
        )

    changed = rank.rerank_person(builder.id)

    assert changed == {"old_decision": "admit", "new_decision": "needs_human"}
    with db.get_session() as session:
        verdicts = list(
            session.scalars(
                select(db.VerdictRow).where(db.VerdictRow.person_id == builder.id)
            ).all()
        )
    assert len(verdicts) == 2
    assert "note: flagged" in " ".join(verdicts[-1].reasons)
    assert verdicts[-1].evidence_ids


def test_missing_and_reserved_github_are_needs_human(tmp_db, monkeypatch) -> None:
    monkeypatch.setattr(rank.events, "emit", lambda event: event)
    professional_without_github = _person(
        "missing",
        "No GitHub",
        role="Engineer",
        org="Acme",
        github=None,
        email="no-github@gmail.com",
    )
    invalid_login = _person(
        "invalid",
        "Invalid Login",
        role="Engineer",
        org="Acme",
        github="in",
    )
    profiles = [
        _profile(professional_without_github, original=0.0, ai=0.0, level="none"),
        _profile(invalid_login, original=0.0, ai=0.0, level="none"),
    ]
    _persist_context([professional_without_github, invalid_login], profiles, {})

    verdicts = rank.rank_all("ignite")
    by_id = {verdict.person_id: verdict for verdict in verdicts}

    assert by_id[professional_without_github.id].decision == "needs_human"
    assert by_id[invalid_login.id].decision == "needs_human"
    assert all(verdict.reasons and verdict.evidence_ids for verdict in verdicts)
    assert "invalid GitHub login" in " ".join(by_id[invalid_login.id].reasons)


def test_disagreements_are_latest_and_deterministic(tmp_db, monkeypatch) -> None:
    monkeypatch.setattr(rank.events, "emit", lambda event: event)
    scoring = load_scoring("scoring.yaml")
    scoring.disagreement_threshold = 0
    monkeypatch.setattr(rank, "_scoring", lambda: scoring)
    farmer = _person(
        "farmer",
        "Fork Farmer",
        role="Developer",
        org="Open Source",
        github="farmer",
    )
    quiet = _person(
        "quiet",
        "Quiet Builder",
        role="Engineer",
        org="Quiet Labs",
        github="quiet",
    )
    farmer_profile = _profile(farmer, original=0.05, ai=0.9, level="thin")
    quiet_profile = _profile(quiet, original=0.9, ai=0.1)
    _persist_context(
        [farmer, quiet],
        [farmer_profile, quiet_profile],
        {
            farmer.id: [_repo("ai-fork-1", fork=True, commits=0, description="ai fork")],
            quiet.id: [_repo("quiet", fork=False, commits=30, description="ordinary service")],
        },
    )

    rank.rank_all("ignite")
    rows = rank.disagreements(limit=20)

    assert {row["person_id"] for row in rows} == {farmer.id, quiet.id}
    assert all(set(row) == {"person_id", "name", "baseline_rank", "ignu_rank", "gap", "reasons"} for row in rows)
    assert rows == sorted(rows, key=lambda row: (-row["gap"], row["person_id"]))
