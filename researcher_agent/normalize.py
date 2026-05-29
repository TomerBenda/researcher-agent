"""Normalization: turn a source-specific RawItem into a canonical Item.

Pure functions, one per source type, all built on `build_item`: it computes the
canonical URL + hash, cleans the title/summary, records provenance, and runs
deterministic entity extraction over the item's text. Each `normalize_*` just
maps its source's payload shape onto those fields.

Full-body storage is intentionally out of scope here (bodies are fetched on
demand by the synthesis agent in a later milestone); provided content/abstracts
are used only to feed entity extraction.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from researcher_agent.canonicalize import canonicalize_url
from researcher_agent.entities import extract_entities
from researcher_agent.models import Item, ItemEntity, RawItem

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Bound untrusted source text: a hostile/huge release body or feed entry must not
# bloat storage or the classifier prompt. Summaries are stored; the extraction
# text is only scanned for entities.
_MAX_SUMMARY_CHARS = 4_000
_MAX_EXTRACTION_CHARS = 20_000


def _strip_html(text: str) -> str:
    """Crude HTML-to-text for entity extraction: drop tags, unescape, collapse ws.

    Feed summaries/content are often HTML; matching entities against raw markup
    produces noise (attributes, tag names) and misses entities split by tags.
    """
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()


class NormalizeError(Exception):
    """Raised when a RawItem cannot be normalized (e.g. it has no URL)."""


def published_at_from_payload(payload: dict[str, Any]) -> datetime | None:
    """Read a publish datetime from the standardized `*_timestamp` epoch fields."""
    for key in ("published_timestamp", "updated_timestamp"):
        ts = payload.get(key)
        if isinstance(ts, int | float) and not isinstance(ts, bool):
            try:
                return datetime.fromtimestamp(ts, tz=UTC)
            except (OSError, OverflowError, ValueError):
                continue  # garbage/out-of-range date -> treat as undated
    return None


def build_item(
    *,
    url: object,
    title: object,
    summary: object,
    published_at: datetime | None,
    now: datetime,
    metadata: dict[str, Any] | None = None,
    extra_text: object = None,
    extra_tracking_params: Iterable[str] = (),
) -> tuple[Item, list[ItemEntity]]:
    """Canonicalize, clean, and build an Item + its entities. Shared by all sources.

    Raises NormalizeError if `url` is not a usable http(s) string. `extra_text`
    (e.g. body/abstract) feeds entity extraction but is not stored.
    """
    if not isinstance(url, str) or not url.strip():
        raise NormalizeError("item has no usable url to canonicalize")

    canonical = canonicalize_url(url, extra_tracking_params=tuple(extra_tracking_params))
    canonical_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    clean_title = title.strip() if isinstance(title, str) and title.strip() else "(untitled)"
    clean_summary = summary.strip() if isinstance(summary, str) and summary.strip() else None
    if clean_summary is not None:
        clean_summary = clean_summary[:_MAX_SUMMARY_CHARS]

    md = dict(metadata or {})
    if url != canonical:
        md.setdefault("original_url", url)

    item = Item(
        canonical_hash=canonical_hash,
        url=canonical,
        title=clean_title,
        summary=clean_summary,
        published_at=published_at,
        ingested_at=now,
        metadata=md,
    )

    text = _strip_html(
        "\n".join(
            part
            for part in (clean_title, clean_summary, extra_text)
            if isinstance(part, str) and part
        )[:_MAX_EXTRACTION_CHARS]
    )
    return item, extract_entities(canonical_hash, text)


def _feed_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    md: dict[str, Any] = {}
    if payload.get("authors"):
        md["authors"] = list(payload["authors"])
    if payload.get("tags"):
        md["tags"] = list(payload["tags"])
    return md


def normalize_rss(
    raw: RawItem, *, now: datetime, extra_tracking_params: Iterable[str] = ()
) -> tuple[Item, list[ItemEntity]]:
    """Normalize one RSS RawItem into an Item plus its extracted entities."""
    payload = raw.payload
    return build_item(
        url=payload.get("link"),
        title=payload.get("title"),
        summary=payload.get("summary"),
        published_at=published_at_from_payload(payload),
        now=now,
        metadata=_feed_metadata(payload),
        extra_text=payload.get("content"),
        extra_tracking_params=extra_tracking_params,
    )


def normalize_arxiv(
    raw: RawItem, *, now: datetime, extra_tracking_params: Iterable[str] = ()
) -> tuple[Item, list[ItemEntity]]:
    """Normalize an arXiv entry. The link is an abs/pdf URL that canonicalizes to
    the version-stripped abs form; the summary is the abstract."""
    payload = raw.payload
    return build_item(
        url=payload.get("link"),
        title=payload.get("title"),
        summary=payload.get("summary"),
        published_at=published_at_from_payload(payload),
        now=now,
        metadata=_feed_metadata(payload),
        extra_text=payload.get("summary"),
        extra_tracking_params=extra_tracking_params,
    )


def normalize_hn(
    raw: RawItem, *, now: datetime, extra_tracking_params: Iterable[str] = ()
) -> tuple[Item, list[ItemEntity]]:
    """Normalize a Hacker News hit. The link is the submitted article (or the HN
    discussion for text posts); points/author/comments go to metadata."""
    payload = raw.payload
    metadata: dict[str, Any] = {"hn_url": payload.get("hn_url")}
    for key in ("author", "points", "num_comments"):
        if payload.get(key) is not None:
            metadata[key] = payload[key]
    return build_item(
        url=payload.get("link"),
        title=payload.get("title"),
        summary=payload.get("story_text"),
        published_at=published_at_from_payload(payload),
        now=now,
        metadata=metadata,
        extra_text=payload.get("story_text"),
        extra_tracking_params=extra_tracking_params,
    )


def normalize_github_release(
    raw: RawItem, *, now: datetime, extra_tracking_params: Iterable[str] = ()
) -> tuple[Item, list[ItemEntity]]:
    """Normalize a GitHub release: title is 'owner/repo tag', summary is the notes."""
    payload = raw.payload
    metadata: dict[str, Any] = {}
    for key in ("repo", "tag", "author"):
        if payload.get(key) is not None:
            metadata[key] = payload[key]
    return build_item(
        url=payload.get("link"),
        title=payload.get("title"),
        summary=payload.get("summary"),
        published_at=published_at_from_payload(payload),
        now=now,
        metadata=metadata,
        extra_text=payload.get("summary"),
        extra_tracking_params=extra_tracking_params,
    )


def normalize_github_repo(
    raw: RawItem, *, now: datetime, extra_tracking_params: Iterable[str] = ()
) -> tuple[Item, list[ItemEntity]]:
    """Normalize a GitHub repo (topic search): title is the full name, summary the
    description; stars/pushed_at recorded in metadata."""
    payload = raw.payload
    metadata: dict[str, Any] = {}
    for key in ("repo", "stars"):
        if payload.get(key) is not None:
            metadata[key] = payload[key]
    return build_item(
        url=payload.get("link"),
        title=payload.get("title"),
        summary=payload.get("summary"),
        published_at=published_at_from_payload(payload),
        now=now,
        metadata=metadata,
        extra_text=payload.get("summary"),
        extra_tracking_params=extra_tracking_params,
    )
