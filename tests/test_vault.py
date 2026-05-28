"""Tests for the vault renderer + atomic writer."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from researcher_agent.models import (
    Classification,
    Followup,
    Item,
    ItemSource,
    WeeklyEntity,
)
from researcher_agent.vault import (
    CollectionEntry,
    SynthesisWindow,
    _atomic_write,
    render_collection_report,
    render_synthesis_report,
    write_collection_report,
    write_synthesis_report,
)
from tests.conftest import assert_matches_snapshot

# --- fixture helpers ----------------------------------------------------------


def _h(seed: str) -> str:
    """Deterministic 64-char hash from a short seed string."""
    base = (seed * 8)[:64]
    return base.ljust(64, "0")[:64]


def _entry(
    *,
    seed: str,
    title: str,
    topic: str,
    score: int,
    secondary: list[str] | None = None,
    sources: list[tuple[str, str]] | None = None,
    published: datetime | None,
    rationale: str = "looks relevant",
) -> CollectionEntry:
    h = _h(seed)
    item = Item(
        canonical_hash=h,
        url=f"https://example.com/{seed}",
        title=title,
        summary=None,
        published_at=published,
        ingested_at=datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC),
    )
    c = Classification(
        canonical_hash=h,
        topic=topic,
        secondary_topics=secondary or [],
        score=score,
        rationale=rationale,
        classifier_version="v1",
        classifier_model="gemini:gemini-2.5-flash",
        classified_at=datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC),
    )
    srcs = [
        ItemSource(
            canonical_hash=h,
            source_name=name,
            source_type=stype,  # type: ignore[arg-type]
            external_id=f"id-{name}",
            first_seen_at=datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC),
        )
        for (name, stype) in (sources or [("rss:default", "rss")])
    ]
    return CollectionEntry(item=item, classification=c, sources=srcs)


# --- collection: empty -------------------------------------------------------


def test_collection_empty_snapshot() -> None:
    out = render_collection_report([], date(2026, 5, 27))
    assert_matches_snapshot(out, "collection_empty.md")


# --- collection: sparse ------------------------------------------------------


def test_collection_sparse_snapshot() -> None:
    entries = [
        _entry(
            seed="a",
            title="MCP server foo released",
            topic="mcp-ecosystem",
            score=6,
            sources=[("gh-releases:foo", "github_releases")],
            published=datetime(2026, 5, 26, 8, 0, 0, tzinfo=UTC),
        ),
        _entry(
            seed="b",
            title="Critical MCP vuln found",
            topic="mcp-security",
            score=9,
            secondary=["mcp-ecosystem", "agent-security"],
            sources=[("rss:embracetheRed", "rss"), ("hn:mcp", "hn_search")],
            published=datetime(2026, 5, 26, 14, 0, 0, tzinfo=UTC),
            rationale="affects all MCP servers using stdio transport",
        ),
        _entry(
            seed="c",
            title="New prompt injection taxonomy",
            topic="prompt-injection",
            score=7,
            sources=[("arxiv:agent-security", "arxiv")],
            published=None,  # missing date
        ),
    ]
    out = render_collection_report(entries, date(2026, 5, 27))
    assert_matches_snapshot(out, "collection_sparse.md")


# --- collection: full --------------------------------------------------------


def test_collection_full_snapshot() -> None:
    entries = [
        _entry(
            seed="m1",
            title="MCP weekly update",
            topic="mcp-ecosystem",
            score=5,
            published=datetime(2026, 5, 26, 8, 0, 0, tzinfo=UTC),
        ),
        _entry(
            seed="m2",
            title="New MCP server: vault",
            topic="mcp-ecosystem",
            score=7,
            published=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        ),
        _entry(
            seed="m3",
            title="MCP transport spec v2",
            topic="mcp-ecosystem",
            score=8,
            secondary=["mcp-security"],
            published=datetime(2026, 5, 26, 16, 0, 0, tzinfo=UTC),
        ),
        _entry(
            seed="m4",
            title="MCP server registry launch",
            topic="mcp-ecosystem",
            score=6,
            published=datetime(2026, 5, 25, 9, 0, 0, tzinfo=UTC),
        ),
        _entry(
            seed="s1",
            title="MCP RCE via crafted manifest",
            topic="mcp-security",
            score=10,
            secondary=["mcp-ecosystem"],
            rationale="affects modelcontextprotocol/servers <1.4.2",
            sources=[("rss:embracetheRed", "rss"), ("hn:mcp", "hn_search")],
            published=datetime(2026, 5, 26, 18, 0, 0, tzinfo=UTC),
        ),
        _entry(
            seed="a1",
            title="Agent jailbreak benchmark v3",
            topic="agent-security",
            score=7,
            published=datetime(2026, 5, 25, 14, 0, 0, tzinfo=UTC),
        ),
        _entry(
            seed="a2",
            title="Multi-agent tool confusion attack",
            topic="agent-security",
            score=8,
            published=datetime(2026, 5, 26, 11, 0, 0, tzinfo=UTC),
        ),
        _entry(
            seed="p1",
            title="Indirect prompt injection in browser agents",
            topic="prompt-injection",
            score=8,
            published=datetime(2026, 5, 26, 10, 0, 0, tzinfo=UTC),
        ),
        _entry(
            seed="p2",
            title="Defense survey: prompt injection",
            topic="prompt-injection",
            score=5,
            published=datetime(2026, 5, 25, 13, 0, 0, tzinfo=UTC),
        ),
        _entry(
            seed="c1",
            title="Claude Code sandbox escape PoC",
            topic="coding-agents",
            score=9,
            published=datetime(2026, 5, 26, 15, 0, 0, tzinfo=UTC),
        ),
        _entry(
            seed="t1", title="Useful: mcp-fuzzer tool", topic="tooling", score=6, published=None
        ),
        _entry(
            seed="o1",
            title="Unrelated AI ethics piece",
            topic="other",
            score=3,
            published=datetime(2026, 5, 25, 8, 0, 0, tzinfo=UTC),
        ),
    ]
    out = render_collection_report(entries, date(2026, 5, 27))
    assert_matches_snapshot(out, "collection_full.md")


# --- collection: idempotency -------------------------------------------------


def test_collection_render_is_idempotent() -> None:
    entries = [
        _entry(
            seed="a",
            title="t",
            topic="mcp-security",
            score=8,
            published=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        ),
        _entry(
            seed="b",
            title="u",
            topic="mcp-security",
            score=8,
            published=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        ),
    ]
    a = render_collection_report(entries, date(2026, 5, 27))
    b = render_collection_report(entries, date(2026, 5, 27))
    assert a == b


# --- collection: write -------------------------------------------------------


def test_write_collection_report_creates_file_and_is_byte_identical_on_rerun(
    tmp_path: Path,
) -> None:
    entries = [
        _entry(
            seed="x",
            title="t",
            topic="mcp-security",
            score=7,
            published=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        ),
    ]
    p = write_collection_report(entries, date(2026, 5, 27), tmp_path)
    assert p.exists()
    assert p.parent.name == "collection"
    assert p.name == "2026-05-27.md"
    first = p.read_bytes()

    p2 = write_collection_report(entries, date(2026, 5, 27), tmp_path)
    assert p == p2
    assert p.read_bytes() == first


# --- atomic write ------------------------------------------------------------


def test_atomic_write_leaves_no_tempfiles_on_success(tmp_path: Path) -> None:
    path = tmp_path / "out.md"
    _atomic_write(path, "hello\n")
    assert path.read_text() == "hello\n"
    leftover = [p.name for p in tmp_path.iterdir() if p.name != "out.md"]
    assert leftover == []


def test_atomic_write_uses_lf_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "out.md"
    _atomic_write(path, "line1\nline2\n")
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert raw == b"line1\nline2\n"


def test_atomic_write_cleans_up_tempfile_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "out.md"

    real_replace = os.replace

    def boom(*args: object, **kw: object) -> None:
        raise OSError("simulated")

    monkeypatch.setattr("researcher_agent.vault.os.replace", boom)
    with pytest.raises(OSError):
        _atomic_write(path, "data")
    monkeypatch.setattr("researcher_agent.vault.os.replace", real_replace)

    leftover = [p.name for p in tmp_path.iterdir()]
    assert leftover == []
    assert not path.exists()


# --- SynthesisWindow ---------------------------------------------------------


def test_synthesis_window_iso_week_label() -> None:
    monday = datetime(2026, 5, 25, 0, 0, 0, tzinfo=UTC)
    w = SynthesisWindow.iso_week(monday)
    assert w.label == "W2026-W22"
    assert w.start == monday
    assert (w.end - w.start).days == 7


def test_synthesis_window_trailing_days_label() -> None:
    end = datetime(2026, 5, 27, 0, 0, 0, tzinfo=UTC)
    w = SynthesisWindow.trailing_days(end, 30)
    assert w.label == "D2026-05-27-30d"
    assert (w.end - w.start).days == 30


def test_synthesis_window_date_range_label() -> None:
    start = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)
    end = datetime(2026, 5, 27, 0, 0, 0, tzinfo=UTC)
    w = SynthesisWindow.date_range(start, end)
    assert w.label == "R2026-05-01-to-2026-05-27"


# --- synthesis: empty --------------------------------------------------------


def test_synthesis_empty_snapshot() -> None:
    w = SynthesisWindow.iso_week(datetime(2026, 5, 25, 0, 0, 0, tzinfo=UTC))
    out = render_synthesis_report(w, agent_body="", entities=[], followups=[])
    assert_matches_snapshot(out, "synthesis_empty.md")


# --- synthesis: full ---------------------------------------------------------


def test_synthesis_full_snapshot() -> None:
    monday = datetime(2026, 5, 25, 0, 0, 0, tzinfo=UTC)
    w = SynthesisWindow.iso_week(monday)
    body = (
        "## Themes\n\n"
        "Two themes dominated this window:\n\n"
        "1. **MCP supply chain.** Multiple advisories about MCP server packaging.\n"
        "2. **Browser agent attacks.** A new family of indirect prompt injections.\n"
    )
    entities = [
        WeeklyEntity(
            week_starting=monday,
            kind="cve",
            value="CVE-2026-12345",
            context="MCP server RCE via crafted manifest",
            related_item_hashes=[_h("s1")],
        ),
        WeeklyEntity(
            week_starting=monday,
            kind="cve",
            value="CVE-2026-99999",
            context="Browser agent sandbox escape",
        ),
        WeeklyEntity(
            week_starting=monday,
            kind="repo",
            value="modelcontextprotocol/servers",
            context="Affected by CVE-2026-12345",
        ),
        WeeklyEntity(
            week_starting=monday,
            kind="repo",
            value="anthropic/claude-code",
            context="Patched in 0.42.1",
        ),
        WeeklyEntity(
            week_starting=monday,
            kind="person",
            value="Simon Willison",
            context="Published a taxonomy of browser-agent attacks",
        ),
        WeeklyEntity(
            week_starting=monday,
            kind="technique",
            value="indirect prompt injection",
            context="Re-popularized as the dominant agent attack vector",
        ),
    ]
    followups = [
        Followup(
            created_at=datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC),
            item_hash=_h("s1"),
            item_title_snapshot="MCP RCE via crafted manifest",
            item_url_snapshot="https://example.com/s1",
            action="read-deep",
            note="full analysis + patch review",
        ),
        Followup(
            created_at=datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC),
            item_hash=_h("c1"),
            item_title_snapshot="Claude Code sandbox escape PoC",
            item_url_snapshot="https://example.com/c1",
            action="audit",
            note="check our own MCP server",
        ),
        Followup(
            created_at=datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC),
            item_hash=_h("p1"),
            item_title_snapshot="Indirect prompt injection in browser agents",
            item_url_snapshot="https://example.com/p1",
            action="track",
            note="",
        ),
        # completed — must not render
        Followup(
            created_at=datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC),
            item_hash=None,
            item_title_snapshot="Done item",
            item_url_snapshot="https://example.com/done",
            action="read-deep",
            completed=True,
        ),
    ]
    out = render_synthesis_report(w, agent_body=body, entities=entities, followups=followups)
    assert_matches_snapshot(out, "synthesis_full.md")


# --- synthesis: write --------------------------------------------------------


def test_write_synthesis_report(tmp_path: Path) -> None:
    w = SynthesisWindow.iso_week(datetime(2026, 5, 25, 0, 0, 0, tzinfo=UTC))
    p = write_synthesis_report(w, "body\n", [], [], tmp_path)
    assert p.exists()
    assert p.parent.name == "synthesis"
    assert p.name == "W2026-W22.md"


def test_write_synthesis_report_ad_hoc_window(tmp_path: Path) -> None:
    w = SynthesisWindow.trailing_days(datetime(2026, 5, 27, 0, 0, 0, tzinfo=UTC), 30)
    p = write_synthesis_report(w, "body\n", [], [], tmp_path)
    assert p.name == "D2026-05-27-30d.md"
