"""Collect orchestration: fetch -> normalize -> store, then classify -> dedupe -> render.

`run_collect` gathers from each source, normalizes every item, stores items /
sources / entities, and advances each source's cursor and health counters.

The M3 post-processing stage (`post_process` and its `classify_pending`,
`dedupe_recent`, `render_collection_to_vault` steps) labels the stored items,
collapses cross-post duplicates, and writes the day's collection report. It is
skipped cleanly when no classifier provider is available (offline / no API key).

Crash-safety: items are inserted before the source cursor is advanced, and all
inserts are idempotent (INSERT OR IGNORE), so an interrupted run re-fetches and
re-inserts without creating duplicates.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from researcher_agent.classify import classify_inputs
from researcher_agent.config import AgentConfig, DedupConfig, Taxonomy
from researcher_agent.dedupe import DedupCandidate, find_duplicates
from researcher_agent.http import PoliteClient
from researcher_agent.llm.base import ClassifierInput, ClassifierProvider
from researcher_agent.models import (
    Classification,
    Item,
    ItemEntity,
    ItemSource,
    RawItem,
    SourceRun,
)
from researcher_agent.normalize import (
    NormalizeError,
    normalize_arxiv,
    normalize_github_release,
    normalize_github_repo,
    normalize_hn,
    normalize_rss,
)
from researcher_agent.prompts import classifier_version, load_prompt, render_system_prompt
from researcher_agent.sources.base import SourceAdapter
from researcher_agent.state import Database
from researcher_agent.vault import CollectionEntry, write_collection_report


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


_NORMALIZERS = {
    "rss": normalize_rss,
    "arxiv": normalize_arxiv,
    "hn_search": normalize_hn,
    "github_releases": normalize_github_release,
    "github_topic": normalize_github_repo,
}


def _normalize(
    raw: RawItem, *, now: datetime, extra_tracking_params: tuple[str, ...]
) -> tuple[Item, list[ItemEntity]]:
    normalizer = _NORMALIZERS.get(raw.source_type)
    if normalizer is None:
        raise NormalizeError(f"no normalizer for source_type {raw.source_type!r}")
    return normalizer(raw, now=now, extra_tracking_params=extra_tracking_params)


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
    prev_error = run.consecutive_error_runs if run else 0
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
                consecutive_empty_runs=prev_empty,  # unchanged: an error is not "empty"
                consecutive_error_runs=prev_error + 1,
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
            consecutive_error_runs=0,  # a successful fetch clears the error streak
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


# --- post-processing: classify -> dedupe -> vault render (M3) -------------------


@dataclass(frozen=True)
class ClassifyStats:
    classified: int
    fallbacks: int
    skipped: int


def classify_pending(
    db: Database,
    *,
    provider: ClassifierProvider,
    taxonomy: Taxonomy,
    system_prompt: str,
    classifier_version: str,
    classifier_model: str,
    now: datetime,
    batch_size: int = 10,
    token_budget: int | None = None,
    limit: int | None = None,
) -> ClassifyStats:
    """Classify every unclassified item and persist the active classifications."""
    items = db.list_unclassified(limit=limit)
    if not items:
        return ClassifyStats(0, 0, 0)

    inputs: list[ClassifierInput] = []
    for item in items:
        sources = db.get_item_sources(item.canonical_hash)
        inputs.append(
            ClassifierInput(
                id=item.canonical_hash,
                title=item.title,
                url=item.url,
                summary=item.summary,
                source=sources[0].source_name if sources else None,
            )
        )

    outcome = classify_inputs(
        inputs,
        provider=provider,
        system_prompt=system_prompt,
        valid_topics=taxonomy.slugs,
        batch_size=batch_size,
        token_budget=token_budget,
    )
    for canonical_hash, rc in outcome.classifications.items():
        db.classify_and_activate(
            Classification(
                canonical_hash=canonical_hash,
                topic=rc.topic,
                secondary_topics=rc.secondary_topics,
                score=rc.score,
                rationale=rc.rationale,
                classifier_version=classifier_version,
                classifier_model=classifier_model,
                classified_at=now,
            )
        )
    return ClassifyStats(
        classified=len(outcome.classifications),
        fallbacks=len(outcome.fallback_ids),
        skipped=len(outcome.skipped_ids),
    )


# Dedupe is pairwise O(n^2) string similarity; above this pool size we skip it
# rather than spend minutes on a pathological cold-backfill (M4 will block/window).
MAX_DEDUPE_POOL = 1000


def dedupe_recent(
    db: Database,
    *,
    since: datetime,
    config: DedupConfig,
    now: datetime,
    max_pool: int = MAX_DEDUPE_POOL,
) -> int:
    """Detect cross-post duplicates among recent items and supersede the losers.

    The winner inherits the loser's sources so the merged item records every feed
    it was seen on. Returns the number of items superseded. Skips entirely (returns
    0) when the candidate pool exceeds `max_pool` to bound the O(n^2) comparison.
    """
    recent = db.list_recent_items(since)
    if len(recent) > max_pool:
        return 0

    candidates: list[DedupCandidate] = []
    for item in recent:
        active = db.get_active_classification(item.canonical_hash)
        candidates.append(
            DedupCandidate(
                canonical_hash=item.canonical_hash,
                title=item.title,
                published_at=item.published_at,
                score=active.score if active is not None else 0,
                entities=frozenset(
                    (e.kind, e.value) for e in db.get_item_entities(item.canonical_hash)
                ),
            )
        )

    pairs = find_duplicates(
        candidates,
        title_threshold=config.fuzzy_title_threshold,
        entity_title_threshold=config.entity_title_threshold,
        window_hours=config.fuzzy_window_hours,
    )
    for loser, winner in pairs:
        # The winner comes from the non-superseded pool, but resolve to its root
        # defensively so a cross-run winner-flip can't point at a gone item.
        root = db.resolve_supersession_root(winner)
        for src in db.get_item_sources(loser):
            db.add_item_source(
                ItemSource(
                    canonical_hash=root,
                    source_name=src.source_name,
                    source_type=src.source_type,
                    external_id=src.external_id,
                    first_seen_at=src.first_seen_at,
                )
            )
        db.mark_superseded(loser, root)
        # Flatten any pre-existing chain: items that had lost to `loser` in an
        # earlier run must now point at the surviving root, not a superseded item.
        db.repoint_supersessions(loser, root)
    return len(pairs)


def render_collection_to_vault(db: Database, *, vault_root: Path, report_date: date) -> Path:
    """Render the classified, non-superseded items ingested on `report_date`.

    Deterministic: the day window is derived from the date and the renderer sorts
    its inputs, so re-running produces byte-identical output.
    """
    start = datetime(report_date.year, report_date.month, report_date.day, tzinfo=UTC)
    end = start + timedelta(days=1)

    entries: list[CollectionEntry] = []
    for item in db.list_recent_items(start):
        if item.ingested_at >= end:
            continue
        active = db.get_active_classification(item.canonical_hash)
        if active is None:
            continue
        entries.append(
            CollectionEntry(
                item=item,
                classification=active,
                sources=db.get_item_sources(item.canonical_hash),
            )
        )
    return write_collection_report(entries, report_date, vault_root)


@dataclass(frozen=True)
class PostStats:
    """Result of the classify -> dedupe -> render post-processing stage."""

    classify: ClassifyStats | None  # None when classification was skipped
    deduped: int
    report_path: Path | None


def post_process(
    db: Database,
    config: AgentConfig,
    provider: ClassifierProvider | None,
    *,
    now: datetime,
    vault_override: Path | None = None,
    dedupe_lookback_days: int = 7,
) -> PostStats:
    """Classify pending items, dedupe recent ones, and render the day's report.

    With `provider=None` the whole stage is skipped (e.g. no API key / offline
    collection) — items are still stored, just left unclassified.
    """
    if provider is None:
        return PostStats(classify=None, deduped=0, report_path=None)

    system_prompt = render_system_prompt(
        load_prompt("classify"),
        taxonomy=config.taxonomy.render(),
        research_focus=config.research_focus,
    )
    version = classifier_version(system_prompt, provider.model_id)
    classify_stats = classify_pending(
        db,
        provider=provider,
        taxonomy=config.taxonomy,
        system_prompt=system_prompt,
        classifier_version=version,
        classifier_model=provider.model_id,
        now=now,
        batch_size=config.classifier.batch_size,
        token_budget=config.classifier.token_budget,
        limit=config.classifier.max_items_per_run,
    )
    deduped = dedupe_recent(
        db,
        since=now - timedelta(days=dedupe_lookback_days),
        config=config.dedup,
        now=now,
    )

    vault_root = vault_override or (Path(config.vault_path) if config.vault_path else None)
    report_path = (
        render_collection_to_vault(db, vault_root=vault_root, report_date=now.date())
        if vault_root is not None
        else None
    )
    return PostStats(classify=classify_stats, deduped=deduped, report_path=report_path)
