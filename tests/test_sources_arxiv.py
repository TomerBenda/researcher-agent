"""Tests for the arXiv source adapter (driven by httpx.MockTransport)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from researcher_agent.http import PoliteClient
from researcher_agent.sources.arxiv import ArxivAdapter, ArxivConfig

NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2505.12345v1</id>
    <title>Indirect Prompt Injection in MCP Servers</title>
    <summary>We study injection affecting agents; references CVE-2025-0001.</summary>
    <published>2026-05-20T00:00:00Z</published>
    <updated>2026-05-21T00:00:00Z</updated>
    <author><name>Alice Researcher</name></author>
    <link href="http://arxiv.org/abs/2505.12345v1" rel="alternate" type="text/html"/>
    <category term="cs.CR"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2505.99999v2</id>
    <title>Sandbox Escapes in Tool-Using Agents</title>
    <summary>Second paper abstract.</summary>
    <published>2026-05-19T00:00:00Z</published>
    <link href="http://arxiv.org/abs/2505.99999v2" rel="alternate" type="text/html"/>
    <category term="cs.CR"/>
  </entry>
</feed>
"""


def _adapter(query: str = 'cat:cs.CR AND abs:"prompt injection"') -> ArxivAdapter:
    return ArxivAdapter(ArxivConfig(name="arxiv:secsec", query=query, max_results=25))


def _client(handler) -> PoliteClient:
    return PoliteClient(transport=httpx.MockTransport(handler), min_interval_seconds=0.0)


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=ATOM.encode())


def test_config_type_defaults_to_arxiv() -> None:
    cfg = ArxivConfig(name="arxiv:x", query="all:mcp")
    assert cfg.type == "arxiv"
    assert cfg.max_results == 50


def test_fetch_parses_entries() -> None:
    with _client(_ok) as c:
        result = _adapter().fetch(c, {}, NOW)
    assert len(result.raw_items) == 2
    titles = [r.payload["title"] for r in result.raw_items]
    assert "Indirect Prompt Injection in MCP Servers" in titles


def test_request_url_carries_query_and_sort() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _ok(request)

    with _client(handler) as c:
        _adapter(query="all:mcp").fetch(c, {}, NOW)
    assert "export.arxiv.org/api/query" in seen[0]
    assert "search_query=all%3Amcp" in seen[0]
    assert "sortBy=submittedDate" in seen[0]


def test_external_id_and_payload() -> None:
    with _client(_ok) as c:
        result = _adapter().fetch(c, {}, NOW)
    first = result.raw_items[0]
    assert first.external_id == "http://arxiv.org/abs/2505.12345v1"
    assert first.payload["link"] == "http://arxiv.org/abs/2505.12345v1"
    assert "published_timestamp" in first.payload
    assert first.source_type == "arxiv"


ERROR_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/api/errors#incorrect_id_format_for_query</id>
    <title>Error</title>
    <summary>incorrect id format for query</summary>
    <link href="http://arxiv.org/api/errors#incorrect_id_format_for_query" rel="alternate"/>
  </entry>
</feed>
"""

MIXED_FEED = ATOM.replace(
    "</feed>",
    """  <entry>
    <id>http://arxiv.org/api/errors#some_transient_problem</id>
    <title>Error</title>
    <link href="http://arxiv.org/api/errors#some_transient_problem" rel="alternate"/>
  </entry>
</feed>""",
)


def test_error_feed_yields_no_items() -> None:
    # arXiv answers a bad query / transient fault with 200 + an error sentinel
    # entry; it must not be stored/classified/rendered as a paper.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ERROR_FEED.encode())

    with _client(handler) as c:
        result = _adapter().fetch(c, {}, NOW)
    assert result.raw_items == []


def test_error_entry_filtered_from_mixed_feed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=MIXED_FEED.encode())

    with _client(handler) as c:
        result = _adapter().fetch(c, {}, NOW)
    # the two real papers survive; the trailing error entry is dropped
    assert len(result.raw_items) == 2
    assert all("errors" not in r.external_id for r in result.raw_items)


def test_server_error_raises() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    import pytest

    with _client(boom) as c, pytest.raises(httpx.HTTPStatusError):
        _adapter().fetch(c, {}, NOW)
