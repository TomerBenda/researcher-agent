"""Synthesis orchestration: load the window, run the agent, render the report.

This is the CLI seam (mirrors `collect.post_process`). It loads the window's
classified items, runs the tool-using agent over them, derives the week's
`WeeklyEntity` roll-up deterministically from the stored `ItemEntity` rows
(invariant #4 — the agent never invents entities), persists the agent's outputs
(followups were written by the tool; weekly entities here), and renders the
synthesis report to the vault.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from researcher_agent.config import AgentConfig
from researcher_agent.http import PoliteClient
from researcher_agent.models import Item, WeeklyEntity
from researcher_agent.prompts import load_prompt, render_system_prompt
from researcher_agent.state import Database
from researcher_agent.synthesis.agent import AgentOutcome, SynthesisProvider, run_agent
from researcher_agent.synthesis.tools import SynthesisTools, tool_specs
from researcher_agent.vault import SynthesisWindow, write_synthesis_report


@dataclass(frozen=True)
class SynthesisStats:
    """Result of one synthesis run."""

    outcome: AgentOutcome
    items_considered: int
    weekly_entities: int
    followups_open: int
    report_path: Path | None


def _week_monday(dt: datetime) -> datetime:
    """Midnight UTC on the Monday of `dt`'s ISO week (WeeklyEntity.week_starting)."""
    day = dt.astimezone(UTC)
    monday = day - timedelta(days=day.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=UTC)


def _window_items(db: Database, window: SynthesisWindow, min_score: int) -> list[Item]:
    """Non-superseded, classified items in the window with score >= min_score."""
    items: list[Item] = []
    for item in db.list_items_in_window(window.start, window.end):
        active = db.get_active_classification(item.canonical_hash)
        if active is not None and active.score >= min_score:
            items.append(item)
    return items


def _initial_user_message(db: Database, items: Sequence[Item], window: SynthesisWindow) -> str:
    """The agent's starting context: the window's items as a compact JSON array."""
    payload = []
    for item in items:
        active = db.get_active_classification(item.canonical_hash)
        payload.append(
            {
                "item_hash": item.canonical_hash,
                "title": item.title,
                "url": item.url,
                "summary": item.summary,
                "topic": active.topic if active else None,
                "secondary_topics": active.secondary_topics if active else [],
                "score": active.score if active else None,
                "published_at": (
                    item.published_at.isoformat() if item.published_at is not None else None
                ),
            }
        )
    header = (
        f"Window {window.label} ({window.start.date()} to {window.end.date()}). "
        f"{len(payload)} classified item(s):"
    )
    return f"{header}\n{json.dumps(payload, ensure_ascii=False)}"


def _derive_weekly_entities(
    db: Database, items: Sequence[Item], *, week_starting: datetime
) -> list[WeeklyEntity]:
    """Roll the window items' stored entities up into per-week entity records.

    Deterministic: one record per (kind, value), with the related item hashes
    sorted and the first non-empty context kept. The agent reads entities via a
    tool but never produces these — they are a function of normalize-time output.
    """
    contexts: dict[tuple[str, str], str] = {}
    hashes: dict[tuple[str, str], set[str]] = {}
    for item in items:
        for entity in db.get_item_entities(item.canonical_hash):
            key = (entity.kind, entity.value)
            hashes.setdefault(key, set()).add(item.canonical_hash)
            if not contexts.get(key) and entity.context:
                contexts[key] = entity.context
    return [
        WeeklyEntity(
            week_starting=week_starting,
            kind=kind,  # type: ignore[arg-type]  # kind came from a stored ItemEntity (valid EntityKind)
            value=value,
            context=contexts.get((kind, value)) or value,
            related_item_hashes=sorted(hashes[(kind, value)]),
        )
        for (kind, value) in sorted(hashes)
    ]


def run_synthesis(
    db: Database,
    client: PoliteClient,
    config: AgentConfig,
    provider: SynthesisProvider,
    *,
    window: SynthesisWindow,
    now: datetime,
    min_score: int = 5,
    max_turns: int = 20,
    token_budget: int | None = None,
    vault_root: Path | None = None,
) -> SynthesisStats:
    """Run the synthesis agent over `window` and render its report."""
    items = _window_items(db, window, min_score)
    system_prompt = render_system_prompt(
        load_prompt("synthesize"),
        taxonomy=config.taxonomy.render(),
        research_focus=config.research_focus,
    )
    tools = SynthesisTools(db, client, window=window, now=now, min_score=min_score)
    outcome = run_agent(
        provider,
        tools,
        system_prompt=system_prompt,
        initial_user_text=_initial_user_message(db, items, window),
        tool_specs=tool_specs(),
        max_turns=max_turns,
        token_budget=token_budget,
    )

    weekly = _derive_weekly_entities(db, items, week_starting=_week_monday(window.start))
    db.insert_weekly_entities(weekly)
    followups = db.list_followups(open_only=True)

    report_path = (
        write_synthesis_report(window, outcome.roundup, weekly, followups, vault_root)
        if vault_root is not None
        else None
    )
    return SynthesisStats(
        outcome=outcome,
        items_considered=len(items),
        weekly_entities=len(weekly),
        followups_open=len(followups),
        report_path=report_path,
    )
