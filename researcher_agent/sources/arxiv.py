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

# arXiv's export API answers a malformed query / transient fault with HTTP 200
# and an Atom feed containing a single sentinel "error" entry (id/link under
# arxiv.org/api/errors). Treat those as zero results so we never store, classify,
# or render the error notice as if it were a paper.
_ERROR_MARKER = "arxiv.org/api/errors"


def _is_error_entry(entry: Any) -> bool:
    ident = str(entry.get("id") or "")
    link = str(entry.get("link") or "")
    return _ERROR_MARKER in ident or _ERROR_MARKER in link


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
            if not _is_error_entry(entry)
        ]
        # arXiv has no conditional-GET; idempotent storage handles re-fetches.
        return FetchResult(raw_items=raw_items, cursor=dict(cursor))
