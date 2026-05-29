"""Hacker News search adapter (Algolia HN Search API, no auth).

Uses `search_by_date` so results are time-ordered, and keeps a `created_at_i`
high-watermark cursor to only pull items newer than the last run.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlencode

from researcher_agent.http import PoliteClient
from researcher_agent.models import RawItem
from researcher_agent.sources.base import FetchResult, SourceConfig

_ITEM_URL = "https://news.ycombinator.com/item?id={}"


class HnSearchConfig(SourceConfig):
    type: Literal["hn_search"] = "hn_search"
    query: str
    tags: str = "story"
    hits_per_page: int = 50


def _hn_payload(hit: dict[str, Any]) -> dict[str, Any]:
    object_id = str(hit.get("objectID", ""))
    hn_url = _ITEM_URL.format(object_id)
    payload: dict[str, Any] = {
        "title": hit.get("title") or hit.get("story_title"),
        # prefer the linked article; fall back to the HN discussion for text posts
        "link": hit.get("url") or hn_url,
        "hn_url": hn_url,
        "story_text": hit.get("story_text"),
        "author": hit.get("author"),
        "points": hit.get("points"),
        "num_comments": hit.get("num_comments"),
    }
    created = hit.get("created_at_i")
    if isinstance(created, int | float) and not isinstance(created, bool):
        payload["published_timestamp"] = int(created)
    return {k: v for k, v in payload.items() if v is not None}


class HnSearchAdapter:
    """Searches Hacker News via Algolia."""

    BASE_URL = "https://hn.algolia.com/api/v1/search_by_date"

    def __init__(self, config: HnSearchConfig) -> None:
        self.config = config

    def fetch(self, client: PoliteClient, cursor: Mapping[str, Any], now: datetime) -> FetchResult:
        params: dict[str, Any] = {
            "query": self.config.query,
            "tags": self.config.tags,
            "hitsPerPage": self.config.hits_per_page,
        }
        watermark = cursor.get("last_created_at_i")
        if isinstance(watermark, int):
            # Strict `>` is safe here (unlike the GitHub-topic pushed_at watermark):
            # created_at_i is the immutable submission time and results come back
            # newest-first, so any item sharing the watermark second was already in
            # the same fetch as the watermark-setting item — never a later run.
            params["numericFilters"] = f"created_at_i>{watermark}"

        response = client.get(f"{self.BASE_URL}?{urlencode(params)}")
        response.raise_for_status()
        body = response.json()
        hits = body.get("hits") if isinstance(body, dict) else None
        if not isinstance(hits, list):
            hits = []  # unexpected 200 shape -> no items

        raw_items: list[RawItem] = []
        new_watermark = watermark if isinstance(watermark, int) else 0
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            object_id = str(hit.get("objectID", ""))
            if not object_id:
                continue
            raw_items.append(
                RawItem(
                    source_name=self.config.name,
                    source_type="hn_search",
                    external_id=object_id,
                    payload=_hn_payload(hit),
                    fetched_at=now,
                )
            )
            created = hit.get("created_at_i")
            if isinstance(created, int):
                new_watermark = max(new_watermark, created)

        new_cursor = dict(cursor)
        new_cursor["last_created_at_i"] = new_watermark
        return FetchResult(raw_items=raw_items, cursor=new_cursor)
