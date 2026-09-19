"""Fetch deterministic GitHub evidence with an offline-friendly disk cache.

The GitHub lane is deliberately a small adapter around the public REST API.
It does not infer anything about a person: it records repository metadata,
the number of commits returned for the requested author, and a cleaned README
excerpt for the profile lane.  Every request is cached so a prerun can warm
the cache once and the visible demo can replay without network access.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from . import events
from .config import get_settings
from .mapping import normalise_github
from .models import PipelineEvent, RepoEvidence

logger = logging.getLogger(__name__)

GitHubStatus = Literal["ok", "not_found", "forbidden", "rate_limited", "error"]

_API_ROOT = "https://api.github.com"
_MAX_CANDIDATES = 8
_DEFAULT_MAX_REPOS = 3
_DEFAULT_README_MAX_CHARS = 4000
_HTTP_TIMEOUT_SECONDS = 15.0
_RAW_README_ACCEPT = "application/vnd.github.raw+json"


class GitHubResult(BaseModel):
    """The non-raising result returned by :func:`fetch`."""

    login: str
    status: GitHubStatus
    repos: list[RepoEvidence] = Field(default_factory=list)
    fetched_at: datetime


@dataclass
class _HttpResponse:
    """The small response subset needed by the extractor and cache."""

    status_code: int
    headers: dict[str, str]
    body: Any
    body_type: Literal["json", "text", "invalid"]
    fetched_at: datetime
    from_cache: bool = False


class _RequestFailure(RuntimeError):
    """Internal marker for a transport failure without exposing request data."""

    def __init__(self, exception_name: str) -> None:
        self.exception_name = exception_name
        super().__init__(exception_name)


class _PayloadFailure(RuntimeError):
    """Internal marker for a successful HTTP response with an unusable body."""


def _setting(settings: Any, name: str, default: Any = None) -> Any:
    """Read a typed settings object or a small mapping used by tests."""

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


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_datetime(value: Any, default: datetime | None = None) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return default


def _display_login(value: Any) -> str:
    """Return a safe, useful login label even when input is malformed."""

    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    normalised = normalise_github(text)
    if normalised:
        return normalised

    # Invalid values are never sent to GitHub.  Keep only the final path
    # segment for a useful result/event label (for example ``/in``), rather
    # than retaining a whole pasted URL.
    text = text.rstrip("/").split("?", 1)[0].split("#", 1)[0]
    return text.rsplit("/", 1)[-1].lstrip("@").casefold()


def _safe_login(value: Any) -> str | None:
    """Normalise a bare handle or profile URL using the dataset contract."""

    text = "" if value is None else str(value).strip()
    if not text:
        return None
    return normalise_github(text)


def _safe_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    """Keep only response headers needed for cached status/pagination logic."""

    kept: dict[str, str] = {}
    for key, value in headers.items():
        lowered = str(key).casefold()
        if lowered in {"x-ratelimit-remaining", "link", "content-type"}:
            kept[lowered] = str(value)
    return kept


def _cache_digest(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _cache_path_candidates(directory: Path, url: str) -> list[Path]:
    digest = _cache_digest(url)
    # The dated name is the format written by this module.  The plain digest
    # fallback makes the reader tolerant of early prerun cache experiments.
    dated = sorted(directory.glob(f"*-{digest}.json"), reverse=True)
    plain = directory / f"{digest}.json"
    if plain.exists():
        dated.append(plain)
    return dated


def _response_from_cache(path: Path, *, url: str) -> _HttpResponse | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None

    # Cache files written by this extractor carry an envelope.  Accepting a
    # raw JSON body as a fallback is useful when a human records a fixture by
    # hand and places it in a cache directory during a demo rehearsal.
    if not isinstance(payload, dict) or "status_code" not in payload:
        try:
            fetched_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            fetched_at = datetime.now(timezone.utc)
        return _HttpResponse(
            status_code=200,
            headers={},
            body=payload,
            body_type="json",
            fetched_at=fetched_at,
            from_cache=True,
        )

    cached_url = payload.get("url")
    if cached_url is not None and cached_url != url:
        return None
    body_type = payload.get("body_type", "json")
    if body_type not in {"json", "text", "invalid"}:
        body_type = "invalid"
    fetched_at = _as_datetime(payload.get("fetched_at"), None)
    if fetched_at is None:
        try:
            fetched_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            fetched_at = datetime.now(timezone.utc)
    return _HttpResponse(
        status_code=_as_int(payload.get("status_code"), 599),
        headers={
            str(key).casefold(): str(value)
            for key, value in (payload.get("headers") or {}).items()
        },
        body=payload.get("body"),
        body_type=body_type,  # type: ignore[arg-type]
        fetched_at=fetched_at,
        from_cache=True,
    )


def _write_response_cache(
    directory: Path,
    url: str,
    response: _HttpResponse,
    *,
    stamp: date,
) -> None:
    temporary: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{stamp.isoformat()}-{_cache_digest(url)}.json"
        payload = {
            "url": url,
            "fetched_at": response.fetched_at.isoformat(),
            "status_code": response.status_code,
            "headers": response.headers,
            "body_type": response.body_type,
            "body": response.body,
        }
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(destination)
    except (OSError, TypeError, ValueError):
        # Cache failure must not turn a usable GitHub response into a failed
        # pipeline stage.  Do not log the URL or headers (which can contain
        # user-controlled values, and must never contain the token).
        try:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        except OSError:
            pass
        logger.debug("github response could not be cached", extra={"exception": "cache_write"})


def _strip_readme_line(line: str) -> str:
    """Remove visual-only README markup while retaining nearby prose."""

    line = html.unescape(line)
    # Images, badges, and their linked variants are not useful evidence text.
    if re.search(
        r"(?:shields\.io|badge|\.(?:png|jpe?g|gif|webp|svg)(?:[?#) ]|$))",
        line,
        flags=re.IGNORECASE,
    ):
        return ""
    if re.search(r"!\[[^\]]*\]\s*\([^)]*\)", line):
        return ""
    if re.search(r"<\s*(?:img|svg|picture|source|video)\b", line, flags=re.IGNORECASE):
        return ""

    # Keep text inside ordinary HTML containers but remove the tags.  This
    # turns a simple ``<p>description</p>`` into useful evidence and removes
    # HTML comments entirely.
    line = re.sub(r"<!--.*?-->", "", line, flags=re.DOTALL)
    line = re.sub(r"<[^>]*>", "", line)
    # A line that was only an HTML tag/image is not evidence.
    if not line.strip():
        return ""
    return re.sub(r"[ \t]+", " ", line).strip()


def clean_readme(text: str | None, max_chars: int = _DEFAULT_README_MAX_CHARS) -> str:
    """Return a bounded prose/code excerpt without HTML or visual assets."""

    if not text or max_chars <= 0:
        return ""
    lines = [_strip_readme_line(line) for line in str(text).splitlines()]
    cleaned_lines: list[str] = []
    blank_pending = False
    for line in lines:
        if line:
            if blank_pending and cleaned_lines:
                cleaned_lines.append("")
            cleaned_lines.append(line)
            blank_pending = False
        elif cleaned_lines:
            blank_pending = True
    cleaned = "\n".join(cleaned_lines).strip()
    return cleaned[:max_chars]


def _decode_readme_content(content: str, encoding: Any = None) -> str:
    if str(encoding or "").casefold() != "base64":
        return content
    try:
        decoded = base64.b64decode(content.replace("\n", ""), validate=False)
        return decoded.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return content


def _is_rate_limited(response: _HttpResponse) -> bool:
    if response.status_code == 429:
        return True
    remaining = response.headers.get("x-ratelimit-remaining")
    try:
        if remaining is not None and int(remaining) <= 0:
            return True
    except (TypeError, ValueError):
        pass
    message = ""
    if isinstance(response.body, Mapping):
        message = str(response.body.get("message", ""))
    return response.status_code in {403, 429} and any(
        marker in message.casefold()
        for marker in ("rate limit", "secondary rate", "abuse detection")
    )


def _response_status(response: _HttpResponse) -> GitHubStatus:
    if _is_rate_limited(response):
        return "rate_limited"
    if 200 <= response.status_code < 300:
        return "ok"
    if response.status_code == 404:
        return "not_found"
    if response.status_code in {401, 403}:
        return "forbidden"
    return "error"


def _timestamp(value: datetime | None) -> float:
    if value is None:
        return float("-inf")
    try:
        return value.timestamp()
    except (OverflowError, OSError, ValueError):
        return float("-inf")


@dataclass
class _Candidate:
    full_name: str
    html_url: str
    is_fork: bool
    stars: int
    primary_language: str | None
    last_push: datetime | None
    description: str | None
    topics: list[str]


def _candidate_from_payload(payload: Any, login: str) -> _Candidate | None:
    if not isinstance(payload, Mapping):
        return None
    full_name = str(payload.get("full_name") or "").strip()
    if not full_name:
        name = str(payload.get("name") or "").strip()
        if name:
            full_name = f"{login}/{name}"
    if not full_name:
        return None
    html_url = str(payload.get("html_url") or f"https://github.com/{full_name}").strip()
    language = payload.get("language")
    topics_value = payload.get("topics") or []
    topics = [str(topic) for topic in topics_value] if isinstance(topics_value, list) else []
    return _Candidate(
        full_name=full_name,
        html_url=html_url,
        is_fork=bool(payload.get("fork", False)),
        stars=max(0, _as_int(payload.get("stargazers_count"), 0)),
        primary_language=str(language) if language is not None else None,
        last_push=_as_datetime(payload.get("pushed_at"), None),
        description=(
            str(payload.get("description"))
            if payload.get("description") is not None
            else None
        ),
        topics=topics,
    )


class _GitHubExtractor:
    def __init__(
        self,
        *,
        settings: Any | None = None,
        client: httpx.Client | Any | None = None,
        transport: httpx.BaseTransport | None = None,
        cache_dir: str | Path | None = None,
        run_id: str | None = None,
        person_id: str | None = None,
        max_repos: int | None = None,
        readme_max_chars: int | None = None,
    ) -> None:
        self.settings = settings if settings is not None else get_settings()
        configured_cache = cache_dir or _setting(self.settings, "cache_dir", "data/cache")
        self.cache_root = Path(configured_cache) / "github"
        self.max_repos = max(
            0,
            _as_int(
                max_repos
                if max_repos is not None
                else _setting(self.settings, "github_max_repos_per_person", _DEFAULT_MAX_REPOS),
                _DEFAULT_MAX_REPOS,
            ),
        )
        self.readme_max_chars = max(
            0,
            _as_int(
                readme_max_chars
                if readme_max_chars is not None
                else _setting(
                    self.settings, "github_readme_max_chars", _DEFAULT_README_MAX_CHARS
                ),
                _DEFAULT_README_MAX_CHARS,
            ),
        )
        self.token = str(_setting(self.settings, "github_token", "") or "")
        self.client = client
        self.transport = transport
        self.run_id = run_id or os.environ.get("RUN_ID", "github")
        self.person_id = person_id
        self.now = datetime.now(timezone.utc)
        # A date (rather than a full timestamp) makes the ``since`` URL stable
        # for the day, which is essential for a second cache-only fetch.
        self.since = (self.now.date() - timedelta(days=90)).isoformat()

    @contextmanager
    def _client_scope(self) -> Iterator[Any]:
        if self.client is not None:
            yield self.client
            return
        client_kwargs: dict[str, Any] = {
            "timeout": _HTTP_TIMEOUT_SECONDS,
            "follow_redirects": True,
        }
        if self.transport is not None:
            client_kwargs["transport"] = self.transport
        owned_client = httpx.Client(**client_kwargs)
        try:
            yield owned_client
        finally:
            close = getattr(owned_client, "close", None)
            if callable(close):
                close()

    def _request_headers(self, accept: str) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ignu-github-evidence/1.0",
        }
        if self.token:
            # The token is intentionally kept only in the in-memory request
            # header; it is never included in events or cache files.
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request_for_login(
        self,
        login: str,
        url: str,
        *,
        accept: str,
        force: bool,
        client: Any,
    ) -> _HttpResponse:
        directory = self.cache_root / login
        if not force:
            for path in _cache_path_candidates(directory, url):
                cached = _response_from_cache(path, url=url)
                if cached is not None:
                    return cached

        try:
            response = client.get(url, headers=self._request_headers(accept))
        except Exception as exc:  # httpx transport and injected-client failures
            raise _RequestFailure(type(exc).__name__) from exc

        response_headers = _safe_headers(response.headers)
        body: Any = None
        body_type: Literal["json", "text", "invalid"]
        if accept.startswith("application/vnd.github.raw"):
            try:
                body = response.text
                body_type = "text"
            except Exception:  # noqa: BLE001 - malformed injected response degrades safely
                body = None
                body_type = "invalid"
        else:
            try:
                body = response.json()
                body_type = "json"
            except Exception:  # noqa: BLE001 - malformed injected response degrades safely
                try:
                    body = response.text
                except Exception:  # noqa: BLE001 - malformed injected response degrades safely
                    body = None
                body_type = "invalid"

        result = _HttpResponse(
            status_code=int(response.status_code),
            headers=response_headers,
            body=body,
            body_type=body_type,
            fetched_at=self.now,
        )
        _write_response_cache(
            directory,
            url,
            result,
            stamp=self.now.date(),
        )
        return result

    def _emit(
        self,
        login: str,
        status: Literal["start", "ok", "skip"],
        msg: str,
        *,
        repos: int = 0,
        reason: str | None = None,
    ) -> None:
        data: dict[str, Any] = {"login": login, "repo_count": repos}
        if reason:
            data["status"] = reason
        try:
            events.emit(
                PipelineEvent(
                    ts=datetime.now(timezone.utc),
                    run_id=self.run_id,
                    stage="github",
                    person_id=self.person_id,
                    status=status,
                    msg=msg,
                    data=data,
                )
            )
        except Exception:  # pragma: no cover - event persistence is defensive
            logger.debug("could not emit github pipeline event", exc_info=True)

    @staticmethod
    def _repo_url(full_name: str, suffix: str) -> str:
        return f"{_API_ROOT}/repos/{quote(full_name, safe='/')}/{suffix}"

    def _fetch_impl(self, login: str, *, force: bool) -> GitHubResult:
        self._emit(login, "start", "fetching GitHub evidence")
        user_url = (
            f"{_API_ROOT}/users/{quote(login, safe='')}/repos"
            "?per_page=100&sort=pushed"
        )
        with self._client_scope() as client:
            first = self._request_for_login(
                login,
                user_url,
                accept="application/vnd.github+json",
                force=force,
                client=client,
            )
            first_status = _response_status(first)
            if first_status != "ok":
                return self._result(login, first_status, [], first.fetched_at)
            if first.body_type != "json" or not isinstance(first.body, list):
                raise _PayloadFailure("repositories")

            raw_repositories = list(first.body)
            if len(raw_repositories) >= 100 or 'rel="next"' in first.headers.get("link", ""):
                second_url = f"{user_url}&page=2"
                second = self._request_for_login(
                    login,
                    second_url,
                    accept="application/vnd.github+json",
                    force=force,
                    client=client,
                )
                second_status = _response_status(second)
                if second_status != "ok":
                    return self._result(login, second_status, [], first.fetched_at)
                if second.body_type != "json" or not isinstance(second.body, list):
                    raise _PayloadFailure("repositories_page_2")
                raw_repositories.extend(second.body)

            candidates = [
                candidate
                for candidate in (
                    _candidate_from_payload(payload, login) for payload in raw_repositories
                )
                if candidate is not None
            ]
            candidates.sort(
                key=lambda candidate: (
                    not candidate.is_fork,
                    _timestamp(candidate.last_push),
                ),
                reverse=True,
            )
            candidates = candidates[:_MAX_CANDIDATES]

            kept: list[RepoEvidence] = []
            positive_repos = 0
            downstream_status: GitHubStatus = "ok"
            for candidate in candidates:
                # Candidates are sorted with originals first.  Once the
                # configured positive-repo budget is full, skip remaining
                # originals but still inspect every fork so zero-author forks
                # remain visible as fork-farming evidence.
                if positive_repos >= self.max_repos and not candidate.is_fork:
                    continue

                total_url = self._repo_url(
                    candidate.full_name,
                    f"commits?author={quote(login, safe='')}&per_page=100",
                )
                total_response = self._request_for_login(
                    login,
                    total_url,
                    accept="application/vnd.github+json",
                    force=force,
                    client=client,
                )
                total_status = _response_status(total_response)
                if total_status in {"rate_limited", "forbidden", "error"}:
                    downstream_status = total_status
                    break
                total_count = self._commit_count(total_response) if total_status == "ok" else 0

                since_url = self._repo_url(
                    candidate.full_name,
                    "commits?"
                    f"author={quote(login, safe='')}&per_page=100&since={quote(self.since, safe='')}",
                )
                since_response = self._request_for_login(
                    login,
                    since_url,
                    accept="application/vnd.github+json",
                    force=force,
                    client=client,
                )
                since_status = _response_status(since_response)
                if since_status in {"rate_limited", "forbidden", "error"}:
                    downstream_status = since_status
                    break
                recent_count = self._commit_count(since_response) if since_status == "ok" else 0

                if total_count > 0:
                    if positive_repos >= self.max_repos:
                        # A fork was queried after the positive budget was
                        # filled only to determine whether it is a zero-commit
                        # fork-farming signal.
                        if not candidate.is_fork:
                            continue
                    else:
                        positive_repos += 1
                elif not candidate.is_fork:
                    continue

                kept.append(
                    RepoEvidence(
                        full_name=candidate.full_name,
                        html_url=candidate.html_url,
                        is_fork=candidate.is_fork,
                        stars=candidate.stars,
                        author_commits_90d=min(100, recent_count),
                        author_commits_total=min(100, total_count),
                        primary_language=candidate.primary_language,
                        last_push=candidate.last_push,
                        description=candidate.description,
                        topics=candidate.topics,
                        readme_excerpt=None,
                    )
                )

            if downstream_status == "ok":
                for index, repo in enumerate(kept[:3]):
                    readme_url = self._repo_url(repo.full_name, "readme")
                    readme_response = self._request_for_login(
                        login,
                        readme_url,
                        accept=_RAW_README_ACCEPT,
                        force=force,
                        client=client,
                    )
                    readme_status = _response_status(readme_response)
                    if readme_status == "not_found":
                        continue
                    if readme_status != "ok":
                        downstream_status = readme_status
                        break
                    raw_readme = self._readme_text(readme_response)
                    cleaned = clean_readme(raw_readme, self.readme_max_chars)
                    kept[index] = repo.model_copy(update={"readme_excerpt": cleaned or None})

            return self._result(login, downstream_status, kept, first.fetched_at)

    @staticmethod
    def _commit_count(response: _HttpResponse) -> int:
        if response.body_type != "json" or not isinstance(response.body, list):
            raise _PayloadFailure("commits")
        return min(100, len(response.body))

    @staticmethod
    def _readme_text(response: _HttpResponse) -> str:
        if response.body_type == "text":
            text = str(response.body or "")
            # Recorded fixtures sometimes preserve the API's JSON envelope
            # even though production requests ask for the raw media type.
            # Decode that shape as a compatibility fallback.
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, Mapping):
                content = parsed.get("content")
                if isinstance(content, str):
                    return _decode_readme_content(content, parsed.get("encoding"))
            if isinstance(parsed, str):
                return parsed
            return text
        if response.body_type == "json":
            if isinstance(response.body, str):
                return response.body
            if isinstance(response.body, Mapping):
                content = response.body.get("content")
                if isinstance(content, str):
                    return _decode_readme_content(content, response.body.get("encoding"))
        raise _PayloadFailure("readme")

    def _result(
        self,
        login: str,
        status: GitHubStatus,
        repos: list[RepoEvidence],
        fetched_at: datetime,
    ) -> GitHubResult:
        result = GitHubResult(
            login=login,
            status=status,
            repos=repos,
            fetched_at=fetched_at,
        )
        if status == "ok":
            self._emit(login, "ok", f"GitHub evidence fetched ({len(repos)} repos)", repos=len(repos))
        else:
            self._emit(
                login,
                "skip",
                f"GitHub evidence skipped ({status})",
                repos=len(repos),
                reason=status,
            )
        return result

    def fetch(self, value: Any, *, force: bool = False) -> GitHubResult:
        login = _safe_login(value)
        display = _display_login(value)
        if login is None:
            self._emit(display, "start", "fetching GitHub evidence")
            return self._result(display, "not_found", [], self.now)
        try:
            return self._fetch_impl(login, force=force)
        except _RequestFailure as exc:
            logger.debug(
                "github request failed",
                extra={"exception": exc.exception_name, "stage": "github"},
            )
            return self._result(login, "error", [], self.now)
        except (_PayloadFailure, ValueError, TypeError, OSError) as exc:
            logger.debug(
                "github response could not be processed",
                extra={"exception": type(exc).__name__, "stage": "github"},
            )
            return self._result(login, "error", [], self.now)
        except Exception as exc:  # noqa: BLE001 - final degrade path must never raise
            logger.debug(
                "github evidence failed",
                extra={"exception": type(exc).__name__, "stage": "github"},
            )
            return self._result(login, "error", [], self.now)


def fetch(
    login: str,
    *,
    force: bool = False,
    settings: Any | None = None,
    client: httpx.Client | Any | None = None,
    transport: httpx.BaseTransport | None = None,
    cache_dir: str | Path | None = None,
    run_id: str | None = None,
    person_id: str | None = None,
    max_repos: int | None = None,
    readme_max_chars: int | None = None,
) -> GitHubResult:
    """Fetch GitHub evidence for a handle or full profile URL.

    ``client``/``transport`` are optional injection points for recorded,
    entirely offline tests.  Production callers normally provide only
    ``login`` and optionally ``force``.  Network, cache, malformed-input, and
    GitHub API failures are converted to a status result and never raised.
    """

    extractor = _GitHubExtractor(
        settings=settings,
        client=client,
        transport=transport,
        cache_dir=cache_dir,
        run_id=run_id,
        person_id=person_id,
        max_repos=max_repos,
        readme_max_chars=readme_max_chars,
    )
    return extractor.fetch(login, force=force)


__all__ = ["GitHubResult", "GitHubStatus", "clean_readme", "fetch"]
