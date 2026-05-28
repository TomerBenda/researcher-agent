"""Tests for the SQLite storage layer."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from researcher_agent.models import (
    Classification,
    Followup,
    Item,
    ItemBody,
    ItemEntity,
    ItemSource,
    SourceRun,
    WeeklyEntity,
)
from researcher_agent.state import Database

HASH = "a" * 64
HASH2 = "b" * 64


def _now() -> datetime:
    return datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)


def _later() -> datetime:
    return datetime(2026, 5, 27, 13, 0, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "state.db")


def _make_item(h: str = HASH, title: str = "hello") -> Item:
    return Item(
        canonical_hash=h,
        url=f"https://example.com/{h[:8]}",
        title=title,
        summary="s",
        published_at=_now(),
        ingested_at=_now(),
        metadata={"foo": "bar"},
    )


def _make_classification(
    h: str = HASH, version: str = "v1", topic: str = "mcp-security"
) -> Classification:
    return Classification(
        canonical_hash=h,
        topic=topic,
        secondary_topics=["agent-security"],
        score=7,
        rationale="looks important",
        classifier_version=version,
        classifier_model="gemini:gemini-2.5-flash",
        classified_at=_now(),
    )


# --- migrations / connection --------------------------------------------------


def test_migrations_apply_to_empty_db(tmp_path: Path) -> None:
    Database(tmp_path / "x.db")
    # second open should be no-op
    db = Database(tmp_path / "x.db")
    rows = db.conn.execute("SELECT filename FROM schema_migrations").fetchall()
    assert len(rows) == 1
    assert rows[0]["filename"] == "001_initial.sql"


def test_wal_mode_and_fk_enabled(db: Database) -> None:
    mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    fk = db.conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1


# --- items --------------------------------------------------------------------


def test_insert_item_is_idempotent(db: Database) -> None:
    item = _make_item()
    assert db.insert_item(item) is True
    assert db.insert_item(item) is False  # already exists, no-op


def test_get_item_roundtrip(db: Database) -> None:
    item = _make_item()
    db.insert_item(item)
    fetched = db.get_item(HASH)
    assert fetched is not None
    assert fetched.canonical_hash == HASH
    assert fetched.title == "hello"
    assert fetched.metadata == {"foo": "bar"}
    assert fetched.published_at == _now()


def test_get_item_returns_none_for_missing(db: Database) -> None:
    assert db.get_item(HASH) is None


def test_mark_superseded(db: Database) -> None:
    db.insert_item(_make_item(HASH, "loser"))
    db.insert_item(_make_item(HASH2, "winner"))
    db.mark_superseded(HASH, HASH2)
    row = db.conn.execute(
        "SELECT superseded_by FROM items WHERE canonical_hash = ?", (HASH,)
    ).fetchone()
    assert row["superseded_by"] == HASH2


# --- bodies -------------------------------------------------------------------


def test_set_item_body_requires_item(db: Database) -> None:
    body = ItemBody(canonical_hash=HASH, body="text", fetched_at=_now())
    with pytest.raises(sqlite3.IntegrityError):
        db.set_item_body(body)


def test_set_and_get_item_body(db: Database) -> None:
    db.insert_item(_make_item())
    body = ItemBody(canonical_hash=HASH, body="text", fetched_at=_now())
    db.set_item_body(body)
    fetched = db.get_item_body(HASH)
    assert fetched is not None
    assert fetched.body == "text"


def test_set_item_body_upserts(db: Database) -> None:
    db.insert_item(_make_item())
    db.set_item_body(ItemBody(canonical_hash=HASH, body="v1", fetched_at=_now()))
    db.set_item_body(ItemBody(canonical_hash=HASH, body="v2", fetched_at=_later()))
    fetched = db.get_item_body(HASH)
    assert fetched is not None
    assert fetched.body == "v2"


# --- sources ------------------------------------------------------------------


def test_add_item_source_idempotent(db: Database) -> None:
    db.insert_item(_make_item())
    src = ItemSource(
        canonical_hash=HASH,
        source_name="rss:a",
        source_type="rss",
        external_id="g1",
        first_seen_at=_now(),
    )
    assert db.add_item_source(src) is True
    assert db.add_item_source(src) is False  # PK conflict, ignored


def test_add_multiple_sources_for_one_item(db: Database) -> None:
    db.insert_item(_make_item())
    for name in ("rss:a", "rss:b", "hn:1"):
        src_type = "hn_search" if name.startswith("hn") else "rss"
        db.add_item_source(
            ItemSource(
                canonical_hash=HASH,
                source_name=name,
                source_type=src_type,
                external_id="g",
                first_seen_at=_now(),
            )
        )
    sources = db.get_item_sources(HASH)
    assert {s.source_name for s in sources} == {"rss:a", "rss:b", "hn:1"}


# --- entities -----------------------------------------------------------------


def test_add_item_entities_idempotent(db: Database) -> None:
    db.insert_item(_make_item())
    es = [
        ItemEntity(canonical_hash=HASH, kind="cve", value="CVE-2026-1"),
        ItemEntity(canonical_hash=HASH, kind="repo", value="anthropic/x"),
    ]
    assert db.add_item_entities(es) == 2
    assert db.add_item_entities(es) == 0  # duplicates filtered
    fetched = db.get_item_entities(HASH)
    assert len(fetched) == 2
    assert {e.value for e in fetched} == {"CVE-2026-1", "anthropic/x"}


def test_add_item_entities_empty(db: Database) -> None:
    assert db.add_item_entities([]) == 0


# --- classifications ----------------------------------------------------------


def test_classify_and_activate(db: Database) -> None:
    db.insert_item(_make_item())
    new_id = db.classify_and_activate(_make_classification())
    active = db.get_active_classification(HASH)
    assert active is not None
    assert active.topic == "mcp-security"
    assert active.score == 7
    # items row should point at this classification
    row = db.conn.execute(
        "SELECT current_classification_id FROM items WHERE canonical_hash = ?", (HASH,)
    ).fetchone()
    assert row["current_classification_id"] == new_id


def test_reclassification_appends_history(db: Database) -> None:
    db.insert_item(_make_item())
    db.classify_and_activate(_make_classification(version="v1", topic="other"))
    db.classify_and_activate(_make_classification(version="v2", topic="mcp-security"))
    history = db.get_classification_history(HASH)
    assert len(history) == 2
    assert [c.classifier_version for c in history] == ["v1", "v2"]
    # active should be v2
    active = db.get_active_classification(HASH)
    assert active is not None
    assert active.classifier_version == "v2"
    assert active.topic == "mcp-security"


def test_no_active_classification_returns_none(db: Database) -> None:
    db.insert_item(_make_item())
    assert db.get_active_classification(HASH) is None


def test_classification_requires_item(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.append_classification(_make_classification())


# --- source runs --------------------------------------------------------------


def test_source_run_upsert(db: Database) -> None:
    db.upsert_source_run(
        SourceRun(
            source_name="rss:a",
            cursor={"last_id": "1"},
            last_run_at=_now(),
            last_success_at=_now(),
        )
    )
    fetched = db.get_source_run("rss:a")
    assert fetched is not None
    assert fetched.cursor == {"last_id": "1"}

    # update
    db.upsert_source_run(
        SourceRun(
            source_name="rss:a",
            cursor={"last_id": "2"},
            last_run_at=_later(),
            consecutive_empty_runs=1,
        )
    )
    fetched = db.get_source_run("rss:a")
    assert fetched is not None
    assert fetched.cursor == {"last_id": "2"}
    assert fetched.consecutive_empty_runs == 1


# --- followups ----------------------------------------------------------------


def test_followup_insert_list_mark_done(db: Database) -> None:
    db.insert_item(_make_item())
    fid = db.insert_followup(
        Followup(
            created_at=_now(),
            item_hash=HASH,
            item_title_snapshot="hello",
            item_url_snapshot="https://example.com",
            action="read-deep",
            note="urgent",
        )
    )
    assert isinstance(fid, int) and fid > 0

    open_ = db.list_followups(open_only=True)
    assert len(open_) == 1
    assert open_[0].note == "urgent"

    db.mark_followup_done(fid)
    assert db.list_followups(open_only=True) == []
    assert len(db.list_followups(open_only=False)) == 1


def test_followup_survives_item_delete(db: Database) -> None:
    db.insert_item(_make_item())
    db.insert_followup(
        Followup(
            created_at=_now(),
            item_hash=HASH,
            item_title_snapshot="hello",
            item_url_snapshot="https://example.com",
            action="read-deep",
        )
    )
    db.conn.execute("DELETE FROM items WHERE canonical_hash = ?", (HASH,))
    fups = db.list_followups(open_only=False)
    assert len(fups) == 1
    assert fups[0].item_hash is None  # FK ON DELETE SET NULL
    assert fups[0].item_title_snapshot == "hello"  # snapshot survives


# --- weekly entities ----------------------------------------------------------


def test_insert_weekly_entities(db: Database) -> None:
    monday = datetime(2026, 5, 25, 0, 0, 0, tzinfo=UTC)
    es = [
        WeeklyEntity(
            week_starting=monday,
            kind="cve",
            value="CVE-2026-1",
            context="x",
            related_item_hashes=[HASH],
        ),
        WeeklyEntity(week_starting=monday, kind="repo", value="anthropic/x", context="y"),
    ]
    assert db.insert_weekly_entities(es) == 2

    rows = db.conn.execute("SELECT * FROM weekly_entities ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0]["value"] == "CVE-2026-1"


# --- cascading deletes --------------------------------------------------------


def test_deleting_item_cascades_to_bodies_and_sources(db: Database) -> None:
    db.insert_item(_make_item())
    db.set_item_body(ItemBody(canonical_hash=HASH, body="t", fetched_at=_now()))
    db.add_item_source(
        ItemSource(
            canonical_hash=HASH,
            source_name="rss:a",
            source_type="rss",
            external_id="g",
            first_seen_at=_now(),
        )
    )
    db.add_item_entities([ItemEntity(canonical_hash=HASH, kind="cve", value="X")])
    db.classify_and_activate(_make_classification())

    db.conn.execute("DELETE FROM items WHERE canonical_hash = ?", (HASH,))

    assert db.get_item_body(HASH) is None
    assert db.get_item_sources(HASH) == []
    assert db.get_item_entities(HASH) == []
    assert db.get_classification_history(HASH) == []


# --- list_unclassified / list_recent_items ------------------------------------


def _item_at(h: str, ingested: datetime) -> Item:
    return Item(
        canonical_hash=h,
        url=f"https://example.com/{h[:8]}",
        title="t",
        ingested_at=ingested,
    )


def test_list_unclassified_returns_only_unclassified(db: Database) -> None:
    db.insert_item(_make_item(HASH))
    db.insert_item(_make_item(HASH2))
    db.classify_and_activate(_make_classification(HASH))
    unclassified = db.list_unclassified()
    assert [i.canonical_hash for i in unclassified] == [HASH2]


def test_list_unclassified_excludes_superseded(db: Database) -> None:
    db.insert_item(_make_item(HASH))
    db.insert_item(_make_item(HASH2))
    db.mark_superseded(HASH, HASH2)
    hashes = {i.canonical_hash for i in db.list_unclassified()}
    assert HASH not in hashes
    assert HASH2 in hashes


def test_list_unclassified_respects_limit(db: Database) -> None:
    for i in range(5):
        db.insert_item(_item_at(f"{i}" * 64, _now() + timedelta(minutes=i)))
    assert len(db.list_unclassified(limit=3)) == 3


def test_list_recent_items_filters_by_ingested(db: Database) -> None:
    old = _item_at("a" * 64, datetime(2026, 5, 1, tzinfo=UTC))
    recent = _item_at("b" * 64, datetime(2026, 5, 27, tzinfo=UTC))
    db.insert_item(old)
    db.insert_item(recent)
    got = {i.canonical_hash for i in db.list_recent_items(datetime(2026, 5, 20, tzinfo=UTC))}
    assert got == {"b" * 64}


def test_list_recent_items_excludes_superseded(db: Database) -> None:
    db.insert_item(_item_at("a" * 64, _now()))
    db.insert_item(_item_at("b" * 64, _now()))
    db.mark_superseded("a" * 64, "b" * 64)
    got = {i.canonical_hash for i in db.list_recent_items(datetime(2026, 1, 1, tzinfo=UTC))}
    assert got == {"b" * 64}
