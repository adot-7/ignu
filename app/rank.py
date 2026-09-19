"""Deterministic eligibility, ranking, and verdict persistence.

The ranker is deliberately independent of the profile and GitHub lanes.  It
consumes their frozen models (or their persisted rows), which keeps the first
run usable while either optional lane is unavailable.  In particular, this
module does not treat an absent email as a signal: the current registration
dataset has no email column, so eligibility is derived from role and
organisation text only.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select

from . import baseline, db, events
from .config import get_settings
from .models import (
    Evidence,
    Note,
    Participation,
    Person,
    PipelineEvent,
    Profile,
    RepoEvidence,
    Verdict,
)

logger = logging.getLogger(__name__)

Eligibility = Literal["pass", "fail", "unknown"]

_DEFAULT_PROFESSIONAL_TITLES = (
    "engineer",
    "developer",
    "sde",
    "swe",
    "founder",
    "cto",
    "ceo",
    "manager",
    "consultant",
    "analyst",
    "designer",
    "architect",
    "lead",
    "director",
    "scientist",
    "devops",
    "associate",
    "specialist",
    "head",
)
_DEFAULT_INTERN_TITLES = ("intern", "internship", "trainee")
_DEFAULT_STUDENT_TITLES = (
    "student",
    "undergrad",
    "fresher",
    "b.tech",
    "btech",
    "computer science engineering",
)
_DEFAULT_UNKNOWN_TITLES = ("na", "n/a", "none", "-", "")
_RESERVED_GITHUB_LOGINS = {
    "about",
    "collections",
    "events",
    "explore",
    "features",
    "home",
    "in",
    "issues",
    "login",
    "marketplace",
    "new",
    "notifications",
    "orgs",
    "organizations",
    "pulls",
    "search",
    "settings",
    "sponsors",
    "topics",
    "trending",
}
_COLLEGE_WORDS = (
    "college",
    "university",
    "institute",
    "school",
    "campus",
    "academy",
)
_TEMPORAL_WORDS = (
    "quarter",
    "quarterly",
    "trajectory",
    "rising",
    "increasing",
    "declining",
    "dormant",
    "maintained",
    "abandoned",
)
_TECHNICAL_ROLE_WORDS = (
    "engineer",
    "developer",
    "sde",
    "swe",
    "architect",
    "scientist",
    "devops",
    "cto",
    "programmer",
)
_CLAIM_SKILLS: dict[str, set[str]] = {
    "design": {"design_product"},
    "designer": {"design_product"},
    "frontend": {"frontend"},
    "backend": {"backend"},
    "mobile": {"mobile"},
    "data": {"data"},
    "analyst": {"data"},
    "devops": {"devops_cloud"},
    "cloud": {"devops_cloud"},
    "pitch": {"pitch_comms"},
    "communications": {"pitch_comms"},
    "ml": {"ml_ai"},
    "ai": {"ml_ai"},
}


def _value(source: Any, key: str, default: Any = None) -> Any:
    """Read a field from a model, SQLAlchemy row, namespace, or mapping."""

    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        if math.isnan(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _normalise(value: Any) -> str:
    return " ".join(_text(value).casefold().split())


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, _number(value)))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)):
        return [value]
    if isinstance(value, Mapping):
        return list(value.values())
    try:
        return list(value)
    except TypeError:
        return [value]


def _title_matches(text: Any, titles: Iterable[Any]) -> bool:
    """Case-insensitive title matching without substring false positives.

    A boundary-aware match matters for the ``na``/``none`` unknown values:
    ``Naina`` must not be classified as ``na``.
    """

    candidate = _normalise(text)
    if not candidate:
        return False
    for raw_title in titles:
        title = _normalise(raw_title)
        if not title:
            continue
        pattern = r"(?<!\w)" + re.escape(title).replace(r"\ ", r"\s+") + r"(?!\w)"
        if re.search(pattern, candidate):
            return True
    return False


def _exact_or_title_unknown(text: Any, unknown_titles: Iterable[Any]) -> bool:
    candidate = _normalise(text)
    if not candidate:
        return True
    return _title_matches(candidate, [item for item in unknown_titles if _normalise(item)])


def _is_college(value: Any) -> bool:
    return _title_matches(value, _COLLEGE_WORDS)


def _scoring(scoring: Any | None = None) -> Any:
    if scoring is not None:
        return scoring
    try:
        return get_settings().scoring()
    except Exception:  # pragma: no cover - defensive startup path  # noqa: BLE001
        return {}


def _predicate(
    predicate: Any,
    *,
    person: Any,
    professional_titles: Sequence[Any],
    intern_titles: Sequence[Any],
    student_titles: Sequence[Any],
    unknown_titles: Sequence[Any],
) -> bool | None:
    """Evaluate the small, intentionally declarative eligibility vocabulary."""

    expression = _normalise(predicate)
    role = _text(_value(person, "role"))
    org = _text(_value(person, "org"))
    student_flag = _value(person, "is_student")

    if "role matches professional_titles" in expression:
        return _title_matches(role, professional_titles) and student_flag is not True
    if "role matches intern_titles" in expression:
        return _title_matches(role, intern_titles)
    if "role matches student_titles" in expression:
        return _title_matches(role, student_titles)
    if "org matches student_titles" in expression:
        matches_student_title = _title_matches(org, student_titles)
        if "org is a college" in expression:
            return matches_student_title or _is_college(org)
        return matches_student_title
    if "org is a college" in expression:
        return _is_college(org)
    if "role matches unknown_titles" in expression:
        return _exact_or_title_unknown(role, unknown_titles)
    if "role is blank" in expression:
        return not bool(role)
    if "student_flag == false" in expression or "student flag is false" in expression:
        return student_flag is False
    if "student_flag == true" in expression or "student flag is true" in expression:
        return student_flag is True
    # The pre-amendment rules mentioned email and answer text.  Returning
    # False/None here is deliberate: current dataset semantics never inspect
    # those fields for eligibility.
    if "email" in expression or "answers" in expression:
        return False
    return None


def eligibility(person: Person, notes: Iterable[Note] | None = None, scoring: Any | None = None) -> Eligibility:
    """Return the role/org eligibility gate for ``person``.

    The active dataset has no email or registration-answer signal.  The
    configured ``fail_if_all`` group is evaluated before pass predicates, so a
    student at a college is not accidentally admitted merely because their
    organisation also contains a company-like word.  Notes are accepted for
    the frozen public signature but affect human routing, not this gate.
    """

    del notes
    active = scoring if scoring is not None else _scoring()
    config = _value(active, "eligibility", {}) or {}
    professional = _as_list(_value(config, "professional_titles", _DEFAULT_PROFESSIONAL_TITLES))
    interns = _as_list(_value(config, "intern_titles", _DEFAULT_INTERN_TITLES))
    students = _as_list(_value(config, "student_titles", _DEFAULT_STUDENT_TITLES))
    unknown = _as_list(_value(config, "unknown_titles", _DEFAULT_UNKNOWN_TITLES))
    professional = professional or list(_DEFAULT_PROFESSIONAL_TITLES)
    interns = interns or list(_DEFAULT_INTERN_TITLES)
    students = students or list(_DEFAULT_STUDENT_TITLES)
    unknown = unknown or list(_DEFAULT_UNKNOWN_TITLES)
    rules = _as_list(_value(config, "rules", []))
    rule = rules[0] if rules else {}

    role = _text(_value(person, "role"))
    org = _text(_value(person, "org"))

    fail_predicates = _as_list(_value(rule, "fail_if_all", []))
    if fail_predicates:
        evaluated = [
            _predicate(
                predicate,
                person=person,
                professional_titles=professional,
                intern_titles=interns,
                student_titles=students,
                unknown_titles=unknown,
            )
            for predicate in fail_predicates
        ]
        if evaluated and all(value is True for value in evaluated):
            return "fail"
    elif _title_matches(role, students) and (_title_matches(org, students) or _is_college(org)):
        # Safe fallback for a caller-provided scoring object that omits the
        # amended rule block.
        return "fail"

    unknown_predicates = _as_list(_value(rule, "unknown_if", []))
    for predicate in unknown_predicates:
        value = _predicate(
            predicate,
            person=person,
            professional_titles=professional,
            intern_titles=interns,
            student_titles=students,
            unknown_titles=unknown,
        )
        if value is True:
            return "unknown"

    if _exact_or_title_unknown(role, unknown):
        return "unknown"

    pass_predicates = _as_list(_value(rule, "pass_if_any", []))
    if pass_predicates:
        for predicate in pass_predicates:
            value = _predicate(
                predicate,
                person=person,
                professional_titles=professional,
                intern_titles=interns,
                student_titles=students,
                unknown_titles=unknown,
            )
            if value is True:
                return "pass"
    elif (
        (_title_matches(role, professional) and _value(person, "is_student") is not True)
        or _title_matches(role, interns)
    ):
        return "pass"

    # A non-empty title which matches none of the configured predicates is an
    # unknown, not an automatic failure.  This leaves a human in the loop for
    # unfamiliar professional titles.
    return "unknown"


def _evidence_items(profile: Any) -> list[Any]:
    return _as_list(_value(profile, "evidence", []))


def _evidence_kind(item: Any) -> str:
    return _normalise(_value(item, "kind", ""))


def _has_temporal_evidence(profile: Any, repos: Sequence[Any] = ()) -> bool:
    for key in (
        "trajectory",
        "trajectory_score",
        "commits_by_quarter",
        "quarterly_commits",
        "commit_quarters",
        "temporal_evidence",
    ):
        if _value(profile, key, None) is not None:
            return True
    for repo in repos:
        for key in ("commits_by_quarter", "quarterly_commits", "commit_quarters"):
            if _value(repo, key, None) is not None:
                return True
    for item in _evidence_items(profile):
        claim = _normalise(_value(item, "claim", ""))
        if _evidence_kind(item) == "commit" or any(word in claim for word in _TEMPORAL_WORDS):
            return True
    return False


def _quarter_values(value: Any) -> list[float]:
    if isinstance(value, Mapping):
        value = list(value.values())
    values: list[float] = []
    for item in _as_list(value):
        if isinstance(item, Mapping):
            item = _value(item, "commits", _value(item, "count", None))
        try:
            number = float(item)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            values.append(max(0.0, number))
    return values


def _trend_score(value: Any) -> float | None:
    if isinstance(value, str):
        trend = _normalise(value)
        if any(word in trend for word in ("rising", "increasing", "active", "maintained")):
            return 0.75
        if any(word in trend for word in ("declining", "dormant", "abandoned")):
            return 0.25
        if "flat" in trend or "steady" in trend:
            return 0.5
        return None
    values = _quarter_values(value)
    if len(values) < 2:
        return None
    scale = max(max(values), 1.0)
    delta = max(-1.0, min(1.0, (values[-1] - values[0]) / scale))
    return _clamp(0.5 + 0.5 * delta)


def _trajectory(profile: Any, previous: Any | None, repos: Sequence[Any]) -> float:
    for key in ("trajectory", "trajectory_score", "temporal_evidence"):
        explicit = _value(profile, key, None)
        if explicit is not None:
            trend = _trend_score(explicit)
            if trend is not None:
                return trend

    for key in ("commits_by_quarter", "quarterly_commits", "commit_quarters"):
        values = _value(profile, key, None)
        if values is not None:
            trend = _trend_score(values)
            if trend is not None:
                return trend
    for repo in repos:
        for key in ("commits_by_quarter", "quarterly_commits", "commit_quarters"):
            values = _value(repo, key, None)
            if values is not None:
                trend = _trend_score(values)
                if trend is not None:
                    return trend

    # A profile version is not temporal evidence by itself.  Only compare
    # versions when the evidence explicitly contains a time-series signal.
    if previous is not None and (
        _has_temporal_evidence(profile, repos) or _has_temporal_evidence(previous)
    ):
        delta = _number(_value(profile, "original_work_score")) - _number(
            _value(previous, "original_work_score")
        )
        return _clamp(0.5 + (delta / 2.0))
    return 0.5


def _skills(profile: Any) -> set[str]:
    result: set[str] = set()
    for skill in _as_list(_value(profile, "skills", [])):
        value = _normalise(_value(skill, "skill", skill))
        if value:
            result.add(value)
    return result


def _claim_consistency(profile: Any, person: Any | None, repos: Sequence[Any]) -> float:
    explicit = _value(profile, "claim_consistency", None)
    if explicit is not None:
        return _clamp(explicit)
    if person is None:
        return 0.5

    role = _normalise(_value(person, "role", ""))
    if not role or _exact_or_title_unknown(role, _DEFAULT_UNKNOWN_TITLES):
        return 0.5

    evidence = _evidence_items(profile)
    evidence_level = _normalise(_value(profile, "evidence_level", ""))
    has_external = bool(repos or evidence) and evidence_level != "none"
    claimed_skills: set[str] = set()
    for word, skills in _CLAIM_SKILLS.items():
        if _title_matches(role, [word]):
            claimed_skills.update(skills)
    technical = _title_matches(role, _TECHNICAL_ROLE_WORDS)

    if not has_external:
        return 0.2 if technical else 0.5

    observed_text = " ".join(
        _normalise(_value(repo, field, ""))
        for repo in repos
        for field in ("full_name", "description", "topics", "readme_excerpt", "primary_language")
    )
    observed_skills = _skills(profile)
    if claimed_skills & observed_skills:
        return 0.9
    if claimed_skills and any(skill.replace("_", " ") in observed_text for skill in claimed_skills):
        return 0.8
    if technical and (repos or any(_evidence_kind(item) in {"repo", "commit", "readme"} for item in evidence)):
        return 0.75
    # Evidence exists but does not support a specific claim.  It is neutral,
    # not a penalty, for non-technical or unfamiliar role text.
    return 0.5


def components(
    profile: Profile,
    participations: Iterable[Participation] | None = None,
    prev_profile: Profile | None = None,
    *,
    person: Person | None = None,
    repos: Iterable[RepoEvidence] | None = None,
) -> dict[str, float]:
    """Return the amended scoring components in the configured vocabulary.

    ``participations`` is retained in the contract, but reliability is always
    neutral at t0: the organizer dataset has no attendance source.  Likewise,
    a profile version alone cannot create trajectory; an explicit GitHub time
    series or temporal evidence is required.
    """

    del participations
    repo_list = list(repos or _as_list(_value(profile, "repos", [])))
    return {
        "original_work": _clamp(_value(profile, "original_work_score", 0.0)),
        "reliability": 0.5,
        "ai_relevance": _clamp(_value(profile, "ai_relevance", 0.0)),
        "trajectory": _trajectory(profile, prev_profile, repo_list),
        "claim_consistency": _claim_consistency(profile, person, repo_list),
    }


def _repo_date(value: Any, fallback: date) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    return fallback


def _fallback_original_work(repos: Sequence[RepoEvidence], today: date) -> float:
    if not repos:
        return 0.0
    ordered = sorted(
        repos,
        key=lambda repo: (
            -int(_number(_value(repo, "author_commits_total", 0))),
            -int(_number(_value(repo, "author_commits_90d", 0))),
            _normalise(_value(repo, "full_name", "")),
        ),
    )[:3]
    values: list[float] = []
    for repo in ordered:
        total = max(0, int(_number(_value(repo, "author_commits_total", 0))))
        recent = max(0, int(_number(_value(repo, "author_commits_90d", 0))))
        is_fork = bool(_value(repo, "is_fork", False))
        author_factor = 0.0 if is_fork and total < 5 else 1.0
        last_push = _repo_date(_value(repo, "last_push", None), date.min)
        recent_push = last_push != date.min and today - timedelta(days=180) <= last_push <= today
        values.append(
            author_factor * min(1.0, math.log1p(total) / math.log1p(50)) * 0.6
            + min(1.0, recent / 20.0) * 0.3
            + (0.1 if recent_push else 0.0)
        )
    non_forks = sum(not bool(_value(repo, "is_fork", False)) for repo in repos)
    return _clamp(sum(values) / len(values) * (0.7 + 0.1 * min(3, non_forks)))


def _fallback_profile(person: Person, repos: Sequence[RepoEvidence], now: datetime) -> Profile:
    today = now.date()
    original = _fallback_original_work(repos, today)
    qualifying = [
        repo
        for repo in repos
        if not bool(_value(repo, "is_fork", False))
        and int(_number(_value(repo, "author_commits_total", 0))) >= 10
    ]
    recent_commits = sum(int(_number(_value(repo, "author_commits_90d", 0))) for repo in repos)
    if not repos:
        level = "none"
    elif len(qualifying) >= 2 or recent_commits >= 50:
        level = "strong"
    elif qualifying:
        level = "solid"
    else:
        level = "thin"
    evidence: list[Evidence] = []
    for repo in sorted(repos, key=lambda item: _normalise(_value(item, "full_name", ""))):
        observed = _repo_date(_value(repo, "last_push", None), today)
        source = _text(_value(repo, "html_url", "")) or "github"
        evidence.append(
            Evidence(
                claim=f"Repository {_text(_value(repo, 'full_name', ''))} was observed",
                source_url=source,
                kind="repo",
                confidence=0.5,
                observed_at=observed,
            )
        )
    ai_values = [_clamp(_value(repo, "ai_relevance", 0.0)) for repo in repos]
    return Profile(
        person_id=person.id,
        version=1,
        built_at=now,
        skills=[],
        evidence_level=level,  # type: ignore[arg-type]
        original_work_score=original,
        ai_relevance=sum(ai_values) / len(ai_values) if ai_values else 0.0,
        reliability=None,
        summary=(
            "No external evidence; registration answers empty."
            if not repos
            else "Deterministic profile from recorded GitHub evidence."
        ),
        evidence=evidence,
        model_used="deterministic-rank-fallback",
        input_tokens=0,
        output_tokens=0,
    )


def _person_from_row(row: Any) -> Person | None:
    if isinstance(row, Person):
        return row
    payload = {
        "id": _value(row, "id"),
        "name": _text(_value(row, "name", "")),
        "email": _text(_value(row, "email", "")),
        "email_domain": _text(_value(row, "email_domain", "")),
        "github_login": _value(row, "github_login", None),
        "linkedin_url": _value(row, "linkedin_url", None),
        "org": _value(row, "org", None),
        "role": _value(row, "role", None),
        "is_student": _value(row, "is_student", None),
        "answers": dict(_value(row, "answers", {}) or {}),
        "registered_at": _value(row, "registered_at", None),
        "alias_of": _value(row, "alias_of", None),
        "source_file": _text(_value(row, "source_file", "")),
        "source_row": int(_number(_value(row, "source_row", 0))),
    }
    if not payload["id"]:
        return None
    try:
        return Person.model_validate(payload)
    except (TypeError, ValueError, ValidationError):
        return None


def _repo_from_row(row: Any) -> RepoEvidence | None:
    if isinstance(row, RepoEvidence):
        return row
    payload = {
        "full_name": _text(_value(row, "full_name", "")),
        "html_url": _text(_value(row, "html_url", "")),
        "is_fork": bool(_value(row, "is_fork", False)),
        "stars": int(_number(_value(row, "stars", 0))),
        "author_commits_90d": int(_number(_value(row, "author_commits_90d", 0))),
        "author_commits_total": int(_number(_value(row, "author_commits_total", 0))),
        "primary_language": _value(row, "primary_language", None),
        "last_push": _value(row, "last_push", None),
        "description": _value(row, "description", None),
        "topics": list(_value(row, "topics", []) or []),
        "readme_excerpt": _value(row, "readme_excerpt", None),
        "ai_relevance": _number(_value(row, "ai_relevance", 0.0)),
    }
    try:
        return RepoEvidence.model_validate(payload)
    except (TypeError, ValueError, ValidationError):
        return None


def _profile_from_row(row: Any) -> Profile | None:
    if isinstance(row, Profile):
        return row
    payload = {
        "person_id": _value(row, "person_id"),
        "version": int(_number(_value(row, "version", 1), 1)),
        "built_at": _value(row, "built_at", datetime.now(timezone.utc)),
        "skills": list(_value(row, "skills", []) or []),
        "evidence_level": _value(row, "evidence_level", "none"),
        "original_work_score": _number(_value(row, "original_work_score", 0.0)),
        "ai_relevance": _number(_value(row, "ai_relevance", 0.0)),
        "reliability": _value(row, "reliability", None),
        "summary": _text(_value(row, "summary", "")),
        "evidence": list(_value(row, "evidence", []) or []),
        "model_used": _text(_value(row, "model_used", "")),
        "input_tokens": int(_number(_value(row, "input_tokens", 0))),
        "output_tokens": int(_number(_value(row, "output_tokens", 0))),
    }
    try:
        return Profile.model_validate(payload)
    except (TypeError, ValueError, ValidationError):
        return None


def _participation_from_row(row: Any) -> Participation | None:
    if isinstance(row, Participation):
        return row
    payload = {
        "person_id": _value(row, "person_id"),
        "event": _text(_value(row, "event", "")),
        "registered": bool(_value(row, "registered", True)),
        "approved": _value(row, "approved", None),
        "checked_in": _value(row, "checked_in", None),
        "submitted": _value(row, "submitted", None),
        "placed": _value(row, "placed", None),
        "team_id": _value(row, "team_id", None),
        "at": _value(row, "at", None),
    }
    try:
        return Participation.model_validate(payload)
    except (TypeError, ValueError, ValidationError):
        return None


def _note_from_row(row: Any) -> Note | None:
    if isinstance(row, Note):
        return row
    payload = {
        "id": _value(row, "id", None),
        "person_id": _value(row, "person_id", None),
        "text": _text(_value(row, "text", "")),
        "author": _text(_value(row, "author", "")),
        "at": _value(row, "at", datetime.now(timezone.utc)),
        "kind": _value(row, "kind", "observation"),
        "source": _value(row, "source", "import"),
    }
    try:
        return Note.model_validate(payload)
    except (TypeError, ValueError, ValidationError):
        return None


@dataclass
class _Context:
    people: dict[str, Person]
    profiles: dict[str, list[Profile]]
    repos: dict[str, list[RepoEvidence]]
    participations: dict[str, list[Participation]]
    notes: dict[str, list[Note]]


def _flatten(source: Any) -> list[Any]:
    if source is None:
        return []
    if isinstance(source, Mapping):
        result: list[Any] = []
        for value in source.values():
            if isinstance(value, (list, tuple, set)):
                result.extend(value)
            else:
                result.append(value)
        return result
    return _as_list(source)


def _load_context(
    *,
    people: Any | None = None,
    profiles: Any | None = None,
    repos: Any | None = None,
    participations: Any | None = None,
    notes: Any | None = None,
) -> _Context:
    """Load persisted context, with optional in-memory inputs for offline runs."""

    db.init_db()
    if people is None:
        with db.get_session() as session:
            people_values = list(session.scalars(select(db.PersonRow).order_by(db.PersonRow.id)).all())
    else:
        people_values = _flatten(people)
    person_map = {
        person.id: person
        for value in people_values
        if (person := _person_from_row(value)) is not None
    }

    if profiles is None:
        with db.get_session() as session:
            profile_values = list(
                session.scalars(
                    select(db.ProfileRow).order_by(
                        db.ProfileRow.person_id,
                        db.ProfileRow.version,
                        db.ProfileRow.id,
                    )
                ).all()
            )
    else:
        profile_values = _flatten(profiles)
    profile_map: dict[str, list[Profile]] = {}
    for value in profile_values:
        profile = _profile_from_row(value)
        if profile is not None:
            profile_map.setdefault(profile.person_id, []).append(profile)
    for values in profile_map.values():
        values.sort(key=lambda item: (item.version, _datetime_sort_key(item.built_at)))

    repo_owners: dict[int, str] = {}
    if repos is None:
        with db.get_session() as session:
            repo_values = list(
                session.scalars(select(db.RepoRow).order_by(db.RepoRow.person_id, db.RepoRow.id)).all()
            )
    else:
        if isinstance(repos, Mapping):
            repo_values = []
            for owner, values in repos.items():
                for value in _as_list(values):
                    repo_owners[id(value)] = _text(owner)
                    repo_values.append(value)
        else:
            repo_values = _flatten(repos)
    repo_map: dict[str, list[RepoEvidence]] = {}
    seen_repos: set[tuple[str, str, str]] = set()
    for value in repo_values:
        repo = _repo_from_row(value)
        person_id = _text(_value(value, "person_id", "")) or repo_owners.get(id(value), "")
        if repo is None or not person_id:
            continue
        key = (person_id, repo.full_name, repo.html_url)
        if key in seen_repos:
            continue
        seen_repos.add(key)
        repo_map.setdefault(person_id, []).append(repo)
    for values in repo_map.values():
        values.sort(key=lambda item: _normalise(item.full_name))

    if participations is None:
        with db.get_session() as session:
            participation_values = list(
                session.scalars(
                    select(db.ParticipationRow).order_by(
                        db.ParticipationRow.person_id,
                        db.ParticipationRow.event,
                    )
                ).all()
            )
    else:
        participation_values = _flatten(participations)
    participation_map: dict[str, list[Participation]] = {}
    for value in participation_values:
        participation = _participation_from_row(value)
        if participation is not None:
            participation_map.setdefault(participation.person_id, []).append(participation)

    if notes is None:
        with db.get_session() as session:
            note_values = list(
                session.scalars(select(db.NoteRow).order_by(db.NoteRow.person_id, db.NoteRow.id)).all()
            )
    else:
        note_values = _flatten(notes)
    note_map: dict[str, list[Note]] = {}
    for value in note_values:
        note = _note_from_row(value)
        if note is not None and note.person_id:
            note_map.setdefault(note.person_id, []).append(note)
    for values in note_map.values():
        values.sort(key=lambda item: (_datetime_sort_key(item.at), item.id or 0))

    return _Context(person_map, profile_map, repo_map, participation_map, note_map)


def _datetime_sort_key(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def _duplicate_ids(people: Iterable[Person]) -> set[str]:
    by_login: dict[str, list[str]] = {}
    by_name_org: dict[tuple[str, str], list[str]] = {}
    for person in people:
        login = _normalise(_value(person, "github_login", ""))
        if login and login not in _RESERVED_GITHUB_LOGINS:
            by_login.setdefault(login, []).append(person.id)
        name_org = (_normalise(person.name), _normalise(person.org))
        if name_org[0] and name_org[1]:
            by_name_org.setdefault(name_org, []).append(person.id)
    duplicates: set[str] = set()
    for values in (*by_login.values(), *by_name_org.values()):
        if len(values) > 1:
            duplicates.update(values)
    for person in people:
        if _value(person, "alias_of", None):
            duplicates.add(person.id)
    return duplicates


def _invalid_github(person: Any) -> bool:
    login = _normalise(_value(person, "github_login", ""))
    if "/" in login:
        login = login.rstrip("/").rsplit("/", 1)[-1]
    return bool(_value(person, "github_login_invalid", False)) or login in _RESERVED_GITHUB_LOGINS


def _has_github_evidence(person: Person, profile: Profile) -> bool:
    # A profile marked none represents no handle, a GitHub 403/404, or another
    # unavailable external result.  All such cases stay in needs_human.
    level = _normalise(_value(profile, "evidence_level", "none"))
    return bool(_value(person, "github_login", None)) and level != "none"


def _weights(scoring: Any) -> dict[str, float]:
    configured = _value(scoring, "weights", {}) or {}
    defaults = {
        "original_work": 0.40,
        "trajectory": 0.25,
        "ai_relevance": 0.20,
        "claim_consistency": 0.15,
    }
    return {
        name: _number(_value(configured, name, default), default)
        for name, default in defaults.items()
    }


def _note_adjustments(scoring: Any) -> dict[str, Any]:
    configured = _value(scoring, "note_adjustments", {}) or {}
    return {
        "flag": _number(_value(configured, "flag", -0.25), -0.25),
        "praise": _number(_value(configured, "praise", 0.15), 0.15),
        "override": _value(configured, "override", "force_human"),
    }


def composite_score(
    component_values: Mapping[str, float],
    scoring: Any | None = None,
    notes: Iterable[Note] | None = None,
) -> float:
    """Apply the active weights and note adjustments to component values."""

    active = scoring if scoring is not None else _scoring()
    weights = _weights(active)
    total = sum(weights[name] * _number(component_values.get(name, 0.5), 0.5) for name in weights)
    adjustments = _note_adjustments(active)
    for note in notes or ():
        kind = _note_kind(note)
        if kind == "flag":
            total += adjustments["flag"]
        elif kind == "praise":
            total += adjustments["praise"]
    return _clamp(total)


def score(
    component_values: Mapping[str, float],
    scoring: Any | None = None,
    notes: Iterable[Note] | None = None,
) -> float:
    """Backward-friendly alias for :func:`composite_score`."""

    return composite_score(component_values, scoring, notes)


def _note_kind(note: Any) -> str:
    return _normalise(_value(note, "kind", ""))


def _note_date(note: Any) -> str:
    value = _value(note, "at", None)
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _text(value)[:10] or "unknown date"


def _evidence_ids(profile: Profile, notes: Sequence[Note]) -> list[int]:
    # Profile evidence has no standalone table in the frozen schema, so its
    # stable local IDs are one-based positions.  Note database IDs are added
    # when available.  ``0`` is the reserved registration/GitHub source ID for
    # a verdict with no profile or note row; this keeps every verdict sourced.
    result: list[int] = list(range(1, len(_evidence_items(profile)) + 1))
    for note in notes:
        note_id = _value(note, "id", None)
        if isinstance(note_id, int) and note_id > 0:
            result.append(note_id)
    if not result:
        result.append(0)
    return list(dict.fromkeys(result))


def _source_label(profile: Profile) -> str:
    evidence = _evidence_items(profile)
    if evidence:
        return "profile evidence #1"
    return "registration/GitHub evidence"


def _original_reason(profile: Profile, repos: Sequence[RepoEvidence]) -> str:
    source = _source_label(profile)
    if repos and all(bool(_value(repo, "is_fork", False)) for repo in repos):
        own_commits = sum(int(_number(_value(repo, "author_commits_total", 0))) for repo in repos)
        return f"{len(repos)} repos, all forks, {own_commits} own commits ({source})"
    if not repos and _normalise(_value(profile, "evidence_level", "none")) == "none":
        return f"original work: 0.00; no GitHub evidence ({source})"
    return f"original work: {_clamp(_value(profile, 'original_work_score', 0.0)):.2f} ({source})"


def _note_human_reason(note: Note) -> str:
    kind = _note_kind(note)
    label = "flagged" if kind == "flag" else "override recorded" if kind == "override" else kind
    return f"note: {label} by organizer on {_note_date(note)} (note evidence)"


def _reason_list(
    *,
    profile: Profile,
    repos: Sequence[RepoEvidence],
    components_value: Mapping[str, float],
    gate: Eligibility,
    score: float,
    human_flags: Sequence[str],
    notes: Sequence[Note],
) -> list[str]:
    reasons: list[str] = [f"eligibility: {gate} from registration role/org"]
    reasons.append(_original_reason(profile, repos))
    reasons.extend(human_flags)
    if not human_flags:
        reasons.append(
            "score "
            f"{score:.2f}: trajectory {components_value['trajectory']:.2f}, "
            f"AI relevance {components_value['ai_relevance']:.2f}, "
            f"claim consistency {components_value['claim_consistency']:.2f} "
            f"({_source_label(profile)})"
        )
    elif notes:
        # Keep the date-bearing note reason visible even when another human
        # trigger (for example an alias) is also present.
        reasons.append(_note_human_reason(notes[0]))
    # Four concise reasons are part of the dashboard contract.  Preserve
    # insertion order so equal-score runs remain explainable and deterministic.
    result: list[str] = []
    for reason in reasons:
        if reason and reason not in result:
            result.append(reason)
        if len(result) >= 4:
            break
    return result or [f"eligibility: {gate} from registration role/org"]


def _human_flags(
    *,
    person: Person,
    profile: Profile,
    components_value: Mapping[str, float],
    gate: Eligibility,
    notes: Sequence[Note],
    duplicate: bool,
    scoring: Any,
) -> list[str]:
    flags: list[str] = []
    if gate == "unknown":
        flags.append("needs human: eligibility is unknown (registration role/org)")
    if _invalid_github(person):
        flags.append("needs human: invalid GitHub login (registration/GitHub evidence)")
    if not _has_github_evidence(person, profile):
        flags.append("needs human: no usable GitHub evidence (registration/GitHub evidence)")
    if _value(person, "alias_of", None):
        flags.append("needs human: alias registration requires review (registration)")
    elif duplicate:
        flags.append("needs human: duplicate candidate requires review (registration)")
    low_claim = _number(components_value.get("claim_consistency", 0.5)) < 0.25
    if low_claim:
        flags.append("needs human: claim consistency is below 0.25 (profile evidence)")

    for note in notes:
        kind = _note_kind(note)
        if kind in {"flag", "override"}:
            flags.append(_note_human_reason(note))
    # The active config can add a future human predicate without allowing it to
    # remove the mandatory amended-dataset gates above.
    configured = {_normalise(item) for item in _as_list(_value(scoring, "needs_human_when", []))}
    if "eligibility == unknown" in configured and gate == "unknown":
        flags.append("needs human: configured eligibility rule (registration)")
    return list(dict.fromkeys(flags))


@dataclass
class _Computed:
    person: Person
    profile: Profile
    repos: list[RepoEvidence]
    notes: list[Note]
    gate: Eligibility
    components: dict[str, float]
    score: float
    decision: str
    baseline_score: float
    baseline_rank: int
    ignu_rank: int = 0
    reasons: list[str] | None = None
    evidence_ids: list[int] | None = None


def _compute_records(context: _Context, scoring: Any, now: datetime) -> list[_Computed]:
    people = sorted(context.people.values(), key=lambda person: person.id)
    baseline_scores: dict[str, float] = {}
    for person in people:
        try:
            baseline_scores[person.id] = baseline.baseline_score(
                context.repos.get(person.id, []), scoring
            )
        except Exception:  # pragma: no cover - malformed YAML should not stop a run  # noqa: BLE001
            baseline_scores[person.id] = 0.0
    try:
        baseline_ranks = baseline.baseline_rank(baseline_scores)
    except Exception:  # pragma: no cover - unavailable baseline lane  # noqa: BLE001
        baseline_ranks = {
            person_id: rank
            for rank, person_id in enumerate(sorted(baseline_scores), 1)
        }

    duplicate_ids = _duplicate_ids(people)
    records: list[_Computed] = []
    for person in people:
        repos = context.repos.get(person.id, [])
        profiles = context.profiles.get(person.id, [])
        profile = profiles[-1] if profiles else _fallback_profile(person, repos, now)
        previous = profiles[-2] if len(profiles) >= 2 else None
        person_notes = context.notes.get(person.id, [])
        values = components(
            profile,
            context.participations.get(person.id, []),
            previous,
            person=person,
            repos=repos,
        )
        gate = eligibility(person, person_notes, scoring)
        score_value = composite_score(values, scoring, person_notes)
        human_flags = _human_flags(
            person=person,
            profile=profile,
            components_value=values,
            gate=gate,
            notes=person_notes,
            duplicate=person.id in duplicate_ids,
            scoring=scoring,
        )
        if human_flags:
            decision = "needs_human"
        elif gate == "fail":
            decision = "decline"
        elif gate == "unknown":
            decision = "needs_human"
        else:
            thresholds = _value(scoring, "thresholds", {}) or {}
            admit = _number(_value(thresholds, "admit", 0.62), 0.62)
            waitlist = _number(_value(thresholds, "waitlist", 0.45), 0.45)
            decision = "admit" if score_value >= admit else "waitlist" if score_value >= waitlist else "decline"
        reasons = _reason_list(
            profile=profile,
            repos=repos,
            components_value=values,
            gate=gate,
            score=score_value,
            human_flags=human_flags,
            notes=person_notes,
        )
        records.append(
            _Computed(
                person=person,
                profile=profile,
                repos=repos,
                notes=person_notes,
                gate=gate,
                components=values,
                score=score_value,
                decision=decision,
                baseline_score=baseline_scores.get(person.id, 0.0),
                baseline_rank=baseline_ranks.get(person.id, len(people)),
                reasons=reasons,
                evidence_ids=_evidence_ids(profile, person_notes),
            )
        )

    ordered = sorted(records, key=lambda item: (-item.score, item.person.id))
    for rank, record in enumerate(ordered, 1):
        record.ignu_rank = rank
    return records


def _event_details(event: Any) -> tuple[str, str]:
    if isinstance(event, str):
        event_name = event.strip() or "unknown-event"
        return event_name, f"rank-{uuid4().hex[:12]}"
    event_name = _text(_value(event, "event", "")) or "unknown-event"
    run_id = _text(_value(event, "run_id", "")) or f"rank-{uuid4().hex[:12]}"
    return event_name, run_id


def _emit(
    *,
    run_id: str,
    person_id: str | None,
    status: str,
    msg: str,
    data: dict[str, Any] | None = None,
) -> None:
    try:
        events.emit(
            PipelineEvent(
                ts=datetime.now(timezone.utc),
                run_id=run_id,
                stage="rank",
                person_id=person_id,
                status=status,  # type: ignore[arg-type]
                msg=msg,
                data=data,
            )
        )
    except Exception:  # pragma: no cover - event persistence is best effort
        logger.debug("could not emit rank pipeline event", exc_info=True)


def _verdict(record: _Computed, at: datetime) -> Verdict:
    return Verdict(
        person_id=record.person.id,
        decision=record.decision,  # type: ignore[arg-type]
        score=record.score,
        eligibility=record.gate,
        baseline_score=record.baseline_score,
        baseline_rank=record.baseline_rank,
        ignu_rank=record.ignu_rank,
        reasons=list(record.reasons or []),
        evidence_ids=list(record.evidence_ids or [0]),
        profile_version=record.profile.version,
        at=at,
    )


def _persist_verdict(verdict: Verdict) -> None:
    # Never query/update an existing verdict here.  A new row is the history;
    # readers select the greatest row id as the latest decision.
    with db.get_session() as session:
        session.add(
            db.VerdictRow(
                person_id=verdict.person_id,
                decision=verdict.decision,
                score=verdict.score,
                eligibility=verdict.eligibility,
                baseline_score=verdict.baseline_score,
                baseline_rank=verdict.baseline_rank,
                ignu_rank=verdict.ignu_rank,
                reasons=list(verdict.reasons),
                evidence_ids=list(verdict.evidence_ids),
                profile_version=verdict.profile_version,
                at=verdict.at,
            )
        )


def rank_all(
    event: Any,
    *,
    people: Any | None = None,
    profiles: Any | None = None,
    repos: Any | None = None,
    participations: Any | None = None,
    notes: Any | None = None,
) -> list[Verdict]:
    """Compute and append verdicts for every person in the current context."""

    event_name, run_id = _event_details(event)
    try:
        context = _load_context(
            people=people,
            profiles=profiles,
            repos=repos,
            participations=participations,
            notes=notes,
        )
    except Exception as exc:  # pragma: no cover - protects a visible demo run  # noqa: BLE001
        _emit(
            run_id=run_id,
            person_id=None,
            status="error",
            msg="rank context unavailable",
            data={"reason": type(exc).__name__},
        )
        return []

    _emit(
        run_id=run_id,
        person_id=None,
        status="start" if context.people else "skip",
        msg="ranking started" if context.people else "ranking skipped: no people",
        data={"event": event_name, "people": len(context.people)},
    )
    if not context.people:
        return []

    scoring = _scoring()
    now = datetime.now(timezone.utc)
    records = _compute_records(context, scoring, now)
    result: list[Verdict] = []
    for record in sorted(records, key=lambda item: item.person.id):
        verdict = _verdict(record, now)
        try:
            _persist_verdict(verdict)
            status = "ok"
            msg = "verdict persisted"
        except Exception as exc:  # pragma: no cover - DB failures should not kill the run
            status = "error"
            msg = "verdict computed but persistence failed"
            logger.debug("could not persist verdict", exc_info=True)
            _emit(
                run_id=run_id,
                person_id=record.person.id,
                status=status,
                msg=msg,
                data={"reason": type(exc).__name__},
            )
        else:
            _emit(
                run_id=run_id,
                person_id=record.person.id,
                status=status,
                msg=msg,
                data={"decision": verdict.decision, "ignu_rank": verdict.ignu_rank},
            )
        result.append(verdict)
    _emit(
        run_id=run_id,
        person_id=None,
        status="ok",
        msg="ranking complete",
        data={"event": event_name, "people": len(result)},
    )
    return sorted(result, key=lambda item: (item.ignu_rank, item.person_id))


def _latest_verdict(person_id: str) -> Verdict | None:
    db.init_db()
    with db.get_session() as session:
        row = session.scalar(
            select(db.VerdictRow)
            .where(db.VerdictRow.person_id == person_id)
            .order_by(db.VerdictRow.id.desc())
            .limit(1)
        )
    if row is None:
        return None
    try:
        return Verdict.model_validate(
            {
                "person_id": row.person_id,
                "decision": row.decision,
                "score": row.score,
                "eligibility": row.eligibility,
                "baseline_score": row.baseline_score,
                "baseline_rank": row.baseline_rank,
                "ignu_rank": row.ignu_rank,
                "reasons": list(row.reasons or []),
                "evidence_ids": list(row.evidence_ids or []),
                "profile_version": row.profile_version,
                "at": row.at,
            }
        )
    except (TypeError, ValueError, ValidationError):
        return None


def rerank_person(person_id: str) -> dict[str, str | None]:
    """Append one updated verdict after a note or other context change."""

    previous = _latest_verdict(person_id)
    run_id = f"rerank-{uuid4().hex[:12]}"
    try:
        context = _load_context()
    except Exception as exc:  # pragma: no cover - visible memory path degrades  # noqa: BLE001
        _emit(
            run_id=run_id,
            person_id=person_id,
            status="error",
            msg="rerank context unavailable",
            data={"reason": type(exc).__name__},
        )
        return {
            "old_decision": previous.decision if previous else None,
            "new_decision": None,
        }
    if person_id not in context.people:
        _emit(
            run_id=run_id,
            person_id=person_id,
            status="skip",
            msg="rerank skipped: person not found",
        )
        return {
            "old_decision": previous.decision if previous else None,
            "new_decision": None,
        }

    _emit(run_id=run_id, person_id=person_id, status="start", msg="rerank started")
    now = datetime.now(timezone.utc)
    records = _compute_records(context, _scoring(), now)
    record = next(item for item in records if item.person.id == person_id)
    verdict = _verdict(record, now)
    try:
        _persist_verdict(verdict)
    except Exception as exc:  # pragma: no cover - memory writes must degrade  # noqa: BLE001
        _emit(
            run_id=run_id,
            person_id=person_id,
            status="error",
            msg="rerank computed but persistence failed",
            data={"reason": type(exc).__name__},
        )
    else:
        _emit(
            run_id=run_id,
            person_id=person_id,
            status="ok",
            msg="rerank complete",
            data={"old_decision": previous.decision if previous else None, "new_decision": verdict.decision},
        )
    return {
        "old_decision": previous.decision if previous else None,
        "new_decision": verdict.decision,
    }


def disagreements(limit: int = 20) -> list[dict[str, Any]]:
    """Return the latest large baseline/ignu rank gaps."""

    if limit <= 0:
        return []
    try:
        db.init_db()
        with db.get_session() as session:
            people = {
                row.id: row.name
                for row in session.scalars(select(db.PersonRow)).all()
            }
            rows = list(
                session.scalars(select(db.VerdictRow).order_by(db.VerdictRow.id)).all()
            )
    except Exception:  # pragma: no cover - dashboard should render an empty table  # noqa: BLE001
        return []

    latest: dict[str, Any] = {}
    for row in rows:
        latest[row.person_id] = row
    threshold = _number(_value(_scoring(), "disagreement_threshold", 100), 100)
    result: list[dict[str, Any]] = []
    for person_id, row in latest.items():
        gap = abs(int(row.baseline_rank) - int(row.ignu_rank))
        if gap < threshold:
            continue
        result.append(
            {
                "person_id": person_id,
                "name": people.get(person_id, ""),
                "baseline_rank": int(row.baseline_rank),
                "ignu_rank": int(row.ignu_rank),
                "gap": gap,
                "reasons": list(row.reasons or []),
            }
        )
    result.sort(key=lambda item: (-item["gap"], item["person_id"]))
    return result[:limit]


__all__ = [
    "components",
    "composite_score",
    "disagreements",
    "eligibility",
    "rank_all",
    "rerank_person",
    "score",
]
