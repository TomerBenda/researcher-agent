"""arXiv source adapter.

Queries the arXiv export API (Atom) using the caller's native arXiv query string
and returns the entries as `RawItem`s. The response is Atom, so it reuses the
shared feedparser helpers; normalization happens downstream in `normalize`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlencode

import feedparser

from researcher_agent.http import PoliteClient
from researcher_agent.models import RawItem
from researcher_agent.sources.base import FetchResult, SourceConfig
from researcher_agent.sources.feed import entry_payload, external_id


class ArxivConfig(SourceConfig):
    type: Literal["arxiv"] = "arxiv"
    query: str  # native arXiv query syntax, e.g. 'cat:cs.CR AND abs:"prompt injection"'
    max_results: int = 50


class ArxivAdapter:
    """Queries one arXiv search and returns the matching papers."""

    BASE_URL = "https://export.arxiv.org/api/query"

    def __init__(self, config: ArxivConfig) -> None:
        self.config = config

    def fetch(self, client: PoliteClient, cursor: Mapping[str, Any], now: datetime) -> FetchResult:
        params = urlencode(
            {
                "search_query": self.config.query,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": self.config.max_results,
            }
        )
        response = client.get(f"{self.BASE_URL}?{params}")
        response.raise_for_status()

        parsed = feedparser.parse(response.content)
        raw_items = [
            RawItem(
                source_name=self.config.name,
                source_type="arxiv",
                external_id=external_id(entry),
                payload=entry_payload(entry),
                fetched_at=now,
            )
            for entry in parsed.entries
        ]
        # arXiv has no conditional-GET; idempotent storage handles re-fetches.
        return FetchResult(raw_items=raw_items, cursor=dict(cursor))
