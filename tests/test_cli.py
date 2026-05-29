"""Tests for the CLI glue (argument handling, config loading, no network)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from researcher_agent.__main__ import (
    _load_agent_config,
    _maybe_post_process,
    _parse_window_dt,
    _safe_source_name,
    app,
)
from researcher_agent.config import load_agent_config
from researcher_agent.state import Database

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


@pytest.mark.parametrize("cmd", ["collect", "synthesize", "status", "validate-config"])
def test_help_runs(cmd: str) -> None:
    result = runner.invoke(app, [cmd, "--help"])
    assert result.exit_code == 0


# --- status -------------------------------------------------------------------


def test_status_no_db(tmp_path: Path) -> None:
    result = runner.invoke(app, ["status", "--db", str(tmp_path / "absent.db")])
    assert result.exit_code == 0
    assert "nothing collected" in result.output.lower()


def test_status_reports_sources_and_totals(tmp_path: Path) -> None:
    from researcher_agent.models import SourceRun

    db_path = tmp_path / "state.db"
    db = Database(db_path)
    db.upsert_source_run(
        SourceRun(
            source_name="rss:healthy",
            last_run_at=datetime.now(UTC),
            last_success_at=datetime.now(UTC),
        )
    )
    db.upsert_source_run(
        SourceRun(source_name="rss:broken", last_run_at=datetime.now(UTC), consecutive_error_runs=4)
    )
    db.close()
    result = runner.invoke(
        app, ["status", "--db", str(db_path), "--sources", str(tmp_path / "none.yaml")]
    )
    assert result.exit_code == 0
    assert "rss:healthy" in result.output
    assert "FAILING" in result.output
    assert "Items:" in result.output


def test_status_max_consecutive_errors_gate(tmp_path: Path) -> None:
    from researcher_agent.models import SourceRun

    db_path = tmp_path / "state.db"
    db = Database(db_path)
    db.upsert_source_run(
        SourceRun(source_name="rss:dead", last_run_at=datetime.now(UTC), consecutive_error_runs=8)
    )
    db.close()
    sources = tmp_path / "none.yaml"
    ok = runner.invoke(app, ["status", "--db", str(db_path), "--sources", str(sources)])
    assert ok.exit_code == 0  # no gate by default
    gated = runner.invoke(
        app,
        [
            "status",
            "--db",
            str(db_path),
            "--sources",
            str(sources),
            "--max-consecutive-errors",
            "5",
        ],
    )
    assert gated.exit_code == 1
    assert "UNHEALTHY" in gated.output


def test_status_redacts_names_in_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from researcher_agent.models import SourceRun

    monkeypatch.setenv("RESEARCHER_LOG_MODE", "production")
    db_path = tmp_path / "state.db"
    db = Database(db_path)
    db.upsert_source_run(SourceRun(source_name="rss:secret-feed", last_run_at=datetime.now(UTC)))
    db.close()
    result = runner.invoke(app, ["status", "--db", str(db_path)])
    assert "secret-feed" not in result.output


# --- validate-config ----------------------------------------------------------


def test_validate_config_ok(tmp_path: Path) -> None:
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        "sources:\n  - name: rss:a\n    type: rss\n    url: https://e.com/feed\n", encoding="utf-8"
    )
    agent = tmp_path / "agent.yaml"
    agent.write_text(AGENT_YAML, encoding="utf-8")
    result = runner.invoke(
        app, ["validate-config", "--sources", str(sources), "--agent", str(agent)]
    )
    assert result.exit_code == 0
    assert "config OK" in result.output


def test_validate_config_bad_source_errors(tmp_path: Path) -> None:
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        "sources:\n  - name: x\n    type: bogus\n    url: https://e.com\n", encoding="utf-8"
    )
    result = runner.invoke(
        app,
        ["validate-config", "--sources", str(sources), "--agent", str(tmp_path / "none.yaml")],
    )
    assert result.exit_code != 0
    assert "bogus" in result.output


# --- post-processing glue (no network) ----------------------------------------

AGENT_YAML = """
taxonomy:
  - slug: tooling
    description: tools
  - slug: other
    description: misc
"""


def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "state.db")


def test_load_agent_config_missing_returns_none(tmp_path: Path) -> None:
    assert _load_agent_config(tmp_path / "absent.yaml", warn_if_missing=False) is None


def test_load_agent_config_reads_file(tmp_path: Path) -> None:
    agent = tmp_path / "agent.yaml"
    agent.write_text(AGENT_YAML, encoding="utf-8")
    cfg = _load_agent_config(agent, warn_if_missing=False)
    assert cfg is not None
    assert cfg.taxonomy.slugs == {"tooling", "other"}


def test_post_process_none_config_returns_none(tmp_path: Path) -> None:
    db = _db(tmp_path)
    assert _maybe_post_process(db, config=None, now=datetime.now(UTC), vault=None) is None


def test_safe_source_name_redacts_in_production() -> None:
    assert _safe_source_name("rss:simon-willison", "dev") == "rss:simon-willison"
    redacted = _safe_source_name("rss:simon-willison", "production")
    assert "simon" not in redacted
    assert redacted.startswith("src#")


def test_post_process_no_api_key_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    agent = tmp_path / "agent.yaml"
    agent.write_text(AGENT_YAML, encoding="utf-8")  # provider defaults to gemini
    config = load_agent_config(agent)
    db = _db(tmp_path)
    out = _maybe_post_process(db, config=config, now=datetime.now(UTC), vault=None)
    assert out is None  # gracefully skipped, no crash
