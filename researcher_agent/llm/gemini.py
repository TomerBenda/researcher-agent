"""Gemini classifier provider (google-genai).

Free tier on Google AI Studio (default model gemini-2.5-flash). The SDK client is
injectable so the wire/parse logic is testable without network; the real API path
is only exercised by the opt-in golden eval.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from researcher_agent.llm.base import (
    ClassifierInput,
    ProviderError,
    RawClassification,
    parse_classifications,
    render_items_message,
)


class GeminiProvider:
    def __init__(
        self,
        *,
        model: str = "gemini-2.5-flash",
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.model_id = f"gemini:{model}"
        if client is not None:
            self._client = client
            return
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise ProviderError("GEMINI_API_KEY is not set")
        from google import genai

        self._client = genai.Client(api_key=key)

    def classify(
        self,
        system_prompt: str,
        inputs: Sequence[ClassifierInput],
        *,
        temperature: float,
    ) -> dict[str, RawClassification]:
        from google.genai import types

        user_message = render_items_message(inputs)
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=user_message,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=temperature,
                    response_mime_type="application/json",
                ),
            )
            text = response.text or ""
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"gemini call failed: {type(exc).__name__}") from exc
        return parse_classifications(text, valid_ids={i.id for i in inputs})
