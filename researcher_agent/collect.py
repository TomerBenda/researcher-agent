"""Collect orchestration: fetch -> normalize -> store.

For M2 this stops at the storage layer — no classification and no vault
rendering (those land with the classifier in M3). The collect command gathers
from each configured source, normalizes every item, stores items / sources /
entities, and advances each source's cursor and health counters.

Crash-safety: items are inserted before the source cursor is advanced, and all
inserts are idempotent (INSERT OR IGNORE), so an interrupted run re-fetches and
re-inserts without creating duplicates.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from researcher_agent.http import PoliteClient
from researcher_agent.models import Item, ItemEntity, ItemSource, RawItem, SourceRun
from researcher_agent.normalize import NormalizeError, normalize_rss
from researcher_agent.sources.base import SourceAdapter
from researcher_agent.state import Database


def _format_error(exc: Exception, log_mode: str) -> str:
    """Render an exception for storage/logging.

    Public CI logs must not leak the (private) source list. The exception
    *message* is the leak vector — not just full URLs but bare hostnames from
    DNS/connect/SSL errors — so in production we drop the message entirely and
    keep only the exception class name plus a short hash of the full message so
    recurring failures stay correlatable across runs without exposing content.
    """
    detail = f"{type(exc).__name__}: {exc}"
    if log_mode != "production":
        return detail
    digest = hashlib.sha256(detail.encode("utf-8")).hexdigest()[:8]
    return f"{type(exc).__name__} (redacted msg#{digest})"


@dataclass(frozen=True)
class SourceOutcome:
    """Per-source result of a collect run."""

    name: str
    fetched: int = 0
    stored_new: int = 0
    entities_new: int = 0
    skipped: int = 0
    not_modified: bool = False
    error: str | None = None


@dataclass(frozen=True)
class CollectStats:
    """Aggregate result of a collect run."""

    outcomes: list[SourceOutcome] = field(default_factory=list)

    @property
    def total_new_items(self) -> int:
        return sum(o.stored_new for o in self.outcomes)

    @property
    def total_fetched(self) -> int:
        return sum(o.fetched for o in self.outcomes)

    @property
    def error_count(self) -> int:
        return sum(1 for o in self.outcomes if o.error is not None)

    @property
    def any_errors(self) -> bool:
        return self.error_count > 0


def _normalize(
    raw: RawItem, *, now: datetime, extra_tracking_params: tuple[str, ...]
) -> tuple[Item, list[ItemEntity]]:
    if raw.source_type == "rss":
        return normalize_rss(raw, now=now, extra_tracking_params=extra_tracking_params)
    raise NormalizeError(f"no normalizer for source_type {raw.source_type!r}")


def _in_window(
    published_at: datetime | None, since: datetime | None, until: datetime | None
) -> bool:
    """Window filter. Undated items are always kept (we can't position them)."""
    if published_at is None:
        return True
    if since is not None and published_at < since:
        return False
    return not (until is not None and published_at >= until)


def _collect_one(
    db: Database,
    adapter: SourceAdapter,
    client: PoliteClient,
    *,
    now: datetime,
    since: datetime | None,
    until: datetime | None,
    extra_tracking_params: tuple[str, ...],
    log_mode: str,
) -> SourceOutcome:
    name = adapter.config.name
    run = db.get_source_run(name)
    cursor = dict(run.cursor) if run else {}
    prev_empty = run.consecutive_empty_runs if run else 0
    prev_success = run.last_success_at if run else None

    try:
        result = adapter.fetch(client, cursor, now)
    except Exception as exc:  # one bad source must not sink the others
        error = _format_error(exc, log_mode)
        db.upsert_source_run(
            SourceRun(
                source_name=name,
                cursor=cursor,
                last_run_at=now,
                last_success_at=prev_success,
                last_error=error,
                consecutive_empty_runs=prev_empty,
            )
        )
        return SourceOutcome(name=name, error=error)

    stored_new = 0
    entities_new = 0
    skipped = 0
    kept = 0
    for raw in result.raw_items:
        try:
            item, entities = _normalize(raw, now=now, extra_tracking_params=extra_tracking_params)
        except Exception:
            # Feed input is fully untrusted and this runs unattended for months:
            # a single malformed item must never crash the run. Skip + count it.
            skipped += 1
            continue
        if not _in_window(item.published_at, since, until):
            continue
        kept += 1
        if db.insert_item(item):
            stored_new += 1
        db.add_item_source(
            ItemSource(
                canonical_hash=item.canonical_hash,
                source_name=raw.source_name,
                source_type=raw.source_type,
                external_id=raw.external_id,
                first_seen_at=now,
            )
        )
        entities_new += db.add_item_entities(entities)

    produced_nothing = result.not_modified or kept == 0
    db.upsert_source_run(
        SourceRun(
            source_name=name,
            cursor=result.cursor,
            last_run_at=now,
            last_success_at=now,
            last_error=None,
            consecutive_empty_runs=prev_empty + 1 if produced_nothing else 0,
        )
    )
    return SourceOutcome(
        name=name,
        fetched=len(result.raw_items),
        stored_new=stored_new,
        entities_new=entities_new,
        skipped=skipped,
        not_modified=result.not_modified,
    )


def run_collect(
    db: Database,
    adapters: Sequence[SourceAdapter],
    client: PoliteClient,
    *,
    now: datetime,
    since: datetime | None = None,
    until: datetime | None = None,
    extra_tracking_params: Iterable[str] = (),
    log_mode: str = "dev",
) -> CollectStats:
    """Run collection across `adapters`, returning per-source and aggregate stats."""
    extra = tuple(extra_tracking_params)
    outcomes = [
        _collect_one(
            db,
            adapter,
            client,
            now=now,
            since=since,
            until=until,
            extra_tracking_params=extra,
            log_mode=log_mode,
        )
        for adapter in adapters
    ]
    return CollectStats(outcomes=outcomes)
