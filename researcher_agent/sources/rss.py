"""RSS / Atom source adapter.

Fetches a feed through the shared polite client, honoring per-source ETag /
Last-Modified cursors for conditional GETs, and returns the parsed entries as
`RawItem`s. It does not normalize, classify, or dedupe — canonicalization and
entity extraction happen downstream in `normalize`.
"""

from __future__ import annotations

import calendar
import hashlib
import warnings
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

import feedparser

from researcher_agent.http import PoliteClient
from researcher_agent.models import RawItem
from researcher_agent.sources.base import FetchResult, SourceConfig


class RssConfig(SourceConfig):
    type: Literal["rss"] = "rss"
    url: str


def _external_id(entry: Any) -> str:
    """Stable id for an entry: guid, else link, else a hash of title+date."""
    candidate = entry.get("id") or entry.get("link")
    if candidate:
        return str(candidate)
    seed = f"{entry.get('title', '')}|{entry.get('published', '')}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _entry_payload(entry: Any) -> dict[str, Any]:
    """Extract the entry fields normalize cares about, JSON-serializable.

    feedparser aliases `updated` <-> `published` (and their `_parsed` forms) with
    a DeprecationWarning when only one is present; that shim is library behavior,
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


class RssAdapter:
    """Polls one RSS/Atom feed."""

    def __init__(self, config: RssConfig) -> None:
        self.config = config

    def fetch(self, client: PoliteClient, cursor: Mapping[str, Any], now: datetime) -> FetchResult:
        headers: dict[str, str] = {}
        if cursor.get("etag"):
            headers["If-None-Match"] = str(cursor["etag"])
        if cursor.get("last_modified"):
            headers["If-Modified-Since"] = str(cursor["last_modified"])

        response = client.get(self.config.url, headers=headers or None)
        if response.status_code == 304:
            return FetchResult(raw_items=[], cursor=dict(cursor), not_modified=True)
        response.raise_for_status()

        parsed = feedparser.parse(response.content)
        raw_items = [
            RawItem(
                source_name=self.config.name,
                source_type="rss",
                external_id=_external_id(entry),
                payload=_entry_payload(entry),
                fetched_at=now,
            )
            for entry in parsed.entries
        ]

        new_cursor = dict(cursor)
        if response.headers.get("ETag"):
            new_cursor["etag"] = response.headers["ETag"]
        if response.headers.get("Last-Modified"):
            new_cursor["last_modified"] = response.headers["Last-Modified"]

        return FetchResult(raw_items=raw_items, cursor=new_cursor)
