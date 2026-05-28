"""Ollama classifier provider (local, fully offline fallback).

The chat callable is injectable for testing; by default it uses `ollama.chat`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from researcher_agent.llm.base import (
    ClassifierInput,
    ProviderError,
    RawClassification,
    parse_classifications,
    render_items_message,
)


def _response_text(response: Any) -> str:
    """Pull the message content out of an ollama response (object or dict)."""
    message = getattr(response, "message", None)
    if message is not None:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content
    if isinstance(response, dict):
        msg = response.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str):
                return content
    return ""


class OllamaProvider:
    def __init__(
        self,
        *,
        model: str = "llama3.1",
        client: Callable[..., Any] | None = None,
    ) -> None:
        self.model = model
        self.model_id = f"ollama:{model}"
        self._chat = client

    def classify(
        self,
        system_prompt: str,
        inputs: Sequence[ClassifierInput],
        *,
        temperature: float,
    ) -> dict[str, RawClassification]:
        chat = self._chat
        if chat is None:
            import ollama

            chat = ollama.chat

        try:
            response = chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": render_items_message(inputs)},
                ],
                format="json",
                options={"temperature": temperature},
            )
            text = _response_text(response)
        except Exception as exc:
            raise ProviderError(f"ollama call failed: {type(exc).__name__}") from exc
        return parse_classifications(text, valid_ids={i.id for i in inputs})
