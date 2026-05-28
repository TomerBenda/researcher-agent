"""Tests for agent.yaml config loading + the config-driven taxonomy."""

from __future__ import annotations

from pathlib import Path

import pytest

from researcher_agent.config import AgentConfig, ConfigError, load_agent_config

MINIMAL = """
taxonomy:
  - slug: mcp-security
    description: MCP-specific vulnerabilities and advisories
  - slug: prompt-injection
    description: Prompt injection findings and defenses
  - slug: other
    description: Worth keeping but does not fit cleanly
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "agent.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_minimal_with_defaults(tmp_path: Path) -> None:
    cfg = load_agent_config(_write(tmp_path, MINIMAL))
    assert isinstance(cfg, AgentConfig)
    assert cfg.classifier.provider == "gemini"
    assert cfg.classifier.model == "gemini-2.5-flash"
    assert cfg.classifier.batch_size == 10
    assert cfg.dedup.fuzzy_title_threshold == 0.92
    assert cfg.dedup.fuzzy_window_hours == 48
    assert cfg.vault_path is None


def test_taxonomy_slugs(tmp_path: Path) -> None:
    cfg = load_agent_config(_write(tmp_path, MINIMAL))
    assert cfg.taxonomy.slugs == {"mcp-security", "prompt-injection", "other"}


def test_taxonomy_render_includes_slugs_and_descriptions(tmp_path: Path) -> None:
    cfg = load_agent_config(_write(tmp_path, MINIMAL))
    rendered = cfg.taxonomy.render()
    assert "mcp-security" in rendered
    assert "Prompt injection findings and defenses" in rendered


def test_classifier_overrides(tmp_path: Path) -> None:
    text = (
        MINIMAL
        + """
classifier:
  provider: ollama
  model: llama3.1
  batch_size: 5
  token_budget: 50000
"""
    )
    cfg = load_agent_config(_write(tmp_path, text))
    assert cfg.classifier.provider == "ollama"
    assert cfg.classifier.model == "llama3.1"
    assert cfg.classifier.batch_size == 5
    assert cfg.classifier.token_budget == 50000


def test_max_items_per_run_default_and_override(tmp_path: Path) -> None:
    assert load_agent_config(_write(tmp_path, MINIMAL)).classifier.max_items_per_run == 200
    text = MINIMAL + "classifier:\n  max_items_per_run: 50\n"
    assert load_agent_config(_write(tmp_path, text)).classifier.max_items_per_run == 50


def test_dedup_and_tracking_and_vault(tmp_path: Path) -> None:
    text = (
        MINIMAL
        + """
vault_path: /some/vault
research_focus: MCP supply chain
tracking_params_to_strip:
  - sid
  - mkt
dedup:
  fuzzy_title_threshold: 0.8
  fuzzy_window_hours: 24
"""
    )
    cfg = load_agent_config(_write(tmp_path, text))
    assert cfg.vault_path == "/some/vault"
    assert cfg.research_focus == "MCP supply chain"
    assert cfg.tracking_params_to_strip == ["sid", "mkt"]
    assert cfg.dedup.fuzzy_title_threshold == 0.8
    assert cfg.dedup.fuzzy_window_hours == 24


def test_unknown_top_level_keys_are_ignored(tmp_path: Path) -> None:
    # forward-compat: weekly/log_level land in later milestones
    text = (
        MINIMAL
        + """
weekly:
  provider: anthropic
log_level: DEBUG
"""
    )
    cfg = load_agent_config(_write(tmp_path, text))
    assert cfg.classifier.provider == "gemini"


def test_missing_taxonomy_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_agent_config(_write(tmp_path, "classifier:\n  provider: gemini\n"))


def test_empty_taxonomy_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_agent_config(_write(tmp_path, "taxonomy: []\n"))


def test_invalid_provider_errors(tmp_path: Path) -> None:
    text = MINIMAL + "classifier:\n  provider: telepathy\n"
    with pytest.raises(ConfigError):
        load_agent_config(_write(tmp_path, text))


def test_missing_file_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_agent_config(tmp_path / "nope.yaml")
