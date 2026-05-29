"""Tests for the GitHub releases adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from researcher_agent.http import PoliteClient
from researcher_agent.sources.github_releases import GithubReleasesAdapter, GithubReleasesConfig

NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)

RELEASES = [
    {
        "id": 1,
        "html_url": "https://github.com/modelcontextprotocol/servers/releases/tag/v0.6.0",
        "tag_name": "v0.6.0",
        "name": "v0.6.0",
        "body": "Adds sandboxed root scoping. Fixes CVE-2025-1.",
        "published_at": "2026-05-20T00:00:00Z",
        "draft": False,
        "author": {"login": "maintainer"},
    },
    {
        "id": 2,
        "html_url": "https://github.com/modelcontextprotocol/servers/releases/tag/v0.5.9",
        "tag_name": "v0.5.9",
        "body": "Older release.",
        "published_at": "2026-05-10T00:00:00Z",
        "draft": True,  # should be skipped
        "author": {"login": "maintainer"},
    },
]


def _adapter(repo: str = "modelcontextprotocol/servers") -> GithubReleasesAdapter:
    return GithubReleasesAdapter(GithubReleasesConfig(name="gh-rel:mcp", repo=repo))


def _client(handler) -> PoliteClient:
    return PoliteClient(transport=httpx.MockTransport(handler), min_interval_seconds=0.0)


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(RELEASES).encode(), headers={"ETag": '"r1"'})


def test_config_type() -> None:
    assert GithubReleasesConfig(name="x", repo="a/b").type == "github_releases"


def test_skips_drafts() -> None:
    with _client(_ok) as c:
        result = _adapter().fetch(c, {}, NOW)
    assert len(result.raw_items) == 1
    assert result.raw_items[0].external_id == "1"


def test_payload_shape() -> None:
    with _client(_ok) as c:
        result = _adapter().fetch(c, {}, NOW)
    p = result.raw_items[0].payload
    assert p["title"] == "modelcontextprotocol/servers v0.6.0"
    assert p["repo"] == "modelcontextprotocol/servers"
    assert p["link"].endswith("/v0.6.0")
    assert "published_timestamp" in p


def test_requests_api_with_github_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(request)

    with _client(handler) as c:
        _adapter().fetch(c, {}, NOW)
    assert "api.github.com/repos/modelcontextprotocol/servers/releases" in str(seen[0].url)
    assert seen[0].headers["Accept"] == "application/vnd.github+json"


def test_etag_cursor_and_304() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("If-None-Match") == '"r1"'
        return httpx.Response(304)

    with _client(handler) as c:
        result = _adapter().fetch(c, {"etag": '"r1"'}, NOW)
    assert result.not_modified is True
    assert result.raw_items == []


def test_new_etag_stored() -> None:
    with _client(_ok) as c:
        result = _adapter().fetch(c, {}, NOW)
    assert result.cursor["etag"] == '"r1"'


def test_server_error_raises() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with _client(boom) as c, pytest.raises(httpx.HTTPStatusError):
        _adapter().fetch(c, {}, NOW)


def test_non_list_body_yields_no_items() -> None:
    # a 200 with an object body (e.g. an error message) must not crash
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"message": "Not Found"}).encode())

    with _client(handler) as c:
        result = _adapter().fetch(c, {}, NOW)
    assert result.raw_items == []


def test_non_dict_elements_and_string_author_are_tolerated() -> None:
    payload = [
        None,
        "garbage",
        {
            "id": 9,
            "html_url": "https://github.com/o/r/releases/tag/v1",
            "tag_name": "v1",
            "author": "not-a-dict",  # must not crash
            "draft": False,
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    with _client(handler) as c:
        result = _adapter().fetch(c, {}, NOW)
    assert len(result.raw_items) == 1
    assert "author" not in result.raw_items[0].payload
