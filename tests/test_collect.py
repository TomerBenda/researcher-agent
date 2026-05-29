"""Tests for the collect orchestration (fetch -> normalize -> store)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from researcher_agent.collect import run_collect
from researcher_agent.http import PoliteClient
from researcher_agent.models import RawItem
from researcher_agent.sources.base import FetchResult
from researcher_agent.sources.rss import RssAdapter, RssConfig
from researcher_agent.state import Database

NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)

FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Blog</title>
  <item>
    <title>Post One about CVE-2025-1234</title>
    <link>https://example.com/one?utm_source=rss</link>
    <guid>https://example.com/one</guid>
    <description>Mentions https://github.com/foo/bar</description>
    <pubDate>Wed, 27 May 2026 10:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Post Two</title>
    <link>https://example.com/two</link>
    <guid>two</guid>
    <description>Nothing special</description>
    <pubDate>Tue, 26 May 2026 09:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "state.db")


class FakeAdapter:
    """Adapter that returns a canned result or raises, without any network."""

    def __init__(
        self, name: str, *, result: FetchResult | None = None, exc: Exception | None = None
    ) -> None:
        self._config = RssConfig(name=name, url="https://fake.test/feed")
        self._result = result
        self._exc = exc

    @property
    def config(self) -> RssConfig:
        return self._config

    def fetch(self, client: PoliteClient, cursor: Mapping[str, Any], now: datetime) -> FetchResult:
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


def _raw(link: str | None, *, title: str = "t", published: datetime | None = None) -> RawItem:
    payload: dict[str, Any] = {"title": title}
    if link is not None:
        payload["link"] = link
    if published is not None:
        payload["published_timestamp"] = int(published.timestamp())
    return RawItem(
        source_name="rss:fake",
        source_type="rss",
        external_id=link or "x",
        payload=payload,
        fetched_at=NOW,
    )


def _feed_client(xml: str = FEED_XML, *, etag: str | None = '"v1"') -> PoliteClient:
    headers = {"ETag": etag} if etag else {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=xml.encode(), headers=headers)

    return PoliteClient(transport=httpx.MockTransport(handler), min_interval_seconds=0.0)


# --- end-to-end with the real RSS adapter -------------------------------------


def test_stores_items_sources_entities(db: Database) -> None:
    adapter = RssAdapter(RssConfig(name="rss:example", url="https://example.com/feed.xml"))
    with _feed_client() as client:
        stats = run_collect(db, [adapter], client, now=NOW)

    assert stats.total_new_items == 2
    # utm stripped during normalization
    item = db.get_item(_hash("https://example.com/one"))
    assert item is not None
    assert item.url == "https://example.com/one"
    sources = db.get_item_sources(item.canonical_hash)
    assert [s.source_name for s in sources] == ["rss:example"]
    entities = {(e.kind, e.value) for e in db.get_item_entities(item.canonical_hash)}
    assert ("cve", "CVE-2025-1234") in entities
    assert ("repo", "foo/bar") in entities


def test_rerun_is_idempotent(db: Database) -> None:
    adapter = RssAdapter(RssConfig(name="rss:example", url="https://example.com/feed.xml"))
    with _feed_client() as client:
        run_collect(db, [adapter], client, now=NOW)
    with _feed_client() as client:
        stats = run_collect(db, [adapter], client, now=NOW)
    assert stats.total_new_items == 0  # nothing new the second time


def test_cursor_advances_from_response(db: Database) -> None:
    adapter = RssAdapter(RssConfig(name="rss:example", url="https://example.com/feed.xml"))
    with _feed_client(etag='"abc123"') as client:
        run_collect(db, [adapter], client, now=NOW)
    run = db.get_source_run("rss:example")
    assert run is not None
    assert run.cursor["etag"] == '"abc123"'
    assert run.last_success_at == NOW
    assert run.last_error is None


def test_304_marks_not_modified_and_increments_empty(db: Database) -> None:
    result = FetchResult(raw_items=[], cursor={"etag": '"v1"'}, not_modified=True)
    adapter = FakeAdapter("rss:fake", result=result)
    with _feed_client() as client:
        run_collect(db, [adapter], client, now=NOW)
        run_collect(db, [adapter], client, now=NOW)
    run = db.get_source_run("rss:fake")
    assert run is not None
    assert run.consecutive_empty_runs == 2


# --- resilience ----------------------------------------------------------------


def test_source_error_is_isolated(db: Database) -> None:
    bad = FakeAdapter("rss:bad", exc=RuntimeError("feed exploded"))
    good = RssAdapter(RssConfig(name="rss:example", url="https://example.com/feed.xml"))
    with _feed_client() as client:
        stats = run_collect(db, [bad, good], client, now=NOW)

    by_name = {o.name: o for o in stats.outcomes}
    assert by_name["rss:bad"].error is not None
    assert by_name["rss:example"].error is None
    assert stats.total_new_items == 2  # the good source still stored its items
    run = db.get_source_run("rss:bad")
    assert run is not None
    assert run.last_error is not None


def test_error_increments_error_counter_not_empty(db: Database) -> None:
    bad = FakeAdapter("rss:bad", exc=RuntimeError("boom"))
    with _feed_client() as client:
        run_collect(db, [bad], client, now=NOW)
        run_collect(db, [bad], client, now=NOW)
    run = db.get_source_run("rss:bad")
    assert run is not None
    assert run.consecutive_error_runs == 2
    assert run.consecutive_empty_runs == 0  # an error is not counted as "empty"


def test_success_clears_error_counter(db: Database) -> None:
    flaky_then_ok = [
        FakeAdapter("rss:s", exc=RuntimeError("boom")),
        FakeAdapter(
            "rss:s", result=FetchResult(raw_items=[_raw("https://example.com/x")], cursor={})
        ),
    ]
    with _feed_client() as client:
        run_collect(db, [flaky_then_ok[0]], client, now=NOW)
        assert db.get_source_run("rss:s").consecutive_error_runs == 1  # type: ignore[union-attr]
        run_collect(db, [flaky_then_ok[1]], client, now=NOW)
    run = db.get_source_run("rss:s")
    assert run is not None
    assert run.consecutive_error_runs == 0  # cleared on success


def test_error_redacted_in_production_mode(db: Database) -> None:
    # production mode must drop the message body entirely: DNS/connect/SSL errors
    # embed the bare private host, not just full URLs, so substring stripping is
    # not enough — only the exception class name (+ a correlation hash) survives.
    bad = FakeAdapter(
        "rss:bad", exc=RuntimeError("Name or service not known: feeds.secret-source.example")
    )
    with _feed_client() as client:
        stats = run_collect(db, [bad], client, now=NOW, log_mode="production")
    err = stats.outcomes[0].error
    assert err is not None
    assert "secret-source.example" not in err
    assert "Name or service not known" not in err
    assert "RuntimeError" in err  # the failure category is still visible


def test_production_error_is_stable_for_same_message(db: Database) -> None:
    exc = RuntimeError("connect to host private.example failed")
    a = FakeAdapter("rss:a", exc=exc)
    b = FakeAdapter("rss:b", exc=RuntimeError("connect to host private.example failed"))
    with _feed_client() as client:
        stats = run_collect(db, [a, b], client, now=NOW, log_mode="production")
    # identical messages -> identical redacted form (correlatable across runs)
    assert stats.outcomes[0].error == stats.outcomes[1].error


def test_error_not_redacted_in_dev_mode(db: Database) -> None:
    bad = FakeAdapter("rss:bad", exc=RuntimeError("boom https://example.com/x.xml"))
    with _feed_client() as client:
        stats = run_collect(db, [bad], client, now=NOW, log_mode="dev")
    assert "example.com/x.xml" in (stats.outcomes[0].error or "")


def test_item_without_link_is_skipped_not_fatal(db: Database) -> None:
    result = FetchResult(
        raw_items=[_raw(None, title="no link"), _raw("https://example.com/ok")],
        cursor={},
    )
    adapter = FakeAdapter("rss:fake", result=result)
    with _feed_client() as client:
        stats = run_collect(db, [adapter], client, now=NOW)
    assert stats.total_new_items == 1
    assert stats.outcomes[0].skipped == 1


def test_dispatch_normalizes_non_rss_source(db: Database) -> None:
    # a non-RSS source_type must route to its own normalizer (here: hn_search)
    hn_raw = RawItem(
        source_name="hn:mcp",
        source_type="hn_search",
        external_id="42",
        payload={
            "title": "MCP discussion",
            "link": "https://news.ycombinator.com/item?id=42",
            "published_timestamp": int(NOW.timestamp()),
        },
        fetched_at=NOW,
    )
    adapter = FakeAdapter("hn:mcp", result=FetchResult(raw_items=[hn_raw], cursor={}))
    with _feed_client() as client:
        stats = run_collect(db, [adapter], client, now=NOW)
    assert stats.total_new_items == 1
    items = db.list_recent_items(datetime(2026, 1, 1, tzinfo=UTC))
    assert items[0].url == "https://news.ycombinator.com/item?id=42"


def test_unexpected_normalize_error_is_contained(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # defense in depth: even an unforeseen (non-NormalizeError) failure during
    # normalization must be contained per-item, never crashing the whole run
    import researcher_agent.collect as collect_mod

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("unexpected parser explosion")

    monkeypatch.setitem(collect_mod._NORMALIZERS, "rss", boom)
    result = FetchResult(raw_items=[_raw("https://example.com/x")], cursor={})
    adapter = FakeAdapter("rss:fake", result=result)
    with _feed_client() as client:
        stats = run_collect(db, [adapter], client, now=NOW)
    assert stats.outcomes[0].skipped == 1
    assert stats.outcomes[0].error is None  # the source itself did not fail


# --- window filtering ----------------------------------------------------------


def test_since_filters_old_items(db: Database) -> None:
    old = _raw("https://example.com/old", published=datetime(2026, 1, 1, tzinfo=UTC))
    new = _raw("https://example.com/new", published=datetime(2026, 5, 20, tzinfo=UTC))
    adapter = FakeAdapter("rss:fake", result=FetchResult(raw_items=[old, new], cursor={}))
    with _feed_client() as client:
        stats = run_collect(db, [adapter], client, now=NOW, since=datetime(2026, 5, 1, tzinfo=UTC))
    assert stats.total_new_items == 1
    assert db.get_item(_hash("https://example.com/old")) is None
    assert db.get_item(_hash("https://example.com/new")) is not None


def test_until_filters_future_items(db: Database) -> None:
    inside = _raw("https://example.com/in", published=datetime(2026, 5, 10, tzinfo=UTC))
    future = _raw("https://example.com/out", published=datetime(2026, 6, 10, tzinfo=UTC))
    adapter = FakeAdapter("rss:fake", result=FetchResult(raw_items=[inside, future], cursor={}))
    with _feed_client() as client:
        stats = run_collect(db, [adapter], client, now=NOW, until=datetime(2026, 6, 1, tzinfo=UTC))
    assert stats.total_new_items == 1
    assert db.get_item(_hash("https://example.com/in")) is not None


def test_extra_tracking_params_reach_normalization(db: Database) -> None:
    # config-supplied tracking params must actually be stripped during collect
    raw = _raw("https://example.com/x?sid=1&keep=2")
    adapter = FakeAdapter("rss:fake", result=FetchResult(raw_items=[raw], cursor={}))
    with _feed_client() as client:
        run_collect(db, [adapter], client, now=NOW, extra_tracking_params=["sid"])
    items = db.list_recent_items(datetime(2026, 1, 1, tzinfo=UTC))
    assert len(items) == 1
    assert items[0].url == "https://example.com/x?keep=2"


def test_undated_items_survive_since_filter(db: Database) -> None:
    undated = _raw("https://example.com/undated")  # no published date
    adapter = FakeAdapter("rss:fake", result=FetchResult(raw_items=[undated], cursor={}))
    with _feed_client() as client:
        stats = run_collect(db, [adapter], client, now=NOW, since=datetime(2026, 5, 1, tzinfo=UTC))
    assert stats.total_new_items == 1


def _hash(url: str) -> str:
    from researcher_agent.canonicalize import canonical_hash

    return canonical_hash(url)
