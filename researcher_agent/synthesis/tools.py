"""Tools available to the synthesis agent.

Each tool returns a structured result dict — `{"ok": True, "result": ...}` or
`{"ok": False, "error": "..."}` — and NEVER raises into the agent loop (a failed
tool call is information the model can react to, not a crash). The agent queries
already-extracted entities via `get_entities` rather than extracting its own
(invariant #4: entities are produced deterministically at normalize time).

Tools are methods on `SynthesisTools`, which holds the run's dependencies (db,
http client, window). `tool_specs()` describes them for the provider, and
`dispatch()` routes a model tool call to the right method by name.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast, get_args

from researcher_agent.canonicalize import canonicalize_url
from researcher_agent.http import PoliteClient
from researcher_agent.models import Followup, FollowupAction, ItemBody
from researcher_agent.state import Database
from researcher_agent.vault import SynthesisWindow

ToolResult = dict[str, Any]

# Derived from the model so the tool's accepted actions never drift from storage.
_VALID_ACTIONS: tuple[str, ...] = get_args(FollowupAction)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Strip script/style bodies entirely before dropping tags (their contents are not
# readable text and would otherwise leak JS/CSS into the fetched body).
_DROP_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


def _ok(result: Any) -> ToolResult:
    return {"ok": True, "result": result}


def _err(message: str) -> ToolResult:
    return {"ok": False, "error": message}


def _html_to_text(raw: str) -> str:
    """Crude HTML-to-readable-text: drop script/style, strip tags, unescape, collapse ws."""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", _DROP_RE.sub(" ", raw)))).strip()


def _cache_key(url: str) -> str:
    """Stable key for the body cache: the sha256 of the canonical URL.

    Matches how `Item.canonical_hash` is computed, so fetching an item's own URL
    reuses any body already stored under that item.
    """
    try:
        canonical = canonicalize_url(url)
    except Exception:
        canonical = url
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolSpec:
    """Provider-agnostic description of one tool (translated to each SDK's format)."""

    name: str
    description: str
    input_schema: dict[str, Any]


class SynthesisTools:
    """The synthesis agent's tools, bound to one run's db / client / window."""

    def __init__(
        self,
        db: Database,
        client: PoliteClient,
        *,
        window: SynthesisWindow,
        now: datetime,
        min_score: int = 5,
        max_fetch_chars: int = 20_000,
    ) -> None:
        self.db = db
        self.client = client
        self.window = window
        self.now = now
        self.min_score = min_score
        self.max_fetch_chars = max_fetch_chars
        # Loop-visible outcomes.
        self.finished_roundup: str | None = None
        self.followups_added = 0

    # --- tools -----------------------------------------------------------------

    def query_items(self, topic: str | None = None, min_score: int | None = None) -> ToolResult:
        """List the window's classified, non-superseded items (highest score first)."""
        floor = self.min_score if min_score is None else min_score
        rows: list[dict[str, Any]] = []
        for item in self.db.list_items_in_window(self.window.start, self.window.end):
            active = self.db.get_active_classification(item.canonical_hash)
            if active is None or active.score < floor:
                continue
            if topic is not None and topic != active.topic and topic not in active.secondary_topics:
                continue
            rows.append(
                {
                    "item_hash": item.canonical_hash,
                    "title": item.title,
                    "url": item.url,
                    "summary": item.summary,
                    "topic": active.topic,
                    "secondary_topics": active.secondary_topics,
                    "score": active.score,
                    "published_at": (
                        item.published_at.isoformat() if item.published_at is not None else None
                    ),
                }
            )
        rows.sort(key=lambda r: (-int(r["score"]), str(r["item_hash"])))
        return _ok({"count": len(rows), "items": rows})

    def get_entities(self, item_hash: object) -> ToolResult:
        """Return the entities already extracted from one item."""
        if not isinstance(item_hash, str) or not item_hash:
            return _err("item_hash must be a non-empty string")
        if self.db.get_item(item_hash) is None:
            return _err(f"no item with hash {item_hash[:12]}…")
        entities = [
            {"kind": e.kind, "value": e.value, "context": e.context}
            for e in self.db.get_item_entities(item_hash)
        ]
        return _ok({"item_hash": item_hash, "entities": entities})

    def fetch_url(self, url: object) -> ToolResult:
        """Fetch readable full text for a URL, caching the body after first fetch."""
        if not isinstance(url, str) or not url.strip():
            return _err("url must be a non-empty string")
        key = _cache_key(url)
        cached = self.db.get_item_body(key)
        if cached is not None:
            return _ok({"url": url, "cached": True, "text": cached.body})
        try:
            response = self.client.get(url)
            response.raise_for_status()
            text = _html_to_text(response.text)[: self.max_fetch_chars]
        except Exception as exc:  # tools never raise into the loop
            return _err(f"fetch failed: {type(exc).__name__}")
        # The body cache (item_bodies) is FK-bound to items, so we can only persist
        # when the URL maps to a known item — the common case (re-reading an item's
        # own page across turns). An arbitrary external URL is returned uncached.
        if self.db.get_item(key) is not None:
            self.db.set_item_body(
                ItemBody(canonical_hash=key, body=text, fetched_from_url=url, fetched_at=self.now)
            )
        return _ok({"url": url, "cached": False, "text": text})

    def add_followup(self, item_hash: object, action: object, note: object = "") -> ToolResult:
        """Queue a follow-up action for the researcher, snapshotting the item."""
        if not isinstance(action, str) or action not in _VALID_ACTIONS:
            return _err(f"action must be one of {list(_VALID_ACTIONS)}")
        if not isinstance(item_hash, str) or not item_hash:
            return _err("item_hash must be a non-empty string")
        item = self.db.get_item(item_hash)
        if item is None:
            return _err(f"no item with hash {item_hash[:12]}…")
        self.db.insert_followup(
            Followup(
                created_at=self.now,
                item_hash=item_hash,
                item_title_snapshot=item.title,
                item_url_snapshot=item.url,
                action=cast(FollowupAction, action),  # validated against _VALID_ACTIONS above
                note=note if isinstance(note, str) else "",
            )
        )
        self.followups_added += 1
        return _ok({"queued": action, "item_hash": item_hash})

    def finish(self, roundup_markdown: object) -> ToolResult:
        """Record the final roundup markdown and signal the loop to stop."""
        if not isinstance(roundup_markdown, str) or not roundup_markdown.strip():
            return _err("roundup_markdown must be a non-empty string")
        self.finished_roundup = roundup_markdown
        return _ok("roundup recorded")

    # --- dispatch --------------------------------------------------------------

    def dispatch(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Route a model tool call to the matching method, validating arguments.

        Unknown tools / bad argument shapes return an error result (never raise),
        so a confused model gets feedback instead of crashing the run.
        """
        if not isinstance(args, dict):
            return _err("tool arguments must be an object")
        try:
            if name == "query_items":
                return self.query_items(topic=args.get("topic"), min_score=args.get("min_score"))
            if name == "get_entities":
                return self.get_entities(args.get("item_hash"))
            if name == "fetch_url":
                return self.fetch_url(args.get("url"))
            if name == "add_followup":
                return self.add_followup(
                    args.get("item_hash"), args.get("action"), args.get("note", "")
                )
            if name == "finish":
                return self.finish(args.get("roundup_markdown"))
        except Exception as exc:  # last-resort guard: a tool must never crash the loop
            return _err(f"tool {name!r} raised {type(exc).__name__}")
        return _err(f"unknown tool {name!r}")


def tool_specs() -> list[ToolSpec]:
    """Provider-agnostic specs for every synthesis tool."""
    return [
        ToolSpec(
            name="query_items",
            description=(
                "List the window's classified items (highest score first). Optionally "
                "filter by topic slug and/or a minimum score. Start here."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Topic slug to filter by."},
                    "min_score": {"type": "integer", "description": "Minimum score (0-10)."},
                },
            },
        ),
        ToolSpec(
            name="fetch_url",
            description=(
                "Fetch readable full text for a URL (cached after first fetch). Use to "
                "verify an assessment before highlighting an item."
            ),
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        ),
        ToolSpec(
            name="get_entities",
            description="Return the entities already extracted from one item, by item_hash.",
            input_schema={
                "type": "object",
                "properties": {"item_hash": {"type": "string"}},
                "required": ["item_hash"],
            },
        ),
        ToolSpec(
            name="add_followup",
            description=(
                "Queue a follow-up action for an item. action is one of "
                "read-deep, audit, track, reach-out."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item_hash": {"type": "string"},
                    "action": {"type": "string", "enum": list(_VALID_ACTIONS)},
                    "note": {"type": "string"},
                },
                "required": ["item_hash", "action"],
            },
        ),
        ToolSpec(
            name="finish",
            description="End the run with the final roundup markdown.",
            input_schema={
                "type": "object",
                "properties": {"roundup_markdown": {"type": "string"}},
                "required": ["roundup_markdown"],
            },
        ),
    ]
