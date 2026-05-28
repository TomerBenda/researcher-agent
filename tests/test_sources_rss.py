"""Tests for the RSS source adapter (driven by httpx.MockTransport)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from researcher_agent.http import PoliteClient
from researcher_agent.sources.rss import RssAdapter, RssConfig

NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)

FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Security Blog</title>
    <link>https://example.com/</link>
    <item>
      <title>First Post about CVE-2025-1234</title>
      <link>https://example.com/first?utm_source=rss</link>
      <guid>https://example.com/first</guid>
      <description>A summary mentioning github.com/foo/bar here.</description>
      <pubDate>Wed, 27 May 2026 10:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Second Post</title>
      <link>https://example.com/second</link>
      <description>No guid on this one.</description>
      <pubDate>Tue, 26 May 2026 09:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


def _adapter(url: str = "https://example.com/feed.xml") -> RssAdapter:
    return RssAdapter(RssConfig(name="rss:example", url=url))


def _client(handler) -> PoliteClient:
    return PoliteClient(transport=httpx.MockTransport(handler), min_interval_seconds=0.0)


def _feed_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        content=FEED_XML.encode(),
        headers={"ETag": '"v1"', "Last-Modified": "Wed, 27 May 2026 10:00:00 GMT"},
    )


def test_config_type_defaults_to_rss() -> None:
    cfg = RssConfig(name="rss:example", url="https://example.com/feed.xml")
    assert cfg.type == "rss"


def test_fetch_parses_entries() -> None:
    with _client(_feed_handler) as c:
        result = _adapter().fetch(c, {}, NOW)

    assert len(result.raw_items) == 2
    assert result.not_modified is False
    titles = [r.payload["title"] for r in result.raw_items]
    assert "First Post about CVE-2025-1234" in titles


def test_fetch_requests_configured_url() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _feed_handler(request)

    with _client(handler) as c:
        _adapter("https://blog.test/atom.xml").fetch(c, {}, NOW)

    assert seen == ["https://blog.test/atom.xml"]


def test_external_id_prefers_guid() -> None:
    with _client(_feed_handler) as c:
        result = _adapter().fetch(c, {}, NOW)
    assert result.raw_items[0].external_id == "https://example.com/first"


def test_external_id_falls_back_to_link() -> None:
    with _client(_feed_handler) as c:
        result = _adapter().fetch(c, {}, NOW)
    # second item has no guid -> falls back to its link
    assert result.raw_items[1].external_id == "https://example.com/second"


def test_payload_preserves_original_link_with_tracking() -> None:
    # the adapter does not normalize; canonicalization happens later
    with _client(_feed_handler) as c:
        result = _adapter().fetch(c, {}, NOW)
    assert result.raw_items[0].payload["link"] == "https://example.com/first?utm_source=rss"


def test_payload_has_published_timestamp() -> None:
    with _client(_feed_handler) as c:
        result = _adapter().fetch(c, {}, NOW)
    assert "published_timestamp" in result.raw_items[0].payload


def test_fetched_at_is_run_now() -> None:
    with _client(_feed_handler) as c:
        result = _adapter().fetch(c, {}, NOW)
    assert result.raw_items[0].fetched_at == NOW


def test_new_cursor_from_response_headers() -> None:
    with _client(_feed_handler) as c:
        result = _adapter().fetch(c, {}, NOW)
    assert result.cursor["etag"] == '"v1"'
    assert result.cursor["last_modified"] == "Wed, 27 May 2026 10:00:00 GMT"


def test_conditional_headers_sent_from_cursor() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(304)

    cursor = {"etag": '"v1"', "last_modified": "Wed, 27 May 2026 10:00:00 GMT"}
    with _client(handler) as c:
        _adapter().fetch(c, cursor, NOW)

    assert seen[0].headers["If-None-Match"] == '"v1"'
    assert seen[0].headers["If-Modified-Since"] == "Wed, 27 May 2026 10:00:00 GMT"


def test_304_returns_not_modified_and_keeps_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    cursor = {"etag": '"v1"'}
    with _client(handler) as c:
        result = _adapter().fetch(c, cursor, NOW)

    assert result.not_modified is True
    assert result.raw_items == []
    assert result.cursor == cursor


def test_server_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with _client(handler) as c, pytest.raises(httpx.HTTPStatusError):
        _adapter().fetch(c, {}, NOW)
