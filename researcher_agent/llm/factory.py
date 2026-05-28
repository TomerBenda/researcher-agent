"""Build a classifier provider from config (explicit dispatch, no magic)."""

from __future__ import annotations

from researcher_agent.config import ClassifierConfig
from researcher_agent.llm.base import ClassifierProvider, ProviderError
from researcher_agent.llm.gemini import GeminiProvider
from researcher_agent.llm.ollama import OllamaProvider


def build_classifier_provider(config: ClassifierConfig) -> ClassifierProvider:
    """Instantiate the configured classifier provider, or raise ProviderError."""
    if config.provider == "gemini":
        return GeminiProvider(model=config.model)
    if config.provider == "ollama":
        return OllamaProvider(model=config.model)
    raise ProviderError(
        f"provider {config.provider!r} is not available for classification "
        "(anthropic is reserved for synthesis in M5)"
    )
