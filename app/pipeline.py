"""The local, cache-aware hero pipeline used by the prerun and demo scripts.

Issue #13 is intentionally optional.  This module therefore keeps the runner
small and synchronous: the same per-person function can be replaced by a
workflow runner later, while the laptop demo always has a safe local path.
The runner persists the evidence/profile boundary in SQLite and records a
source-hash snapshot in ``events.data``.  The frozen foundation schema has no
``meta`` table, so using the event row for this tiny piece of run metadata
keeps the change within the issue's ownership boundary.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from . import baseline, events, github_evidence, ingest, profile, rank
from . import graph as graph_module
from .config import get_settings
from .db import (
    EventRow,
    NoteRow,
    ParticipationRow,
    PersonRow,
    ProfileRow,
    RepoRow,
    TeamRow,
    VerdictRow,
    get_session,
    init_db,
)
from .mapping import load as load_mapping
from .models import (
    Evidence,
    Participation,
    Person,
    PipelineEvent,
    Profile,
    RepoEvidence,
)

logger = logging.getLogger(__name__)

_SNAPSHOT_KIND = "pipeline_snapshot"
_DEFAULT_EVENT = "unknown-event"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _event_name(event: Any) -> str:
    if isinstance(event, str):
        return event.strip() or _DEFAULT_EVENT
    return _text(_value(event, "event", "")) or _DEFAULT_EVENT


def _new_run_id(event: Any, run_id: str | None) -> str:
    if run_id:
        return run_id
    inherited = _text(_value(event, "run_id", ""))
    return inherited or f"run-{uuid.uuid4().hex[:12]}"


def _emit(
    *,
    run_id: str,
    stage: str,
    status: str,
    msg: str,
    person_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    """Emit an event without allowing the bus to stop a visible run."""

    try:
        events.emit(
            PipelineEvent(
                ts=_utc_now(),
                run_id=run_id,
                stage=stage,  # type: ignore[arg-type]
                person_id=person_id,
                status=status,  # type: ignore[arg-type]
                msg=msg,
                data=data,
            )
        )
    except Exception:  # pragma: no cover - event persistence is independently defensive
        logger.debug("pipeline event emission failed", exc_info=True)


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_sha256_file(path: str | Path | None) -> str | None:
    if not path:
        return None
    try:
        return _sha256_file(path)
    except (OSError, TypeError, ValueError):
        return None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _same_or_after(value: Any, boundary: datetime) -> bool:
    parsed = _as_datetime(value)
    if parsed is None:
        return False
    return parsed.astimezone(timezone.utc) > boundary.astimezone(timezone.utc)


def _load_people() -> list[Person]:
    init_db()
    with get_session() as session:
        rows = list(session.scalars(select(PersonRow).order_by(PersonRow.source_row, PersonRow.id)).all())
    result: list[Person] = []
    for row in rows:
        try:
            result.append(
                Person.model_validate(
                    {
                        "id": row.id,
                        "name": row.name,
                        "email": row.email,
                        "email_domain": row.email_domain,
                        "github_login": row.github_login,
                        "linkedin_url": row.linkedin_url,
                        "org": row.org,
                        "role": row.role,
                        "is_student": row.is_student,
                        "answers": dict(row.answers or {}),
                        "registered_at": row.registered_at,
                        "alias_of": row.alias_of,
                        "source_file": row.source_file,
                        "source_row": row.source_row,
                    }
                )
            )
        except (TypeError, ValueError):
            logger.debug("ignoring invalid persisted person", extra={"exception": "validation"})
    return result


def list_people() -> list[Person]:
    """Return persisted people for the demo selector and API wiring."""

    return _load_people()


def _load_participations() -> dict[str, list[Participation]]:
    init_db()
    with get_session() as session:
        rows = list(
            session.scalars(
                select(ParticipationRow).order_by(
                    ParticipationRow.person_id,
                    ParticipationRow.event,
                )
            ).all()
        )
    result: dict[str, list[Participation]] = {}
    for row in rows:
        try:
            item = Participation.model_validate(
                {
                    "person_id": row.person_id,
                    "event": row.event,
                    "registered": row.registered,
                    "approved": row.approved,
                    "checked_in": row.checked_in,
                    "submitted": row.submitted,
                    "placed": row.placed,
                    "team_id": row.team_id,
                    "at": row.at,
                }
            )
        except (TypeError, ValueError):
            continue
        result.setdefault(item.person_id, []).append(item)
    return result


def _repo_from_row(row: RepoRow) -> RepoEvidence | None:
    try:
        return RepoEvidence.model_validate(
            {
                "full_name": row.full_name,
                "html_url": row.html_url,
                "is_fork": row.is_fork,
                "stars": row.stars,
                "author_commits_90d": row.author_commits_90d,
                "author_commits_total": row.author_commits_total,
                "primary_language": row.primary_language,
                "last_push": row.last_push,
                "description": row.description,
                "topics": list(row.topics or []),
                "readme_excerpt": row.readme_excerpt,
                "ai_relevance": row.ai_relevance,
            }
        )
    except (TypeError, ValueError):
        return None


def _load_repos() -> dict[str, list[RepoEvidence]]:
    init_db()
    with get_session() as session:
        rows = list(session.scalars(select(RepoRow).order_by(RepoRow.person_id, RepoRow.id)).all())
    result: dict[str, list[RepoEvidence]] = {}
    for row in rows:
        repo = _repo_from_row(row)
        if repo is not None:
            result.setdefault(row.person_id, []).append(repo)
    return result


def _profile_from_row(row: ProfileRow) -> Profile | None:
    try:
        return Profile.model_validate(
            {
                "person_id": row.person_id,
                "version": row.version,
                "built_at": row.built_at,
                "skills": list(row.skills or []),
                "evidence_level": row.evidence_level,
                "original_work_score": row.original_work_score,
                "ai_relevance": row.ai_relevance,
                "reliability": row.reliability,
                "summary": row.summary,
                "evidence": list(row.evidence or []),
                "model_used": row.model_used,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
            }
        )
    except (TypeError, ValueError):
        return None


def _load_profiles() -> dict[str, list[Profile]]:
    init_db()
    with get_session() as session:
        rows = list(
            session.scalars(
                select(ProfileRow).order_by(ProfileRow.person_id, ProfileRow.version, ProfileRow.id)
            ).all()
        )
    result: dict[str, list[Profile]] = {}
    for row in rows:
        item = _profile_from_row(row)
        if item is not None:
            result.setdefault(item.person_id, []).append(item)
    return result


def _latest_profile(profiles: dict[str, list[Profile]], person_id: str) -> Profile | None:
    values = profiles.get(person_id, [])
    return values[-1] if values else None


def _repo_values(value: Any) -> list[RepoEvidence]:
    if value is None:
        return []
    raw = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[RepoEvidence] = []
    for item in raw:
        if isinstance(item, RepoEvidence):
            result.append(item)
        else:
            try:
                result.append(RepoEvidence.model_validate(item))
            except (TypeError, ValueError):
                continue
    return result


def _persist_repos(person_id: str, repos: Iterable[RepoEvidence], *, replace: bool) -> None:
    """Upsert repository evidence, replacing stale rows only on a fresh fetch."""

    values = _repo_values(list(repos))
    init_db()
    with get_session() as session:
        existing = list(session.scalars(select(RepoRow).where(RepoRow.person_id == person_id)).all())
        by_key = {(row.full_name, row.html_url): row for row in existing}
        seen: set[tuple[str, str]] = set()
        for repo in values:
            key = (repo.full_name, repo.html_url)
            seen.add(key)
            row = by_key.get(key)
            payload = repo.model_dump()
            if row is None:
                session.add(RepoRow(person_id=person_id, **payload))
                continue
            for name, value in payload.items():
                setattr(row, name, value)
        if replace:
            for row in existing:
                if (row.full_name, row.html_url) not in seen:
                    session.delete(row)


def _persist_profile(item: Profile) -> None:
    payload = item.model_dump(mode="json")
    init_db()
    with get_session() as session:
        existing = session.scalar(
            select(ProfileRow)
            .where(ProfileRow.person_id == item.person_id, ProfileRow.version == item.version)
            .limit(1)
        )
        if existing is not None:
            return
        session.add(
            ProfileRow(
                person_id=item.person_id,
                version=item.version,
                built_at=item.built_at,
                skills=payload["skills"],
                evidence_level=item.evidence_level,
                original_work_score=item.original_work_score,
                ai_relevance=item.ai_relevance,
                reliability=item.reliability,
                summary=item.summary,
                evidence=payload["evidence"],
                model_used=item.model_used,
                input_tokens=item.input_tokens,
                output_tokens=item.output_tokens,
            )
        )


def _persist_profile_failure_note(person_id: str, *, reason: str) -> None:
    """Route an isolated synthesis failure to the human-review bucket."""

    text = f"Profile synthesis unavailable ({reason}); manual review required."
    init_db()
    with get_session() as session:
        existing = session.scalar(
            select(NoteRow)
            .where(
                NoteRow.person_id == person_id,
                NoteRow.kind == "flag",
                NoteRow.text == text,
            )
            .limit(1)
        )
        if existing is None:
            session.add(
                NoteRow(
                    person_id=person_id,
                    text=text,
                    author="pipeline",
                    at=_utc_now(),
                    kind="flag",
                    source="import",
                )
            )


def _report_dump(report: Any) -> dict[str, Any]:
    if hasattr(report, "__dataclass_fields__"):
        values = {name: getattr(report, name) for name in report.__dataclass_fields__}
    elif hasattr(report, "model_dump"):
        values = report.model_dump()
    elif isinstance(report, Mapping):
        values = dict(report)
    else:
        values = {}
    return {
        "rows": int(_number(values.get("rows"), 0)),
        "persons": int(_number(values.get("persons"), 0)),
        "with_github": int(_number(values.get("with_github"), 0)),
        "aliases": int(_number(values.get("aliases"), 0)),
        "students": int(_number(values.get("students"), 0)),
        "professionals_by_domain": {
            str(key): int(_number(value))
            for key, value in (values.get("professionals_by_domain") or {}).items()
        },
        "errors": [str(item) for item in (values.get("errors") or [])],
    }


def _report_from_payload(payload: Any) -> ingest.IngestReport:
    values = payload if isinstance(payload, Mapping) else {}
    return ingest.IngestReport(
        rows=int(_number(values.get("rows"), 0)),
        persons=int(_number(values.get("persons"), 0)),
        with_github=int(_number(values.get("with_github"), 0)),
        aliases=int(_number(values.get("aliases"), 0)),
        students=int(_number(values.get("students"), 0)),
        professionals_by_domain={
            str(key): int(_number(value))
            for key, value in (values.get("professionals_by_domain") or {}).items()
        },
        errors=[str(item) for item in (values.get("errors") or [])],
    )


def _metadata_row(
    *,
    file_hash: str | None = None,
    event_name: str | None = None,
    snapshot_only: bool = False,
) -> tuple[EventRow | None, dict[str, Any] | None]:
    init_db()
    with get_session() as session:
        rows = list(session.scalars(select(EventRow).order_by(EventRow.id.desc())).all())
    for row in rows:
        data = row.data if isinstance(row.data, dict) else {}
        kind = data.get("kind")
        if kind not in {_SNAPSHOT_KIND, "meta", "run_snapshot"} and "snapshot_ts" not in data:
            continue
        if snapshot_only and not data.get("snapshot_ts"):
            continue
        if file_hash is not None and data.get("file_hash") != file_hash:
            continue
        if event_name is not None and data.get("event") != event_name:
            continue
        return row, data
    return None, None


def _source_cache(
    file_hash: str,
    event_name: str,
) -> tuple[bool, dict[str, Any] | None]:
    _row, data = _metadata_row(file_hash=file_hash, event_name=event_name)
    return bool(data and data.get("status") == "complete"), data


def _fallback_report(path: str | Path) -> ingest.IngestReport:
    """Reconstruct counters if an older snapshot has no report payload."""

    people = [person for person in _load_people() if person.source_file == str(path)]
    aliases = sum(1 for person in people if person.alias_of)
    students = sum(1 for person in people if person.is_student is True and not person.alias_of)
    domains: dict[str, int] = {}
    for person in people:
        if person.alias_of or person.is_student is not False or not person.email_domain:
            continue
        domains[person.email_domain] = domains.get(person.email_domain, 0) + 1
    return ingest.IngestReport(
        rows=len(people),
        persons=len(people) - aliases,
        with_github=sum(bool(person.github_login) for person in people),
        aliases=aliases,
        students=students,
        professionals_by_domain=domains,
    )


def _safe_status(value: Any, default: str = "error") -> str:
    status = _text(value)
    return status or default


def _github_status(result: Any) -> str:
    return _safe_status(_value(result, "status", "error"))


def _github_repos(result: Any) -> list[RepoEvidence]:
    return _repo_values(_value(result, "repos", []))


def _fetch_github(person: Person, *, run_id: str, settings: Any) -> tuple[str, list[RepoEvidence]]:
    if not person.github_login:
        _emit(
            run_id=run_id,
            stage="github",
            status="skip",
            msg="GitHub evidence skipped: no handle",
            person_id=person.id,
            data={"reason": "no_handle"},
        )
        return "not_found", []
    try:
        result = github_evidence.fetch(
            person.github_login,
            settings=settings,
            cache_dir=_value(settings, "cache_dir", "data/cache"),
            run_id=run_id,
            person_id=person.id,
        )
    except TypeError:
        # Small test doubles and an eventual #13 adapter may expose the
        # original one-argument API.  Keep the local fallback compatible.
        result = github_evidence.fetch(person.github_login)
    return _github_status(result), _github_repos(result)


def _registration_evidence(person: Person, now: datetime) -> list[Evidence]:
    source = f"registration://{Path(person.source_file).name or 'source'}#row={person.source_row}"
    claims: list[str] = []
    if person.org:
        claims.append(f"Registration lists the organisation as {person.org}.")
    if person.role:
        claims.append(f"Registration lists the role as {person.role}.")
    if person.is_student is not None:
        claims.append(f"Registration marks student status as {person.is_student}.")
    for key, value in sorted((person.answers or {}).items()):
        if _text(value):
            claims.append(f"Registration answer {key}: {value}.")
    return [
        Evidence(
            claim=claim,
            source_url=source,
            kind="registration",
            confidence=0.7,
            observed_at=(person.registered_at.date() if person.registered_at else now.date()),
        )
        for claim in claims
    ]


def _repo_evidence(repos: Iterable[RepoEvidence], now: datetime) -> list[Evidence]:
    result: list[Evidence] = []
    for repo in repos:
        result.append(
            Evidence(
                claim=(
                    f"Repository {repo.full_name} records {repo.author_commits_total} author commits, "
                    f"including {repo.author_commits_90d} in the last 90 days."
                    + (" It is marked as a fork." if repo.is_fork else "")
                ),
                source_url=repo.html_url,
                kind="repo",
                confidence=max(0.0, min(1.0, 0.5 + repo.author_commits_total / 100)),
                observed_at=(repo.last_push.date() if repo.last_push else now.date()),
            )
        )
    return result


def _deterministic_profile(
    person: Person,
    repos: list[RepoEvidence],
    participations: Iterable[Participation],
    previous: Profile | None,
    *,
    run_id: str,
    reason: str,
) -> Profile:
    now = _utc_now()
    version = previous.version + 1 if previous is not None else 1
    level = profile.evidence_level(repos)
    evidence = _repo_evidence(repos, now) if repos else _registration_evidence(person, now)
    if not repos and not evidence:
        summary = "No external evidence; registration answers empty."
        level = "none"
    elif repos:
        summary = "Deterministic profile from recorded GitHub evidence. [repo]"
    else:
        summary = "Deterministic profile from registration evidence. [registration]"
    item = Profile(
        person_id=person.id,
        version=version,
        built_at=now,
        skills=[],
        evidence_level=level,  # type: ignore[arg-type]
        original_work_score=profile.original_work_score(repos),
        ai_relevance=0.0,
        reliability=profile.reliability(participations),
        summary=summary,
        evidence=evidence,
        model_used=f"deterministic:{reason}",
        input_tokens=0,
        output_tokens=0,
    )
    _emit(
        run_id=run_id,
        stage="profile",
        status="skip",
        msg="profile synthesis skipped; deterministic evidence retained",
        person_id=person.id,
        data={"reason": reason, "version": version},
    )
    return item


def _profile_gate(scoring: Any, people: list[Person], repos: dict[str, list[RepoEvidence]]) -> set[str]:
    """Choose the bounded set where a batch LLM call can change a decision."""

    config = _value(scoring, "llm_gating", {}) or {}
    max_profiles = max(0, int(_number(_value(config, "max_profiles", 300), 300)))
    top_n = max(0, int(_number(_value(config, "always_profile_top_n", 220), 220)))
    near = max(0.0, _number(_value(config, "always_profile_decisions_near_threshold", 0.08), 0.08))
    thresholds = _value(scoring, "thresholds", {}) or {}
    admit = _number(_value(thresholds, "admit", 0.62), 0.62)
    waitlist = _number(_value(thresholds, "waitlist", 0.45), 0.45)

    weights = _value(scoring, "weights", {}) or {}
    original_weight = _number(_value(weights, "original_work", 0.4), 0.4)
    trajectory_weight = _number(_value(weights, "trajectory", 0.25), 0.25)
    claim_weight = _number(_value(weights, "claim_consistency", 0.15), 0.15)

    def deterministic_score(person: Person) -> float:
        original = profile.original_work_score(repos.get(person.id, []))
        # Unsupported components are neutral at t0; this is only a gating
        # estimate, never the persisted verdict.
        return original * original_weight + 0.5 * (trajectory_weight + claim_weight)

    eligible: list[Person] = []
    for person in people:
        has_input = bool(repos.get(person.id)) or any(_text(value) for value in person.answers.values())
        if not has_input:
            continue
        if not repos.get(person.id) and not _text(person.org):
            # Honour the real-dataset skip rule even when the top-N is large.
            continue
        eligible.append(person)

    ordered = sorted(
        eligible,
        key=lambda person: (-deterministic_score(person), person.id),
    )
    selected = {person.id for person in ordered[:top_n]}
    for person in ordered[top_n:]:
        estimated = deterministic_score(person)
        if min(abs(estimated - admit), abs(estimated - waitlist)) <= near:
            selected.add(person.id)
    if len(selected) <= max_profiles:
        return selected
    return {
        person.id
        for person in sorted(
            (candidate for candidate in ordered if candidate.id in selected),
            key=lambda person: (-deterministic_score(person), person.id),
        )[:max_profiles]
    }


def _graph_for_run(settings: Any, run_id: str) -> Any | None:
    enabled = bool(_value(settings, "graph_enabled", False))
    # The foundation defaults GRAPH_ENABLED=true for the eventual deployment,
    # but an empty local .env has no usable Aura credentials.  Avoid a network
    # attempt in that safe local configuration.
    uri = _text(_value(settings, "neo4j_uri", ""))
    password = _text(_value(settings, "neo4j_password", ""))
    if not enabled or not uri or not password:
        return None
    try:
        return graph_module.Graph(settings=settings, run_id=run_id)
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - optional driver
        _emit(
            run_id=run_id,
            stage="graph",
            status="error",
            msg="graph unavailable; continuing with SQLite",
            data={"exception": type(exc).__name__},
        )
        return None


def _graph_person(
    graph: Any | None,
    person: Person,
    item: Profile,
    participations: list[Participation],
    *,
    run_id: str,
) -> None:
    if graph is None:
        _emit(
            run_id=run_id,
            stage="graph",
            status="skip",
            msg="graph skipped: local SQLite path",
            person_id=person.id,
            data={"reason": "graph_disabled_or_unavailable"},
        )
        return
    try:
        graph.upsert_person(person)
        graph.upsert_profile(item)
        if participations:
            graph.upsert_participation(participations)
        if person.alias_of:
            graph.upsert_alias(person.id, person.alias_of)
        _emit(
            run_id=run_id,
            stage="graph",
            status="ok",
            msg="graph context upserted",
            person_id=person.id,
        )
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - external graph failure
        _emit(
            run_id=run_id,
            stage="graph",
            status="error",
            msg="graph upsert failed; continuing with SQLite",
            person_id=person.id,
            data={"exception": type(exc).__name__},
        )


def run_build(
    run_id: str,
    person_ids: Iterable[str],
    *,
    slow: float = 0.0,
    settings: Any | None = None,
    source_cached: bool = False,
    cached_meta: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Run the per-person local fallback, continuing after isolated failures."""

    active_settings = settings or get_settings()
    try:
        delay = max(0.0, float(slow))
    except (TypeError, ValueError):
        delay = 0.0
    people_by_id = {person.id: person for person in _load_people()}
    selected = [people_by_id[person_id] for person_id in person_ids if person_id in people_by_id]
    participations = _load_participations()
    repos_by_person = _load_repos()
    profiles_by_person = _load_profiles()
    scoring = active_settings.scoring()
    graph = _graph_for_run(active_settings, run_id)
    cached_people = cached_meta.get("people", {}) if isinstance(cached_meta, dict) else {}
    if not isinstance(cached_people, Mapping):
        cached_people = {}

    evidence: dict[str, tuple[str, list[RepoEvidence], bool]] = {}
    for person in selected:
        cached_person = cached_people.get(person.id, {})
        if source_cached:
            existing = list(repos_by_person.get(person.id, []))
            status = _safe_status(_value(cached_person, "github_status", "ok" if existing else "not_found"))
            _emit(
                run_id=run_id,
                stage="github",
                status="skip",
                msg="cached GitHub evidence reused",
                person_id=person.id,
                data={"status": status, "cached": True, "repo_count": len(existing)},
            )
            evidence[person.id] = (status, existing, True)
            continue

        try:
            status, repos = _fetch_github(person, run_id=run_id, settings=active_settings)
            if status == "ok":
                _persist_repos(person.id, repos, replace=True)
            evidence[person.id] = (status, repos, False)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - adapter boundary
            _emit(
                run_id=run_id,
                stage="github",
                status="error",
                msg="GitHub evidence failed; continuing",
                person_id=person.id,
                data={"exception": type(exc).__name__},
            )
            evidence[person.id] = ("error", [], False)

    # Evidence is available only after the fetch/cache pass.  Gate the
    # bounded synthesis set from the actual repositories, including the
    # organizer-shaped mapping whose rows have no free-text answers.
    profile_ids = _profile_gate(
        scoring,
        selected,
        {
            person_id: repos
            for person_id, (_status, repos, _cached) in evidence.items()
        },
    )

    results: dict[str, dict[str, Any]] = {}
    for person in selected:
        status, fetched_repos, was_cached = evidence.get(person.id, ("error", [], False))
        repos = fetched_repos if not was_cached else list(repos_by_person.get(person.id, fetched_repos))
        previous = _latest_profile(profiles_by_person, person.id)
        item: Profile | None = None
        profile_cached = False
        try:
            if source_cached and previous is not None:
                item = previous
                profile_cached = True
                _emit(
                    run_id=run_id,
                    stage="profile",
                    status="skip",
                    msg="cached profile reused",
                    person_id=person.id,
                    data={"version": item.version, "cached": True},
                )
            elif person.id not in profile_ids and repos:
                item = _deterministic_profile(
                    person,
                    repos,
                    participations.get(person.id, []),
                    previous,
                    run_id=run_id,
                    reason="budget_gate",
                )
            elif person.id not in profile_ids and not repos and not any(
                _text(value) for value in person.answers.values()
            ):
                # Let the profile lane provide its canonical no-evidence
                # summary and event shape; it never calls the LLM here.
                item = profile.build_profile(
                    person,
                    repos,
                    participations.get(person.id, []),
                    previous,
                    run_id=run_id,
                )
            else:
                try:
                    item = profile.build_profile(
                        person,
                        repos,
                        participations.get(person.id, []),
                        previous,
                        run_id=run_id,
                    )
                except TypeError:
                    item = profile.build_profile(
                        person,
                        repos,
                        participations.get(person.id, []),
                        previous,
                    )
            if item is None:
                raise RuntimeError("profile returned no result")
            _persist_profile(item)
            profiles_by_person.setdefault(person.id, []).append(item)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - isolated profile failure
            _emit(
                run_id=run_id,
                stage="profile",
                status="error",
                msg="profile failed; deterministic fallback retained",
                person_id=person.id,
                data={"exception": type(exc).__name__},
            )
            item = _deterministic_profile(
                person,
                repos,
                participations.get(person.id, []),
                previous,
                run_id=run_id,
                reason="profile_error",
            )
            _persist_profile(item)
            profiles_by_person.setdefault(person.id, []).append(item)

        if item.summary.startswith("LLM synthesis failed") or item.summary.startswith(
            "LLM budget exhausted"
        ) or item.model_used == "deterministic:profile_error":
            try:
                _persist_profile_failure_note(
                    person.id,
                    reason=("budget_exhausted" if "budget exhausted" in item.summary else "synthesis_failed"),
                )
            except Exception:  # pragma: no cover - a note is only a best-effort safety net
                logger.debug("could not persist profile failure note", exc_info=True)

        _graph_person(
            graph,
            person,
            item,
            participations.get(person.id, []),
            run_id=run_id,
        )
        results[person.id] = {
            "person_id": person.id,
            "github_status": status,
            "repo_count": len(repos),
            "profile_version": item.version,
            "profile_cached": profile_cached,
            "github_cached": was_cached,
            "profile": item,
        }
        if delay > 0:
            time.sleep(delay)

    close = getattr(graph, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # pragma: no cover - external driver cleanup
            logger.debug("graph close failed", exc_info=True)
    return results


def _baseline_snapshot(people: list[Person], repos: dict[str, list[RepoEvidence]], scoring: Any, run_id: str) -> tuple[dict[str, float], dict[str, int]]:
    _emit(
        run_id=run_id,
        stage="rank",
        status="start" if people else "skip",
        msg="baseline scoring started" if people else "baseline scoring skipped: no people",
        data={"people": len(people)},
    )
    scores: dict[str, float] = {}
    for person in people:
        try:
            scores[person.id] = float(baseline.baseline_score(repos.get(person.id, []), scoring))
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - malformed source/config
            scores[person.id] = 0.0
            _emit(
                run_id=run_id,
                stage="rank",
                status="error",
                msg="baseline score failed; using zero",
                person_id=person.id,
                data={"exception": type(exc).__name__},
            )
    try:
        ranks = baseline.baseline_rank(scores)
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - optional lane boundary
        ranks = {person_id: index for index, person_id in enumerate(sorted(scores), 1)}
        _emit(
            run_id=run_id,
            stage="rank",
            status="error",
            msg="baseline ranking failed; using deterministic fallback",
            data={"exception": type(exc).__name__},
        )
    _emit(
        run_id=run_id,
        stage="rank",
        status="ok" if people else "skip",
        msg="baseline scoring complete" if people else "baseline scoring skipped: no people",
        data={"people": len(people)},
    )
    return scores, ranks


def run(
    file: str | Path | None,
    mapping: Any | None,
    event: Any,
    person_ids: Iterable[str] | None = None,
    slow: float = 0.0,
    run_id: str | None = None,
    *,
    prev_file: str | Path | None = None,
) -> dict[str, Any]:
    """Run ingest, evidence, profile, baseline, and rank with safe fallbacks."""

    settings = get_settings()
    source = Path(file or settings.registrations_file)
    mapping_value = mapping or settings.mapping_file
    if isinstance(mapping_value, (str, Path)):
        mapping_value = load_mapping(mapping_value)
    event_name = _event_name(event or settings.event_name)
    pipeline_run_id = _new_run_id(event, run_id)
    try:
        delay = max(0.0, float(slow))
    except (TypeError, ValueError):
        delay = 0.0
    result: dict[str, Any] = {
        "run_id": pipeline_run_id,
        "event": event_name,
        "file": str(source),
        "file_hash": None,
        "ingest": ingest.IngestReport(),
        "previous": [],
        "built": {},
        "baseline_scores": {},
        "baseline_ranks": {},
        "verdicts": [],
        "disagreements": [],
    }
    _emit(
        run_id=pipeline_run_id,
        stage="ingest",
        status="start",
        msg="pipeline started",
        data={"event": event_name},
    )

    try:
        source_hash = _sha256_file(source)
        result["file_hash"] = source_hash
    except (OSError, TypeError, ValueError) as exc:
        _emit(
            run_id=pipeline_run_id,
            stage="ingest",
            status="error",
            msg="registration source unavailable",
            data={"exception": type(exc).__name__},
        )
        _emit(
            run_id=pipeline_run_id,
            stage="rank",
            status="skip",
            msg="pipeline skipped: no registration source",
        )
        return result

    source_cached, cached_meta = _source_cache(source_hash, event_name)
    if source_cached:
        report_payload = (cached_meta or {}).get("report")
        result["ingest"] = _report_from_payload(report_payload) if report_payload else _fallback_report(source)
        _emit(
            run_id=pipeline_run_id,
            stage="ingest",
            status="skip",
            msg="registration source already ingested; cache reused",
            data={"file_hash": source_hash, "cached": True},
        )
    else:
        try:
            result["ingest"] = ingest.ingest_registrations(
                source,
                mapping_value,
                event_name,
                run_id=pipeline_run_id,
            )
            _emit(
                run_id=pipeline_run_id,
                stage="ingest",
                status="ok",
                msg="registration ingest complete",
                data={"rows": int(_number(_value(result["ingest"], "rows", 0)))},
            )
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - input adapter boundary
            result["ingest"] = ingest.IngestReport(errors=[f"ingest_failed:{type(exc).__name__}"])
            _emit(
                run_id=pipeline_run_id,
                stage="ingest",
                status="error",
                msg="registration ingest failed; continuing with persisted people",
                data={"exception": type(exc).__name__},
            )

    previous_path = prev_file if prev_file is not None else _value(settings, "prev_event_file", None)
    previous_hash = _optional_sha256_file(previous_path)
    cached_previous_hash = (cached_meta or {}).get("previous_hash") if cached_meta else None
    if previous_path and Path(previous_path).exists() and (not source_cached or previous_hash != cached_previous_hash):
        try:
            result["previous"] = ingest.ingest_previous_event(
                previous_path,
                mapping_value,
                event_name,
                run_id=pipeline_run_id,
            )
            _emit(
                run_id=pipeline_run_id,
                stage="ingest",
                status="ok",
                msg="previous-event participation import complete",
                data={"rows": len(result["previous"])},
            )
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - optional source boundary
            _emit(
                run_id=pipeline_run_id,
                stage="ingest",
                status="error",
                msg="previous-event import failed; continuing",
                data={"exception": type(exc).__name__},
            )
    else:
        _emit(
            run_id=pipeline_run_id,
            stage="ingest",
            status="skip",
            msg="previous-event import skipped",
            data={"reason": "missing_or_cached"},
        )

    people = _load_people()
    selected_ids = list(person_ids) if person_ids is not None else [person.id for person in people]
    if person_ids is not None:
        selected_ids = list(dict.fromkeys(selected_ids))
    try:
        built = run_build(
            pipeline_run_id,
            selected_ids,
            slow=delay,
            settings=settings,
            source_cached=source_cached,
            cached_meta=cached_meta,
        )
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - outer run boundary
        built = {}
        _emit(
            run_id=pipeline_run_id,
            stage="profile",
            status="error",
            msg="local person runner failed; ranking persisted people",
            data={"exception": type(exc).__name__},
        )
    result["built"] = built

    people = _load_people()
    repos = _load_repos()
    scoring = settings.scoring()
    baseline_scores, baseline_ranks = _baseline_snapshot(people, repos, scoring, pipeline_run_id)
    result["baseline_scores"] = baseline_scores
    result["baseline_ranks"] = baseline_ranks

    rank_event = SimpleNamespace(event=event_name, run_id=pipeline_run_id)
    try:
        result["verdicts"] = rank.rank_all(rank_event)
        _emit(
            run_id=pipeline_run_id,
            stage="rank",
            status="ok" if result["verdicts"] else "skip",
            msg="rank complete" if result["verdicts"] else "rank skipped: no verdicts",
            data={"people": len(result["verdicts"])},
        )
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - rank lane boundary
        result["verdicts"] = []
        _emit(
            run_id=pipeline_run_id,
            stage="rank",
            status="error",
            msg="rank failed; pipeline kept partial results",
            data={"exception": type(exc).__name__},
        )
    try:
        result["disagreements"] = rank.disagreements(limit=20)
    except Exception:  # noqa: BLE001  # pragma: no cover - dashboard fallback owns this path
        result["disagreements"] = []

    # A slice is a replay of the prerun state, not a new reset baseline.  Only
    # complete full runs advance the durable snapshot used by reset_demo.
    if person_ids is None:
        snapshot_ts = _utc_now()
        metadata = {
            "kind": _SNAPSHOT_KIND,
            "status": "complete",
            "event": event_name,
            "file_hash": source_hash,
            "snapshot_ts": snapshot_ts.isoformat(),
            "previous_hash": previous_hash,
            "report": _report_dump(result["ingest"]),
            "people": {
                person_id: {
                    "github_status": values.get("github_status", "error"),
                    "profile_version": values.get("profile_version"),
                }
                for person_id, values in built.items()
            },
        }
        _emit(
            run_id=pipeline_run_id,
            stage="ingest",
            status="ok",
            msg="prerun snapshot recorded",
            data=metadata,
        )
    _emit(
        run_id=pipeline_run_id,
        stage="rank",
        status="ok",
        msg="pipeline complete",
        data={"event": event_name, "people": len(people), "selected": len(selected_ids)},
    )
    return result


def reset_demo(event: str | None = None) -> dict[str, Any]:
    """Restore the latest full-prerun snapshot in one small SQLite transaction."""

    settings = get_settings()
    run_id = f"reset-{uuid.uuid4().hex[:12]}"
    _row, metadata = _metadata_row(event_name=event, snapshot_only=True)
    event_name = event or _text((metadata or {}).get("event")) or settings.event_name
    if not metadata:
        _emit(
            run_id=run_id,
            stage="rank",
            status="skip",
            msg="demo reset skipped: no prerun snapshot",
            data={"reason": "snapshot_missing"},
        )
        return {"status": "skip", "reason": "snapshot_missing", "event": event_name}

    snapshot_ts = _as_datetime(metadata.get("snapshot_ts"))
    if snapshot_ts is None:
        _emit(
            run_id=run_id,
            stage="rank",
            status="error",
            msg="demo reset failed: invalid snapshot timestamp",
            data={"reason": "snapshot_invalid"},
        )
        return {"status": "error", "reason": "snapshot_invalid", "event": event_name}

    notes_removed = 0
    verdicts_removed = 0
    teams_removed = 0
    try:
        init_db()
        with get_session() as session:
            notes = list(session.scalars(select(NoteRow)).all())
            for row in notes:
                if _same_or_after(row.at, snapshot_ts):
                    session.delete(row)
                    notes_removed += 1
            verdicts = list(session.scalars(select(VerdictRow)).all())
            for row in verdicts:
                if _same_or_after(row.at, snapshot_ts):
                    session.delete(row)
                    verdicts_removed += 1
            teams = list(session.scalars(select(TeamRow).where(TeamRow.event == event_name)).all())
            for row in teams:
                session.delete(row)
                teams_removed += 1
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - database boundary
        _emit(
            run_id=run_id,
            stage="rank",
            status="error",
            msg="demo reset failed; database transaction rolled back",
            data={"exception": type(exc).__name__},
        )
        return {"status": "error", "reason": type(exc).__name__, "event": event_name}

    payload = {
        "status": "ok",
        "event": event_name,
        "snapshot_ts": snapshot_ts.isoformat(),
        "notes_removed": notes_removed,
        "verdicts_removed": verdicts_removed,
        "teams_removed": teams_removed,
    }
    _emit(
        run_id=run_id,
        stage="rank",
        status="ok",
        msg="demo reset complete",
        data={key: value for key, value in payload.items() if key != "snapshot_ts"},
    )
    return payload


def select_demo_ids(people: Iterable[Person], n: int = 15, ids: Iterable[str] | None = None) -> list[str]:
    """Select planted demo shapes first, then fill deterministically."""

    values = list(people)
    if n <= 0:
        return []
    by_id = {person.id: person for person in values}
    chosen: list[str] = []

    for person_id in ids or ():
        if person_id in by_id and person_id not in chosen:
            chosen.append(person_id)

    def priority(person: Person) -> tuple[int, str, str]:
        text = f"{person.name} {person.github_login or ''}".casefold()
        if "fork" in text:
            bucket = 0
        elif "quiet" in text or "builder" in text:
            bucket = 1
        elif "alias" in text:
            bucket = 2
        elif "rohan" in text or "no-show" in text or "noshow" in text:
            bucket = 3
        else:
            bucket = 4
        return bucket, text, person.id

    for person in sorted(values, key=priority):
        if person.id not in chosen:
            chosen.append(person.id)
        if len(chosen) >= n:
            break
    return chosen[:n]


__all__ = ["list_people", "reset_demo", "run", "run_build", "select_demo_ids"]
