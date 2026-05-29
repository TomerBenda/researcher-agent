"""RSS / Atom source adapter.

Fetches a feed through the shared polite client, honoring per-source ETag /
Last-Modified cursors for conditional GETs, and returns the parsed entries as
`RawItem`s. It does not normalize, classify, or dedupe — canonicalization and
entity extraction happen downstream in `normalize`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

import feedparser

from researcher_agent.http import PoliteClient
from researcher_agent.models import RawItem
from researcher_agent.sources.base import FetchResult, SourceConfig
from researcher_agent.sources.feed import entry_payload, external_id


class RssConfig(SourceConfig):
    type: Literal["rss"] = "rss"
    url: str


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
                external_id=external_id(entry),
                payload=entry_payload(entry),
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
