"""Tests for RSS normalization (RawItem -> Item + entities)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from researcher_agent.canonicalize import canonical_hash
from researcher_agent.models import RawItem
from researcher_agent.normalize import (
    NormalizeError,
    normalize_arxiv,
    normalize_hn,
    normalize_rss,
)

NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
PUB = datetime(2026, 5, 27, 10, 0, 0, tzinfo=UTC)


def _raw(payload: dict[str, Any]) -> RawItem:
    return RawItem(
        source_name="rss:example",
        source_type="rss",
        external_id=payload.get("id") or payload.get("link") or "x",
        payload=payload,
        fetched_at=NOW,
    )


def test_normalizes_basic_entry() -> None:
    raw = _raw(
        {
            "title": "  A Post  ",
            "link": "https://example.com/post",
            "summary": "Some summary text.",
            "published_timestamp": int(PUB.timestamp()),
        }
    )
    item, _ = normalize_rss(raw, now=NOW)
    assert item.title == "A Post"
    assert item.summary == "Some summary text."
    assert item.url == "https://example.com/post"
    assert item.ingested_at == NOW
    assert item.published_at == PUB


def test_hash_matches_canonical_hash_of_link() -> None:
    raw = _raw({"title": "t", "link": "https://example.com/post?utm_source=x"})
    item, _ = normalize_rss(raw, now=NOW)
    assert item.canonical_hash == canonical_hash("https://example.com/post?utm_source=x")
    assert len(item.canonical_hash) == 64


def test_url_is_canonicalized() -> None:
    raw = _raw({"title": "t", "link": "HTTPS://Example.com/post/?utm_source=x&id=7"})
    item, _ = normalize_rss(raw, now=NOW)
    assert item.url == "https://example.com/post?id=7"


def test_arxiv_link_canonicalized() -> None:
    raw = _raw({"title": "Paper", "link": "https://arxiv.org/pdf/2305.12345v2.pdf"})
    item, _ = normalize_rss(raw, now=NOW)
    assert item.url == "https://arxiv.org/abs/2305.12345"


def test_missing_link_raises() -> None:
    raw = _raw({"title": "no link here"})
    with pytest.raises(NormalizeError):
        normalize_rss(raw, now=NOW)


def test_missing_title_uses_placeholder() -> None:
    raw = _raw({"link": "https://example.com/x"})
    item, _ = normalize_rss(raw, now=NOW)
    assert item.title == "(untitled)"


def test_no_date_yields_none_published_at() -> None:
    raw = _raw({"title": "t", "link": "https://example.com/x"})
    item, _ = normalize_rss(raw, now=NOW)
    assert item.published_at is None


def test_falls_back_to_updated_timestamp() -> None:
    raw = _raw(
        {"title": "t", "link": "https://example.com/x", "updated_timestamp": int(PUB.timestamp())}
    )
    item, _ = normalize_rss(raw, now=NOW)
    assert item.published_at == PUB


def test_out_of_range_timestamp_is_treated_as_undated() -> None:
    # a feed with a garbage/extreme pubDate must not crash the run
    raw = _raw({"title": "t", "link": "https://example.com/x", "published_timestamp": 10**20})
    item, _ = normalize_rss(raw, now=NOW)
    assert item.published_at is None


def test_bad_published_falls_back_to_good_updated() -> None:
    raw = _raw(
        {
            "title": "t",
            "link": "https://example.com/x",
            "published_timestamp": 10**20,  # bad
            "updated_timestamp": int(PUB.timestamp()),  # good
        }
    )
    item, _ = normalize_rss(raw, now=NOW)
    assert item.published_at == PUB


def test_non_string_link_raises_normalize_error() -> None:
    raw = _raw({"id": "ext-1", "title": "t", "link": 12345})
    with pytest.raises(NormalizeError):
        normalize_rss(raw, now=NOW)


def test_non_string_title_uses_placeholder() -> None:
    raw = _raw({"title": ["weird"], "link": "https://example.com/x"})
    item, _ = normalize_rss(raw, now=NOW)
    assert item.title == "(untitled)"


def test_extracts_entities_from_title_and_summary() -> None:
    raw = _raw(
        {
            "title": "Advisory CVE-2026-1234",
            "link": "https://example.com/post",
            "summary": "Patch at https://github.com/foo/bar and npm:evil-pkg.",
        }
    )
    item, entities = normalize_rss(raw, now=NOW)
    got = {(e.kind, e.value) for e in entities}
    assert got == {
        ("cve", "CVE-2026-1234"),
        ("repo", "foo/bar"),
        ("package", "npm:evil-pkg"),
    }
    assert all(e.canonical_hash == item.canonical_hash for e in entities)


def test_metadata_captures_authors_and_tags() -> None:
    raw = _raw(
        {
            "title": "t",
            "link": "https://example.com/x",
            "authors": ["Alice", "Bob"],
            "tags": ["security", "mcp"],
        }
    )
    item, _ = normalize_rss(raw, now=NOW)
    assert item.metadata["authors"] == ["Alice", "Bob"]
    assert item.metadata["tags"] == ["security", "mcp"]


def test_metadata_records_original_url_when_canonicalization_changed_it() -> None:
    raw = _raw({"title": "t", "link": "https://example.com/x?utm_source=feed"})
    item, _ = normalize_rss(raw, now=NOW)
    assert item.metadata["original_url"] == "https://example.com/x?utm_source=feed"


def test_metadata_omits_original_url_when_unchanged() -> None:
    raw = _raw({"title": "t", "link": "https://example.com/x"})
    item, _ = normalize_rss(raw, now=NOW)
    assert "original_url" not in item.metadata


def test_extra_tracking_params_honored() -> None:
    raw = _raw({"title": "t", "link": "https://example.com/x?sid=1&keep=2"})
    item, _ = normalize_rss(raw, now=NOW, extra_tracking_params=["sid"])
    assert item.url == "https://example.com/x?keep=2"


def test_html_in_summary_does_not_create_bogus_entities() -> None:
    # markup like </div> or class="a/b" must not be mined as repos; the real
    # CVE inside the HTML should still be found
    raw = _raw(
        {
            "title": "t",
            "link": "https://example.com/x",
            "summary": '<div class="post/body"><p>Advisory CVE-2026-7777</p></div>',
        }
    )
    _, entities = normalize_rss(raw, now=NOW)
    kinds_values = {(e.kind, e.value) for e in entities}
    assert ("cve", "CVE-2026-7777") in kinds_values
    assert ("repo", "post/body") not in kinds_values


def test_normalize_arxiv_canonicalizes_to_abs() -> None:
    raw = RawItem(
        source_name="arxiv:x",
        source_type="arxiv",
        external_id="http://arxiv.org/abs/2505.12345v2",
        payload={
            "link": "http://arxiv.org/pdf/2505.12345v2",
            "title": "A Paper on CVE-2025-0001",
            "summary": "Abstract mentioning npm:evil-pkg.",
            "published_timestamp": int(PUB.timestamp()),
            "authors": ["Alice"],
            "tags": ["cs.CR"],
        },
        fetched_at=NOW,
    )
    item, entities = normalize_arxiv(raw, now=NOW)
    assert item.url == "https://arxiv.org/abs/2505.12345"
    assert item.published_at == PUB
    assert item.metadata["authors"] == ["Alice"]
    got = {(e.kind, e.value) for e in entities}
    assert ("cve", "CVE-2025-0001") in got
    assert ("package", "npm:evil-pkg") in got


def test_normalize_hn_uses_link_and_metadata() -> None:
    raw = RawItem(
        source_name="hn:mcp",
        source_type="hn_search",
        external_id="222",
        payload={
            "title": "Ask HN: securing MCP",
            "link": "https://news.ycombinator.com/item?id=222",
            "hn_url": "https://news.ycombinator.com/item?id=222",
            "story_text": "About CVE-2025-7777.",
            "author": "bob",
            "points": 5,
            "published_timestamp": int(PUB.timestamp()),
        },
        fetched_at=NOW,
    )
    item, entities = normalize_hn(raw, now=NOW)
    assert item.url == "https://news.ycombinator.com/item?id=222"
    assert item.metadata["author"] == "bob"
    assert item.metadata["points"] == 5
    assert ("cve", "CVE-2025-7777") in {(e.kind, e.value) for e in entities}


def test_oversized_summary_is_truncated() -> None:
    raw = _raw({"title": "t", "link": "https://example.com/x", "summary": "x" * 50_000})
    item, _ = normalize_rss(raw, now=NOW)
    assert item.summary is not None
    assert len(item.summary) <= 4_000


def test_content_feeds_entity_extraction() -> None:
    raw = _raw(
        {
            "title": "t",
            "link": "https://example.com/x",
            "content": "deep dive into CVE-2026-5555 details",
        }
    )
    _, entities = normalize_rss(raw, now=NOW)
    assert ("cve", "CVE-2026-5555") in {(e.kind, e.value) for e in entities}
