"""LLM provider contract for classification.

The orchestration (`classify.py`) depends only on the `ClassifierProvider`
Protocol, so it is testable with a fake. Real providers (Gemini, Ollama) share
`render_items_message` (the user-message wire format) and `parse_classifications`
(turning the model's JSON reply into validated results keyed by the echoed item
id), which is the risky part and is unit-tested here without any network.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# Bound untrusted model output: a well-behaved batch reply is a few KB.
_MAX_RESPONSE_CHARS = 200_000
_MAX_RESPONSE_ENTRIES = 2_000
_MAX_RATIONALE_CHARS = 300


class ProviderError(Exception):
    """Raised when a provider call fails or returns an unparseable response."""


@dataclass(frozen=True)
class ClassifierInput:
    """One item presented to the classifier. `id` is echoed back for alignment."""

    id: str
    title: str
    url: str
    summary: str | None = None
    source: str | None = None


class RawClassification(BaseModel):
    """A single parsed classification result, before taxonomy validation.

    Field bounds harden against hostile/oversized model output (a feed-influenced
    LLM reply is untrusted): an absurd `topic` is rejected (the item then falls
    back), and `rationale` is cleaned + truncated since it is rendered verbatim
    into the public vault report.
    """

    model_config = ConfigDict(frozen=True)

    topic: str = Field(max_length=128)
    score: int = Field(ge=0, le=10)
    rationale: str = ""
    secondary_topics: list[str] = Field(default_factory=list)

    @field_validator("rationale")
    @classmethod
    def _clean_rationale(cls, v: str) -> str:
        printable = "".join(c for c in v if c.isprintable() or c.isspace())
        return " ".join(printable.split())[:_MAX_RATIONALE_CHARS]

    @field_validator("secondary_topics")
    @classmethod
    def _cap_secondaries(cls, v: list[str]) -> list[str]:
        return [s for s in v if isinstance(s, str) and len(s) <= 128][:10]


@runtime_checkable
class ClassifierProvider(Protocol):
    """A backend that classifies a batch of items into RawClassifications."""

    model_id: str  # "provider:model", e.g. "gemini:gemini-2.5-flash"

    def classify(
        self,
        system_prompt: str,
        inputs: Sequence[ClassifierInput],
        *,
        temperature: float,
    ) -> dict[str, RawClassification]:
        """Return results keyed by input id, for items it parsed successfully."""
        ...


def render_items_message(inputs: Sequence[ClassifierInput]) -> str:
    """Format a batch of items as a JSON array — the classifier's user message.

    JSON encoding escapes the untrusted title/summary/url so feed content cannot
    forge item boundaries or inject fake instruction/schema lines (a raw
    concatenation would be vulnerable to delimiter spoofing).
    """
    return json.dumps(
        [
            {
                "id": item.id,
                "title": item.title,
                "source": item.source,
                "summary": item.summary,
                "url": item.url,
            }
            for item in inputs
        ],
        ensure_ascii=False,
    )


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _extract_list(text: str) -> list[Any]:
    """Parse a JSON array from the model's reply, tolerating fences/wrappers."""
    if len(text) > _MAX_RESPONSE_CHARS:
        raise ProviderError(
            f"classifier response too large ({len(text)} chars > {_MAX_RESPONSE_CHARS})"
        )
    stripped = _FENCE.sub("", text.strip()).strip()
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ProviderError(
            f"classifier response was not valid JSON: {type(exc).__name__}"
        ) from exc
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "items", "classifications"):
            value = data.get(key)
            if isinstance(value, list):
                return list(value)
        # a dict with a single list value is unambiguous
        lists = [v for v in data.values() if isinstance(v, list)]
        if len(lists) == 1:
            return list(lists[0])
    raise ProviderError("classifier response was not a JSON array of objects")


def parse_classifications(text: str, *, valid_ids: set[str]) -> dict[str, RawClassification]:
    """Parse a classifier reply into validated results keyed by echoed id.

    Unknown ids and individually-malformed objects are dropped (per-item
    resilience); a totally unparseable reply raises ProviderError.
    """
    results: dict[str, RawClassification] = {}
    for obj in _extract_list(text)[:_MAX_RESPONSE_ENTRIES]:
        if not isinstance(obj, dict):
            continue
        item_id = obj.get("id")
        # require a known id, and keep the FIRST result for it (no overwrite —
        # a duplicate id from the model must not steer the winning label)
        if not isinstance(item_id, str) or item_id not in valid_ids or item_id in results:
            continue
        try:
            results[item_id] = RawClassification(
                topic=obj.get("topic", ""),
                score=obj.get("score"),
                rationale=obj.get("rationale", ""),
                secondary_topics=obj.get("secondary_topics") or [],
            )
        except ValidationError:
            continue  # skip this item; orchestration will retry or fall back
    return results
