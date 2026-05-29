"""Shared helpers for the GitHub adapters (releases + topic search).

Uses the REST API directly via the polite client (no PyGithub). A GITHUB_TOKEN
in the environment is used when present (raises the rate limit and is the
default token in GitHub Actions), but every endpoint here works unauthenticated.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

API_BASE = "https://api.github.com"


def github_headers(*, etag: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if etag:
        headers["If-None-Match"] = etag
    return headers


def iso_to_epoch(value: object) -> int | None:
    """Parse a GitHub ISO-8601 timestamp (e.g. '2026-05-20T00:00:00Z') to epoch."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())
