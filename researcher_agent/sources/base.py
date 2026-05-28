"""Source-adapter contract: config models, fetch result, and the Protocol.

Each concrete adapter declares its own `SourceConfig` subclass (so config is
validated at startup, not mid-run) and implements `fetch`. The loader in
`researcher_agent.sources` maps a `type:` string to the right config + adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from researcher_agent.http import PoliteClient
from researcher_agent.models import RawItem, SourceType


class SourceConfigError(Exception):
    """Raised when a sources config file is malformed or references a bad type."""


class SourceConfig(BaseModel):
    """Base config shared by every source. Subclasses add their own fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    type: SourceType


@dataclass(frozen=True)
class FetchResult:
    """What an adapter returns from one fetch.

    - `raw_items`: items pulled this run (may be empty).
    - `cursor`: the cursor to persist for next run (e.g. ETag / Last-Modified).
    - `not_modified`: True when the source reported no change (HTTP 304); the
      cursor is unchanged and `raw_items` is empty.
    """

    raw_items: list[RawItem]
    cursor: dict[str, Any] = field(default_factory=dict)
    not_modified: bool = False


@runtime_checkable
class SourceAdapter(Protocol):
    """A source adapter: holds its config and fetches raw items."""

    @property
    def config(self) -> SourceConfig:
        """The adapter's validated config (a SourceConfig subclass)."""
        ...

    def fetch(
        self, client: PoliteClient, cursor: Mapping[str, Any], now: datetime
    ) -> FetchResult: ...
