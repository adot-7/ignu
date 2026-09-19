from __future__ import annotations

import os
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import graph as graph_module
from app.models import Note, Participation, Person, Profile, SkillScore, Team


class FakeResult:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []

    def data(self) -> list[dict]:
        return self.rows

    def consume(self) -> None:
        return None


class FakeSession:
    def __init__(self, driver: "FakeDriver") -> None:
        self.driver = driver

    def run(self, query: str, **parameters):
        self.driver.calls.append((query, parameters))
        rows = self.driver.responses.pop(0) if self.driver.responses else []
        return FakeResult(rows)

    def close(self) -> None:
        return None


class FakeDriver:
    def __init__(self, responses: list[list[dict]] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses = list(responses or [])

    def session(self, **_kwargs) -> FakeSession:
        return FakeSession(self)

    def close(self) -> None:
        return None


def _models() -> tuple[Person, Profile, Participation, Team, Note]:
    built_at = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
    person = Person(
        id="person-1",
        name="Ada Example",
        email="ada@example.org",
        email_domain="example.org",
        github_login="ada-example",
        linkedin_url=None,
        org="Example Org",
        role="Engineer",
        is_student=False,
        answers={},
        registered_at=built_at,
        alias_of=None,
        source_file="sample.csv",
        source_row=1,
    )
    profile = Profile(
        person_id=person.id,
        version=1,
        built_at=built_at,
        skills=[
            SkillScore(skill="backend", confidence=0.8),
            SkillScore(skill="data", confidence=0.5),
        ],
        evidence_level="solid",
        original_work_score=0.7,
        ai_relevance=0.6,
        reliability=1.0,
        summary="Built a demo.",
        evidence=[],
        model_used="test",
        input_tokens=0,
        output_tokens=0,
    )
    participation = Participation(
        person_id=person.id,
        event="hackarena-bangalore",
        registered=True,
        approved=True,
        checked_in=True,
        submitted=True,
        placed=2,
        team_id="team-1",
        at=date(2026, 9, 19),
    )
    team = Team(
        id="team-1",
        event=participation.event,
        member_ids=[person.id, "person-2"],
        coverage={"backend": 0.8},
        balance=0.8,
        why="Backend coverage.",
    )
    note = Note(
        id=11,
        person_id=person.id,
        text="Strong demo.",
        author="organizer",
        at=built_at,
        kind="praise",
        source="import",
    )
    return person, profile, participation, team, note


def test_graph_disabled_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted = []
    monkeypatch.setattr(graph_module.event_bus, "emit", emitted.append)
    driver = FakeDriver()
    graph = graph_module.Graph(
        settings=SimpleNamespace(graph_enabled=False, neo4j_database="neo4j"),
        driver=driver,
    )
    person, profile, participation, team, note = _models()

    assert graph.available() is False
    graph.ensure_constraints()
    graph.upsert_person(person)
    graph.upsert_profile(profile)
    graph.upsert_participation(participation)
    graph.upsert_team(team)
    graph.upsert_note(note)
    graph.upsert_alias(person.id, "canonical-1")

    assert graph.aliases() == []
    assert graph.returning(participation.event) == []
    assert graph.skill_coverage([person.id]) == {}
    assert graph.prior_teammates(person.id) == []
    assert graph.cohort_skill_histogram() == {}
    assert driver.calls == []
    assert emitted
    assert all(event.status == "skip" for event in emitted)


def test_upserts_use_merge_and_emit_graph_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted = []
    monkeypatch.setattr(graph_module.event_bus, "emit", emitted.append)
    driver = FakeDriver()
    graph = graph_module.Graph(
        settings=SimpleNamespace(graph_enabled=True, neo4j_database="neo4j"),
        driver=driver,
        run_id="test-run",
    )
    person, profile, participation, team, note = _models()

    graph.ensure_constraints()
    graph.upsert_person(person)
    graph.upsert_profile(profile)
    graph.upsert_participation(participation)
    graph.upsert_team(team)
    graph.upsert_note(note)
    graph.upsert_alias(person.id, "canonical-1")

    constraint_queries = [
        query for query, _parameters in driver.calls if "CREATE CONSTRAINT" in query
    ]
    write_queries = [
        query for query, _parameters in driver.calls if "CREATE CONSTRAINT" not in query
    ]
    assert len(constraint_queries) == 6
    assert write_queries
    assert all("MERGE" in query for query in write_queries)
    assert all("CREATE" not in query.upper() for query in write_queries)
    assert any("DELETE stale" in query for query in write_queries)
    assert all(event.run_id == "test-run" for event in emitted)
    assert {event.status for event in emitted} == {"ok"}


def test_cohort_queries_normalize_fake_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(graph_module.event_bus, "emit", lambda _event: None)
    driver = FakeDriver(
        responses=[
            [{"person_id": "alias-1", "alias_of": "person-1"}],
            [
                {
                    "person_id": "person-1",
                    "events": [
                        {"event": "hackarena-bangalore", "checked_in": True, "placed": 2},
                        {"event": "ignite-2025", "checked_in": False, "placed": None},
                    ],
                }
            ],
            [
                {"skill": "backend", "confidence": 0.8},
                {"skill": "backend", "confidence": 0.6},
                {"skill": "data", "confidence": 0.5},
            ],
            [{"person_id": "person-2", "event": "ignite-2025", "placed": 1}],
            [{"skill": "backend", "count": 4}, {"skill": "data", "count": 2}],
        ]
    )
    graph = graph_module.Graph(
        settings=SimpleNamespace(graph_enabled=True, neo4j_database="neo4j"),
        driver=driver,
    )

    assert graph.aliases() == [{"person_id": "alias-1", "alias_of": "person-1"}]
    assert graph.returning("hackarena-bangalore") == [
        {
            "person_id": "person-1",
            "events": [
                {"event": "hackarena-bangalore", "checked_in": True, "placed": 2},
                {"event": "ignite-2025", "checked_in": False, "placed": None},
            ],
        }
    ]
    assert graph.skill_coverage(["person-1", "person-2"]) == {
        "backend": 0.8,
        "data": 0.5,
    }
    assert graph.prior_teammates("person-1") == [
        {"person_id": "person-2", "event": "ignite-2025", "placed": 1}
    ]
    assert graph.cohort_skill_histogram() == {"backend": 4, "data": 2}
    assert len(driver.calls) == 5
    assert all("MATCH" in query or "UNWIND" in query for query, _ in driver.calls)


@pytest.mark.skipif(
    not os.environ.get("NEO4J_URI"),
    reason="optional live Neo4j verification requires NEO4J_URI",
)
def test_optional_live_person_upsert_is_idempotent() -> None:
    neo4j = pytest.importorskip("neo4j")
    uri = os.environ["NEO4J_URI"]
    username = os.environ.get("NEO4J_USERNAME", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "")
    database = os.environ.get("NEO4J_DATABASE", "neo4j")
    driver = neo4j.GraphDatabase.driver(uri, auth=(username, password))
    person_id = f"graph-test-{uuid4().hex[:12]}"
    login = f"graph-test-{uuid4().hex[:12]}"
    person = Person(
        id=person_id,
        name="Graph Test",
        email="",
        email_domain="example.org",
        github_login=login,
        linkedin_url=None,
        org=None,
        role=None,
        is_student=None,
        answers={},
        registered_at=None,
        alias_of=None,
        source_file="test",
        source_row=0,
    )
    graph = graph_module.Graph(
        settings=SimpleNamespace(graph_enabled=True, neo4j_database=database),
        driver=driver,
    )
    try:
        graph.upsert_person(person)
        graph.upsert_person(person)
        with driver.session(database=database) as session:
            record = session.run(
                "MATCH (p:Person {id: $person_id}) RETURN count(p) AS count",
                person_id=person_id,
            ).single()
            assert record["count"] == 1
    finally:
        with driver.session(database=database) as session:
            session.run(
                "MATCH (p:Person {id: $person_id}) DETACH DELETE p",
                person_id=person_id,
            ).consume()
            session.run(
                "MATCH (g:GitHubAccount {login: $login}) DETACH DELETE g",
                login=login,
            ).consume()
        driver.close()
