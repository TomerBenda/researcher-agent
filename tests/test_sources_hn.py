"""Tests for the Hacker News (Algolia) search adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from researcher_agent.http import PoliteClient
from researcher_agent.sources.hn_search import HnSearchAdapter, HnSearchConfig

NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)

HITS = {
    "hits": [
        {
            "objectID": "111",
            "title": "Show HN: mcp-scan for malicious servers",
            "url": "https://example.com/mcp-scan",
            "author": "alice",
            "points": 120,
            "num_comments": 30,
            "created_at_i": 1_900_000_200,
        },
        {
            "objectID": "222",
            "title": "Ask HN: how do you secure MCP?",
            "story_text": "Discussion about CVE-2025-7777 mitigations.",
            "author": "bob",
            "points": 5,
            "num_comments": 2,
            "created_at_i": 1_900_000_100,
        },
    ]
}


def _adapter(query: str = '"model context protocol"') -> HnSearchAdapter:
    return HnSearchAdapter(HnSearchConfig(name="hn:mcp", query=query))


def _client(handler) -> PoliteClient:
    return PoliteClient(transport=httpx.MockTransport(handler), min_interval_seconds=0.0)


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(HITS).encode())


def test_config_defaults() -> None:
    cfg = HnSearchConfig(name="hn:x", query="mcp")
    assert cfg.type == "hn_search"
    assert cfg.tags == "story"


def test_fetch_parses_hits() -> None:
    with _client(_ok) as c:
        result = _adapter().fetch(c, {}, NOW)
    assert {r.external_id for r in result.raw_items} == {"111", "222"}
    assert result.raw_items[0].source_type == "hn_search"


def test_link_prefers_article_else_hn_item() -> None:
    with _client(_ok) as c:
        result = _adapter().fetch(c, {}, NOW)
    by_id = {r.external_id: r for r in result.raw_items}
    assert by_id["111"].payload["link"] == "https://example.com/mcp-scan"
    # text post with no url falls back to the HN discussion link
    assert by_id["222"].payload["link"] == "https://news.ycombinator.com/item?id=222"


def test_cursor_advances_to_max_created_at() -> None:
    with _client(_ok) as c:
        result = _adapter().fetch(c, {}, NOW)
    assert result.cursor["last_created_at_i"] == 1_900_000_200


def test_cursor_applies_numeric_filter() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _ok(request)

    with _client(handler) as c:
        _adapter().fetch(c, {"last_created_at_i": 1_900_000_000}, NOW)
    assert "created_at_i%3E1900000000" in seen[0] or "created_at_i>1900000000" in seen[0]


def test_published_timestamp_set() -> None:
    with _client(_ok) as c:
        result = _adapter().fetch(c, {}, NOW)
    assert result.raw_items[0].payload["published_timestamp"] == 1_900_000_200


def test_server_error_raises() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with _client(boom) as c, pytest.raises(httpx.HTTPStatusError):
        _adapter().fetch(c, {}, NOW)


def test_unexpected_shape_yields_no_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"error": "bad query"}).encode())

    with _client(handler) as c:
        result = _adapter().fetch(c, {}, NOW)
    assert result.raw_items == []
