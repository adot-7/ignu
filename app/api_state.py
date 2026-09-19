"""Build the JSON snapshot consumed by the dashboard.

The dashboard is useful before a complete pipeline run, so this module keeps
the HTTP payload deliberately boring and defensive.  Every collection has a
stable empty value and optional pipeline lanes are imported only when their
implementation is available.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
import yaml

from .config import get_settings
from .db import (
    EventRow,
    PersonRow,
    TeamRow,
    VerdictRow,
    get_session,
    init_db,
)

logger = logging.getLogger(__name__)

_DECISIONS = ("admit", "waitlist", "decline", "needs_human")
_DEFAULT_DISAGREEMENT_LIMIT = 20


def _empty_state() -> dict[str, Any]:
    return {
        "counters": {
            "rows": 0,
            "with_github": 0,
            "with_github_pct": 0.0,
            "aliases": 0,
            "admitted": 0,
            "waitlist": 0,
            "decline": 0,
            "needs_human": 0,
        },
        "spend": 0.0,
        "budget": 6.0,
        "disagreements": [],
        "teams": [],
        "last_run": None,
        "scoring": {},
    }


def _model_dump(value: Any) -> dict[str, Any]:
    """Turn a pydantic model or mapping returned by a later lane into JSON data."""

    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {}
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _sort_key(row: Any) -> tuple[float, int]:
    timestamp = getattr(row, "at", None) or getattr(row, "ts", None)
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        value = timestamp.timestamp()
    else:
        value = 0.0
    return value, _safe_int(getattr(row, "id", 0))


def _latest_verdicts(rows: Iterable[VerdictRow]) -> dict[str, VerdictRow]:
    latest: dict[str, VerdictRow] = {}
    for row in rows:
        previous = latest.get(row.person_id)
        if previous is None or _sort_key(row) >= _sort_key(previous):
            latest[row.person_id] = row
    return latest


def _load_scoring() -> dict[str, Any]:
    try:
        settings = get_settings()
        payload = settings.scoring().model_dump(mode="json")
        # ``ScoringWeights`` retains a legacy reliability default for older
        # callers.  The dashboard should show the keys actually configured in
        # scoring.yaml, especially for the real dataset where reliability has
        # no source column at t0.
        scoring_path = settings.scoring_file
        try:
            with open(scoring_path, "r", encoding="utf-8") as handle:
                raw = yaml.safe_load(handle)
        except OSError:
            raw = None
        configured_weights = raw.get("weights") if isinstance(raw, dict) else None
        if isinstance(configured_weights, dict) and isinstance(payload.get("weights"), dict):
            payload["weights"] = {
                key: value
                for key, value in payload["weights"].items()
                if key in configured_weights
            }
        return payload
    except Exception:  # pragma: no cover - configuration is operator supplied
        logger.warning("could not load scoring configuration", exc_info=True)
        return {}


def _load_spend() -> float:
    try:
        from .llm import spend_usd

        return round(_safe_float(spend_usd()), 6)
    except Exception:  # pragma: no cover - dashboard must survive a DB problem
        logger.warning("could not read LLM spend", exc_info=True)
        return 0.0


def _load_budget() -> float:
    try:
        return _safe_float(get_settings().llm_budget_usd, 6.0)
    except Exception:  # pragma: no cover - settings have a safe default
        return 6.0


def _latest_run(events: list[EventRow]) -> dict[str, Any] | None:
    if not events:
        return None
    latest = max(events, key=_sort_key)
    run_events = [event for event in events if event.run_id == latest.run_id]
    last_for_run = max(run_events, key=_sort_key)
    return {
        "run_id": latest.run_id,
        "ts": _iso(last_for_run.ts),
        "stage": last_for_run.stage,
        "status": last_for_run.status,
        "msg": last_for_run.msg,
        "event_count": len(run_events),
    }


def _team_payload(row: TeamRow, people: dict[str, PersonRow]) -> dict[str, Any]:
    member_ids = row.member_ids if isinstance(row.member_ids, list) else []
    coverage = row.coverage if isinstance(row.coverage, dict) else {}
    return {
        "id": row.id,
        "event": row.event,
        "member_ids": member_ids,
        "members": [
            {
                "id": person_id,
                "name": people[person_id].name if person_id in people else "Unknown participant",
            }
            for person_id in member_ids
        ],
        "coverage": {
            str(skill): max(0.0, min(1.0, _safe_float(value)))
            for skill, value in coverage.items()
        },
        "balance": max(0.0, min(1.0, _safe_float(row.balance))),
        "why": row.why or "",
    }


def _normalise_disagreement(value: Any, people: dict[str, PersonRow]) -> dict[str, Any] | None:
    raw = _model_dump(value)
    if not raw:
        return None
    person_id = str(raw.get("person_id") or raw.get("id") or "")
    person = people.get(person_id)
    baseline_rank = _safe_int(raw.get("baseline_rank"), 0)
    ignu_rank = _safe_int(raw.get("ignu_rank"), 0)
    gap = _safe_int(raw.get("gap"), abs(baseline_rank - ignu_rank))
    reasons = raw.get("reasons")
    if isinstance(reasons, str):
        reasons = [reasons]
    if not isinstance(reasons, list):
        reasons = []
    result: dict[str, Any] = {
        "person_id": person_id,
        "name": str(raw.get("name") or (person.name if person else "Unknown participant")),
        "baseline_rank": baseline_rank,
        "ignu_rank": ignu_rank,
        "gap": abs(gap),
        "reasons": [str(reason) for reason in reasons],
    }
    # The ranker can attach richer detail for the drawer.  Preserve it when
    # present without requiring the dashboard to know the ranker's internals.
    for key in (
        "score",
        "decision",
        "eligibility",
        "profile",
        "skills",
        "evidence",
        "verdict_history",
        "history",
    ):
        if key in raw:
            result[key] = raw[key]
    return result


def _rank_disagreements(people: dict[str, PersonRow], verdicts: list[VerdictRow], scoring: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the ranker when present, with a persisted-verdict fallback.

    The rank lane is intentionally optional while parallel issues are being
    merged.  Once it exists, its disagreement function is the source of truth;
    before then, persisted verdicts still make a partially completed run useful
    to the dashboard.
    """

    try:
        rank = importlib.import_module("app.rank")
    except (ImportError, ModuleNotFoundError):
        rank = None
    except Exception:  # pragma: no cover - a broken optional lane must not break UI
        logger.warning("rank module could not be imported", exc_info=True)
        rank = None

    if rank is not None:
        function = getattr(rank, "disagreements", None)
        if callable(function):
            try:
                values = function(limit=_DEFAULT_DISAGREEMENT_LIMIT)
            except TypeError:
                values = function()
            except Exception:  # pragma: no cover - ranker owns its own degradation
                logger.warning("rank disagreements could not be read", exc_info=True)
                return []
            if isinstance(values, Mapping):
                values = values.get("disagreements", [])
            if not isinstance(values, Iterable) or isinstance(values, (str, bytes)):
                return []
            result = [
                item
                for item in (_normalise_disagreement(value, people) for value in values)
                if item is not None
            ]
            return result[:_DEFAULT_DISAGREEMENT_LIMIT]

    threshold = _safe_int(scoring.get("disagreement_threshold"), 100)
    result: list[dict[str, Any]] = []
    for row in verdicts:
        gap = abs(_safe_int(row.baseline_rank) - _safe_int(row.ignu_rank))
        if gap < threshold:
            continue
        result.append(
            {
                "person_id": row.person_id,
                "name": people[row.person_id].name if row.person_id in people else "Unknown participant",
                "baseline_rank": row.baseline_rank,
                "ignu_rank": row.ignu_rank,
                "gap": gap,
                "reasons": list(row.reasons or []),
                "score": row.score,
                "decision": row.decision,
                "eligibility": row.eligibility,
            }
        )
    result.sort(key=lambda item: (-item["gap"], item["person_id"]))
    return result[:_DEFAULT_DISAGREEMENT_LIMIT]


def build_state() -> dict[str, Any]:
    """Return an empty-safe, JSON-serialisable dashboard snapshot."""

    state = _empty_state()
    state["budget"] = _load_budget()
    state["spend"] = _load_spend()
    state["scoring"] = _load_scoring()

    try:
        init_db()
        with get_session() as session:
            people_rows = list(session.scalars(select(PersonRow)).all())
            verdict_rows = list(session.scalars(select(VerdictRow)).all())
            team_rows = list(session.scalars(select(TeamRow)).all())
            event_rows = list(session.scalars(select(EventRow)).all())
    except Exception:  # pragma: no cover - the UI remains useful during DB startup
        logger.warning("could not build dashboard state from SQLite", exc_info=True)
        return state

    people = {row.id: row for row in people_rows}
    latest_verdicts = _latest_verdicts(verdict_rows)
    counters = state["counters"]
    counters["rows"] = len(people_rows)
    counters["with_github"] = sum(1 for row in people_rows if (row.github_login or "").strip())
    counters["with_github_pct"] = round(
        counters["with_github"] / len(people_rows) * 100, 1
    ) if people_rows else 0.0
    counters["aliases"] = sum(1 for row in people_rows if (row.alias_of or "").strip())
    for verdict in latest_verdicts.values():
        decision = str(verdict.decision).lower()
        if decision in _DECISIONS:
            key = "admitted" if decision == "admit" else decision
            counters[key] += 1

    state["disagreements"] = _rank_disagreements(
        people,
        list(latest_verdicts.values()),
        state["scoring"],
    )
    state["teams"] = [_team_payload(row, people) for row in team_rows]
    state["last_run"] = _latest_run(event_rows)
    return state


__all__ = ["build_state"]
