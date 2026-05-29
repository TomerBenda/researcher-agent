"""Shared feedparser helpers for Atom/RSS-shaped sources (RSS feeds, arXiv API).

Turns a feedparser entry into a plain JSON-serializable payload dict and a stable
external id, used by both the RSS adapter and the arXiv adapter.
"""

from __future__ import annotations

import calendar
import hashlib
import warnings
from typing import Any


def external_id(entry: Any) -> str:
    """Stable id for an entry: guid/id, else link, else a hash of title+date."""
    candidate = entry.get("id") or entry.get("link")
    if candidate:
        return str(candidate)
    seed = f"{entry.get('title', '')}|{entry.get('published', '')}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def entry_payload(entry: Any) -> dict[str, Any]:
    """Extract the entry fields normalize cares about, JSON-serializable.

    feedparser aliases `updated` <-> `published` (and `_parsed` forms) with a
    DeprecationWarning when only one is present; that shim is library behavior,
    not ours, so we suppress it locally to keep output clean.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        payload: dict[str, Any] = {
            "title": entry.get("title"),
            "link": entry.get("link"),
            "id": entry.get("id"),
            "summary": entry.get("summary"),
            "published": entry.get("published"),
            "updated": entry.get("updated"),
            "authors": [a.get("name") for a in entry.get("authors", []) if a.get("name")],
            "tags": [t.get("term") for t in entry.get("tags", []) if t.get("term")],
        }
        for parsed_key, ts_key in (
            ("published_parsed", "published_timestamp"),
            ("updated_parsed", "updated_timestamp"),
        ):
            struct = entry.get(parsed_key)
            if struct is not None:
                payload[ts_key] = calendar.timegm(struct)
        content = entry.get("content")
        if content:
            payload["content"] = content[0].get("value")
    return {k: v for k, v in payload.items() if v not in (None, [], "")}
