"""GitHub releases source adapter (REST, raw httpx via the polite client).

Polls `/repos/{owner}/{repo}/releases`, honoring an ETag cursor for conditional
GETs. Draft releases are skipped. Normalization happens downstream.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from researcher_agent.http import PoliteClient
from researcher_agent.models import RawItem
from researcher_agent.sources.base import FetchResult, SourceConfig
from researcher_agent.sources.github import API_BASE, github_headers, iso_to_epoch


class GithubReleasesConfig(SourceConfig):
    type: Literal["github_releases"] = "github_releases"
    repo: str  # "owner/name"
    per_page: int = 30


def _release_payload(release: dict[str, Any], repo: str) -> dict[str, Any]:
    tag = release.get("tag_name") or release.get("name") or ""
    author = release.get("author")
    payload: dict[str, Any] = {
        "link": release.get("html_url"),
        "title": f"{repo} {tag}".strip(),
        "summary": release.get("body"),
        "repo": repo,
        "tag": release.get("tag_name"),
        "author": author.get("login") if isinstance(author, dict) else None,
    }
    epoch = iso_to_epoch(release.get("published_at"))
    if epoch is not None:
        payload["published_timestamp"] = epoch
    return {k: v for k, v in payload.items() if v not in (None, "")}


class GithubReleasesAdapter:
    """Polls one repo's releases."""

    def __init__(self, config: GithubReleasesConfig) -> None:
        self.config = config

    def fetch(self, client: PoliteClient, cursor: Mapping[str, Any], now: datetime) -> FetchResult:
        url = f"{API_BASE}/repos/{self.config.repo}/releases?per_page={self.config.per_page}"
        etag = cursor.get("etag")
        response = client.get(
            url, headers=github_headers(etag=etag if isinstance(etag, str) else None)
        )
        if response.status_code == 304:
            return FetchResult(raw_items=[], cursor=dict(cursor), not_modified=True)
        response.raise_for_status()

        releases = response.json()
        if not isinstance(releases, list):
            releases = []  # unexpected 200 shape (e.g. an error object) -> no items
        raw_items: list[RawItem] = []
        for release in releases:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            external = str(release.get("id") or release.get("html_url") or "")
            if not external:
                continue
            raw_items.append(
                RawItem(
                    source_name=self.config.name,
                    source_type="github_releases",
                    external_id=external,
                    payload=_release_payload(release, self.config.repo),
                    fetched_at=now,
                )
            )

        new_cursor = dict(cursor)
        if response.headers.get("ETag"):
            new_cursor["etag"] = response.headers["ETag"]
        return FetchResult(raw_items=raw_items, cursor=new_cursor)
