"""Registration and previous-event ingestion.

This lane is intentionally persistence-oriented: source rows are normalised
by :mod:`app.mapping`, canonical people and aliases are stored in SQLite, and
each row produces an ``ingest`` pipeline event for the dashboard.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import select

from . import events
from .db import ParticipationRow, PersonRow, get_session, init_db
from .mapping import (
    _MISSING,
    _column_value,
    _mapping_columns,
    _parse_datetime,
    _row_view,
    _value,
    apply,
    normalise_github,
)
from .models import Participation, Person, PipelineEvent


@dataclass
class IngestReport:
    """Counters and non-fatal errors produced by a registration import."""

    rows: int = 0
    persons: int = 0
    with_github: int = 0
    aliases: int = 0
    students: int = 0
    professionals_by_domain: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    try:
        not_equal = value != value
        if isinstance(not_equal, bool) and not_equal:
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _normalised_email(value: Any) -> str:
    return _text(value).casefold()


def _normalised_part(value: Any) -> str:
    return " ".join(_text(value).split()).casefold()


def _event_name(event: Any) -> str:
    if isinstance(event, str):
        return event.strip() or "unknown-event"
    value = getattr(event, "event", None)
    return _text(value) or _text(event) or "unknown-event"


def _new_run_id(event: Any, run_id: str | None) -> str:
    if run_id:
        return run_id
    existing = getattr(event, "run_id", None)
    return _text(existing) or f"ingest-{uuid.uuid4().hex[:12]}"


def _emit(
    *,
    run_id: str,
    person_id: str | None,
    status: str,
    msg: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Emit defensively so an event-subscriber problem never kills ingest."""

    try:
        events.emit(
            PipelineEvent(
                ts=datetime.now(timezone.utc),
                run_id=run_id,
                stage="ingest",
                person_id=person_id,
                status=status,  # type: ignore[arg-type]
                msg=msg,
                data=data,
            )
        )
    except Exception:  # pragma: no cover - event bus is independently defensive
        # The event bus already degrades persistence failures.  This final
        # guard also covers a test/application replacement of emit().
        return


def _read_rows(path: str | Path, mapping: Any) -> list[dict[str, Any]]:
    source = Path(path)
    suffix = source.suffix.casefold()
    if suffix in {".xlsx", ".xlsm", ".xls"}:
        frame = pd.read_excel(
            source,
            sheet_name=_value(mapping, "sheet", 0),
            dtype=object,
            keep_default_na=False,
        )
    else:
        frame = pd.read_csv(source, dtype=object, keep_default_na=False)
    if isinstance(frame, dict):
        # ``sheet_name=None`` is not part of the normal Mapping contract, but
        # accepting it makes the reader degrade predictably if a caller uses
        # it for a multi-sheet workbook.
        frame = next(iter(frame.values()), pd.DataFrame())
    return frame.to_dict(orient="records")


def _safe_read(
    path: str | Path,
    mapping: Any,
    *,
    run_id: str,
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        return _read_rows(path, mapping), None
    except Exception as exc:
        # Do not pass source values or a full exception payload into a
        # user-facing pipeline event.  The exception type is enough to aid a
        # developer while keeping PII out of logs and event storage.
        reason = f"read_failed:{type(exc).__name__}"
        _emit(
            run_id=run_id,
            person_id=None,
            status="error",
            msg="could not read registration source",
            data={"reason": reason},
        )
        return [], reason


def _dedupe_keys(mapping: Any) -> list[str]:
    dedupe = _value(mapping, "dedupe", {}) or {}
    configured = _value(dedupe, "keys", None)
    if configured is None:
        configured = ["email", "github"]
    return [_text(value).casefold() for value in configured if _text(value)]


def _person_key(person: Person | PersonRow, key: str) -> str | None:
    key = key.casefold()
    if key in {"github", "github_login", "login"}:
        value = _text(person.github_login).casefold()
    elif key in {"email", "email_address"}:
        value = _normalised_email(person.email)
    elif key == "name":
        value = _normalised_part(person.name)
    elif key == "org":
        value = _normalised_part(person.org)
    else:
        value = _normalised_part(getattr(person, key, None))
    return value or None


def _root_id(person_id: str, by_id: dict[str, PersonRow]) -> str:
    current = person_id
    seen: set[str] = set()
    while current in by_id and by_id[current].alias_of and current not in seen:
        seen.add(current)
        current = by_id[current].alias_of or current
    return current


def _load_existing() -> tuple[list[PersonRow], dict[str, PersonRow]]:
    with get_session() as session:
        rows = list(session.scalars(select(PersonRow)).all())
    by_id = {row.id: row for row in rows}
    return rows, by_id


def _build_key_index(
    rows: list[PersonRow],
    by_id: dict[str, PersonRow],
    keys: list[str],
) -> dict[tuple[str, str], str]:
    index: dict[tuple[str, str], str] = {}
    for row in rows:
        root = _root_id(row.id, by_id)
        for key in keys:
            value = _person_key(row, key)
            if value:
                index.setdefault((key, value), root)
    return index


def _row_from_person(person: Person) -> PersonRow:
    return PersonRow(
        id=person.id,
        name=person.name,
        email=person.email,
        email_domain=person.email_domain,
        github_login=person.github_login,
        linkedin_url=person.linkedin_url,
        org=person.org,
        role=person.role,
        is_student=person.is_student,
        answers=dict(person.answers),
        registered_at=person.registered_at,
        alias_of=person.alias_of,
        source_file=person.source_file,
        source_row=person.source_row,
    )


def _collision_id(person: Person, occupied: set[str], *, purpose: str) -> str:
    """Derive a stable second ID when a contract identity collides.

    A duplicate GitHub login must retain a stored alias row, but the frozen
    person table has a primary-key ID.  The first row retains the exact
    login/name+org identity; later rows get a deterministic collision ID and
    point at the canonical winner.  The same mechanism keeps two no-GitHub
    people with identical names and orgs distinct instead of silently merging
    them.
    """

    counter = 0
    while True:
        salt = f"{person.id}|{person.source_file}|{person.source_row}|{purpose}|{counter}"
        candidate = hashlib.sha1(salt.encode("utf-8")).hexdigest()[:12]
        if candidate not in occupied:
            return candidate
        counter += 1


def _persist_person(
    person: Person,
    *,
    event_name: str,
    canonical_id: str,
    is_alias: bool,
    checked_in: bool | None = None,
) -> None:
    with get_session() as session:
        if session.get(PersonRow, person.id) is None:
            session.add(_row_from_person(person))
        if not is_alias:
            key = (canonical_id, event_name)
            participation = session.get(ParticipationRow, key)
            if participation is None:
                session.add(
                    ParticipationRow(
                        person_id=canonical_id,
                        event=event_name,
                        registered=True,
                        approved=None,
                        checked_in=checked_in,
                        submitted=None,
                        placed=None,
                        team_id=None,
                        at=None,
                    )
                )
            else:
                participation.registered = True
                if checked_in is not None:
                    participation.checked_in = checked_in


def _is_empty_row(row: dict[str, Any]) -> bool:
    return not any(_text(value) for value in row.values())


def _invalid_github_submission(row: dict[str, Any], mapping: Any) -> bool:
    """Whether a non-empty GitHub field was rejected by normalisation.

    A reserved path such as ``/in`` is not an identity.  It must not fall
    through to another configured dedupe key (for example an incidental email
    in a mixed source), otherwise two people who made the same bad submission
    could still be merged.
    """

    columns = _mapping_columns(mapping)
    raw = _column_value(_row_view(row), _value(columns, "github", None))
    return raw is not _MISSING and bool(_text(raw)) and normalise_github(raw, mapping) is None


def _registration_checked_in(row: dict[str, Any], mapping: Any) -> bool | None:
    columns = _mapping_columns(mapping)
    raw = _column_value(_row_view(row), _value(columns, "checked_in", None))
    return _parse_bool(raw)


def ingest_registrations(
    path: str | Path,
    mapping: Any,
    event: str,
    *,
    run_id: str | None = None,
) -> IngestReport:
    """Read registrations, persist people/participations, and emit row events."""

    init_db()
    pipeline_run_id = _new_run_id(event, run_id)
    event_name = _event_name(event)
    source_file = str(path)
    report = IngestReport()
    rows, read_error = _safe_read(path, mapping, run_id=pipeline_run_id)
    if read_error:
        report.errors.append(read_error)
        return report
    report.rows = len(rows)

    keys = _dedupe_keys(mapping)
    existing_rows, by_id = _load_existing()
    key_index = _build_key_index(existing_rows, by_id, keys)
    source_index = {(row.source_file, row.source_row): row for row in existing_rows}
    occupied = set(by_id)
    counted_persons: set[str] = set()
    counted_students: set[str] = set()
    counted_domains: set[tuple[str, str]] = set()
    seen_identity_ids: set[str] = set()
    professionals = Counter()

    for row_no, raw_row in enumerate(rows, start=2):
        if _is_empty_row(raw_row):
            _emit(
                run_id=pipeline_run_id,
                person_id=None,
                status="skip",
                msg=f"row {row_no} skipped: empty row",
                data={"row": row_no, "reason": "empty_row"},
            )
            continue
        try:
            person = apply(raw_row, mapping, source_file, row_no)
        except Exception as exc:
            reason = f"apply_failed:{type(exc).__name__}"
            report.errors.append(f"row {row_no}: {reason}")
            _emit(
                run_id=pipeline_run_id,
                person_id=None,
                status="skip",
                msg=f"row {row_no} skipped: invalid row",
                data={"row": row_no, "reason": reason},
            )
            continue

        if person.github_login:
            report.with_github += 1
        invalid_github = _invalid_github_submission(raw_row, mapping)
        checked_in = _registration_checked_in(raw_row, mapping)

        source_key = (person.source_file, person.source_row)
        existing_source = source_index.get(source_key)
        winner_id: str | None = None
        if not invalid_github:
            for key in keys:
                value = _person_key(person, key)
                if value and (key, value) in key_index:
                    winner_id = key_index[(key, value)]
                    break

        if existing_source is not None:
            # Replaying a cached/full source is idempotent.  Use the stored
            # row's canonical state and do not create a second alias.
            stored_id = existing_source.id
            is_alias = bool(existing_source.alias_of)
            canonical_id = _root_id(stored_id, by_id)
            person = person.model_copy(update={"id": stored_id, "alias_of": existing_source.alias_of})
            winner_id = canonical_id if is_alias else None
            if is_alias:
                report.aliases += 1
            else:
                # A source replay can legitimately target a new event.  Keep
                # the person upsert idempotent while ensuring that event still
                # receives its registered participation.
                _persist_person(
                    person,
                    event_name=event_name,
                    canonical_id=canonical_id,
                    is_alias=False,
                    checked_in=checked_in,
                )
        elif not invalid_github and person.id in by_id and person.id not in seen_identity_ids:
            # A stable identity already present from an equivalent source is
            # an upsert, not a fresh alias.  Track identities seen in this
            # import so a second row with that same key still becomes an
            # alias (the first row wins within the source).
            stored = by_id[person.id]
            is_alias = bool(stored.alias_of)
            canonical_id = _root_id(stored.id, by_id)
            person = person.model_copy(update={"id": stored.id, "alias_of": stored.alias_of})
            seen_identity_ids.add(person.id)
            if is_alias:
                winner_id = canonical_id
                report.aliases += 1
            else:
                winner_id = None
                _persist_person(
                    person,
                    event_name=event_name,
                    canonical_id=canonical_id,
                    is_alias=False,
                    checked_in=checked_in,
                )
        elif winner_id is not None:
            alias_id = _collision_id(person, occupied, purpose="alias")
            person = person.model_copy(update={"id": alias_id, "alias_of": winner_id})
            canonical_id = winner_id
            is_alias = True
            report.aliases += 1
            _persist_person(
                person,
                event_name=event_name,
                canonical_id=canonical_id,
                is_alias=True,
            )
            stored_row = _row_from_person(person)
            by_id[person.id] = stored_row
            source_index[source_key] = stored_row
            occupied.add(person.id)
            _emit(
                run_id=pipeline_run_id,
                person_id=person.id,
                status="ok",
                msg=f"row {row_no} aliased to {canonical_id}",
                data={"row": row_no, "reason": "duplicate", "alias_of": canonical_id},
            )
            continue
        else:
            # Person IDs are stable by design.  A collision with no dedupe key
            # (for example two blank-GitHub rows with the same name and org)
            # is still two registrations, not an implicit alias.
            if person.id in occupied:
                person = person.model_copy(
                    update={"id": _collision_id(person, occupied, purpose="distinct")}
                )
            canonical_id = person.id
            is_alias = False
            _persist_person(
                person,
                event_name=event_name,
                canonical_id=canonical_id,
                is_alias=False,
                checked_in=checked_in,
            )
            stored_row = _row_from_person(person)
            by_id[person.id] = stored_row
            source_index[source_key] = stored_row
            occupied.add(person.id)
            seen_identity_ids.add(person.id)
            if not invalid_github:
                for key in keys:
                    value = _person_key(person, key)
                    if value:
                        key_index.setdefault((key, value), canonical_id)

        if not is_alias:
            counted_persons.add(canonical_id)
            if person.is_student is True:
                counted_students.add(canonical_id)
            elif person.is_student is False and person.email_domain:
                domain_key = (canonical_id, person.email_domain)
                if domain_key not in counted_domains:
                    counted_domains.add(domain_key)
                    professionals[person.email_domain] += 1

        _emit(
            run_id=pipeline_run_id,
            person_id=person.id,
            status="ok",
            msg=f"row {row_no} ingested",
            data={
                "row": row_no,
                "reason": (
                    "github_login_invalid"
                    if invalid_github
                    else "no_github"
                    if person.github_login is None
                    else "registered"
                ),
                "alias_of": person.alias_of,
            },
        )

    report.persons = len(counted_persons)
    report.students = len(counted_students)
    report.professionals_by_domain = dict(professionals)
    return report


def _pick_value(
    view: dict[str, Any],
    configured: Any,
    fallbacks: tuple[str, ...],
) -> Any:
    if configured is not None:
        value = _column_value(view, configured)
        if value is not _MISSING:
            return value
    for fallback in fallbacks:
        value = _column_value(view, fallback)
        if value is not _MISSING:
            return value
    return _MISSING


def _parse_bool(value: Any, *, default: bool | None = None) -> bool | None:
    if value is _MISSING:
        return default
    text = _text(value).casefold()
    if not text:
        return default
    if text in {"1", "true", "t", "yes", "y", "approved", "checked in", "submitted"}:
        return True
    if text in {"0", "false", "f", "no", "n", "not approved", "not checked in", "not submitted"}:
        return False
    return default


def _parse_placed(value: Any) -> int | None:
    if value is _MISSING:
        return None
    text = _text(value)
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _parse_at(value: Any) -> date | None:
    parsed = _parse_datetime(value)
    return parsed.date() if parsed is not None else None


def _previous_indexes(
    rows: list[PersonRow],
    by_id: dict[str, PersonRow],
) -> tuple[dict[str, str], dict[str, str]]:
    by_email: dict[str, str] = {}
    by_github: dict[str, str] = {}
    for row in rows:
        root = _root_id(row.id, by_id)
        email = _normalised_email(row.email)
        if email:
            by_email.setdefault(email, root)
        if row.github_login:
            by_github.setdefault(row.github_login.casefold(), root)
    return by_email, by_github


def ingest_previous_event(
    path: str | Path,
    mapping: Any,
    event: str,
    *,
    run_id: str | None = None,
) -> list[Participation]:
    """Import prior-event outcomes and link them by email, then GitHub login."""

    init_db()
    pipeline_run_id = _new_run_id(event, run_id)
    event_name = _event_name(event)
    rows, read_error = _safe_read(path, mapping, run_id=pipeline_run_id)
    if read_error:
        return []

    existing_rows, by_id = _load_existing()
    by_email, by_github = _previous_indexes(existing_rows, by_id)
    columns = _mapping_columns(mapping)
    email_spec = _value(columns, "email", None)
    github_spec = _value(columns, "github", None)
    checked_spec = _value(columns, "checked_in", None)
    approved_spec = _value(columns, "approved", None)
    submitted_spec = _value(columns, "submitted", None)
    placed_spec = _value(columns, "placed", None)
    team_spec = _value(columns, "team", None)
    at_spec = _value(columns, "at", None)
    registered_spec = _value(columns, "registered", None)

    participations: list[Participation] = []
    for row_no, raw_row in enumerate(rows, start=2):
        view = _row_view(raw_row)
        email_raw = _pick_value(view, email_spec, ("Email", "email", "Email Address"))
        github_raw = _pick_value(view, github_spec, ("GitHub", "GitHub username", "Github"))
        email = _normalised_email(email_raw if email_raw is not _MISSING else "")
        github = (
            normalise_github(github_raw, mapping)
            if github_raw is not _MISSING
            else None
        )

        person_id: str | None = None
        matched_by: str | None = None
        if email:
            person_id = by_email.get(email)
            if person_id:
                matched_by = "email"
        if person_id is None and github:
            person_id = by_github.get(github)
            if person_id:
                matched_by = "github"

        if person_id is None:
            _emit(
                run_id=pipeline_run_id,
                person_id=None,
                status="skip",
                msg=f"row {row_no} skipped: person not found",
                data={"row": row_no, "reason": "person_not_found"},
            )
            continue

        approved_raw = _pick_value(view, approved_spec, ("Approved", "approved"))
        checked_raw = _pick_value(view, checked_spec, ("Checked In", "checked_in", "Check In"))
        submitted_raw = _pick_value(view, submitted_spec, ("Submitted", "submitted"))
        placed_raw = _pick_value(view, placed_spec, ("Placed", "placed", "Rank"))
        team_raw = _pick_value(view, team_spec, ("Team", "team", "Team ID", "team_id"))
        at_raw = _pick_value(view, at_spec, ("At", "Date", "Event Date", "registered_at"))
        registered_raw = _pick_value(view, registered_spec, ("Registered", "registered"))

        participation = Participation(
            person_id=person_id,
            event=event_name,
            registered=_parse_bool(registered_raw, default=True) is not False,
            approved=_parse_bool(approved_raw),
            checked_in=_parse_bool(checked_raw),
            submitted=_parse_bool(submitted_raw),
            placed=_parse_placed(placed_raw),
            team_id=(
                _text(team_raw) or None if team_raw is not _MISSING else None
            ),
            at=_parse_at(at_raw),
        )
        with get_session() as session:
            existing = session.get(ParticipationRow, (person_id, event_name))
            if existing is None:
                session.add(
                    ParticipationRow(**participation.model_dump())
                )
            else:
                for field_name, value in participation.model_dump().items():
                    setattr(existing, field_name, value)
        participations.append(participation)
        _emit(
            run_id=pipeline_run_id,
            person_id=person_id,
            status="ok",
            msg=f"row {row_no} linked to person",
            data={"row": row_no, "reason": "matched", "matched_by": matched_by},
        )

    return participations


__all__ = ["IngestReport", "ingest_previous_event", "ingest_registrations"]
