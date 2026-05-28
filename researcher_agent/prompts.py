"""Prompt loading + versioning.

Prompts live as markdown files under `prompts/` and are hashed at load — never
inlined in code — so prompt iteration is git-trackable separately from logic.

`classifier_version` is the audit key (invariant #3): a hash of the *rendered*
system prompt (which already embeds the taxonomy and research focus) plus the
model id. Any change to the prompt template, taxonomy, focus, or model produces a
new version, so `classifications.classifier_version` records exactly what
produced each label.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str, *, prompts_dir: Path = PROMPTS_DIR) -> str:
    """Read `{prompts_dir}/{name}.md`. Raises FileNotFoundError if absent."""
    return (prompts_dir / f"{name}.md").read_text(encoding="utf-8")


def render_system_prompt(template: str, *, taxonomy: str, research_focus: str | None) -> str:
    """Fill the placeholders in a prompt template.

    Uses literal `<<...>>` markers (not str.format) so JSON braces in the
    template survive untouched.
    """
    return template.replace("<<TAXONOMY>>", taxonomy).replace(
        "<<RESEARCH_FOCUS>>", research_focus or "(not specified)"
    )


def classifier_version(system_prompt: str, model_id: str) -> str:
    """Stable short version string for an (effective prompt, model) pair."""
    payload = f"{system_prompt}\x00{model_id}".encode()
    return "cls-" + hashlib.sha256(payload).hexdigest()[:12]
