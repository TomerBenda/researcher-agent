"""Tests for the M3 post-processing steps: classify, dedupe, vault render."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from researcher_agent.collect import (
    classify_pending,
    dedupe_recent,
    post_process,
    render_collection_to_vault,
)
from researcher_agent.config import AgentConfig, DedupConfig, Taxonomy, TopicConfig
from researcher_agent.llm.base import ClassifierInput, RawClassification
from researcher_agent.models import Item, ItemEntity, ItemSource
from researcher_agent.state import Database

NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)

TAXONOMY = Taxonomy(
    topics=[
        TopicConfig(slug="mcp-security", description="mcp vulns"),
        TopicConfig(slug="tooling", description="tools"),
        TopicConfig(slug="other", description="misc"),
    ]
)


class FakeProvider:
    model_id = "fake:test"

    def __init__(self, topic_by_title: dict[str, str] | None = None) -> None:
        self.topic_by_title = topic_by_title or {}
        self.seen_inputs: list[ClassifierInput] = []

    def classify(
        self, system_prompt: str, inputs: Sequence[ClassifierInput], *, temperature: float
    ) -> dict[str, RawClassification]:
        self.seen_inputs.extend(inputs)
        out: dict[str, RawClassification] = {}
        for i in inputs:
            topic = self.topic_by_title.get(i.title, "tooling")
            out[i.id] = RawClassification(topic=topic, score=7, rationale="r")
        return out


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "state.db")


def _add_item(
    db: Database,
    h: str,
    title: str,
    *,
    source: str = "rss:a",
    ingested: datetime = NOW,
    published: datetime | None = NOW,
    entities: list[tuple[str, str]] | None = None,
) -> str:
    canonical = h * 64 if len(h) == 1 else h
    db.insert_item(
        Item(
            canonical_hash=canonical,
            url=f"https://example.com/{canonical[:8]}",
            title=title,
            published_at=published,
            ingested_at=ingested,
        )
    )
    db.add_item_source(
        ItemSource(
            canonical_hash=canonical,
            source_name=source,
            source_type="rss",
            external_id=canonical,
            first_seen_at=ingested,
        )
    )
    for kind, value in entities or []:
        db.add_item_entities([ItemEntity(canonical_hash=canonical, kind=kind, value=value)])  # type: ignore[arg-type]
    return canonical


def _classify(db: Database, provider: FakeProvider) -> object:
    return classify_pending(
        db,
        provider=provider,
        taxonomy=TAXONOMY,
        system_prompt="SYS",
        classifier_version="cls-test",
        classifier_model=provider.model_id,
        now=NOW,
        batch_size=10,
        token_budget=None,
    )


# --- classify_pending ----------------------------------------------------------


def test_classify_pending_labels_unclassified(db: Database) -> None:
    h = _add_item(db, "a", "MCP server flaw")
    _classify(db, FakeProvider({"MCP server flaw": "mcp-security"}))
    active = db.get_active_classification(h)
    assert active is not None
    assert active.topic == "mcp-security"
    assert active.classifier_version == "cls-test"


def test_classify_pending_passes_source(db: Database) -> None:
    _add_item(db, "a", "Post", source="rss:simon")
    provider = FakeProvider()
    _classify(db, provider)
    assert provider.seen_inputs[0].source == "rss:simon"


def test_classify_pending_skips_already_classified(db: Database) -> None:
    _add_item(db, "a", "Post")
    provider = FakeProvider()
    _classify(db, provider)
    provider.seen_inputs.clear()
    _classify(db, provider)  # nothing left unclassified
    assert provider.seen_inputs == []


def test_classify_pending_reports_counts(db: Database) -> None:
    _add_item(db, "a", "One")
    _add_item(db, "b", "Two")
    stats = _classify(db, FakeProvider())
    assert stats.classified == 2  # type: ignore[attr-defined]


# --- dedupe_recent -------------------------------------------------------------


def test_dedupe_supersedes_lower_scored_crosspost(db: Database) -> None:
    a = _add_item(db, "a", "Same Headline", source="rss:a")
    b = _add_item(db, "b", "Same Headline", source="rss:b")
    # give them different scores via classification
    from researcher_agent.models import Classification

    db.classify_and_activate(
        Classification(
            canonical_hash=a,
            topic="tooling",
            score=3,
            rationale="r",
            classifier_version="v",
            classifier_model="m",
            classified_at=NOW,
        )
    )
    db.classify_and_activate(
        Classification(
            canonical_hash=b,
            topic="tooling",
            score=9,
            rationale="r",
            classifier_version="v",
            classifier_model="m",
            classified_at=NOW,
        )
    )
    pairs = dedupe_recent(db, since=NOW - timedelta(days=7), config=DedupConfig(), now=NOW)
    assert pairs == 1
    superseded = db.conn.execute(
        "SELECT superseded_by FROM items WHERE canonical_hash = ?", (a,)
    ).fetchone()["superseded_by"]
    assert superseded == b
    # winner inherits the loser's source
    assert {s.source_name for s in db.get_item_sources(b)} == {"rss:a", "rss:b"}


def test_dedupe_leaves_distinct_items(db: Database) -> None:
    _add_item(db, "a", "Totally different headline one")
    _add_item(db, "b", "An unrelated second article entirely")
    pairs = dedupe_recent(db, since=NOW - timedelta(days=7), config=DedupConfig(), now=NOW)
    assert pairs == 0


# --- render_collection_to_vault ------------------------------------------------


def test_render_writes_report_with_classified_items(db: Database, tmp_path: Path) -> None:
    _add_item(db, "a", "MCP server flaw")
    _classify(db, FakeProvider({"MCP server flaw": "mcp-security"}))
    vault = tmp_path / "vault"
    path = render_collection_to_vault(db, vault_root=vault, report_date=NOW.date())
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "mcp-security" in text
    assert "MCP server flaw" in text
    assert path == vault / "collection" / "2026-05-28.md"


def test_render_excludes_unclassified_and_superseded(db: Database, tmp_path: Path) -> None:
    # classify_pending labels ALL pending items, so to keep one unclassified we
    # add it only after the final classify call.
    classified = _add_item(db, "a", "Classified item")
    _classify(db, FakeProvider({"Classified item": "tooling"}))

    gone = _add_item(db, "c", "Superseded item")
    _classify(db, FakeProvider({"Superseded item": "tooling"}))
    db.mark_superseded(gone, classified)

    _add_item(db, "b", "Unclassified item")  # added last; never classified

    path = render_collection_to_vault(db, vault_root=tmp_path / "v", report_date=NOW.date())
    text = path.read_text(encoding="utf-8")
    assert "Classified item" in text
    assert "Unclassified item" not in text
    assert "Superseded item" not in text


def test_render_returns_path_even_when_empty(db: Database, tmp_path: Path) -> None:
    path = render_collection_to_vault(db, vault_root=tmp_path / "v", report_date=NOW.date())
    assert path.exists()
    assert "No items collected" in path.read_text(encoding="utf-8")


# --- post_process (the CLI seam) ----------------------------------------------


def _config(vault_path: str | None = None) -> AgentConfig:
    return AgentConfig(taxonomy=TAXONOMY, vault_path=vault_path)


def test_post_process_runs_full_stage(db: Database, tmp_path: Path) -> None:
    _add_item(db, "a", "MCP server flaw")
    provider = FakeProvider({"MCP server flaw": "mcp-security"})
    stats = post_process(db, _config(), provider, now=NOW, vault_override=tmp_path / "vault")
    assert stats.classify is not None
    assert stats.classify.classified == 1
    assert stats.report_path is not None
    assert stats.report_path.exists()
    assert db.get_active_classification("a" * 64) is not None


def test_post_process_skips_when_no_provider(db: Database, tmp_path: Path) -> None:
    _add_item(db, "a", "Some item")
    stats = post_process(db, _config(), None, now=NOW, vault_override=tmp_path / "vault")
    assert stats.classify is None
    assert stats.report_path is None
    assert db.get_active_classification("a" * 64) is None  # left unclassified


def test_post_process_no_vault_when_unset(db: Database) -> None:
    _add_item(db, "a", "Item")
    stats = post_process(db, _config(vault_path=None), FakeProvider(), now=NOW)
    assert stats.report_path is None
    assert stats.classify is not None  # classification still happened


def test_post_process_respects_item_cap(db: Database) -> None:
    from researcher_agent.config import ClassifierConfig

    _add_item(db, "a", "One", ingested=NOW - timedelta(minutes=2))
    _add_item(db, "b", "Two", ingested=NOW - timedelta(minutes=1))
    cfg = AgentConfig(taxonomy=TAXONOMY, classifier=ClassifierConfig(max_items_per_run=1))
    stats = post_process(db, cfg, FakeProvider(), now=NOW)
    assert stats.classify is not None
    assert stats.classify.classified == 1  # cold backlog drains one per run


def test_dedupe_skips_oversized_pool(db: Database) -> None:
    a = _add_item(db, "a", "Same Headline")
    _add_item(db, "b", "Same Headline")
    pairs = dedupe_recent(
        db, since=NOW - timedelta(days=7), config=DedupConfig(), now=NOW, max_pool=1
    )
    assert pairs == 0  # pool (2) > max_pool (1) -> dedupe skipped, nothing superseded
    assert db.get_item(a) is not None
