"""Normalization: turn a source-specific RawItem into a canonical Item.

Pure functions, one per source type. For RSS this computes the canonical URL +
hash, parses the publish date, captures a little provenance metadata, and runs
deterministic entity extraction over the item's text.

Full-body storage is intentionally out of scope here (bodies are fetched on
demand by the synthesis agent in a later milestone); feed-provided content is
used only to feed entity extraction.
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


def _strip_html(text: str) -> str:
    """Crude HTML-to-text for entity extraction: drop tags, unescape, collapse ws.

    Feed summaries/content are often HTML; matching entities against raw markup
    produces noise (attributes, tag names) and misses entities split by tags.
    """
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()


class NormalizeError(Exception):
    """Raised when a RawItem cannot be normalized (e.g. it has no URL)."""


def _published_at(payload: dict[str, Any]) -> datetime | None:
    for key in ("published_timestamp", "updated_timestamp"):
        ts = payload.get(key)
        if isinstance(ts, int | float) and not isinstance(ts, bool):
            try:
                return datetime.fromtimestamp(ts, tz=UTC)
            except (OSError, OverflowError, ValueError):
                continue  # garbage/out-of-range date -> treat as undated
    return None


def _metadata(payload: dict[str, Any], *, original_url: str, canonical_url: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    authors = payload.get("authors")
    if authors:
        metadata["authors"] = list(authors)
    tags = payload.get("tags")
    if tags:
        metadata["tags"] = list(tags)
    if original_url != canonical_url:
        metadata["original_url"] = original_url
    return metadata


def normalize_rss(
    raw: RawItem, *, now: datetime, extra_tracking_params: Iterable[str] = ()
) -> tuple[Item, list[ItemEntity]]:
    """Normalize one RSS RawItem into an Item plus its extracted entities."""
    link = raw.payload.get("link")
    if not isinstance(link, str) or not link.strip():
        raise NormalizeError(f"rss item {raw.external_id!r} has no usable link to canonicalize")

    extra = tuple(extra_tracking_params)
    canonical = canonicalize_url(link, extra_tracking_params=extra)
    canonical_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    title_raw = raw.payload.get("title")
    title = title_raw.strip() if isinstance(title_raw, str) and title_raw.strip() else "(untitled)"
    summary_raw = raw.payload.get("summary")
    summary = summary_raw.strip() if isinstance(summary_raw, str) and summary_raw.strip() else None

    item = Item(
        canonical_hash=canonical_hash,
        url=canonical,
        title=title,
        summary=summary,
        published_at=_published_at(raw.payload),
        ingested_at=now,
        metadata=_metadata(raw.payload, original_url=link, canonical_url=canonical),
    )

    text = _strip_html(
        "\n".join(
            part
            for part in (title, summary, raw.payload.get("content"))
            if isinstance(part, str) and part
        )
    )
    entities = extract_entities(canonical_hash, text)
    return item, entities
