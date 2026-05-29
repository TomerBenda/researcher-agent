"""Tests for the GitHub topic-search adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from researcher_agent.http import PoliteClient
from researcher_agent.sources.github_topic import GithubTopicAdapter, GithubTopicConfig

NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)

RESULT = {
    "items": [
        {
            "id": 10,
            "full_name": "modelcontextprotocol/servers",
            "html_url": "https://github.com/modelcontextprotocol/servers",
            "description": "Reference MCP servers.",
            "stargazers_count": 4200,
            "pushed_at": "2026-05-25T00:00:00Z",
        },
        {
            "id": 11,
            "full_name": "someone/old-mcp-thing",
            "html_url": "https://github.com/someone/old-mcp-thing",
            "description": "Older repo.",
            "stargazers_count": 12,
            "pushed_at": "2026-05-01T00:00:00Z",
        },
    ]
}


def _adapter(topic: str = "mcp-server", min_stars: int = 5) -> GithubTopicAdapter:
    return GithubTopicAdapter(
        GithubTopicConfig(name="gh-topic:mcp", topic=topic, min_stars=min_stars)
    )


def _client(handler) -> PoliteClient:
    return PoliteClient(transport=httpx.MockTransport(handler), min_interval_seconds=0.0)


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(RESULT).encode())


def test_config_type() -> None:
    assert GithubTopicConfig(name="x", topic="mcp").type == "github_topic"


def test_fetch_parses_repos() -> None:
    with _client(_ok) as c:
        result = _adapter().fetch(c, {}, NOW)
    assert {r.external_id for r in result.raw_items} == {"10", "11"}
    p = next(r.payload for r in result.raw_items if r.external_id == "10")
    assert p["title"] == "modelcontextprotocol/servers"
    assert p["stars"] == 4200


def test_query_carries_topic_and_stars() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _ok(request)

    with _client(handler) as c:
        _adapter(topic="mcp-server", min_stars=5).fetch(c, {}, NOW)
    url = seen[0]
    assert "search/repositories" in url
    assert "topic%3Amcp-server" in url
    assert "stars%3A%3E%3D5" in url


def test_watermark_skips_unchanged_repos() -> None:
    with _client(_ok) as c:
        first = _adapter().fetch(c, {}, NOW)
        # second run with the watermark from the first: nothing newer -> empty
        second = _adapter().fetch(c, first.cursor, NOW)
    assert len(first.raw_items) == 2
    assert second.raw_items == []
    # watermark is the newest pushed_at across the result
    from researcher_agent.sources.github import iso_to_epoch

    assert first.cursor["last_pushed_epoch"] == iso_to_epoch("2026-05-25T00:00:00Z")


def test_same_second_new_repo_not_lost_at_watermark() -> None:
    # The first run sets the watermark to repo 10's pushed_at (2026-05-25). A
    # later run returns a genuinely-NEW repo (id 12) pushed in that SAME second.
    # A strict `pushed <= watermark` skip would silently drop it; the boundary-id
    # cursor must still emit 12 while not re-emitting the already-seen repo 10.
    second_result = {
        "items": [
            RESULT["items"][0],  # id 10, pushed 2026-05-25 — already seen
            {
                "id": 12,
                "full_name": "new/same-second-repo",
                "html_url": "https://github.com/new/same-second-repo",
                "description": "Pushed in the same second as the watermark.",
                "stargazers_count": 50,
                "pushed_at": "2026-05-25T00:00:00Z",
            },
        ]
    }
    bodies = iter([RESULT, second_result])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(next(bodies)).encode())

    with _client(handler) as c:
        adapter = _adapter()
        first = adapter.fetch(c, {}, NOW)
        second = adapter.fetch(c, first.cursor, NOW)
    assert {r.external_id for r in first.raw_items} == {"10", "11"}
    assert {r.external_id for r in second.raw_items} == {"12"}


def test_server_error_raises() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    with _client(boom) as c, pytest.raises(httpx.HTTPStatusError):
        _adapter().fetch(c, {}, NOW)
