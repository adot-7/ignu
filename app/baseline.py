"""Deterministic baseline comparison scoring.

This is a faithful reimplementation of the organizer's keyword-count script
for comparison with ignu's richer evidence-based ranking.  The organizer's
script counts repositories whose configured fields mention an AI keyword; it
does not infer whether a repository is original work unless ``count_forks`` is
disabled in the active scoring configuration.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import logging
from typing import Any
from uuid import uuid4

from . import events
from .models import PipelineEvent, RepoEvidence

logger = logging.getLogger(__name__)


def _value(source: Any, key: str, default: Any = None) -> Any:
    """Read a value from a model, namespace, or mapping without assumptions."""

    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _baseline_settings(scoring: Any) -> tuple[list[Any], list[Any], bool]:
    """Extract only the active baseline settings from a scoring object."""

    baseline = _value(scoring, "baseline", scoring)
    keywords = _value(baseline, "keywords", ())
    fields = _value(baseline, "fields", ())
    count_forks = _value(baseline, "count_forks", True)

    if keywords is None:
        keywords = ()
    if fields is None:
        fields = ()

    return list(keywords), list(fields), bool(count_forks)


def _repo_value(repo: RepoEvidence, field: Any) -> Any:
    """Return a configured repository field, including the contract's name alias."""

    field_name = str(field).strip()
    if not field_name:
        return None

    if field_name.casefold() == "name":
        full_name = getattr(repo, "full_name", "") or ""
        return str(full_name).rsplit("/", 1)[-1]

    if hasattr(repo, field_name):
        return getattr(repo, field_name)

    # Human-edited YAML occasionally varies casing.  Resolve that harmlessly
    # without introducing a second list of supported, hardcoded fields.
    folded_field = field_name.casefold()
    for candidate in getattr(type(repo), "model_fields", {}):
        if candidate.casefold() == folded_field:
            return getattr(repo, candidate, None)

    return None


def _contains_keyword(value: Any, keyword: str) -> bool:
    """Check a scalar or collection using case-insensitive substring matching."""

    if value is None:
        return False
    if isinstance(value, str):
        return keyword in value.casefold()
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return any(_contains_keyword(item, keyword) for item in value)
    return keyword in str(value).casefold()


def _emit(status: str, msg: str, *, data: dict[str, Any] | None = None) -> None:
    """Emit a rank-stage event while keeping scoring usable in degraded runs."""

    try:
        events.emit(
            PipelineEvent(
                ts=datetime.now(timezone.utc),
                run_id=f"baseline-{uuid4().hex[:12]}",
                stage="rank",
                person_id=None,
                status=status,  # type: ignore[arg-type]
                msg=msg,
                data=data,
            )
        )
    except Exception:  # pragma: no cover - event persistence is best effort
        logger.debug("could not emit baseline pipeline event", exc_info=True)


def baseline_score(repos: list[RepoEvidence], scoring: Any) -> float:
    """Count configured-keyword repositories for the organizer comparison.

    Keywords and fields are read from ``scoring.baseline`` (or an equivalent
    mapping).  Each repository contributes at most one point when any keyword
    appears in any configured field.  Forks contribute only when the active
    ``count_forks`` setting permits them.
    """

    keywords, fields, count_forks = _baseline_settings(scoring)
    normalised_keywords = [
        str(keyword).strip().casefold() for keyword in keywords if str(keyword).strip()
    ]

    matched = 0
    for repo in repos:
        if not count_forks and repo.is_fork:
            continue
        values = (_repo_value(repo, field) for field in fields)
        if any(
            _contains_keyword(value, keyword)
            for value in values
            for keyword in normalised_keywords
        ):
            matched += 1

    _emit(
        "ok" if repos else "skip",
        "baseline score computed" if repos else "baseline score skipped: no repositories",
        data={"repositories": len(repos), "matches": matched},
    )
    return float(matched)


def baseline_rank(scores: dict[str, float]) -> dict[str, int]:
    """Return deterministic one-based ranks, ordered by score then person id."""

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ranks = {person_id: rank for rank, (person_id, _score) in enumerate(ordered, 1)}
    _emit(
        "ok" if scores else "skip",
        "baseline ranks computed" if scores else "baseline ranks skipped: no scores",
        data={"people": len(scores)},
    )
    return ranks


__all__ = ["baseline_rank", "baseline_score"]
