"""Tests for prompt loading, system-prompt rendering, and classifier_version."""

from __future__ import annotations

from pathlib import Path

import pytest

from researcher_agent.prompts import (
    classifier_version,
    load_prompt,
    render_system_prompt,
)

TEMPLATE = 'Focus: <<RESEARCH_FOCUS>>\nTaxonomy:\n<<TAXONOMY>>\nOutput JSON like {"topic": "x"}.'


def test_load_prompt_reads_file(tmp_path: Path) -> None:
    (tmp_path / "classify.md").write_text("hello prompt", encoding="utf-8")
    assert load_prompt("classify", prompts_dir=tmp_path) == "hello prompt"


def test_load_prompt_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt("nope", prompts_dir=tmp_path)


def test_real_classify_prompt_exists_and_has_placeholders() -> None:
    text = load_prompt("classify")
    assert "<<TAXONOMY>>" in text
    assert "<<RESEARCH_FOCUS>>" in text


def test_render_substitutes_placeholders() -> None:
    out = render_system_prompt(TEMPLATE, taxonomy="- a: x\n- b: y", research_focus="MCP")
    assert "Focus: MCP" in out
    assert "- a: x" in out
    assert "<<TAXONOMY>>" not in out
    assert "<<RESEARCH_FOCUS>>" not in out


def test_render_preserves_json_braces() -> None:
    # the template contains literal JSON braces that must survive rendering
    out = render_system_prompt(TEMPLATE, taxonomy="-", research_focus=None)
    assert '{"topic": "x"}' in out


def test_render_handles_missing_focus() -> None:
    out = render_system_prompt(TEMPLATE, taxonomy="-", research_focus=None)
    assert "<<RESEARCH_FOCUS>>" not in out


def test_classifier_version_is_stable() -> None:
    a = classifier_version("system prompt", "gemini:gemini-2.5-flash")
    b = classifier_version("system prompt", "gemini:gemini-2.5-flash")
    assert a == b


def test_classifier_version_changes_with_prompt() -> None:
    a = classifier_version("system prompt v1", "m")
    b = classifier_version("system prompt v2", "m")
    assert a != b


def test_classifier_version_changes_with_model() -> None:
    a = classifier_version("p", "gemini:gemini-2.5-flash")
    b = classifier_version("p", "ollama:llama3.1")
    assert a != b


def test_classifier_version_shape() -> None:
    v = classifier_version("p", "m")
    assert v.startswith("cls-")
    assert len(v) == len("cls-") + 12
