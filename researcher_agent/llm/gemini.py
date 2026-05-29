"""Gemini classifier provider (google-genai).

Free tier on Google AI Studio (default model gemini-2.5-flash). The SDK client is
injectable so the wire/parse logic is testable without network; the real API path
is only exercised by the opt-in golden eval.

On a rate-limit (HTTP 429 / RESOURCE_EXHAUSTED) the call is retried a few times
with exponential backoff so the daily cron survives normal free-tier pacing
instead of immediately surfacing a transient failure.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from typing import Any

from researcher_agent.llm.base import (
    ClassifierInput,
    ProviderError,
    RawClassification,
    parse_classifications,
    render_items_message,
)


def _is_rate_limited(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 429:
        return True
    text = str(exc).lower()
    return any(s in text for s in ("429", "resource_exhausted", "rate limit", "quota"))


class GeminiProvider:
    def __init__(
        self,
        *,
        model: str = "gemini-2.5-flash",
        api_key: str | None = None,
        client: Any | None = None,
        max_rate_limit_retries: int = 3,
        backoff_base_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.model_id = f"gemini:{model}"
        self._max_rate_limit_retries = max_rate_limit_retries
        self._backoff_base = backoff_base_seconds
        self._sleep = sleep
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
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            response_mime_type="application/json",
        )
        valid_ids = {i.id for i in inputs}

        attempt = 0
        while True:
            try:
                response = self._client.models.generate_content(
                    model=self.model, contents=user_message, config=config
                )
                text = response.text or ""
                return parse_classifications(text, valid_ids=valid_ids)
            except ProviderError:
                raise  # bad/unparseable response — not a transient rate limit
            except Exception as exc:
                if _is_rate_limited(exc) and attempt < self._max_rate_limit_retries:
                    self._sleep(self._backoff_base**attempt)
                    attempt += 1
                    continue
                raise ProviderError(f"gemini call failed: {type(exc).__name__}") from exc
