"""Tests for the CLI glue (argument handling, config loading, no network)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from researcher_agent.__main__ import _parse_window_dt, app

runner = CliRunner()


def test_parse_window_dt_date_only_is_utc() -> None:
    dt = _parse_window_dt("2026-05-01")
    assert dt == datetime(2026, 5, 1, tzinfo=UTC)


def test_parse_window_dt_preserves_offset() -> None:
    dt = _parse_window_dt("2026-05-01T12:00:00+00:00")
    assert dt == datetime(2026, 5, 1, 12, tzinfo=UTC)


def test_collect_missing_sources_file_errors(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["collect", "--sources", str(tmp_path / "nope.yaml"), "--db", str(tmp_path / "s.db")],
    )
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_collect_empty_sources_is_noop(tmp_path: Path) -> None:
    sources = tmp_path / "sources.yaml"
    sources.write_text("sources: []\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["collect", "--sources", str(sources), "--db", str(tmp_path / "s.db")],
    )
    assert result.exit_code == 0
    assert "no sources" in result.output.lower()


def test_collect_bad_since_errors(tmp_path: Path) -> None:
    sources = tmp_path / "sources.yaml"
    sources.write_text("sources: []\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "collect",
            "--sources",
            str(sources),
            "--db",
            str(tmp_path / "s.db"),
            "--since",
            "not-a-date",
        ],
    )
    assert result.exit_code != 0


def test_collect_unknown_source_type_errors(tmp_path: Path) -> None:
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        "sources:\n  - name: x\n    type: bogus\n    url: https://e.com\n", encoding="utf-8"
    )
    result = runner.invoke(
        app,
        ["collect", "--sources", str(sources), "--db", str(tmp_path / "s.db")],
    )
    assert result.exit_code != 0
    assert "bogus" in result.output


def test_collect_source_filter_no_match_is_noop(tmp_path: Path) -> None:
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        "sources:\n  - name: rss:a\n    type: rss\n    url: https://e.com/feed\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "collect",
            "--sources",
            str(sources),
            "--db",
            str(tmp_path / "s.db"),
            "--source",
            "rss:does-not-exist",
        ],
    )
    assert result.exit_code == 0
    assert "no sources" in result.output.lower()


def test_synthesize_still_stub(tmp_path: Path) -> None:
    result = runner.invoke(app, ["synthesize"])
    assert result.exit_code != 0


@pytest.mark.parametrize("cmd", ["collect", "synthesize"])
def test_help_runs(cmd: str) -> None:
    result = runner.invoke(app, [cmd, "--help"])
    assert result.exit_code == 0
