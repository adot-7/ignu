from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import github_evidence

FIXTURES = Path(__file__).parent / "fixtures" / "github"


def _fixture(name: str) -> Any:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def _json_response(request: httpx.Request, payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        headers={"X-RateLimit-Remaining": "4999"},
        request=request,
    )


class _RecordedGitHub:
    """Serve recorded JSON responses while counting every attempted request."""

    def __init__(self, *, mode: str = "quiet") -> None:
        self.mode = mode
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        path = request.url.path
        if self.mode == "quiet":
            if path == "/users/quiet-builder/repos":
                return _json_response(request, _fixture("quiet_builder_repos.json"))
            if path == "/repos/quiet-builder/quiet-rag/commits":
                name = (
                    "quiet_builder_commits_90d.json"
                    if request.url.params.get("since")
                    else "quiet_builder_commits_total.json"
                )
                return _json_response(request, _fixture(name))
            if path == "/repos/quiet-builder/old-not-owned/commits":
                return _json_response(request, _fixture("quiet_builder_empty_commits.json"))
            if path == "/repos/quiet-builder/forked-template/commits":
                return _json_response(request, _fixture("quiet_builder_empty_commits.json"))
            if path == "/repos/quiet-builder/quiet-rag/readme":
                return httpx.Response(
                    200,
                    text=_fixture("quiet_builder_readme.json")["text"],
                    headers={
                        "X-RateLimit-Remaining": "4999",
                        "Content-Type": "text/plain",
                    },
                    request=request,
                )
            if path == "/repos/quiet-builder/forked-template/readme":
                return httpx.Response(
                    200,
                    text="# inherited template\n",
                    headers={"X-RateLimit-Remaining": "4999"},
                    request=request,
                )
        elif self.mode == "forks":
            if path == "/users/fork-farmer/repos":
                return _json_response(request, _fixture("fork_farmer_repos.json"))
            if path.startswith("/repos/fork-farmer/") and path.endswith("/commits"):
                return _json_response(request, _fixture("quiet_builder_empty_commits.json"))
            if path.startswith("/repos/fork-farmer/") and path.endswith("/readme"):
                return httpx.Response(
                    200,
                    text=_fixture("fork_farmer_readme.json")["text"],
                    headers={"X-RateLimit-Remaining": "4999"},
                    request=request,
                )
        raise AssertionError(f"unexpected recorded request: {request.url}")


def _settings(tmp_path: Path, *, max_repos: int = 3) -> dict[str, Any]:
    return {
        "cache_dir": str(tmp_path / "cache"),
        "github_max_repos_per_person": max_repos,
        "github_readme_max_chars": 4000,
        "github_token": "fixture-token",
    }


def test_fetch_normalises_url_collects_commits_cleans_readme_and_hits_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _RecordedGitHub()
    transport = httpx.MockTransport(recorded)
    emitted = []
    monkeypatch.setattr(github_evidence.events, "emit", emitted.append)

    with httpx.Client(transport=transport) as client:
        first = github_evidence.fetch(
            "https://github.com/Quiet-Builder/",
            settings=_settings(tmp_path, max_repos=2),
            client=client,
            run_id="offline-run",
            person_id="person-1",
        )
        calls_after_first = len(recorded.calls)
        second = github_evidence.fetch(
            "quiet-builder",
            settings=_settings(tmp_path, max_repos=2),
            client=client,
            run_id="offline-run",
            person_id="person-1",
        )

    assert first.status == "ok"
    assert second == first
    assert len(recorded.calls) == calls_after_first
    assert first.login == "quiet-builder"
    assert [repo.full_name for repo in first.repos] == [
        "quiet-builder/quiet-rag",
        "quiet-builder/forked-template",
    ]
    quiet = first.repos[0]
    assert quiet.author_commits_total == 12
    assert quiet.author_commits_90d == 3
    assert quiet.readme_excerpt is not None
    assert "retrieval augmented generation pipeline" in quiet.readme_excerpt
    assert "shields.io" not in quiet.readme_excerpt
    assert "![" not in quiet.readme_excerpt
    assert "<" not in quiet.readme_excerpt

    assert [event.status for event in emitted] == ["start", "ok", "start", "ok"]
    assert all(event.stage == "github" for event in emitted)
    assert all(event.run_id == "offline-run" for event in emitted)
    assert all("fixture-token" not in repr(event) for event in emitted)
    cache_files = list((tmp_path / "cache" / "github" / "quiet-builder").glob("*.json"))
    assert cache_files
    assert all("-" in path.name for path in cache_files)
    assert all("fixture-token" not in path.read_text(encoding="utf-8") for path in cache_files)


def test_fork_farming_keeps_zero_commit_forks_beyond_positive_repo_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _RecordedGitHub(mode="forks")
    emitted = []
    monkeypatch.setattr(github_evidence.events, "emit", emitted.append)

    with httpx.Client(transport=httpx.MockTransport(recorded)) as client:
        result = github_evidence.fetch(
            "fork-farmer",
            settings=_settings(tmp_path, max_repos=2),
            client=client,
        )

    assert result.status == "ok"
    assert len(result.repos) == 5
    assert all(repo.is_fork for repo in result.repos)
    assert all(repo.author_commits_total == 0 for repo in result.repos)
    assert emitted[-1].status == "ok"
    assert emitted[-1].data == {"login": "fork-farmer", "repo_count": 5}


@pytest.mark.parametrize(
    ("login", "status", "response_status", "headers", "fixture"),
    [
        ("missing-user", "not_found", 404, {"X-RateLimit-Remaining": "4999"}, "not_found.json"),
        ("private-user", "forbidden", 403, {"X-RateLimit-Remaining": "4999"}, "forbidden.json"),
        ("limited-user", "rate_limited", 403, {"X-RateLimit-Remaining": "0"}, "rate_limited.json"),
    ],
)
def test_http_statuses_degrade_and_emit_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    login: str,
    status: str,
    response_status: int,
    headers: dict[str, str],
    fixture: str,
) -> None:
    emitted = []
    monkeypatch.setattr(github_evidence.events, "emit", emitted.append)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            response_status,
            json=_fixture(fixture),
            headers=headers,
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = github_evidence.fetch(login, settings=_settings(tmp_path), client=client)

    assert result.status == status
    assert result.repos == []
    assert [event.status for event in emitted] == ["start", "skip"]
    assert emitted[-1].data["status"] == status


def test_transport_error_and_reserved_dataset_url_are_non_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted = []
    monkeypatch.setattr(github_evidence.events, "emit", emitted.append)

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(transport=httpx.MockTransport(offline)) as client:
        failed = github_evidence.fetch(
            "valid-user", settings=_settings(tmp_path), client=client
        )
        invalid = github_evidence.fetch(
            "https://github.com/in", settings=_settings(tmp_path), client=client
        )

    assert failed.status == "error"
    assert failed.repos == []
    assert invalid.login == "in"
    assert invalid.status == "not_found"
    # The invalid reserved path is rejected before a client request is made.
    assert len(emitted) == 4
    assert [event.status for event in emitted] == ["start", "skip", "start", "skip"]
