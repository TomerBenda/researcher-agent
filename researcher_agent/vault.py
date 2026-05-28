"""Vault writer: render collection reports and synthesis reports to markdown files.

Two output kinds, both deterministic:
- **Collection report** — what the collect step gathered/classified in a window.
  Default cadence is daily but the renderer doesn't know about cadence; it just
  takes entries and a report date.
- **Synthesis report** — what the synthesize step (LLM agent) produced over a
  window. The window's start/end and a human-readable label are passed in via
  `SynthesisWindow`, decoupling content from any specific cadence.

Design notes:
- Rendering is a pure function of its inputs — no `now()` calls, no random
  ordering. Re-rendering the same data produces byte-identical output. This is
  required because the inbox is a git repo and idempotent renders mean re-runs
  produce no diff (= no noise commits).
- Writes are atomic via tempfile + os.replace, so a crash mid-write never
  leaves a half-written report.
- Two version numbers live in frontmatter:
    schema_version    — bump when frontmatter field names/values change (Dataview contract)
    renderer_version  — bump when body markdown structure changes
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from researcher_agent.models import (
    Classification,
    Followup,
    Item,
    ItemSource,
    WeeklyEntity,
)

SCHEMA_VERSION = 2
RENDERER_VERSION = 1


# --- input shapes --------------------------------------------------------------


@dataclass(frozen=True)
class CollectionEntry:
    """One entry in a collection report: item + its active classification + sources."""

    item: Item
    classification: Classification
    sources: list[ItemSource]


@dataclass(frozen=True)
class SynthesisWindow:
    """A time window over which synthesis was performed.

    `label` is the stable identifier used in filenames and headings. Use the
    classmethod constructors below for standard label shapes.
    """

    start: datetime
    end: datetime
    label: str

    @classmethod
    def iso_week(cls, monday: datetime) -> SynthesisWindow:
        """A 7-day window starting on the given Monday (midnight UTC)."""
        iso = monday.isocalendar()
        return cls(
            start=monday,
            end=monday + timedelta(days=7),
            label=f"W{iso.year}-W{iso.week:02d}",
        )

    @classmethod
    def trailing_days(cls, end: datetime, days: int) -> SynthesisWindow:
        """The N days ending at `end` (exclusive)."""
        return cls(
            start=end - timedelta(days=days),
            end=end,
            label=f"D{end.date().isoformat()}-{days}d",
        )

    @classmethod
    def date_range(cls, start: datetime, end: datetime) -> SynthesisWindow:
        """An arbitrary [start, end) range."""
        return cls(
            start=start,
            end=end,
            label=f"R{start.date().isoformat()}-to-{end.date().isoformat()}",
        )


# --- helpers -------------------------------------------------------------------


def _yaml_block(data: dict[str, object]) -> str:
    """Render a YAML frontmatter block (between `---` fences)."""
    body = yaml.safe_dump(
        data,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
    )
    return f"---\n{body}---\n"


def _atomic_write(path: Path, content: str) -> None:
    """Write `content` to `path` atomically.

    Writes to a sibling tempfile in the same directory, then os.replace to the
    target. A crash mid-write leaves either the old file intact or no file —
    never a half-written one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # best-effort cleanup
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _topic_sort_key(entries: Sequence[CollectionEntry]) -> tuple[int, str]:
    """Sort key for a topic group: highest-score first, then alpha by topic."""
    max_score = max((e.classification.score for e in entries), default=0)
    topic = entries[0].classification.topic if entries else ""
    return (-max_score, topic)


def _entry_sort_key(entry: CollectionEntry) -> tuple[int, float, str]:
    """Sort key within a topic group.

    Score desc, then published_at desc (None last), then canonical_hash asc
    for a fully deterministic tie-break.
    """
    pub_ts = entry.item.published_at.timestamp() if entry.item.published_at else 0.0
    has_pub = 0 if entry.item.published_at is not None else 1
    return (
        -entry.classification.score,
        has_pub * 10**12 + (-pub_ts),
        entry.item.canonical_hash,
    )


def _group_by_topic(entries: Iterable[CollectionEntry]) -> dict[str, list[CollectionEntry]]:
    groups: dict[str, list[CollectionEntry]] = {}
    for e in entries:
        groups.setdefault(e.classification.topic, []).append(e)
    return groups


def _format_secondary_topics(secs: Sequence[str]) -> str:
    if not secs:
        return ""
    return " · also " + ", ".join(sorted(secs))


def _format_pub_date(dt: datetime | None) -> str:
    return "" if dt is None else dt.date().isoformat()


# --- collection report ---------------------------------------------------------


def render_collection_report(entries: Sequence[CollectionEntry], report_date: date_cls) -> str:
    """Render a collection report as a complete markdown document."""
    by_topic = _group_by_topic(entries)
    for topic_entries in by_topic.values():
        topic_entries.sort(key=_entry_sort_key)

    ordered_topics = sorted(by_topic.items(), key=lambda kv: _topic_sort_key(kv[1]))

    frontmatter = _yaml_block(
        {
            "date": report_date.isoformat(),
            "type": "collection-report",
            "schema_version": SCHEMA_VERSION,
            "renderer_version": RENDERER_VERSION,
            "counts": {
                "by_topic": {topic: len(es) for topic, es in by_topic.items()},
                "total": len(entries),
            },
        }
    )

    parts: list[str] = [frontmatter, "", f"# Collection — {report_date.isoformat()}", ""]

    if not entries:
        parts.append("_No items collected._")
        parts.append("")
        return "\n".join(parts)

    for topic, topic_entries in ordered_topics:
        parts.append(f"## {topic} ({len(topic_entries)})")
        parts.append("")
        for entry in topic_entries:
            parts.extend(_render_entry(entry))
            parts.append("")

    return "\n".join(parts)


def _render_entry(entry: CollectionEntry) -> list[str]:
    item = entry.item
    c = entry.classification
    title_line = (
        f"### [{item.title}]({item.url}) — score {c.score}"
        f"{_format_secondary_topics(c.secondary_topics)}"
    )
    lines = [
        title_line,
        f"> {c.rationale}",
    ]
    source_names = sorted({s.source_name for s in entry.sources})
    if source_names:
        lines.append(f"- **Sources:** {', '.join(source_names)}")
    pub = _format_pub_date(item.published_at)
    if pub:
        lines.append(f"- **Published:** {pub}")
    return lines


def write_collection_report(
    entries: Sequence[CollectionEntry],
    report_date: date_cls,
    vault_root: Path,
) -> Path:
    """Render and write a collection report. Returns the written path."""
    content = render_collection_report(entries, report_date)
    path = vault_root / "collection" / f"{report_date.isoformat()}.md"
    _atomic_write(path, content)
    return path


# --- synthesis report ----------------------------------------------------------


def render_synthesis_report(
    window: SynthesisWindow,
    agent_body: str,
    entities: Sequence[WeeklyEntity],
    followups: Sequence[Followup],
) -> str:
    """Render a synthesis report.

    `agent_body` is the markdown the synthesis LLM agent produced — we don't
    interpret it, just embed it between our frontmatter and our deterministic
    entities/followups tails.
    """
    entity_counts: dict[str, int] = {}
    for e in entities:
        entity_counts[e.kind] = entity_counts.get(e.kind, 0) + 1

    frontmatter = _yaml_block(
        {
            "type": "synthesis-report",
            "schema_version": SCHEMA_VERSION,
            "renderer_version": RENDERER_VERSION,
            "window": {
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
                "label": window.label,
            },
            "entity_counts": dict(entity_counts),
            "followup_count": len(followups),
        }
    )

    parts: list[str] = [
        frontmatter,
        "",
        f"# Synthesis — {window.label}",
        "",
        agent_body.rstrip() + "\n" if agent_body.strip() else "_(no agent body)_\n",
        "",
        _render_entities_section(entities),
        "",
        _render_followups_section(followups),
        "",
    ]
    return "\n".join(parts)


_ENTITY_KIND_HEADINGS: dict[str, str] = {
    "cve": "CVEs",
    "repo": "Repos",
    "package": "Packages",
    "project": "Projects",
    "person": "People",
    "technique": "Techniques",
    "venue": "Venues",
}


def _render_entities_section(entities: Sequence[WeeklyEntity]) -> str:
    if not entities:
        return "## Entities\n\n_None extracted in this window._\n"

    by_kind: dict[str, list[WeeklyEntity]] = {}
    for e in entities:
        by_kind.setdefault(e.kind, []).append(e)

    out: list[str] = ["## Entities", ""]
    for kind in sorted(by_kind.keys()):
        heading = _ENTITY_KIND_HEADINGS.get(kind, kind)
        out.append(f"### {heading}")
        out.append("")
        out.append("| Value | Context |")
        out.append("|---|---|")
        for e in sorted(by_kind[kind], key=lambda x: x.value):
            ctx = e.context.replace("|", "\\|").replace("\n", " ").strip()
            out.append(f"| {e.value} | {ctx} |")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _render_followups_section(followups: Sequence[Followup]) -> str:
    if not followups:
        return "## Followups\n\n_None queued._\n"

    open_only = [f for f in followups if not f.completed]
    if not open_only:
        return "## Followups\n\n_All caught up._\n"

    out: list[str] = ["## Followups", ""]
    for f in sorted(open_only, key=lambda x: (x.action, x.item_title_snapshot)):
        note = f" — {f.note}" if f.note else ""
        out.append(f"- **[{f.action}]** [{f.item_title_snapshot}]({f.item_url_snapshot}){note}")
    out.append("")
    return "\n".join(out)


def write_synthesis_report(
    window: SynthesisWindow,
    agent_body: str,
    entities: Sequence[WeeklyEntity],
    followups: Sequence[Followup],
    vault_root: Path,
) -> Path:
    """Render and write a synthesis report. Returns the written path."""
    content = render_synthesis_report(window, agent_body, entities, followups)
    path = vault_root / "synthesis" / f"{window.label}.md"
    _atomic_write(path, content)
    return path
