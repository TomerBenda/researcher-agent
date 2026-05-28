"""Golden-set accuracy eval for the real classifier.

Opt-in only: marked `golden` (deselected from the default suite) and skipped
unless config/agent.yaml exists and a provider can be built (i.e. an API key is
present). Run it with `make test-golden`. It calls the real LLM and costs tokens.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from researcher_agent.classify import classify_inputs
from researcher_agent.config import ConfigError, load_agent_config
from researcher_agent.llm.base import ClassifierInput, ProviderError
from researcher_agent.llm.factory import build_classifier_provider
from researcher_agent.prompts import load_prompt, render_system_prompt

pytestmark = pytest.mark.golden

GOLDEN_SET = Path("config/golden_set.jsonl")
AGENT_CONFIG = Path("config/agent.yaml")
ACCURACY_THRESHOLD = 0.85


def _load_golden() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with GOLDEN_SET.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def test_classifier_meets_accuracy_on_golden_set() -> None:
    if not GOLDEN_SET.exists():
        pytest.skip("config/golden_set.jsonl not present")
    if not AGENT_CONFIG.exists():
        pytest.skip("config/agent.yaml not present (copy agent.example.yaml + set API key)")
    try:
        config = load_agent_config(AGENT_CONFIG)
        provider = build_classifier_provider(config.classifier)
    except (ConfigError, ProviderError) as exc:
        pytest.skip(f"classifier unavailable: {exc}")

    rows = _load_golden()
    inputs = [
        ClassifierInput(
            id=str(i),
            title=str(row["title"]),
            url=str(row["url"]),
            summary=str(row.get("summary")) if row.get("summary") is not None else None,
            source=str(row.get("source")) if row.get("source") is not None else None,
        )
        for i, row in enumerate(rows)
    ]
    expected = {str(i): str(row["topic"]) for i, row in enumerate(rows)}

    system_prompt = render_system_prompt(
        load_prompt("classify"),
        taxonomy=config.taxonomy.render(),
        research_focus=config.research_focus,
    )
    outcome = classify_inputs(
        inputs,
        provider=provider,
        system_prompt=system_prompt,
        valid_topics=config.taxonomy.slugs,
        batch_size=config.classifier.batch_size,
    )

    correct = sum(
        1
        for cid, exp in expected.items()
        if cid in outcome.classifications and outcome.classifications[cid].topic == exp
    )
    accuracy = correct / len(expected)
    misses = [
        f"{cid}: expected {exp}, got "
        f"{outcome.classifications[cid].topic if cid in outcome.classifications else 'MISSING'}"
        for cid, exp in expected.items()
        if cid not in outcome.classifications or outcome.classifications[cid].topic != exp
    ]
    assert accuracy >= ACCURACY_THRESHOLD, (
        f"top-1 accuracy {accuracy:.0%} < {ACCURACY_THRESHOLD:.0%}\nmisses:\n" + "\n".join(misses)
    )
