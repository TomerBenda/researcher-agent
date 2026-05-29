"""Tests for the synthesis providers' SDK translation (fake clients, no network)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from researcher_agent.llm.base import ProviderError
from researcher_agent.synthesis.agent import AgentMessage, TextBlock, ToolResultBlock, ToolUseBlock
from researcher_agent.synthesis.providers import (
    AnthropicSynthesisProvider,
    _from_anthropic_response,
    _from_gemini_response,
    _to_anthropic_message,
    _to_gemini_content,
    build_synthesis_provider,
)
from researcher_agent.synthesis.tools import tool_specs

SPECS = tool_specs()


# --- Anthropic: request translation -------------------------------------------


def test_to_anthropic_message_maps_every_block_type() -> None:
    msg = _to_anthropic_message(
        AgentMessage(
            "assistant",
            [TextBlock("hi"), ToolUseBlock("u1", "query_items", {"min_score": 5})],
        )
    )
    assert msg == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "u1", "name": "query_items", "input": {"min_score": 5}},
        ],
    }
    res = _to_anthropic_message(AgentMessage("user", [ToolResultBlock("u1", "query_items", "{}")]))
    assert res["content"][0] == {"type": "tool_result", "tool_use_id": "u1", "content": "{}"}


def test_from_anthropic_response_parses_text_tools_usage() -> None:
    resp = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="some prose"),
            SimpleNamespace(
                type="tool_use", id="t1", name="finish", input={"roundup_markdown": "R"}
            ),
        ],
        usage=SimpleNamespace(input_tokens=12, output_tokens=5),
        stop_reason="tool_use",
    )
    reply = _from_anthropic_response(resp)
    assert reply.text == "some prose"
    assert reply.tool_calls[0].name == "finish"
    assert reply.tool_calls[0].arguments == {"roundup_markdown": "R"}
    assert reply.usage.input_tokens == 12
    assert reply.usage.output_tokens == 5
    assert reply.stop_reason == "tool_use"


class _FakeAnthropic:
    def __init__(self, response: Any = None, errors: list[Exception] | None = None) -> None:
        self.response = response
        self.errors = list(errors or [])
        self.calls: list[dict[str, Any]] = []
        self.messages = self  # so `client.messages.create(...)` works

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return self.response


def test_anthropic_provider_round_trip() -> None:
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        stop_reason="end_turn",
    )
    client = _FakeAnthropic(response=resp)
    prov = AnthropicSynthesisProvider(client=client, model="claude-test")
    reply = prov.complete(
        system="SYS", messages=[AgentMessage("user", [TextBlock("hi")])], tools=SPECS
    )
    assert reply.text == "ok"
    assert prov.model_id == "anthropic:claude-test"
    sent = client.calls[0]
    assert sent["system"] == "SYS"
    assert sent["messages"][0] == {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    assert {t["name"] for t in sent["tools"]} == {s.name for s in SPECS}


def test_anthropic_retries_on_rate_limit_then_succeeds() -> None:
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        stop_reason="end_turn",
    )
    sleeps: list[float] = []
    client = _FakeAnthropic(response=resp, errors=[Exception("HTTP 429 rate limited")])
    prov = AnthropicSynthesisProvider(client=client, sleep=sleeps.append)
    reply = prov.complete(system="S", messages=[AgentMessage("user", [TextBlock("x")])], tools=[])
    assert reply.text == "ok"
    assert len(client.calls) == 2
    assert sleeps  # backed off before retry


def test_anthropic_non_rate_limit_error_raises_providererror() -> None:
    client = _FakeAnthropic(errors=[ValueError("boom")])
    prov = AnthropicSynthesisProvider(client=client)
    with pytest.raises(ProviderError):
        prov.complete(system="S", messages=[AgentMessage("user", [TextBlock("x")])], tools=[])
    assert len(client.calls) == 1  # no retry on a non-transient error


# --- Gemini: translation helpers ----------------------------------------------


def test_from_gemini_response_parses_function_calls_and_text() -> None:
    text_part = SimpleNamespace(function_call=None, text="prose")
    fc = SimpleNamespace(name="finish", args={"roundup_markdown": "# R"})
    fc_part = SimpleNamespace(function_call=fc, text=None)
    cand = SimpleNamespace(content=SimpleNamespace(parts=[text_part, fc_part]))
    resp = SimpleNamespace(
        candidates=[cand],
        usage_metadata=SimpleNamespace(prompt_token_count=7, candidates_token_count=3),
    )
    reply = _from_gemini_response(resp)
    assert reply.text == "prose"
    assert reply.tool_calls[0].name == "finish"
    assert reply.tool_calls[0].arguments == {"roundup_markdown": "# R"}
    assert reply.usage.input_tokens == 7
    assert reply.usage.output_tokens == 3


def _fake_genai_types() -> SimpleNamespace:
    return SimpleNamespace(
        Part=lambda **kw: ("Part", kw),
        FunctionCall=lambda **kw: ("FunctionCall", kw),
        FunctionResponse=lambda **kw: ("FunctionResponse", kw),
        Content=lambda role, parts: ("Content", role, parts),
    )


def test_to_gemini_content_maps_blocks() -> None:
    types_mod = _fake_genai_types()
    content = _to_gemini_content(
        AgentMessage("assistant", [TextBlock("hi"), ToolUseBlock("u1", "query_items", {"x": 1})]),
        types_mod,
    )
    assert content[0] == "Content"
    assert content[1] == "model"  # assistant -> model
    parts = content[2]
    assert parts[0] == ("Part", {"text": "hi"})
    assert parts[1][0] == "Part"
    assert parts[1][1]["function_call"][0] == "FunctionCall"


def test_to_gemini_content_maps_tool_result() -> None:
    types_mod = _fake_genai_types()
    content = _to_gemini_content(
        AgentMessage("user", [ToolResultBlock("u1", "query_items", '{"ok": true}')]),
        types_mod,
    )
    assert content[1] == "user"
    fr_part = content[2][0]
    assert fr_part[1]["function_response"][0] == "FunctionResponse"
    assert fr_part[1]["function_response"][1]["name"] == "query_items"


# --- factory -------------------------------------------------------------------


def test_build_prefers_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert build_synthesis_provider().model_id.startswith("anthropic:")


def test_build_falls_back_to_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert build_synthesis_provider().model_id.startswith("gemini:")


def test_build_raises_without_any_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ProviderError):
        build_synthesis_provider()
