"""GitHub topic-search source adapter (REST search API, raw httpx).

Searches repositories by topic (and a minimum star count), newest-pushed first,
and keeps a `pushed_at` high-watermark cursor so unchanged repos aren't re-emitted
every run. Normalization happens downstream.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlencode

from researcher_agent.http import PoliteClient
from researcher_agent.models import RawItem
from researcher_agent.sources.base import FetchResult, SourceConfig
from researcher_agent.sources.github import API_BASE, github_headers, iso_to_epoch


class GithubTopicConfig(SourceConfig):
    type: Literal["github_topic"] = "github_topic"
    topic: str
    min_stars: int = 0
    per_page: int = 30


def _repo_payload(repo: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "link": repo.get("html_url"),
        "title": repo.get("full_name"),
        "summary": repo.get("description"),
        "repo": repo.get("full_name"),
        "stars": repo.get("stargazers_count"),
    }
    epoch = iso_to_epoch(repo.get("pushed_at") or repo.get("updated_at"))
    if epoch is not None:
        payload["published_timestamp"] = epoch
    return {k: v for k, v in payload.items() if v not in (None, "")}


class GithubTopicAdapter:
    """Searches repositories by topic."""

    def __init__(self, config: GithubTopicConfig) -> None:
        self.config = config

    def fetch(self, client: PoliteClient, cursor: Mapping[str, Any], now: datetime) -> FetchResult:
        query = f"topic:{self.config.topic} stars:>={self.config.min_stars}"
        params = urlencode(
            {"q": query, "sort": "updated", "order": "desc", "per_page": self.config.per_page}
        )
        response = client.get(f"{API_BASE}/search/repositories?{params}", headers=github_headers())
        response.raise_for_status()
        body = response.json()
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            items = []  # unexpected 200 shape -> no items

        watermark = cursor.get("last_pushed_epoch")
        watermark = watermark if isinstance(watermark, int) else 0
        new_watermark = watermark
        raw_items: list[RawItem] = []
        for repo in items:
            if not isinstance(repo, dict):
                continue
            external = str(repo.get("id") or repo.get("full_name") or "")
            if not external:
                continue
            pushed = iso_to_epoch(repo.get("pushed_at") or repo.get("updated_at"))
            if pushed is not None and pushed <= watermark:
                continue  # unchanged since last run
            raw_items.append(
                RawItem(
                    source_name=self.config.name,
                    source_type="github_topic",
                    external_id=external,
                    payload=_repo_payload(repo),
                    fetched_at=now,
                )
            )
            if pushed is not None:
                new_watermark = max(new_watermark, pushed)

        new_cursor = dict(cursor)
        new_cursor["last_pushed_epoch"] = new_watermark
        return FetchResult(raw_items=raw_items, cursor=new_cursor)
