"""Build versioned, sourced profiles from registration and GitHub evidence.

The profile stage deliberately has a deterministic core.  Repository activity
is scored before the optional Haiku synthesis call, which means a missing
GitHub response, an exhausted budget, or an unavailable model can never stop a
pipeline run from producing a useful profile.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from pydantic import BaseModel, Field, ValidationError

from . import events, llm
from .config import get_settings
from .models import (
    Evidence,
    Participation,
    Person,
    PipelineEvent,
    Profile,
    RepoEvidence,
    SkillScore,
)

logger = logging.getLogger(__name__)

PROMPT_VERSION = "profile-v2-dataset-empty-answers"
"""Bump when the profile prompt or its input contract changes."""

_PROMPT_PATH = Path(__file__).with_name("prompts") / "profile.md"
_MAX_README_CHARS = 1_200
_MAX_DESCRIPTION_CHARS = 600
_MAX_ANSWER_CHARS = 800
_LOGIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$", re.IGNORECASE)
_INVALID_GITHUB_LOGINS = {
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


class ProfileLLM(BaseModel):
    """The only model-shaped payload accepted from the profile LLM call."""

    skills: list[SkillScore] = Field(default_factory=list)
    ai_relevance: float = Field(ge=0.0, le=1.0)
    summary: str
    evidence: list[Evidence] = Field(default_factory=list)


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    if not math.isfinite(number):
        return low
    return max(low, min(high, number))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | date | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)


def _observed_date(person: Person, now: datetime) -> date:
    registered_at = _as_utc(person.registered_at)
    return (registered_at or now).date()


def _normalise_login(value: Any) -> str | None:
    """Return a safe GitHub login, rejecting reserved URL paths.

    ``mapping.normalise_github`` normally performs this work upstream.  The
    profile stage repeats the small validation here so a direct caller cannot
    accidentally turn ``github.com/in`` into external evidence.
    """

    candidate = _text(value)
    if not candidate:
        return None
    candidate = re.sub(
        r"^https?://(?:www\.)?github\.com/?",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(r"^github\.com/", "", candidate, flags=re.IGNORECASE)
    candidate = candidate.lstrip("@").rstrip("/").strip().casefold()
    if (
        not candidate
        or any(character in candidate for character in "/?#")
        or candidate in _INVALID_GITHUB_LOGINS
        or not _LOGIN_RE.fullmatch(candidate)
    ):
        return None
    return candidate


def _coerce_repos(repos: Iterable[RepoEvidence] | None) -> list[RepoEvidence]:
    """Validate repository-shaped inputs without allowing one bad row to crash."""

    result: list[RepoEvidence] = []
    for repo in repos or []:
        if isinstance(repo, RepoEvidence):
            result.append(repo)
            continue
        try:
            if isinstance(repo, dict):
                result.append(RepoEvidence.model_validate(repo))
            else:
                result.append(RepoEvidence.model_validate(repo, from_attributes=True))
        except (TypeError, ValueError, ValidationError):
            logger.warning("ignoring invalid repository evidence row")
    return result


def _commit_total(repo: RepoEvidence) -> int:
    return max(0, int(repo.author_commits_total or 0))


def _commit_90d(repo: RepoEvidence) -> int:
    return max(0, int(repo.author_commits_90d or 0))


def _top_repos(repos: Sequence[RepoEvidence]) -> list[RepoEvidence]:
    """Select the top three repositories by total author commits."""

    return sorted(
        repos,
        key=lambda repo: (
            -_commit_total(repo),
            -_commit_90d(repo),
            _text(repo.full_name).casefold(),
            _text(repo.html_url),
        ),
    )[:3]


def _pushed_within_180_days(last_push: datetime | None, now: datetime) -> bool:
    pushed_at = _as_utc(last_push)
    if pushed_at is None:
        return False
    return pushed_at >= now - timedelta(days=180)


def _repo_score(repo: RepoEvidence, now: datetime) -> float:
    """Apply the frozen §6.1 formula to one repository exactly."""

    total = _commit_total(repo)
    recent = _commit_90d(repo)
    author_factor = 0.0 if repo.is_fork and total < 5 else 1.0
    total_component = author_factor * min(1.0, math.log1p(total) / math.log1p(50)) * 0.6
    recent_component = min(1.0, recent / 20.0) * 0.3
    recency_component = 0.1 if _pushed_within_180_days(repo.last_push, now) else 0.0
    return total_component + recent_component + recency_component


def original_work_score(repos: Sequence[RepoEvidence]) -> float:
    """Return the deterministic original-work score from the top three repos."""

    selected = _top_repos(repos)
    if not selected:
        return 0.0
    now = _utc_now()
    mean_score = sum(_repo_score(repo, now) for repo in selected) / len(selected)
    non_fork_count = sum(1 for repo in selected if not repo.is_fork)
    multiplier = 0.7 + 0.1 * min(3, non_fork_count)
    return _clamp(mean_score * multiplier)


def _original_work_score(repos: Sequence[RepoEvidence]) -> float:
    """Compatibility alias useful to callers that treat helpers as private."""

    return original_work_score(repos)


def _evidence_level(repos: Sequence[RepoEvidence]) -> str:
    if not repos:
        return "none"
    original_repos = [
        repo for repo in repos if not repo.is_fork and _commit_total(repo) >= 10
    ]
    if len(original_repos) >= 2 or sum(_commit_90d(repo) for repo in repos) >= 50:
        return "strong"
    if original_repos:
        return "solid"
    return "thin"


def evidence_level(repos: Sequence[RepoEvidence]) -> str:
    """Public deterministic evidence-level helper."""

    return _evidence_level(_top_repos(repos))


def _participations_list(
    participations: Iterable[Participation] | None,
) -> list[Any]:
    return list(participations or [])


def reliability(participations: Iterable[Participation] | None) -> float | None:
    """Calculate checked-in/approved reliability with Laplace smoothing.

    Only approved events with a known check-in value are trials.  This keeps a
    missing check-in field neutral instead of treating incomplete source data
    as a no-show.
    """

    trials = 0
    checked_in = 0
    for participation in _participations_list(participations):
        if _value(participation, "approved") is not True:
            continue
        checked_in_value = _value(participation, "checked_in")
        if checked_in_value is None:
            continue
        trials += 1
        if checked_in_value is True:
            checked_in += 1
    if trials == 0:
        return None
    return (checked_in + 1.0) / (trials + 2.0)


def _registration_source(person: Person) -> str:
    source_name = Path(_text(person.source_file)).name or "registration"
    return f"registration://{source_name}#row={person.source_row}"


def _registration_evidence(person: Person, now: datetime) -> list[Evidence]:
    """Create sourced fallback claims for the registration-only path."""

    claims: list[str] = []
    if _text(person.org):
        claims.append(f"Registration lists the organisation as {person.org}.")
    if _text(person.role):
        claims.append(f"Registration lists the role as {person.role}.")
    if person.is_student is not None:
        claims.append(f"Registration marks student status as {person.is_student}.")
    for key, value in sorted((person.answers or {}).items()):
        if _text(value):
            claims.append(f"Registration answer {key}: {value}.")
    return [
        Evidence(
            claim=claim,
            source_url=_registration_source(person),
            kind="registration",
            confidence=0.7,
            observed_at=_observed_date(person, now),
        )
        for claim in claims
    ]


def _repo_evidence(repos: Sequence[RepoEvidence], now: datetime) -> list[Evidence]:
    claims: list[Evidence] = []
    for repo in repos:
        total = _commit_total(repo)
        recent = _commit_90d(repo)
        fork_note = " It is marked as a fork." if repo.is_fork else ""
        claims.append(
            Evidence(
                claim=(
                    f"Repository {repo.full_name} records {total} author commits, "
                    f"including {recent} in the last 90 days.{fork_note}"
                ),
                source_url=_text(repo.html_url),
                kind="repo",
                confidence=_clamp(0.5 + min(0.5, total / 50.0)),
                observed_at=(_as_utc(repo.last_push) or now).date(),
            )
        )
    return claims


def _nonempty_answers(person: Person) -> bool:
    return any(_text(value) for value in (person.answers or {}).values())


def _shorten(value: Any, limit: int) -> str:
    text = _text(value)
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def _prompt_text() -> str:
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        # The checked-in prompt is required, but a short fallback keeps an
        # installed package usable if an asset was accidentally omitted.
        return (
            "Synthesize a profile from the JSON input. Use only the allowed "
            "skills, cite every claim, and keep the summary under 80 words."
        )


def _repo_prompt_payload(repo: RepoEvidence) -> dict[str, Any]:
    return {
        "full_name": repo.full_name,
        "source_url": repo.html_url,
        "is_fork": repo.is_fork,
        "description": _shorten(repo.description, _MAX_DESCRIPTION_CHARS),
        "topics": [_shorten(topic, 80) for topic in repo.topics[:20]],
        "primary_language": repo.primary_language,
        "author_commits_90d": _commit_90d(repo),
        "author_commits_total": _commit_total(repo),
        "last_push": (_as_utc(repo.last_push).isoformat() if repo.last_push else None),
        "readme_excerpt": _shorten(repo.readme_excerpt, _MAX_README_CHARS),
    }


def build_profile_prompt(person: Person, repos: Sequence[RepoEvidence]) -> str:
    """Render the bounded prompt sent to the structured batch wrapper."""

    answers = {
        _shorten(key, 160): _shorten(value, _MAX_ANSWER_CHARS)
        for key, value in sorted((person.answers or {}).items())
        if _text(value)
    }
    payload = {
        "registration": {
            "name": person.name,
            "organisation": person.org,
            "role": person.role,
            "is_student": person.is_student,
            "answers": answers,
            "source": _registration_source(person),
        },
        "repositories": [_repo_prompt_payload(repo) for repo in _top_repos(repos)],
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    return f"{_prompt_text()}\n\nINPUT JSON:\n```json\n{encoded}\n```"


def _evidence_hash(person: Person, repos: Sequence[RepoEvidence]) -> str:
    payload = {
        "person_id": person.id,
        "name": person.name,
        "organisation": person.org,
        "role": person.role,
        "is_student": person.is_student,
        "answers": dict(sorted((person.answers or {}).items())),
        "repositories": [_repo_prompt_payload(repo) for repo in _top_repos(repos)],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def profile_cache_key(person: Person, repos: Sequence[RepoEvidence]) -> str:
    """Return the stable cache key required by the profile contract."""

    material = f"{person.id}|{_evidence_hash(person, repos)}|{PROMPT_VERSION}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _summary_with_limit(summary: Any, *, fallback_tag: str) -> str:
    text = " ".join(_text(summary).split())
    if not text:
        return f"No synthesized summary was returned. {fallback_tag}"
    words = text.split()
    if len(words) <= 80:
        return text
    # Leave one word for a traceability tag when truncation is necessary.
    return " ".join(words[:79] + [fallback_tag])


def _metadata_value(result: Any, name: str) -> int:
    raw = _value(result, name, 0)
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _configured_batch_model() -> str:
    try:
        return get_settings().llm_model_batch
    except Exception:  # pragma: no cover - only malformed local settings
        return "batch"


def _emit(
    *,
    run_id: str,
    person_id: str,
    status: str,
    msg: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Emit profile events without making the event bus a stage dependency."""

    try:
        events.emit(
            PipelineEvent(
                ts=_utc_now(),
                run_id=run_id,
                stage="profile",
                person_id=person_id,
                status=status,  # type: ignore[arg-type]
                msg=msg,
                data=data,
            )
        )
    except Exception:  # pragma: no cover - event bus independently degrades
        logger.debug("profile event emission failed", exc_info=True)


def _profile(
    *,
    person: Person,
    version: int,
    built_at: datetime,
    skills: Sequence[SkillScore],
    evidence_level_value: str,
    score: float,
    ai_relevance_value: float,
    reliability_value: float | None,
    summary: str,
    evidence: Sequence[Evidence],
    model_used: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> Profile:
    return Profile(
        person_id=person.id,
        version=version,
        built_at=built_at,
        skills=list(skills),
        evidence_level=evidence_level_value,  # type: ignore[arg-type]
        original_work_score=score,
        ai_relevance=_clamp(ai_relevance_value),
        reliability=reliability_value,
        summary=summary,
        evidence=list(evidence),
        model_used=model_used,
        input_tokens=max(0, int(input_tokens)),
        output_tokens=max(0, int(output_tokens)),
    )


def build_profile(
    person: Person,
    repos: list[RepoEvidence],
    participations: Iterable[Participation] | None,
    prev_profile: Profile | None,
    *,
    run_id: str | None = None,
) -> Profile:
    """Build one versioned profile, degrading safely when synthesis is absent."""

    built_at = _utc_now()
    version = (prev_profile.version + 1) if prev_profile is not None else 1
    run_id = run_id or f"profile-{person.id}"
    participation_values = _participations_list(participations)
    valid_login = _normalise_login(person.github_login)
    input_repos = _coerce_repos(repos)
    selected_repos = _top_repos(input_repos) if valid_login else []
    score = original_work_score(selected_repos)
    level = _evidence_level(selected_repos)
    reliability_value = reliability(participation_values)
    answers_present = _nonempty_answers(person)

    _emit(
        run_id=run_id,
        person_id=person.id,
        status="start",
        msg="building profile",
        data={"version": version, "evidence_level": level},
    )

    if not selected_repos and not answers_present:
        profile = _profile(
            person=person,
            version=version,
            built_at=built_at,
            skills=[],
            evidence_level_value="none",
            score=0.0,
            ai_relevance_value=0.0,
            reliability_value=reliability_value,
            summary="No external evidence; registration answers empty.",
            evidence=[],
            model_used="deterministic",
        )
        _emit(
            run_id=run_id,
            person_id=person.id,
            status="skip",
            msg="no external evidence and registration answers empty",
            data={"version": version, "reason": "no_external_evidence"},
        )
        return profile

    prompt = build_profile_prompt(person, selected_repos)
    cache_key = profile_cache_key(person, selected_repos)
    try:
        raw_result = llm.structured(
            prompt,
            ProfileLLM,
            tier="batch",
            cache_key=cache_key,
        )
        result = (
            raw_result
            if isinstance(raw_result, ProfileLLM)
            else ProfileLLM.model_validate(raw_result)
        )
    except llm.BudgetExceeded:
        fallback_evidence = _repo_evidence(selected_repos, built_at)
        if not selected_repos:
            fallback_evidence = _registration_evidence(person, built_at)
        profile = _profile(
            person=person,
            version=version,
            built_at=built_at,
            skills=[],
            evidence_level_value=level,
            score=score,
            ai_relevance_value=0.0,
            reliability_value=reliability_value,
            summary="LLM budget exhausted; deterministic evidence only.",
            evidence=fallback_evidence,
            model_used="deterministic",
        )
        _emit(
            run_id=run_id,
            person_id=person.id,
            status="error",
            msg="profile synthesis skipped because the LLM budget is exhausted",
            data={"version": version, "reason": "budget_exhausted"},
        )
        return profile
    except Exception as exc:
        # ``llm.structured`` owns its validation retry.  Any remaining model,
        # network, or schema failure is isolated to this person and surfaced as
        # an error event for the later needs_human bucket.
        fallback_evidence = _repo_evidence(selected_repos, built_at)
        if not selected_repos:
            fallback_evidence = _registration_evidence(person, built_at)
        profile = _profile(
            person=person,
            version=version,
            built_at=built_at,
            skills=[],
            evidence_level_value=level,
            score=score,
            ai_relevance_value=0.0,
            reliability_value=reliability_value,
            summary="LLM synthesis failed; deterministic evidence only.",
            evidence=fallback_evidence,
            model_used="deterministic",
        )
        _emit(
            run_id=run_id,
            person_id=person.id,
            status="error",
            msg="profile synthesis failed; deterministic evidence retained",
            data={"version": version, "reason": type(exc).__name__},
        )
        return profile

    skills = [
        SkillScore(skill=item.skill, confidence=_clamp(item.confidence))
        for item in result.skills
    ]
    if not selected_repos:
        # The prompt asks for this too; enforce it at the boundary so a model
        # cannot manufacture high-confidence skills from a registration row.
        skills = [
            SkillScore(skill=item.skill, confidence=min(0.4, item.confidence))
            for item in skills
        ]
    evidence = list(result.evidence)
    fallback_tag = "[repo]" if selected_repos else "[registration]"
    profile = _profile(
        person=person,
        version=version,
        built_at=built_at,
        skills=skills,
        evidence_level_value=level,
        score=score,
        ai_relevance_value=result.ai_relevance,
        reliability_value=reliability_value,
        summary=_summary_with_limit(result.summary, fallback_tag=fallback_tag),
        evidence=evidence,
        model_used=_configured_batch_model(),
        input_tokens=_metadata_value(raw_result, "input_tokens"),
        output_tokens=_metadata_value(raw_result, "output_tokens"),
    )
    _emit(
        run_id=run_id,
        person_id=person.id,
        status="ok",
        msg="profile built",
        data={
            "version": version,
            "evidence_level": level,
            "original_work_score": score,
            "model_used": profile.model_used,
        },
    )
    return profile


__all__ = [
    "PROMPT_VERSION",
    "ProfileLLM",
    "build_profile",
    "build_profile_prompt",
    "evidence_level",
    "original_work_score",
    "profile_cache_key",
    "reliability",
]
