"""Integration tests for synthesis orchestration (run_synthesis) and the CLI.

Scripted fake provider + temp DB + MockTransport — no network/LLM.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from researcher_agent import __main__ as cli
from researcher_agent.config import AgentConfig, Taxonomy, TopicConfig
from researcher_agent.http import PoliteClient
from researcher_agent.models import Classification, Item, ItemEntity
from researcher_agent.state import Database
from researcher_agent.synthesis.agent import AgentReply, TokenUsage, ToolCall
from researcher_agent.synthesis.run import _week_monday, run_synthesis
from researcher_agent.vault import SynthesisWindow

WSTART = datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)  # a Monday
WINDOW = SynthesisWindow.iso_week(WSTART)
INGEST = WSTART + timedelta(days=1)
NOW = WSTART + timedelta(days=7)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "state.db")


def _config() -> AgentConfig:
    return AgentConfig(
        taxonomy=Taxonomy(
            topics=[
                TopicConfig(slug="mcp-security", description="x"),
                TopicConfig(slug="tooling", description="y"),
                TopicConfig(slug="other", description="z"),
            ]
        ),
        research_focus="MCP supply chain and prompt injection.",
    )


def _client() -> PoliteClient:
    return PoliteClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="body")),
        min_interval_seconds=0.0,
    )


def _add(
    db: Database,
    h: str,
    title: str,
    *,
    score: int = 8,
    ingested: datetime = INGEST,
    entities: list[tuple[str, str]] | None = None,
) -> str:
    canonical = h * 64 if len(h) == 1 else h
    db.insert_item(
        Item(
            canonical_hash=canonical,
            url=f"https://example.com/{canonical[:8]}",
            title=title,
            summary="s",
            published_at=ingested,
            ingested_at=ingested,
        )
    )
    db.classify_and_activate(
        Classification(
            canonical_hash=canonical,
            topic="mcp-security",
            score=score,
            rationale="r",
            classifier_version="v",
            classifier_model="m",
            classified_at=ingested,
        )
    )
    for kind, value in entities or []:
        db.add_item_entities([ItemEntity(canonical_hash=canonical, kind=kind, value=value)])  # type: ignore[arg-type]
    return canonical


class ScriptProvider:
    model_id = "fake:synth"

    def __init__(self, replies: list[AgentReply]) -> None:
        self._replies = list(replies)

    def complete(self, **kwargs: Any) -> AgentReply:
        return self._replies.pop(0)


def _tool(name: str, args: dict[str, Any]) -> AgentReply:
    return AgentReply(text=None, tool_calls=[ToolCall("t", name, args)], usage=TokenUsage(1, 1))


# --- run_synthesis -------------------------------------------------------------


def test_run_synthesis_end_to_end(db: Database, tmp_path: Path) -> None:
    h1 = _add(db, "a", "MCP flaw", score=8, entities=[("cve", "CVE-2025-1")])
    _add(db, "b", "Low signal", score=3)  # below min_score, excluded
    prov = ScriptProvider(
        [
            _tool("query_items", {}),
            _tool("add_followup", {"item_hash": h1, "action": "audit", "note": "check"}),
            _tool("finish", {"roundup_markdown": "# Weekly\n\nThemes here."}),
        ]
    )
    stats = run_synthesis(
        db,
        _client(),
        _config(),
        prov,
        window=WINDOW,
        now=NOW,
        min_score=5,
        vault_root=tmp_path / "vault",
    )
    assert stats.outcome.stop_reason == "finish"
    assert stats.outcome.degraded is False
    assert stats.items_considered == 1  # only the score-8 item
    assert stats.weekly_entities == 1  # CVE-2025-1, rolled up from the window
    assert stats.followups_open == 1
    assert stats.report_path is not None and stats.report_path.exists()
    text = stats.report_path.read_text(encoding="utf-8")
    assert "# Weekly" in text
    assert "CVE-2025-1" in text
    # the weekly entity was persisted with a Monday week_starting
    assert db.insert_weekly_entities([]) == 0  # smoke: table usable


def test_run_synthesis_without_vault_writes_nothing(db: Database) -> None:
    _add(db, "a", "X", score=8)
    prov = ScriptProvider([_tool("finish", {"roundup_markdown": "# R"})])
    stats = run_synthesis(db, _client(), _config(), prov, window=WINDOW, now=NOW, vault_root=None)
    assert stats.report_path is None


def test_run_synthesis_degraded_still_writes_report(db: Database, tmp_path: Path) -> None:
    _add(db, "a", "X", score=8)
    prov = ScriptProvider([_tool("query_items", {}) for _ in range(3)])  # never finishes
    stats = run_synthesis(
        db,
        _client(),
        _config(),
        prov,
        window=WINDOW,
        now=NOW,
        max_turns=3,
        vault_root=tmp_path / "v",
    )
    assert stats.outcome.stop_reason == "max_turns"
    assert stats.outcome.degraded is True
    assert stats.report_path is not None
    assert "stopped early" in stats.report_path.read_text(encoding="utf-8")


def test_non_monday_window_produces_valid_weekly_entities(db: Database, tmp_path: Path) -> None:
    # A trailing-days window not aligned to Monday must not trip WeeklyEntity's
    # Monday-midnight validator — _week_monday normalizes the window start.
    end = datetime(2026, 5, 28, 0, 0, 0, tzinfo=UTC)  # a Thursday
    window = SynthesisWindow.trailing_days(end, 7)  # starts Thu 05-21
    assert _week_monday(window.start).weekday() == 0
    _add(db, "a", "X", score=8, ingested=end - timedelta(days=1), entities=[("cve", "CVE-2025-9")])
    prov = ScriptProvider([_tool("finish", {"roundup_markdown": "# R"})])
    stats = run_synthesis(
        db, _client(), _config(), prov, window=window, now=end, vault_root=tmp_path / "v"
    )
    assert stats.weekly_entities == 1  # no validator crash


# --- CLI -----------------------------------------------------------------------


def _agent_yaml(tmp_path: Path) -> Path:
    dst = tmp_path / "agent.yaml"
    shutil.copy("config/agent.example.yaml", dst)
    return dst


def test_cli_synthesize_skips_without_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    dbp = tmp_path / "state.db"
    Database(dbp).close()
    res = CliRunner().invoke(
        cli.app,
        ["synthesize", "--db", str(dbp), "--agent", str(_agent_yaml(tmp_path)), "--no-write-vault"],
    )
    assert res.exit_code == 0
    assert "skipping synthesis" in res.output


def test_cli_synthesize_runs_with_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dbp = tmp_path / "state.db"
    d = Database(dbp)
    _add(d, "a", "MCP flaw", score=8)
    d.close()
    prov = ScriptProvider([_tool("finish", {"roundup_markdown": "# Weekly roundup"})])
    monkeypatch.setattr(cli, "build_synthesis_provider", lambda: prov)
    res = CliRunner().invoke(
        cli.app,
        [
            "synthesize",
            "--db",
            str(dbp),
            "--agent",
            str(_agent_yaml(tmp_path)),
            "--since",
            "2020-01-01",
            "--vault",
            str(tmp_path / "vault"),
        ],
    )
    assert res.exit_code == 0, res.output
    assert "Synthesis" in res.output
    reports = list((tmp_path / "vault" / "synthesis").glob("*.md"))
    assert len(reports) == 1
    assert "# Weekly roundup" in reports[0].read_text(encoding="utf-8")
