"""Source adapters and the config-driven loader.

`load_adapters` reads a `sources.yaml`, dispatches each entry on its `type:` to
the matching config model + adapter class, and validates everything up front so
a malformed config fails at startup rather than mid-run.

The registry is an explicit dict — adding a source type in a later milestone is
one line here, no decorators or import-time magic.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from researcher_agent.sources.arxiv import ArxivAdapter, ArxivConfig
from researcher_agent.sources.base import (
    FetchResult,
    SourceAdapter,
    SourceConfig,
    SourceConfigError,
)
from researcher_agent.sources.github_releases import (
    GithubReleasesAdapter,
    GithubReleasesConfig,
)
from researcher_agent.sources.github_topic import GithubTopicAdapter, GithubTopicConfig
from researcher_agent.sources.hn_search import HnSearchAdapter, HnSearchConfig
from researcher_agent.sources.rss import RssAdapter, RssConfig

__all__ = [
    "FetchResult",
    "SourceAdapter",
    "SourceConfig",
    "SourceConfigError",
    "load_adapters",
]

_ADAPTERS: dict[str, tuple[type[SourceConfig], Callable[[Any], SourceAdapter]]] = {
    "rss": (RssConfig, RssAdapter),
    "arxiv": (ArxivConfig, ArxivAdapter),
    "hn_search": (HnSearchConfig, HnSearchAdapter),
    "github_releases": (GithubReleasesConfig, GithubReleasesAdapter),
    "github_topic": (GithubTopicConfig, GithubTopicAdapter),
}


def load_adapters(path: Path) -> list[SourceAdapter]:
    """Load and validate all adapters from a sources YAML file."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SourceConfigError("sources file must be a mapping with a 'sources' key")

    entries = raw.get("sources") or []
    if not isinstance(entries, list):
        raise SourceConfigError("'sources' must be a list")

    adapters: list[SourceAdapter] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SourceConfigError(f"source #{i} must be a mapping")

        stype = entry.get("type")
        if stype not in _ADAPTERS:
            known = ", ".join(sorted(_ADAPTERS)) or "(none)"
            raise SourceConfigError(
                f"unknown source type {stype!r} for source #{i}; known types: {known}"
            )

        config_cls, adapter_cls = _ADAPTERS[stype]
        try:
            config = config_cls(**entry)
        except ValidationError as exc:
            name = entry.get("name", f"#{i}")
            raise SourceConfigError(f"invalid config for source {name!r}: {exc}") from exc

        if config.name in seen:
            raise SourceConfigError(f"duplicate source name {config.name!r}")
        seen.add(config.name)
        adapters.append(adapter_cls(config))

    return adapters
