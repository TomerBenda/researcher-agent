"""Load and validate `config/agent.yaml`.

The taxonomy is config-driven (invariant #9), not a Python enum, so the slug set
can evolve without code changes/migrations. Unknown top-level keys are ignored
so a full agent.yaml (with later-milestone sections like `weekly:`) still loads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ConfigError(Exception):
    """Raised when agent.yaml is missing or malformed."""


class TopicConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: str
    description: str
    examples: list[str] = Field(default_factory=list)


class Taxonomy(BaseModel):
    model_config = ConfigDict(frozen=True)

    topics: list[TopicConfig]

    @property
    def slugs(self) -> set[str]:
        return {t.slug for t in self.topics}

    def render(self) -> str:
        """Render the taxonomy as a bulleted list for the classifier prompt."""
        lines = []
        for t in self.topics:
            line = f"- {t.slug}: {t.description}"
            if t.examples:
                line += f" (e.g. {', '.join(t.examples)})"
            lines.append(line)
        return "\n".join(lines)


class ClassifierConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: Literal["gemini", "ollama", "anthropic"] = "gemini"
    model: str = "gemini-2.5-flash"
    batch_size: int = Field(default=10, ge=1)
    token_budget: int | None = Field(default=None, ge=1)
    temperature: float = Field(default=0.2, ge=0.0)
    # Cap items classified per run so a cold backlog drains over several runs
    # instead of bursting against the free-tier API. None = no cap.
    max_items_per_run: int | None = Field(default=200, ge=1)


class DedupConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    fuzzy_title_threshold: float = Field(default=0.92, ge=0.0, le=1.0)
    fuzzy_window_hours: int = Field(default=48, ge=0)
    entity_title_threshold: float = Field(default=0.85, ge=0.0, le=1.0)


class AgentConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    taxonomy: Taxonomy
    classifier: ClassifierConfig = ClassifierConfig()
    dedup: DedupConfig = DedupConfig()
    vault_path: str | None = None
    research_focus: str | None = None
    tracking_params_to_strip: list[str] = Field(default_factory=list)


def load_agent_config(path: Path) -> AgentConfig:
    """Read and validate an agent.yaml, raising ConfigError on any problem."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"agent config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"agent config is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("agent config must be a mapping")

    topics = raw.get("taxonomy")
    if not topics:
        raise ConfigError("agent config must define a non-empty 'taxonomy'")

    try:
        return AgentConfig(
            taxonomy=Taxonomy(topics=topics),
            classifier=ClassifierConfig(**(raw.get("classifier") or {})),
            dedup=DedupConfig(**(raw.get("dedup") or {})),
            vault_path=raw.get("vault_path"),
            research_focus=raw.get("research_focus"),
            tracking_params_to_strip=raw.get("tracking_params_to_strip") or [],
        )
    except ValidationError as exc:
        raise ConfigError(f"invalid agent config: {exc}") from exc
