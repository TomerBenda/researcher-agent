"""Tests for the config-driven source loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from researcher_agent.sources import SourceConfigError, load_adapters
from researcher_agent.sources.rss import RssAdapter


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "sources.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_rss_adapters(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        sources:
          - name: rss:a
            type: rss
            url: https://a.example.com/feed.xml
          - name: rss:b
            type: rss
            url: https://b.example.com/atom.xml
        """,
    )
    adapters = load_adapters(path)
    assert len(adapters) == 2
    assert all(isinstance(a, RssAdapter) for a in adapters)
    assert adapters[0].config.name == "rss:a"
    assert adapters[0].config.url == "https://a.example.com/feed.xml"


def test_unknown_type_errors_at_load(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        sources:
          - name: x
            type: telepathy
            url: https://example.com
        """,
    )
    with pytest.raises(SourceConfigError, match="telepathy"):
        load_adapters(path)


def test_missing_required_field_errors_at_load(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        sources:
          - name: rss:a
            type: rss
        """,
    )
    with pytest.raises(SourceConfigError, match="rss:a"):
        load_adapters(path)


def test_extra_field_errors_at_load(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        sources:
          - name: rss:a
            type: rss
            url: https://example.com/feed
            surprise: 1
        """,
    )
    with pytest.raises(SourceConfigError):
        load_adapters(path)


def test_duplicate_names_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        sources:
          - name: rss:a
            type: rss
            url: https://a.example.com/feed
          - name: rss:a
            type: rss
            url: https://b.example.com/feed
        """,
    )
    with pytest.raises(SourceConfigError, match="duplicate"):
        load_adapters(path)


def test_empty_sources_returns_empty_list(tmp_path: Path) -> None:
    path = _write(tmp_path, "sources: []\n")
    assert load_adapters(path) == []


def test_missing_sources_key_returns_empty_list(tmp_path: Path) -> None:
    path = _write(tmp_path, "other: 1\n")
    assert load_adapters(path) == []
