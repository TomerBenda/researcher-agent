"""Tests for the synthesis agent's tools.

Tools must return structured {ok, ...} results and never raise into the loop.
Driven by a temp DB and httpx.MockTransport — no network, no real LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from researcher_agent.http import PoliteClient
from researcher_agent.models import Classification, Item, ItemEntity
from researcher_agent.state import Database
from researcher_agent.synthesis.tools import SynthesisTools, _cache_key, tool_specs
from researcher_agent.vault import SynthesisWindow

WSTART = datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)  # a Monday
WINDOW = SynthesisWindow.iso_week(WSTART)
INGEST = WSTART + timedelta(days=1)  # inside the window
NOW = WSTART + timedelta(days=7)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "state.db")


def _add(
    db: Database,
    h: str,
    title: str,
    *,
    score: int = 7,
    topic: str = "mcp-security",
    secondary: list[str] | None = None,
    ingested: datetime = INGEST,
    url: str | None = None,
    entities: list[tuple[str, str]] | None = None,
) -> str:
    canonical = h * 64 if len(h) == 1 else h
    db.insert_item(
        Item(
            canonical_hash=canonical,
            url=url or f"https://example.com/{canonical[:8]}",
            title=title,
            summary="a summary",
            published_at=ingested,
            ingested_at=ingested,
        )
    )
    db.classify_and_activate(
        Classification(
            canonical_hash=canonical,
            topic=topic,
            secondary_topics=secondary or [],
            score=score,
            rationale="r",
            classifier_version="v",
            classifier_model="m",
            classified_at=ingested,
        )
    )
    for kind, value in entities or []:
        db.add_item_entities([ItemEntity(canonical_hash=canonical, kind=kind, value=value)])  # type: ignore[arg-type]
    return canonical


def _tools(db: Database, handler=None, **kw) -> SynthesisTools:  # type: ignore[no-untyped-def]
    handler = handler or (lambda request: httpx.Response(200, text="x"))
    client = PoliteClient(transport=httpx.MockTransport(handler), min_interval_seconds=0.0)
    return SynthesisTools(db, client, window=WINDOW, now=NOW, **kw)


# --- query_items ---------------------------------------------------------------


def test_query_items_returns_window_items_sorted_by_score(db: Database) -> None:
    _add(db, "a", "Low", score=5)
    _add(db, "b", "High", score=9)
    out = _tools(db).query_items()
    assert out["ok"] is True
    items = out["result"]["items"]
    assert [i["title"] for i in items] == ["High", "Low"]
    assert out["result"]["count"] == 2


def test_query_items_applies_min_score_floor(db: Database) -> None:
    _add(db, "a", "Below", score=4)
    _add(db, "b", "Above", score=6)
    out = _tools(db, min_score=5).query_items()  # default floor 5
    assert [i["title"] for i in out["result"]["items"]] == ["Above"]


def test_query_items_explicit_min_score_overrides_default(db: Database) -> None:
    _add(db, "a", "Two", score=2)
    out = _tools(db, min_score=5).query_items(min_score=0)
    assert [i["title"] for i in out["result"]["items"]] == ["Two"]


def test_query_items_filters_by_topic_including_secondary(db: Database) -> None:
    _add(db, "a", "Primary", topic="mcp-security")
    _add(db, "b", "Secondary", topic="tooling", secondary=["mcp-security"])
    _add(db, "c", "Other", topic="tooling")
    titles = {i["title"] for i in _tools(db).query_items(topic="mcp-security")["result"]["items"]}
    assert titles == {"Primary", "Secondary"}


def test_query_items_excludes_items_outside_the_window(db: Database) -> None:
    _add(db, "a", "Inside", ingested=INGEST)
    _add(db, "b", "Before", ingested=WSTART - timedelta(days=1))
    _add(db, "c", "After", ingested=WINDOW.end + timedelta(hours=1))
    titles = [i["title"] for i in _tools(db).query_items()["result"]["items"]]
    assert titles == ["Inside"]


# --- get_entities --------------------------------------------------------------


def test_get_entities_returns_extracted_entities(db: Database) -> None:
    h = _add(db, "a", "Flaw", entities=[("cve", "CVE-2025-1"), ("repo", "owner/proj")])
    out = _tools(db).get_entities(h)
    kinds = {(e["kind"], e["value"]) for e in out["result"]["entities"]}
    assert kinds == {("cve", "CVE-2025-1"), ("repo", "owner/proj")}


def test_get_entities_unknown_item_is_an_error(db: Database) -> None:
    out = _tools(db).get_entities("z" * 64)
    assert out["ok"] is False
    assert "no item" in out["error"]


# --- fetch_url -----------------------------------------------------------------


def test_fetch_url_strips_html_and_caches(db: Database) -> None:
    # The body cache is FK-bound to items, so caching applies when the URL maps to
    # a stored item (an item whose canonical_hash == hash of its canonical URL).
    url = "https://example.com/post"
    item_hash = _cache_key(url)
    db.insert_item(Item(canonical_hash=item_hash, url=url, title="Post", ingested_at=INGEST))

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            text="<html><head><style>x{}</style></head><body><p>Hello &amp; world</p></body></html>",
        )

    tools = _tools(db, handler)
    first = tools.fetch_url(url)
    assert first["ok"] is True
    assert first["result"]["cached"] is False
    assert first["result"]["text"] == "Hello & world"  # tags/style dropped, entity unescaped

    second = tools.fetch_url(url)
    assert second["result"]["cached"] is True
    assert calls == 1  # served from the body cache, no second HTTP call


def test_fetch_url_external_url_not_cached(db: Database) -> None:
    # A URL with no matching item can't be cached (item_bodies FK) — it just
    # returns the text and re-fetches next time.
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="<p>external</p>")

    tools = _tools(db, handler)
    assert tools.fetch_url("https://elsewhere.test/x")["result"]["text"] == "external"
    assert tools.fetch_url("https://elsewhere.test/x")["result"]["cached"] is False
    assert calls == 2


def test_fetch_url_http_error_is_returned_not_raised(db: Database) -> None:
    out = _tools(db, lambda request: httpx.Response(500)).fetch_url("https://example.com/x")
    assert out["ok"] is False
    assert "fetch failed" in out["error"]


def test_fetch_url_rejects_empty_url(db: Database) -> None:
    assert _tools(db).fetch_url("")["ok"] is False


# --- add_followup --------------------------------------------------------------


def test_add_followup_queues_with_snapshot(db: Database) -> None:
    h = _add(db, "a", "Audit me", url="https://example.com/audit")
    tools = _tools(db)
    out = tools.add_followup(h, "audit", "check the supply chain")
    assert out["ok"] is True
    assert tools.followups_added == 1
    fups = db.list_followups(open_only=True)
    assert len(fups) == 1
    assert fups[0].action == "audit"
    assert fups[0].item_title_snapshot == "Audit me"
    assert fups[0].item_url_snapshot == "https://example.com/audit"


def test_add_followup_rejects_bad_action(db: Database) -> None:
    h = _add(db, "a", "x")
    out = _tools(db).add_followup(h, "delete-everything", "")
    assert out["ok"] is False
    assert db.list_followups() == []


def test_add_followup_rejects_unknown_item(db: Database) -> None:
    out = _tools(db).add_followup("z" * 64, "track", "")
    assert out["ok"] is False


# --- finish --------------------------------------------------------------------


def test_finish_records_roundup(db: Database) -> None:
    tools = _tools(db)
    out = tools.finish("# Roundup\n\nthemes...")
    assert out["ok"] is True
    assert tools.finished_roundup == "# Roundup\n\nthemes..."


def test_finish_rejects_empty(db: Database) -> None:
    tools = _tools(db)
    assert tools.finish("   ")["ok"] is False
    assert tools.finished_roundup is None


# --- dispatch ------------------------------------------------------------------


def test_dispatch_routes_by_name(db: Database) -> None:
    h = _add(db, "a", "x")
    tools = _tools(db)
    assert tools.dispatch("get_entities", {"item_hash": h})["ok"] is True
    assert tools.dispatch("finish", {"roundup_markdown": "done"})["ok"] is True
    assert tools.finished_roundup == "done"


def test_dispatch_unknown_tool_is_an_error(db: Database) -> None:
    out = _tools(db).dispatch("rm_rf", {})
    assert out["ok"] is False
    assert "unknown tool" in out["error"]


def test_dispatch_non_dict_args_is_an_error(db: Database) -> None:
    assert _tools(db).dispatch("query_items", ["not", "a", "dict"])["ok"] is False  # type: ignore[arg-type]


def test_tool_specs_cover_all_tools() -> None:
    names = {s.name for s in tool_specs()}
    assert names == {"query_items", "fetch_url", "get_entities", "add_followup", "finish"}
    for spec in tool_specs():
        assert spec.input_schema["type"] == "object"
