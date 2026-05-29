"""Synthesis providers: thin translators between the normalized agent types and
each SDK's tool-use API.

Anthropic is preferred (synthesis quality matters); Gemini is the fallback when
`ANTHROPIC_API_KEY` is absent. Both are kept deliberately thin and injectable —
the loop logic lives in `agent.run_agent` and is tested with a fake provider, so
these translators are only exercised against the live APIs (like the classifier's
opt-in golden eval). Unit tests here drive a fake SDK client to pin the
translation in both directions.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence
from typing import Any

from researcher_agent.llm.base import ProviderError
from researcher_agent.synthesis.agent import (
    AgentMessage,
    AgentReply,
    SynthesisProvider,
    TextBlock,
    TokenUsage,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
)
from researcher_agent.synthesis.tools import ToolSpec

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


def _is_rate_limited(exc: Exception) -> bool:
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if code == 429:
        return True
    name = type(exc).__name__.lower()
    if "ratelimit" in name or "resourceexhausted" in name:
        return True
    text = str(exc).lower()
    return any(s in text for s in ("429", "rate limit", "resource_exhausted", "quota"))


# --- Anthropic -----------------------------------------------------------------


def _to_anthropic_message(message: AgentMessage) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            content.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
        elif isinstance(block, ToolResultBlock):
            content.append(
                {"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content}
            )
    return {"role": message.role, "content": content}


def _from_anthropic_response(resp: Any) -> AgentReply:
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in getattr(resp, "content", []) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            texts.append(getattr(block, "text", "") or "")
        elif btype == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=str(getattr(block, "id", "")),
                    name=str(getattr(block, "name", "")),
                    arguments=dict(getattr(block, "input", {}) or {}),
                )
            )
    usage_obj = getattr(resp, "usage", None)
    usage = TokenUsage(
        int(getattr(usage_obj, "input_tokens", 0) or 0),
        int(getattr(usage_obj, "output_tokens", 0) or 0),
    )
    joined = "\n".join(t for t in texts if t)
    return AgentReply(
        text=joined or None,
        tool_calls=tool_calls,
        usage=usage,
        stop_reason=str(getattr(resp, "stop_reason", "") or ""),
    )


class AnthropicSynthesisProvider:
    """Primary synthesis provider (Claude via the `anthropic` SDK)."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        api_key: str | None = None,
        client: Any | None = None,
        max_tokens: int = 4096,
        max_rate_limit_retries: int = 3,
        backoff_base_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.model_id = f"anthropic:{model}"
        self.max_tokens = max_tokens
        self._max_rate_limit_retries = max_rate_limit_retries
        self._backoff_base = backoff_base_seconds
        self._sleep = sleep
        if client is not None:
            self._client = client
            return
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        import anthropic

        self._client = anthropic.Anthropic(api_key=key)

    def complete(
        self, *, system: str, messages: Sequence[AgentMessage], tools: Sequence[ToolSpec]
    ) -> AgentReply:
        api_messages = [_to_anthropic_message(m) for m in messages]
        api_tools = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        attempt = 0
        while True:
            try:
                resp = self._client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system,
                    tools=api_tools,
                    messages=api_messages,
                )
                return _from_anthropic_response(resp)
            except ProviderError:
                raise
            except Exception as exc:
                if _is_rate_limited(exc) and attempt < self._max_rate_limit_retries:
                    self._sleep(self._backoff_base**attempt)
                    attempt += 1
                    continue
                raise ProviderError(f"anthropic call failed: {type(exc).__name__}") from exc


# --- Gemini (fallback) ---------------------------------------------------------


def _to_gemini_content(message: AgentMessage, types_mod: Any) -> Any:
    """Translate one normalized message to a google-genai Content."""
    role = "user" if message.role == "user" else "model"
    parts: list[Any] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(types_mod.Part(text=block.text))
        elif isinstance(block, ToolUseBlock):
            parts.append(
                types_mod.Part(
                    function_call=types_mod.FunctionCall(name=block.name, args=block.input)
                )
            )
        elif isinstance(block, ToolResultBlock):
            try:
                payload = json.loads(block.content)
            except ValueError:
                payload = {"result": block.content}
            parts.append(
                types_mod.Part(
                    function_response=types_mod.FunctionResponse(
                        name=block.name, response={"result": payload}
                    )
                )
            )
    return types_mod.Content(role=role, parts=parts)


def _from_gemini_response(resp: Any) -> AgentReply:
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    candidates = getattr(resp, "candidates", None) or []
    parts: list[Any] = []
    if candidates:
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None) or []
    for idx, part in enumerate(parts):
        fc = getattr(part, "function_call", None)
        if fc is not None:
            tool_calls.append(
                ToolCall(
                    id=f"{getattr(fc, 'name', 'tool')}-{idx}",
                    name=str(getattr(fc, "name", "")),
                    arguments=dict(getattr(fc, "args", {}) or {}),
                )
            )
            continue
        text = getattr(part, "text", None)
        if text:
            texts.append(text)
    meta = getattr(resp, "usage_metadata", None)
    usage = TokenUsage(
        int(getattr(meta, "prompt_token_count", 0) or 0),
        int(getattr(meta, "candidates_token_count", 0) or 0),
    )
    joined = "\n".join(texts)
    return AgentReply(text=joined or None, tool_calls=tool_calls, usage=usage)


class GeminiSynthesisProvider:
    """Fallback synthesis provider (Gemini function calling via google-genai)."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_GEMINI_MODEL,
        api_key: str | None = None,
        client: Any | None = None,
        temperature: float = 0.3,
        max_rate_limit_retries: int = 3,
        backoff_base_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.model_id = f"gemini:{model}"
        self.temperature = temperature
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

    def complete(
        self, *, system: str, messages: Sequence[AgentMessage], tools: Sequence[ToolSpec]
    ) -> AgentReply:
        from google.genai import types

        declarations = [
            types.FunctionDeclaration(
                name=t.name, description=t.description, parameters=t.input_schema
            )
            for t in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.temperature,
            tools=[types.Tool(function_declarations=declarations)],
        )
        contents = [_to_gemini_content(m, types) for m in messages]
        attempt = 0
        while True:
            try:
                resp = self._client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
                return _from_gemini_response(resp)
            except ProviderError:
                raise
            except Exception as exc:
                if _is_rate_limited(exc) and attempt < self._max_rate_limit_retries:
                    self._sleep(self._backoff_base**attempt)
                    attempt += 1
                    continue
                raise ProviderError(f"gemini synthesis call failed: {type(exc).__name__}") from exc


def build_synthesis_provider(
    *,
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL,
    gemini_model: str = DEFAULT_GEMINI_MODEL,
) -> SynthesisProvider:
    """Prefer Anthropic; fall back to Gemini when ANTHROPIC_API_KEY is absent.

    Raises ProviderError if neither key is set.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicSynthesisProvider(model=anthropic_model)
    if os.environ.get("GEMINI_API_KEY"):
        return GeminiSynthesisProvider(model=gemini_model)
    raise ProviderError(
        "no synthesis provider available (set ANTHROPIC_API_KEY, or GEMINI_API_KEY to fall back)"
    )
