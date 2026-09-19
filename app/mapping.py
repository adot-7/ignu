"""Load source mappings and turn a source row into a :class:`Person`.

The registration files used by ignu are deliberately not required to share a
schema.  This module keeps the source-specific details in ``mapping.yaml`` and
leaves the rest of the pipeline with the frozen ``Person`` contract.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from .config import ColumnSpec, Mapping as MappingConfig
from .models import Person


# These are useful defaults for a mapping which does not spell out the URL
# forms it accepts.  A mapping may add its own prefixes; it does not need to
# repeat these common forms.
_DEFAULT_GITHUB_PREFIXES = (
    "https://www.github.com/",
    "http://www.github.com/",
    "https://github.com/",
    "http://github.com/",
    "www.github.com/",
    "github.com/",
    "@",
)
_DEFAULT_INVALID_LOGINS = {"in", "login", "home", "settings", "about"}
_GITHUB_LOGIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$", re.IGNORECASE)
_MISSING = object()


# Re-export the typed config model from the foundation-facing module.  This
# makes ``from app.mapping import Mapping`` useful to pipeline callers while
# keeping one source of truth for the YAML contract.
Mapping = MappingConfig


def load(path: str | Path) -> Mapping:
    """Load a typed mapping from YAML.

    ``app.config`` owns the application's general YAML model.  Loading here
    still happens through this lane's public API, while accepting the dataset
    amendment's forward-compatible keys such as ``identity`` and
    ``columns.name_parts`` (the foundation models intentionally allow them).
    """

    file_path = Path(path)
    if not file_path.exists():
        # Match the foundation loader's safe behaviour for an optional mapping
        # during application startup.
        return Mapping.model_validate({})
    with file_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return Mapping.model_validate(raw if isinstance(raw, dict) else {})


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _text(value: Any) -> str:
    """Return a scalar as trimmed text, treating common nulls as empty."""

    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    # pandas/numpy null scalars do not always behave like a Python float.  Do
    # this without importing pandas so apply() remains cheap and dependency
    # independent when used directly in a caller or test.
    try:
        not_equal = value != value
        if isinstance(not_equal, bool) and not_equal:
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _header_key(value: Any) -> str:
    # Collapsing whitespace as well as trimming makes headers copied from an
    # Excel export match their mapping even when a cell has a trailing space.
    return " ".join(_text(value).split()).casefold()


def _row_view(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        items = row.items()
    elif hasattr(row, "items"):
        items = row.items()
    else:
        raise TypeError("row must be a mapping of source headers to values")
    result: dict[str, Any] = {}
    for key, value in items:
        normalised = _header_key(key)
        if normalised and normalised not in result:
            result[normalised] = value
    return result


def _column_name(spec: Any) -> str:
    if spec is None:
        return ""
    if isinstance(spec, str):
        return _text(spec)
    return _text(_value(spec, "column", ""))


def _column_value(view: dict[str, Any], spec: Any) -> Any:
    name = _column_name(spec)
    if not name:
        return _MISSING
    return view.get(_header_key(name), _MISSING)


def _optional_text(view: dict[str, Any], spec: Any) -> str | None:
    raw = _column_value(view, spec)
    if raw is _MISSING:
        return None
    text = _text(raw)
    return text or None


def _mapping_columns(mapping: Any) -> Any:
    return _value(mapping, "columns", {}) or {}


def _github_spec(mapping: Any) -> Any:
    return _value(_mapping_columns(mapping), "github", {}) or {}


def _invalid_logins(mapping: Any) -> set[str]:
    identity = _value(mapping, "identity", {}) or {}
    configured = _value(identity, "invalid_logins", None)
    if configured is None:
        return set(_DEFAULT_INVALID_LOGINS)
    return {_text(value).casefold() for value in configured if _text(value)}


def _github_prefixes(mapping: Any) -> tuple[str, ...]:
    configured = _value(_github_spec(mapping), "strip_prefixes", None) or []
    # Longest first prevents a shorter configured prefix from consuming the
    # beginning of a longer URL form.
    values = {_text(value) for value in (*_DEFAULT_GITHUB_PREFIXES, *configured) if _text(value)}
    return tuple(sorted(values, key=len, reverse=True))


def normalise_github(value: Any, mapping: Any | None = None) -> str | None:
    """Normalise a GitHub URL/handle, returning ``None`` for invalid input.

    The organizer export contains full URLs, whereas the sample export uses a
    mixture of URLs, ``@`` handles, and bare handles.  Invalid GitHub paths
    such as ``/in`` are deliberately treated as missing evidence so they can
    never become a dedupe key.
    """

    candidate = _text(value)
    if not candidate or any(character.isspace() for character in candidate):
        return None

    for prefix in _github_prefixes(mapping):
        if candidate.casefold().startswith(prefix.casefold()):
            candidate = candidate[len(prefix) :]
            break

    # Be tolerant of a URL without the slash included in a custom prefix.
    candidate = re.sub(
        r"^https?://(?:www\.)?github\.com/?",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = candidate.rstrip("/").lstrip("@").rstrip("/").strip()
    if (
        not candidate
        or any(character.isspace() for character in candidate)
        or any(character in candidate for character in "/?#")
    ):
        return None

    candidate = candidate.casefold()
    if candidate in _invalid_logins(mapping):
        return None
    if not _GITHUB_LOGIN_RE.fullmatch(candidate):
        return None
    return candidate


# American spelling is convenient for callers outside this module; the
# project docs use the British spelling, so retain both names.
normalize_github = normalise_github


def _normalise_identity_part(value: str | None) -> str:
    return " ".join(_text(value).split()).casefold()


def identity_id(name: str, org: str | None, github_login: str | None) -> str:
    """Return the amendment-compatible stable person identity."""

    if github_login:
        material = github_login.casefold()
    else:
        material = f"{_normalise_identity_part(name)}|{_normalise_identity_part(org)}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value is _MISSING:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _truthy(value: Any, truthy: list[Any], *, contains: bool = False) -> bool | None:
    text = _text(value).casefold()
    if not text:
        return None
    choices = {_text(item).casefold() for item in truthy if _text(item)}
    if not choices:
        return None
    if contains:
        return any(choice in text for choice in choices)
    return text in choices


def _student_flag(view: dict[str, Any], spec: Any) -> bool | None:
    column = _column_name(spec)
    truthy = list(_value(spec, "truthy", []) or [])
    if column:
        raw = _column_value(view, spec)
        if raw is _MISSING:
            return None
        return _truthy(raw, truthy)

    derive_from = list(_value(spec, "derive_from", []) or [])
    if not derive_from:
        return None
    values = [_column_value(view, item) for item in derive_from]
    present = [item for item in values if item is not _MISSING and _text(item)]
    if not present:
        return None
    return _truthy(" ".join(_text(item) for item in present), truthy, contains=True)


def _name(view: dict[str, Any], columns: Any) -> str:
    name_parts = list(_value(columns, "name_parts", []) or [])
    if name_parts:
        return " ".join(
            _text(_column_value(view, part))
            for part in name_parts
            if _column_value(view, part) is not _MISSING and _text(_column_value(view, part))
        ).strip()
    raw = _column_value(view, _value(columns, "name", "Full Name"))
    if raw is _MISSING:
        return ""
    return _text(raw)


def _email_domain(email: str) -> str:
    if "@" not in email:
        return ""
    domain = email.rsplit("@", 1)[1].strip().casefold()
    return domain if domain and " " not in domain else ""


def apply(row: Any, mapping: Mapping, source_file: str | Path, row_no: int) -> Person:
    """Apply ``mapping`` to one source row and return a frozen-contract Person."""

    view = _row_view(row)
    columns = _mapping_columns(mapping)
    github_raw = _column_value(view, _value(columns, "github", {}))
    github_login = None if github_raw is _MISSING else normalise_github(github_raw, mapping)

    name = _name(view, columns)
    org = _optional_text(view, _value(columns, "org", None))
    role = _optional_text(view, _value(columns, "role", None))
    email_value = _column_value(view, _value(columns, "email", "Email"))
    email = "" if email_value is _MISSING else _text(email_value)

    linkedin = _optional_text(view, _value(columns, "linkedin", None))
    registered_raw = _column_value(view, _value(columns, "registered_at", None))
    registered_at = _parse_datetime(registered_raw)

    answers: dict[str, str] = {}
    for header in list(_value(columns, "answers", []) or []):
        raw = _column_value(view, header)
        if raw is _MISSING:
            continue
        answer = _text(raw)
        if answer:
            answers[_text(header)] = answer

    return Person(
        id=identity_id(name, org, github_login),
        name=name,
        email=email,
        email_domain=_email_domain(email),
        github_login=github_login,
        linkedin_url=linkedin,
        org=org,
        role=role,
        is_student=_student_flag(view, _value(columns, "student_flag", {})),
        answers=answers,
        registered_at=registered_at,
        alias_of=None,
        source_file=str(source_file),
        source_row=int(row_no),
    )


__all__ = [
    "ColumnSpec",
    "Mapping",
    "apply",
    "identity_id",
    "load",
    "normalise_github",
    "normalize_github",
]
