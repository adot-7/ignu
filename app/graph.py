"""Optional Neo4j graph persistence and cohort queries.

The graph is deliberately kept behind a small synchronous adapter.  The
pipeline can therefore run with ``GRAPH_ENABLED=false`` (or without the
optional Neo4j driver) while callers keep the same interface.  All writes use
``MERGE`` and are safe to replay from the cached demo run.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

try:  # Neo4j is an optional integration in the local/demo installation.
    from neo4j import GraphDatabase
except ImportError:  # pragma: no cover - exercised when the optional package is absent
    GraphDatabase = None  # type: ignore[assignment,misc]

from . import events as event_bus
from .config import Settings, get_settings
from .models import Note, Participation, Person, PipelineEvent, Profile, Team

logger = logging.getLogger(__name__)


_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    (
        "person_id_unique",
        "CREATE CONSTRAINT person_id_unique IF NOT EXISTS "
        "FOR (p:Person) REQUIRE p.id IS UNIQUE",
    ),
    (
        "github_login_unique",
        "CREATE CONSTRAINT github_login_unique IF NOT EXISTS "
        "FOR (g:GitHubAccount) REQUIRE g.login IS UNIQUE",
    ),
    (
        "skill_name_unique",
        "CREATE CONSTRAINT skill_name_unique IF NOT EXISTS "
        "FOR (s:Skill) REQUIRE s.name IS UNIQUE",
    ),
    (
        "event_name_unique",
        "CREATE CONSTRAINT event_name_unique IF NOT EXISTS "
        "FOR (e:Event) REQUIRE e.name IS UNIQUE",
    ),
    (
        "team_id_unique",
        "CREATE CONSTRAINT team_id_unique IF NOT EXISTS "
        "FOR (t:Team) REQUIRE t.id IS UNIQUE",
    ),
    (
        "note_id_unique",
        "CREATE CONSTRAINT note_id_unique IF NOT EXISTS "
        "FOR (n:Note) REQUIRE n.id IS UNIQUE",
    ),
)


_UPSERT_PERSON = """
UNWIND $persons AS row
MERGE (p:Person {id: row.id})
SET p.name = row.name,
    p.email_domain = row.email_domain
FOREACH (login IN CASE
    WHEN row.github_login IS NULL OR row.github_login = '' THEN []
    ELSE [row.github_login]
END |
    MERGE (g:GitHubAccount {login: login})
    MERGE (p)-[:HAS_GITHUB]->(g)
)
FOREACH (domain IN CASE
    WHEN row.email_domain IS NULL OR row.email_domain = '' THEN []
    ELSE [row.email_domain]
END |
    MERGE (o:Org {domain: domain})
    MERGE (p)-[:FROM]->(o)
)
"""


_UPSERT_PROFILE = """
MERGE (p:Person {id: $person_id})
OPTIONAL MATCH (p)-[stale:HAS_SKILL]->(:Skill)
DELETE stale
WITH p
SET p.profile_version = $version,
    p.evidence_level = $evidence_level,
    p.original_work_score = $original_work_score,
    p.ai_relevance = $ai_relevance,
    p.reliability = $reliability,
    p.summary = $summary,
    p.profile_built_at = $built_at
WITH p
UNWIND $skills AS skill
    MERGE (s:Skill {name: skill.name})
    MERGE (p)-[r:HAS_SKILL]->(s)
    SET r.confidence = skill.confidence
"""


_UPSERT_PARTICIPATION = """
UNWIND $participations AS row
MERGE (p:Person {id: row.person_id})
MERGE (e:Event {name: row.event})
MERGE (p)-[r:PARTICIPATED]->(e)
SET r.registered = row.registered,
    r.approved = row.approved,
    r.checked_in = row.checked_in,
    r.submitted = row.submitted,
    r.placed = row.placed,
    r.at = row.at
FOREACH (team_id IN CASE
    WHEN row.team_id IS NULL OR row.team_id = '' THEN []
    ELSE [row.team_id]
END |
    MERGE (t:Team {id: team_id})
    MERGE (p)-[:MEMBER_OF]->(t)
    MERGE (t)-[:AT]->(e)
)
"""


_UPSERT_TEAM = """
UNWIND $teams AS row
MERGE (t:Team {id: row.id})
SET t.event = row.event,
    t.coverage = row.coverage,
    t.balance = row.balance,
    t.why = row.why
MERGE (e:Event {name: row.event})
MERGE (t)-[:AT]->(e)
FOREACH (member_id IN coalesce(row.member_ids, []) |
    MERGE (p:Person {id: member_id})
    MERGE (p)-[:MEMBER_OF]->(t)
)
"""


_UPSERT_NOTE = """
UNWIND $notes AS row
MERGE (n:Note {id: row.id})
SET n.kind = row.kind,
    n.at = row.at,
    n.text = row.text,
    n.author = row.author,
    n.source = row.source
FOREACH (person_id IN CASE
    WHEN row.person_id IS NULL OR row.person_id = '' THEN []
    ELSE [row.person_id]
END |
    MERGE (p:Person {id: person_id})
    MERGE (p)-[:NOTED]->(n)
)
"""


_UPSERT_ALIAS = """
MERGE (p:Person {id: $person_id})
MERGE (canonical:Person {id: $alias_of})
MERGE (p)-[:ALIAS_OF]->(canonical)
SET p.alias_of = canonical.id
"""


_ALIASES = """
MATCH (alias:Person)-[:ALIAS_OF]->(canonical:Person)
RETURN alias.id AS person_id, canonical.id AS alias_of
ORDER BY person_id
"""


_RETURNING = """
MATCH (p:Person)-[:PARTICIPATED]->(:Event {name: $event})
MATCH (p)-[r:PARTICIPATED]->(history:Event)
RETURN p.id AS person_id,
       collect({event: history.name, checked_in: r.checked_in, placed: r.placed}) AS events
ORDER BY person_id
"""


_SKILL_COVERAGE = """
UNWIND $member_ids AS member_id
MATCH (p:Person {id: member_id})-[r:HAS_SKILL]->(s:Skill)
RETURN s.name AS skill, max(r.confidence) AS confidence
ORDER BY skill
"""


_PRIOR_TEAMMATES = """
MATCH (p:Person {id: $person_id})-[:MEMBER_OF]->(t:Team)<-[:MEMBER_OF]-(other:Person)
WHERE other.id <> p.id
OPTIONAL MATCH (t)-[:AT]->(event:Event)
OPTIONAL MATCH (other)-[r:PARTICIPATED]->(event)
RETURN DISTINCT other.id AS person_id, event.name AS event, r.placed AS placed
ORDER BY event, person_id
"""


_COHORT_SKILL_HISTOGRAM = """
MATCH (p:Person)-[:HAS_SKILL]->(s:Skill)
RETURN s.name AS skill, count(DISTINCT p) AS count
ORDER BY skill
"""


def _setting(settings: Any, name: str, default: Any = None) -> Any:
    """Read either a typed settings object or a small test settings mapping."""

    if settings is None:
        return default
    if isinstance(settings, Mapping):
        if name in settings:
            return settings[name]
        upper = name.upper()
        if upper in settings:
            return settings[upper]
    value = getattr(settings, name, None)
    if value is not None:
        return value
    return getattr(settings, name.upper(), default)


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _batch(value: Any) -> list[Any]:
    """Accept one model or an iterable so callers can use natural batches."""

    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)) or hasattr(value, "model_dump"):
        return [value]
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def _plain(value: Any) -> Any:
    """Turn Neo4j Records and nested values into ordinary Python values."""

    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    data_method = getattr(value, "data", None)
    if callable(data_method):
        try:
            return _plain(data_method())
        except Exception:
            pass
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _records(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    data_method = getattr(result, "data", None)
    if callable(data_method):
        rows = data_method()
    elif isinstance(result, Mapping):
        rows = [result]
    else:
        try:
            rows = list(result)
        except TypeError:
            rows = []

    if isinstance(rows, Mapping):
        rows = [rows]
    output: list[dict[str, Any]] = []
    for row in rows:
        plain = _plain(row)
        if isinstance(plain, Mapping):
            output.append(dict(plain))
        else:
            output.append({})
    return output


class Graph:
    """Feature-flagged Neo4j adapter used by the graph pipeline stage.

    ``driver`` is injectable for offline tests and for callers that manage a
    driver lifecycle themselves.  When omitted, the driver is created lazily
    from ``NEO4J_*`` settings without making a connectivity call in the
    constructor.
    """

    def __init__(
        self,
        settings: Settings | Any | None = None,
        driver: Any | None = None,
        *,
        run_id: str | None = None,
    ) -> None:
        # Supporting both Graph(settings=..., driver=...) and the common
        # positional Graph(driver, settings) shape keeps the adapter simple
        # to embed in later lanes without coupling them to a constructor order.
        if settings is not None and driver is not None:
            settings_looks_typed = _setting(settings, "graph_enabled", None) is not None
            driver_looks_typed = _setting(driver, "graph_enabled", None) is not None
            if not settings_looks_typed and driver_looks_typed:
                settings, driver = driver, settings
        elif settings is not None and driver is None:
            if _setting(settings, "graph_enabled", None) is None and hasattr(
                settings, "session"
            ):
                driver, settings = settings, None

        self.settings = settings or get_settings()
        self._enabled = _as_bool(_setting(self.settings, "graph_enabled", False))
        self._database = _setting(self.settings, "neo4j_database", "neo4j") or "neo4j"
        self._run_id = run_id or os.environ.get("RUN_ID", "graph")
        self._owns_driver = driver is None
        self._driver = driver if self._enabled else None

        if self._enabled and self._driver is None:
            self._driver = self._build_driver()

    def _build_driver(self) -> Any | None:
        if GraphDatabase is None:
            logger.warning(
                "graph unavailable: optional neo4j package is not installed",
                extra={"stage": "graph", "exception": "ModuleNotFoundError"},
            )
            return None

        uri = _setting(self.settings, "neo4j_uri", "")
        username = _setting(self.settings, "neo4j_username", "neo4j")
        password = _setting(self.settings, "neo4j_password", "")
        try:
            return GraphDatabase.driver(uri, auth=(username, password))
        except Exception as exc:  # pragma: no cover - depends on driver/runtime
            logger.warning(
                "graph driver could not be created",
                extra={"stage": "graph", "exception": exc.__class__.__name__},
            )
            return None

    def available(self) -> bool:
        """Return whether graph calls are enabled and a driver is available."""

        return self._enabled and self._driver is not None

    @contextmanager
    def _session(self) -> Iterator[Any]:
        if self._driver is None:
            yield None
            return

        session = None
        try:
            try:
                session = self._driver.session(database=self._database)
            except TypeError:
                # Tiny fake drivers often omit the optional database keyword.
                session = self._driver.session()

            if hasattr(session, "__enter__") and hasattr(session, "__exit__"):
                with session as active_session:
                    yield active_session
            else:
                yield session
        finally:
            if session is not None and not (
                hasattr(session, "__enter__") and hasattr(session, "__exit__")
            ):
                close = getattr(session, "close", None)
                if callable(close):
                    close()

    def _emit(
        self,
        status: str,
        msg: str,
        *,
        person_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        try:
            event_bus.emit(
                PipelineEvent(
                    ts=datetime.now(timezone.utc),
                    run_id=self._run_id,
                    stage="graph",
                    person_id=person_id,
                    status=status,  # type: ignore[arg-type]
                    msg=msg,
                    data=data,
                )
            )
        except Exception:  # pragma: no cover - event persistence is best effort
            logger.debug("could not emit graph pipeline event", exc_info=True)

    def _execute(
        self,
        query: str,
        params: dict[str, Any],
        *,
        operation: str,
        read: bool = False,
    ) -> bool | list[dict[str, Any]]:
        try:
            with self._session() as session:
                if session is None:
                    return [] if read else False
                result = session.run(query, **params)
                if read:
                    return _records(result)
                consume = getattr(result, "consume", None)
                if callable(consume):
                    consume()
                return True
        except Exception as exc:
            # Do not include params in logs/events: they may contain note text
            # or other organizer data, and the driver parameters can contain
            # credentials in custom test/runtime implementations.
            logger.warning(
                "graph operation failed",
                extra={"stage": "graph", "operation": operation, "exception": exc.__class__.__name__},
            )
            self._emit(
                "error",
                f"{operation} failed",
                data={"exception": exc.__class__.__name__},
            )
            return [] if read else False

    def _write_batch(
        self,
        query: str,
        params: dict[str, Any],
        *,
        operation: str,
        count: int,
        person_id: str | None = None,
    ) -> None:
        if not count:
            self._emit("skip", f"{operation} skipped: empty batch", person_id=person_id)
            return
        if not self._enabled:
            self._emit("skip", f"{operation} skipped: graph disabled", person_id=person_id)
            return
        if not self.available():
            self._emit("skip", f"{operation} skipped: graph unavailable", person_id=person_id)
            return

        result = self._execute(query, params, operation=operation)
        if result is True:
            self._emit(
                "ok",
                f"{operation} upserted",
                person_id=person_id,
                data={"count": count},
            )

    def ensure_constraints(self) -> None:
        """Create the six node uniqueness constraints, idempotently."""

        if not self._enabled:
            self._emit("skip", "constraints skipped: graph disabled")
            return
        if not self.available():
            self._emit("skip", "constraints skipped: graph unavailable")
            return

        for constraint_name, query in _CONSTRAINTS:
            result = self._execute(query, {}, operation=f"constraint {constraint_name}")
            if result is not True:
                return
        self._emit("ok", "graph constraints ensured", data={"count": len(_CONSTRAINTS)})

    def upsert_person(self, person: Person | Iterable[Person]) -> None:
        people = _batch(person)
        rows = [
            {
                "id": _value(item, "id", ""),
                "name": _value(item, "name", ""),
                "email_domain": _value(item, "email_domain", "") or "",
                "github_login": _value(item, "github_login") or None,
            }
            for item in people
        ]
        person_id = _value(people[0], "id") if len(people) == 1 else None
        self._write_batch(
            _UPSERT_PERSON,
            {"persons": rows},
            operation="person",
            count=len(rows),
            person_id=person_id,
        )

    def upsert_profile(self, profile: Profile) -> None:
        skills = _value(profile, "skills", []) or []
        skill_rows = [
            {
                "name": _value(skill, "skill", ""),
                "confidence": _value(skill, "confidence", 0.0),
            }
            for skill in skills
        ]
        params = {
            "person_id": _value(profile, "person_id", ""),
            "version": _value(profile, "version", 0),
            "evidence_level": _value(profile, "evidence_level", "none"),
            "original_work_score": _value(profile, "original_work_score", 0.0),
            "ai_relevance": _value(profile, "ai_relevance", 0.0),
            "reliability": _value(profile, "reliability"),
            "summary": _value(profile, "summary", ""),
            "built_at": _value(profile, "built_at"),
            "skills": skill_rows,
        }
        self._write_batch(
            _UPSERT_PROFILE,
            params,
            operation="profile",
            count=1,
            person_id=params["person_id"],
        )

    def upsert_participation(
        self, participation: Participation | Iterable[Participation]
    ) -> None:
        participations = _batch(participation)
        rows = [
            {
                "person_id": _value(item, "person_id", ""),
                "event": _value(item, "event", ""),
                "registered": _value(item, "registered", False),
                "approved": _value(item, "approved"),
                "checked_in": _value(item, "checked_in"),
                "submitted": _value(item, "submitted"),
                "placed": _value(item, "placed"),
                "team_id": _value(item, "team_id") or None,
                "at": _value(item, "at"),
            }
            for item in participations
        ]
        person_id = _value(participations[0], "person_id") if len(participations) == 1 else None
        self._write_batch(
            _UPSERT_PARTICIPATION,
            {"participations": rows},
            operation="participation",
            count=len(rows),
            person_id=person_id,
        )

    def upsert_team(self, team: Team | Iterable[Team]) -> None:
        teams = _batch(team)
        rows = [
            {
                "id": _value(item, "id", ""),
                "event": _value(item, "event", ""),
                "member_ids": list(_value(item, "member_ids", []) or []),
                "coverage": dict(_value(item, "coverage", {}) or {}),
                "balance": _value(item, "balance", 0.0),
                "why": _value(item, "why", ""),
            }
            for item in teams
        ]
        self._write_batch(
            _UPSERT_TEAM,
            {"teams": rows},
            operation="team",
            count=len(rows),
        )

    def upsert_note(self, note: Note | Iterable[Note]) -> None:
        notes = _batch(note)
        rows: list[dict[str, Any]] = []
        for item in notes:
            note_id = _value(item, "id")
            if note_id is None:
                # A note normally receives its SQLite id before this method is
                # called.  Keep direct callers idempotent as well by deriving a
                # stable, non-PII identifier from the note contents.
                fingerprint = "|".join(
                    str(_value(item, field, ""))
                    for field in ("person_id", "text", "author", "at", "kind", "source")
                )
                note_id = f"note-{hashlib.sha1(fingerprint.encode('utf-8')).hexdigest()[:16]}"
            rows.append(
                {
                    "id": note_id,
                    "person_id": _value(item, "person_id") or None,
                    "text": _value(item, "text", ""),
                    "author": _value(item, "author", ""),
                    "at": _value(item, "at"),
                    "kind": _value(item, "kind", "observation"),
                    "source": _value(item, "source", "import"),
                }
            )
        person_id = _value(notes[0], "person_id") if len(notes) == 1 else None
        self._write_batch(
            _UPSERT_NOTE,
            {"notes": rows},
            operation="note",
            count=len(rows),
            person_id=person_id,
        )

    def upsert_alias(self, person_id: str, alias_of: str) -> None:
        if not person_id or not alias_of:
            self._emit("skip", "alias skipped: missing person id")
            return
        self._write_batch(
            _UPSERT_ALIAS,
            {"person_id": person_id, "alias_of": alias_of},
            operation="alias",
            count=1,
            person_id=person_id,
        )

    def aliases(self) -> list[dict[str, Any]]:
        if not self.available():
            return []
        rows = self._execute(_ALIASES, {}, operation="aliases", read=True)
        if not isinstance(rows, list):
            return []
        return [
            {"person_id": row.get("person_id"), "alias_of": row.get("alias_of")}
            for row in rows
        ]

    def returning(self, event: str) -> list[dict[str, Any]]:
        if not self.available() or not event:
            return []
        rows = self._execute(
            _RETURNING,
            {"event": event},
            operation="returning",
            read=True,
        )
        if not isinstance(rows, list):
            return []
        output: list[dict[str, Any]] = []
        for row in rows:
            history = row.get("events") or []
            output.append(
                {
                    "person_id": row.get("person_id"),
                    "events": [
                        {
                            "event": _value(item, "event"),
                            "checked_in": _value(item, "checked_in"),
                            "placed": _value(item, "placed"),
                        }
                        for item in history
                    ],
                }
            )
        return output

    def skill_coverage(self, member_ids: Iterable[str]) -> dict[str, float]:
        ids = list(member_ids)
        if not self.available() or not ids:
            return {}
        rows = self._execute(
            _SKILL_COVERAGE,
            {"member_ids": ids},
            operation="skill coverage",
            read=True,
        )
        if not isinstance(rows, list):
            return {}
        coverage: dict[str, float] = {}
        for row in rows:
            skill = row.get("skill")
            confidence = row.get("confidence")
            if skill is None or confidence is None:
                continue
            value = float(confidence)
            coverage[str(skill)] = max(coverage.get(str(skill), 0.0), value)
        return coverage

    def prior_teammates(self, person_id: str) -> list[dict[str, Any]]:
        if not self.available() or not person_id:
            return []
        rows = self._execute(
            _PRIOR_TEAMMATES,
            {"person_id": person_id},
            operation="prior teammates",
            read=True,
        )
        if not isinstance(rows, list):
            return []
        return [
            {
                "person_id": row.get("person_id"),
                "event": row.get("event"),
                "placed": row.get("placed"),
            }
            for row in rows
        ]

    def cohort_skill_histogram(self) -> dict[str, int]:
        if not self.available():
            return {}
        rows = self._execute(
            _COHORT_SKILL_HISTOGRAM,
            {},
            operation="cohort skill histogram",
            read=True,
        )
        if not isinstance(rows, list):
            return {}
        histogram: dict[str, int] = {}
        for row in rows:
            skill = row.get("skill")
            count = row.get("count", row.get("people", 0))
            if skill is not None:
                histogram[str(skill)] = int(count or 0)
        return histogram

    def close(self) -> None:
        """Close a driver created by this adapter."""

        if self._owns_driver and self._driver is not None:
            close = getattr(self._driver, "close", None)
            if callable(close):
                close()
            self._driver = None

    def __enter__(self) -> "Graph":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


__all__ = ["Graph"]
