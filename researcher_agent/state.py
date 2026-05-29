"""SQLite storage layer. All SQL lives here; callers use typed functions.

Conventions:
- Connection opens with WAL mode and FK enforcement.
- Datetimes serialized as ISO-8601 with explicit UTC offset (+00:00).
- JSON fields serialized via stdlib json; opaque to SQL.
- Migrations applied on `Database(path)` construction via a tiny runner.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


# --- (de)serialization helpers --------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("naive datetime cannot be persisted")
    return dt.astimezone(UTC).isoformat()


def _from_iso(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def _loads(s: str) -> Any:
    return json.loads(s)


# --- Connection / migrations ----------------------------------------------------


def _connect(path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + FK enforcement + Row factory."""
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit; we use explicit txns
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _applied_migrations(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    )
    if cur.fetchone() is None:
        return set()
    cur = conn.execute("SELECT filename FROM schema_migrations")
    return {row["filename"] for row in cur.fetchall()}


def _apply_migrations(conn: sqlite3.Connection, migrations_dir: Path) -> list[str]:
    """Apply any unapplied .sql files in lexicographic order. Returns applied names."""
    applied = _applied_migrations(conn)
    files = sorted(p for p in migrations_dir.glob("*.sql"))
    newly_applied: list[str] = []
    for path in files:
        if path.name in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        with conn:  # transaction
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
                (path.name, _iso(datetime.now(UTC))),
            )
        newly_applied.append(path.name)
    return newly_applied


# --- Database object ------------------------------------------------------------


class Database:
    """Thin wrapper around a SQLite connection. All SQL goes through here."""

    def __init__(self, path: Path | str, migrations_dir: Path | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = _connect(self.path)
        _apply_migrations(self.conn, migrations_dir or MIGRATIONS_DIR)

    def checkpoint(self) -> None:
        """Flush the WAL into the main db file and truncate it.

        Must be run before committing `state.db` to git: in WAL mode recent
        writes live in the `-wal` sidecar (which is gitignored), so without a
        checkpoint the committed main file would be stale/torn.
        """
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def integrity_check(self) -> bool:
        """Return True iff SQLite reports the database is well-formed."""
        row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return row is not None and row[0] == "ok"

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- items --------------------------------------------------------------

    def insert_item(self, item: Item) -> bool:
        """Insert an item. Returns True if newly inserted, False if it already existed.

        Existing rows are not updated — re-ingesting an item is idempotent. To change
        a stored item, callers must explicitly update it.
        """
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO items
                (canonical_hash, url, title, summary, published_at, ingested_at,
                 metadata_json, canonicalization_version, current_classification_id,
                 superseded_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                item.canonical_hash,
                item.url,
                item.title,
                item.summary,
                _iso(item.published_at),
                _iso(item.ingested_at),
                _dumps(item.metadata),
                item.canonicalization_version,
            ),
        )
        return cur.rowcount > 0

    def get_item(self, canonical_hash: str) -> Item | None:
        row = self.conn.execute(
            "SELECT * FROM items WHERE canonical_hash = ?", (canonical_hash,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_item(row)

    def mark_superseded(self, loser_hash: str, winner_hash: str) -> None:
        self.conn.execute(
            "UPDATE items SET superseded_by = ? WHERE canonical_hash = ?",
            (winner_hash, loser_hash),
        )

    def list_unclassified(self, *, limit: int | None = None) -> list[Item]:
        """Items with no active classification (and not superseded), oldest first."""
        sql = (
            "SELECT * FROM items "
            "WHERE current_classification_id IS NULL AND superseded_by IS NULL "
            "ORDER BY ingested_at, canonical_hash"
        )
        params: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_item(r) for r in rows]

    def list_recent_items(self, since: datetime) -> list[Item]:
        """Non-superseded items ingested at/after `since`, oldest first (dedupe pool)."""
        rows = self.conn.execute(
            "SELECT * FROM items WHERE ingested_at >= ? AND superseded_by IS NULL "
            "ORDER BY ingested_at, canonical_hash",
            (_iso(since),),
        ).fetchall()
        return [_row_to_item(r) for r in rows]

    # --- bodies -------------------------------------------------------------

    def set_item_body(self, body: ItemBody) -> None:
        self.conn.execute(
            """
            INSERT INTO item_bodies (canonical_hash, body, fetched_from_url, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(canonical_hash) DO UPDATE SET
                body = excluded.body,
                fetched_from_url = excluded.fetched_from_url,
                fetched_at = excluded.fetched_at
            """,
            (body.canonical_hash, body.body, body.fetched_from_url, _iso(body.fetched_at)),
        )

    def get_item_body(self, canonical_hash: str) -> ItemBody | None:
        row = self.conn.execute(
            "SELECT * FROM item_bodies WHERE canonical_hash = ?", (canonical_hash,)
        ).fetchone()
        if row is None:
            return None
        fetched_at = _from_iso(row["fetched_at"])
        assert fetched_at is not None  # NOT NULL in schema
        return ItemBody(
            canonical_hash=row["canonical_hash"],
            body=row["body"],
            fetched_from_url=row["fetched_from_url"],
            fetched_at=fetched_at,
        )

    # --- sources ------------------------------------------------------------

    def add_item_source(self, source: ItemSource) -> bool:
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO item_sources
                (canonical_hash, source_name, source_type, external_id, first_seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                source.canonical_hash,
                source.source_name,
                source.source_type,
                source.external_id,
                _iso(source.first_seen_at),
            ),
        )
        return cur.rowcount > 0

    def get_item_sources(self, canonical_hash: str) -> list[ItemSource]:
        rows = self.conn.execute(
            "SELECT * FROM item_sources WHERE canonical_hash = ? ORDER BY first_seen_at",
            (canonical_hash,),
        ).fetchall()
        result: list[ItemSource] = []
        for row in rows:
            first_seen_at = _from_iso(row["first_seen_at"])
            assert first_seen_at is not None
            result.append(
                ItemSource(
                    canonical_hash=row["canonical_hash"],
                    source_name=row["source_name"],
                    source_type=row["source_type"],
                    external_id=row["external_id"],
                    first_seen_at=first_seen_at,
                )
            )
        return result

    # --- entities -----------------------------------------------------------

    def add_item_entities(self, entities: Iterable[ItemEntity]) -> int:
        """Insert entities. Returns count of newly inserted rows."""
        rows = [(e.canonical_hash, e.kind, e.value, e.context) for e in entities]
        if not rows:
            return 0
        before = self.conn.total_changes
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO item_entities (canonical_hash, kind, value, context)
            VALUES (?, ?, ?, ?)
            """,
            rows,
        )
        return self.conn.total_changes - before

    def get_item_entities(self, canonical_hash: str) -> list[ItemEntity]:
        rows = self.conn.execute(
            "SELECT canonical_hash, kind, value, context FROM item_entities "
            "WHERE canonical_hash = ? ORDER BY kind, value",
            (canonical_hash,),
        ).fetchall()
        return [
            ItemEntity(
                canonical_hash=r["canonical_hash"],
                kind=r["kind"],
                value=r["value"],
                context=r["context"],
            )
            for r in rows
        ]

    # --- classifications ----------------------------------------------------

    def append_classification(self, c: Classification) -> int:
        """Append a classification row and return its new id.

        Does NOT update items.current_classification_id — call
        set_current_classification separately.
        """
        cur = self.conn.execute(
            """
            INSERT INTO classifications
                (canonical_hash, topic, secondary_topics_json, score, rationale,
                 classifier_version, classifier_model, classified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                c.canonical_hash,
                c.topic,
                _dumps(c.secondary_topics),
                c.score,
                c.rationale,
                c.classifier_version,
                c.classifier_model,
                _iso(c.classified_at),
            ),
        )
        new_id = cur.lastrowid
        assert new_id is not None
        return new_id

    def set_current_classification(self, canonical_hash: str, classification_id: int) -> None:
        self.conn.execute(
            "UPDATE items SET current_classification_id = ? WHERE canonical_hash = ?",
            (classification_id, canonical_hash),
        )

    def classify_and_activate(self, c: Classification) -> int:
        """Common case: append a classification and mark it active."""
        new_id = self.append_classification(c)
        self.set_current_classification(c.canonical_hash, new_id)
        return new_id

    def get_active_classification(self, canonical_hash: str) -> Classification | None:
        row = self.conn.execute(
            """
            SELECT c.*
            FROM items i
            JOIN classifications c ON c.id = i.current_classification_id
            WHERE i.canonical_hash = ?
            """,
            (canonical_hash,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_classification(row)

    def get_classification_history(self, canonical_hash: str) -> list[Classification]:
        rows = self.conn.execute(
            "SELECT * FROM classifications WHERE canonical_hash = ? ORDER BY classified_at",
            (canonical_hash,),
        ).fetchall()
        return [_row_to_classification(r) for r in rows]

    # --- source runs --------------------------------------------------------

    def get_source_run(self, source_name: str) -> SourceRun | None:
        row = self.conn.execute(
            "SELECT * FROM source_runs WHERE source_name = ?", (source_name,)
        ).fetchone()
        if row is None:
            return None
        return SourceRun(
            source_name=row["source_name"],
            cursor=_loads(row["cursor_json"]),
            last_run_at=_from_iso(row["last_run_at"]),
            last_success_at=_from_iso(row["last_success_at"]),
            last_error=row["last_error"],
            consecutive_empty_runs=row["consecutive_empty_runs"],
            consecutive_error_runs=row["consecutive_error_runs"],
        )

    def upsert_source_run(self, run: SourceRun) -> None:
        self.conn.execute(
            """
            INSERT INTO source_runs
                (source_name, cursor_json, last_run_at, last_success_at, last_error,
                 consecutive_empty_runs, consecutive_error_runs)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_name) DO UPDATE SET
                cursor_json = excluded.cursor_json,
                last_run_at = excluded.last_run_at,
                last_success_at = excluded.last_success_at,
                last_error = excluded.last_error,
                consecutive_empty_runs = excluded.consecutive_empty_runs,
                consecutive_error_runs = excluded.consecutive_error_runs
            """,
            (
                run.source_name,
                _dumps(run.cursor),
                _iso(run.last_run_at),
                _iso(run.last_success_at),
                run.last_error,
                run.consecutive_empty_runs,
                run.consecutive_error_runs,
            ),
        )

    # --- followups ----------------------------------------------------------

    def insert_followup(self, f: Followup) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO followups
                (created_at, item_hash, item_title_snapshot, item_url_snapshot,
                 action, note, completed)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _iso(f.created_at),
                f.item_hash,
                f.item_title_snapshot,
                f.item_url_snapshot,
                f.action,
                f.note,
                1 if f.completed else 0,
            ),
        )
        new_id = cur.lastrowid
        assert new_id is not None
        return new_id

    def list_followups(self, *, open_only: bool = True) -> list[Followup]:
        sql = (
            "SELECT * FROM followups WHERE completed = 0 ORDER BY created_at"
            if open_only
            else "SELECT * FROM followups ORDER BY created_at"
        )
        rows = self.conn.execute(sql).fetchall()
        return [_row_to_followup(r) for r in rows]

    def mark_followup_done(self, followup_id: int) -> None:
        self.conn.execute("UPDATE followups SET completed = 1 WHERE id = ?", (followup_id,))

    # --- weekly entities ----------------------------------------------------

    def insert_weekly_entities(self, entities: Iterable[WeeklyEntity]) -> int:
        rows = [
            (
                _iso(e.week_starting),
                e.kind,
                e.value,
                e.context,
                _dumps(e.related_item_hashes),
            )
            for e in entities
        ]
        if not rows:
            return 0
        before = self.conn.total_changes
        self.conn.executemany(
            """
            INSERT INTO weekly_entities
                (week_starting, kind, value, context, related_item_hashes_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )
        return self.conn.total_changes - before


# --- row → model adapters --------------------------------------------------------


def _row_to_item(row: sqlite3.Row) -> Item:
    ingested_at = _from_iso(row["ingested_at"])
    assert ingested_at is not None
    return Item(
        canonical_hash=row["canonical_hash"],
        url=row["url"],
        title=row["title"],
        summary=row["summary"],
        published_at=_from_iso(row["published_at"]),
        ingested_at=ingested_at,
        metadata=_loads(row["metadata_json"]),
        canonicalization_version=row["canonicalization_version"],
    )


def _row_to_classification(row: sqlite3.Row) -> Classification:
    classified_at = _from_iso(row["classified_at"])
    assert classified_at is not None
    return Classification(
        canonical_hash=row["canonical_hash"],
        topic=row["topic"],
        secondary_topics=_loads(row["secondary_topics_json"]),
        score=row["score"],
        rationale=row["rationale"],
        classifier_version=row["classifier_version"],
        classifier_model=row["classifier_model"],
        classified_at=classified_at,
    )


def _row_to_followup(row: sqlite3.Row) -> Followup:
    created_at = _from_iso(row["created_at"])
    assert created_at is not None
    return Followup(
        id=row["id"],
        created_at=created_at,
        item_hash=row["item_hash"],
        item_title_snapshot=row["item_title_snapshot"],
        item_url_snapshot=row["item_url_snapshot"],
        action=row["action"],
        note=row["note"],
        completed=bool(row["completed"]),
    )
