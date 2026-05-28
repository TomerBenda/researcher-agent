-- Initial schema for researcher-agent.
--
-- Conventions:
--   * Timestamps stored as TEXT in ISO-8601 with explicit UTC offset (+00:00).
--   * JSON-shaped fields stored as TEXT, parsed at the storage-layer boundary.
--   * `canonical_hash` is sha256 hex (64 chars). It's the primary key for items
--     and the foreign key everywhere else.
--   * Foreign-key ON DELETE policies are explicit; FK enforcement must be turned
--     on per-connection via `PRAGMA foreign_keys = ON;`.

CREATE TABLE schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- ----------------------------------------------------------------------------
-- Items: canonical, source-agnostic. One row per deduplicated "thing".
-- ----------------------------------------------------------------------------
CREATE TABLE items (
    canonical_hash TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    published_at TEXT,                  -- nullable: some sources omit dates
    ingested_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    canonicalization_version INTEGER NOT NULL DEFAULT 1,
    -- Pointer to the active classification (NULL = not yet classified).
    current_classification_id INTEGER,
    -- If this item was deduped against a different surviving item, point to it.
    superseded_by TEXT,
    FOREIGN KEY (current_classification_id)
        REFERENCES classifications(id) ON DELETE SET NULL,
    FOREIGN KEY (superseded_by)
        REFERENCES items(canonical_hash) ON DELETE SET NULL
);

CREATE INDEX idx_items_published ON items(published_at);
CREATE INDEX idx_items_ingested ON items(ingested_at);
CREATE INDEX idx_items_superseded ON items(superseded_by);

-- ----------------------------------------------------------------------------
-- Item bodies: kept in a separate table so `items` stays narrow.
-- ----------------------------------------------------------------------------
CREATE TABLE item_bodies (
    canonical_hash TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    fetched_from_url TEXT,
    fetched_at TEXT NOT NULL,
    FOREIGN KEY (canonical_hash)
        REFERENCES items(canonical_hash) ON DELETE CASCADE
);

-- ----------------------------------------------------------------------------
-- Item sources: which source(s) reported each item.
-- A (canonical_hash, source_name) pair is unique — a single feed surfacing the
-- same item twice doesn't create duplicate rows.
-- ----------------------------------------------------------------------------
CREATE TABLE item_sources (
    canonical_hash TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    external_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (canonical_hash, source_name),
    FOREIGN KEY (canonical_hash)
        REFERENCES items(canonical_hash) ON DELETE CASCADE
);

CREATE INDEX idx_item_sources_source ON item_sources(source_name);
CREATE INDEX idx_item_sources_source_seen ON item_sources(source_name, first_seen_at);

-- ----------------------------------------------------------------------------
-- Item entities: CVEs, repos, packages, etc. extracted at normalize time.
-- Multiple entities per item; same entity across many items is the trend signal.
-- ----------------------------------------------------------------------------
CREATE TABLE item_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_hash TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    context TEXT,
    FOREIGN KEY (canonical_hash)
        REFERENCES items(canonical_hash) ON DELETE CASCADE
);

CREATE INDEX idx_item_entities_kv ON item_entities(kind, value);
CREATE INDEX idx_item_entities_hash ON item_entities(canonical_hash);
-- Same (item, kind, value) shouldn't double-insert.
CREATE UNIQUE INDEX uniq_item_entities ON item_entities(canonical_hash, kind, value);

-- ----------------------------------------------------------------------------
-- Classifications: append-only history. Each row is one classification act.
-- The "active" classification is whichever row is referenced by
-- items.current_classification_id.
-- ----------------------------------------------------------------------------
CREATE TABLE classifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_hash TEXT NOT NULL,
    topic TEXT NOT NULL,
    secondary_topics_json TEXT NOT NULL DEFAULT '[]',
    score INTEGER NOT NULL CHECK (score >= 0 AND score <= 10),
    rationale TEXT NOT NULL,
    classifier_version TEXT NOT NULL,
    classifier_model TEXT NOT NULL,
    classified_at TEXT NOT NULL,
    FOREIGN KEY (canonical_hash)
        REFERENCES items(canonical_hash) ON DELETE CASCADE
);

CREATE INDEX idx_classifications_hash ON classifications(canonical_hash);
CREATE INDEX idx_classifications_topic_score ON classifications(topic, score DESC);
CREATE INDEX idx_classifications_version ON classifications(classifier_version);

-- ----------------------------------------------------------------------------
-- Source runs: per-source cursor + health counters.
-- `cursor_json` is opaque, shape determined by the adapter.
-- ----------------------------------------------------------------------------
CREATE TABLE source_runs (
    source_name TEXT PRIMARY KEY,
    cursor_json TEXT NOT NULL DEFAULT '{}',
    last_run_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    consecutive_empty_runs INTEGER NOT NULL DEFAULT 0
);

-- ----------------------------------------------------------------------------
-- Followups: agent-queued actions.
-- Title/url snapshots make followups self-sufficient even if the item is pruned.
-- ----------------------------------------------------------------------------
CREATE TABLE followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    item_hash TEXT,
    item_title_snapshot TEXT NOT NULL,
    item_url_snapshot TEXT NOT NULL,
    action TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    completed INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (item_hash)
        REFERENCES items(canonical_hash) ON DELETE SET NULL
);

CREATE INDEX idx_followups_open ON followups(completed, created_at);

-- ----------------------------------------------------------------------------
-- Weekly entities: synthesized by the weekly agent.
-- ----------------------------------------------------------------------------
CREATE TABLE weekly_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_starting TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    context TEXT NOT NULL,
    related_item_hashes_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX idx_weekly_entities_week ON weekly_entities(week_starting);
CREATE INDEX idx_weekly_entities_kv ON weekly_entities(kind, value);
