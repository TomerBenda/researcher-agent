"""Pydantic data models — the spine of the system.

Design notes:
- `Item` is purely the canonical thing. Source attribution lives in `ItemSource`
  so a single item reported by multiple feeds gets one `Item` and many `ItemSource`s.
- `Classification` is append-only history; the active classification is identified
  via `items.current_classification_id` in storage.
- Topic slugs are validated against a config-loaded taxonomy at the classifier
  boundary, not in this module — keeping the taxonomy out of code lets it evolve
  via `config/agent.yaml` without migrations.
- All datetimes are tz-aware UTC. Naive datetimes raise.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SourceType = Literal[
    "rss",
    "github_releases",
    "github_topic",
    "arxiv",
    "hn_search",
]

EntityKind = Literal[
    "cve",  # CVE-YYYY-NNNNN
    "repo",  # owner/name on GitHub
    "package",  # ecosystem:name (npm:foo, pypi:bar)
    "project",  # named software project (broader than a single repo)
    "person",  # researcher / author / maintainer
    "technique",  # attack or defense technique label
    "venue",  # conference, journal, or publication venue
]

FollowupAction = Literal[
    "read-deep",
    "audit",
    "track",
    "reach-out",
]


def _utc(value: datetime) -> datetime:
    """Ensure a datetime is tz-aware UTC. Raise on naive input."""
    if value.tzinfo is None:
        raise ValueError("datetime must be tz-aware (UTC expected)")
    return value.astimezone(UTC)


# --- Source-side shapes ---------------------------------------------------------


class RawItem(BaseModel):
    """Whatever a source adapter pulled out, before normalization."""

    model_config = ConfigDict(frozen=True)

    source_name: str
    source_type: SourceType
    external_id: str
    payload: dict[str, Any]
    fetched_at: datetime

    @field_validator("fetched_at")
    @classmethod
    def _v_fetched_at(cls, v: datetime) -> datetime:
        return _utc(v)


# --- Canonical-side shapes -------------------------------------------------------


class Item(BaseModel):
    """The unit of currency. Canonical, source-agnostic."""

    model_config = ConfigDict(frozen=True)

    canonical_hash: str = Field(min_length=64, max_length=64)
    url: str
    title: str
    summary: str | None = None
    published_at: datetime | None = None
    ingested_at: datetime
    # source-specific structured fields, opaque to the rest of the system
    metadata: dict[str, Any] = Field(default_factory=dict)
    # bump when canonicalization rules change so we can re-canonicalize selectively
    canonicalization_version: int = 1

    @field_validator("published_at")
    @classmethod
    def _v_published_at(cls, v: datetime | None) -> datetime | None:
        return None if v is None else _utc(v)

    @field_validator("ingested_at")
    @classmethod
    def _v_ingested_at(cls, v: datetime) -> datetime:
        return _utc(v)

    @field_validator("url")
    @classmethod
    def _v_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(f"url must be http(s): {v!r}")
        return v


class ItemBody(BaseModel):
    """Full body text, stored separately so the hot `items` table stays lean."""

    model_config = ConfigDict(frozen=True)

    canonical_hash: str = Field(min_length=64, max_length=64)
    body: str
    fetched_from_url: str | None = None  # where we obtained the body, if not the item's url
    fetched_at: datetime

    @field_validator("fetched_at")
    @classmethod
    def _v_fetched_at(cls, v: datetime) -> datetime:
        return _utc(v)


class ItemSource(BaseModel):
    """One source's report of one canonical item.

    The same article surfaced via two RSS feeds is one `Item` and two `ItemSource`s.
    """

    model_config = ConfigDict(frozen=True)

    canonical_hash: str = Field(min_length=64, max_length=64)
    source_name: str
    source_type: SourceType
    external_id: str
    first_seen_at: datetime

    @field_validator("first_seen_at")
    @classmethod
    def _v_first_seen_at(cls, v: datetime) -> datetime:
        return _utc(v)


class ItemEntity(BaseModel):
    """Entity extracted at normalize time (CVE, repo ref, etc.).

    Cheap, deterministic extraction — regex / pattern matching, no LLM. The weekly
    agent consumes these via a tool instead of re-deriving them from raw text.
    """

    model_config = ConfigDict(frozen=True)

    canonical_hash: str = Field(min_length=64, max_length=64)
    kind: EntityKind
    value: str  # normalized form (uppercase CVE, lowercase owner/repo, etc.)
    context: str | None = None  # ~one sentence quote from where it appeared


# --- Classification --------------------------------------------------------------


class Classification(BaseModel):
    """A single classification act. Append-only — never updated in place.

    The 'active' classification for an item is whichever row is referenced by
    `items.current_classification_id`. Switching to a new classification is an
    INSERT here plus an UPDATE on `items`.
    """

    model_config = ConfigDict(frozen=True)

    canonical_hash: str = Field(min_length=64, max_length=64)
    topic: str  # primary topic slug; validated against config taxonomy at boundary
    secondary_topics: list[str] = Field(default_factory=list)
    score: int = Field(ge=0, le=10)
    rationale: str
    classifier_version: str  # hash of prompt + model + relevant config
    classifier_model: str  # provider:model identifier (e.g., "gemini:gemini-2.5-flash")
    classified_at: datetime

    @field_validator("classified_at")
    @classmethod
    def _v_classified_at(cls, v: datetime) -> datetime:
        return _utc(v)


# --- Agent outputs ---------------------------------------------------------------


class Followup(BaseModel):
    """A queued action for the user to handle.

    Title/url snapshots so the followup survives even if the underlying item is
    later pruned (FK uses ON DELETE SET NULL on item_hash).
    """

    model_config = ConfigDict(frozen=True)

    id: int | None = None  # set by SQLite on insert
    created_at: datetime
    item_hash: str | None = None  # nullable: snapshot fields keep the followup useful
    item_title_snapshot: str
    item_url_snapshot: str
    action: FollowupAction
    note: str = ""
    completed: bool = False

    @field_validator("created_at")
    @classmethod
    def _v_created_at(cls, v: datetime) -> datetime:
        return _utc(v)


class WeeklyEntity(BaseModel):
    """Entity surfaced by the weekly agent, scoped to a week."""

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    week_starting: datetime  # midnight UTC on the Monday that starts the ISO week
    kind: EntityKind
    value: str
    context: str
    related_item_hashes: list[str] = Field(default_factory=list)

    @field_validator("week_starting")
    @classmethod
    def _v_week_starting(cls, v: datetime) -> datetime:
        v = _utc(v)
        if v.weekday() != 0 or (v.hour, v.minute, v.second, v.microsecond) != (0, 0, 0, 0):
            raise ValueError("week_starting must be midnight UTC on a Monday")
        return v


# --- Source health ---------------------------------------------------------------


class SourceRun(BaseModel):
    """Persisted state of one source between runs."""

    model_config = ConfigDict(frozen=True)

    source_name: str
    cursor: dict[str, Any] = Field(default_factory=dict)  # opaque, per-adapter shape
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    consecutive_empty_runs: int = 0

    @field_validator("last_run_at", "last_success_at")
    @classmethod
    def _v_optional_ts(cls, v: datetime | None) -> datetime | None:
        return None if v is None else _utc(v)
