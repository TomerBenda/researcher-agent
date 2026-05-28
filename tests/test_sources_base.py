"""Tests for the source-adapter base: config models, FetchResult, Protocol."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from researcher_agent.http import PoliteClient
from researcher_agent.models import RawItem
from researcher_agent.sources.base import FetchResult, SourceAdapter, SourceConfig


class _TinyConfig(SourceConfig):
    type: str = "rss"
    url: str


class _TinyAdapter:
    def __init__(self, config: _TinyConfig) -> None:
        self.config = config

    def fetch(self, client: PoliteClient, cursor: dict, now: datetime) -> FetchResult:
        return FetchResult(raw_items=[], cursor=cursor)


def test_source_config_requires_name_and_type() -> None:
    with pytest.raises(ValidationError):
        SourceConfig(type="rss")  # type: ignore[call-arg]


def test_source_config_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SourceConfig(name="x", type="rss", bogus=1)  # type: ignore[call-arg]


def test_source_config_is_frozen() -> None:
    cfg = SourceConfig(name="rss:a", type="rss")
    with pytest.raises(ValidationError):
        cfg.name = "other"  # type: ignore[misc]


def test_subclass_config_adds_fields() -> None:
    cfg = _TinyConfig(name="rss:a", url="https://example.com/feed")
    assert cfg.url == "https://example.com/feed"
    assert cfg.type == "rss"


def test_fetch_result_defaults() -> None:
    result = FetchResult(raw_items=[], cursor={})
    assert result.not_modified is False
    assert result.raw_items == []


def test_fetch_result_carries_items() -> None:
    raw = RawItem(
        source_name="rss:a",
        source_type="rss",
        external_id="g1",
        payload={"title": "t"},
        fetched_at=datetime(2026, 5, 27, tzinfo=UTC),
    )
    result = FetchResult(raw_items=[raw], cursor={"etag": '"x"'}, not_modified=False)
    assert result.raw_items[0].external_id == "g1"
    assert result.cursor == {"etag": '"x"'}


def test_adapter_satisfies_protocol() -> None:
    adapter = _TinyAdapter(_TinyConfig(name="rss:a", url="https://example.com/feed"))
    assert isinstance(adapter, SourceAdapter)


def test_non_adapter_fails_protocol() -> None:
    class NotAnAdapter:
        pass

    assert not isinstance(NotAnAdapter(), SourceAdapter)
