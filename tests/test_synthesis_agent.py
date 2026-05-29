"""Tests for the synthesis agent loop (no network/LLM — scripted fake provider)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from researcher_agent.llm.base import ProviderError
from researcher_agent.synthesis.agent import (
    AgentMessage,
    AgentReply,
    ToolCall,
    ToolResultBlock,
    TokenUsage,
    run_agent,
)
from researcher_agent.synthesis.tools import ToolSpec, tool_specs

SPECS = tool_specs()


class FakeProvider:
    """Returns scripted replies; falls back to `default` once the script is spent."""

    model_id = "fake:agent"

    def __init__(
        self, replies: list[AgentReply] | None = None, default: AgentReply | None = None
    ) -> None:
        self._replies = list(replies or [])
        self._default = default
        self.calls = 0
        self.last_messages: list[AgentMessage] = []

    def complete(
        self, *, system: str, messages: Sequence[AgentMessage], tools: Sequence[ToolSpec]
    ) -> AgentReply:
        self.calls += 1
        self.last_messages = list(messages)
        if self._replies:
            return self._replies.pop(0)
        if self._default is not None:
            return self._default
        raise AssertionError("FakeProvider ran out of scripted replies")


class FakeTools:
    def __init__(self) -> None:
        self.finished_roundup: str | None = None
        self.dispatched: list[tuple[str, dict[str, Any]]] = []

    def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.dispatched.append((name, args))
        if name == "finish":
            self.finished_roundup = args.get("roundup_markdown", "ROUNDUP")
            return {"ok": True, "result": "recorded"}
        return {"ok": True, "result": {"echo": name}}


def _tool(name: str, args: dict[str, Any], usage: tuple[int, int] = (0, 0)) -> AgentReply:
    return AgentReply(
        text=None,
        tool_calls=[ToolCall(id="t1", name=name, arguments=args)],
        usage=TokenUsage(*usage),
    )


def _text(text: str, usage: tuple[int, int] = (0, 0)) -> AgentReply:
    return AgentReply(text=text, tool_calls=[], usage=TokenUsage(*usage))


def _run(provider: Any, tools: Any, **kw: Any):  # type: ignore[no-untyped-def]
    return run_agent(
        provider, tools, system_prompt="S", initial_user_text="items", tool_specs=SPECS, **kw
    )


def test_clean_finish_returns_roundup() -> None:
    prov = FakeProvider([_tool("query_items", {}), _tool("finish", {"roundup_markdown": "# R"})])
    tools = FakeTools()
    out = _run(prov, tools)
    assert out.stop_reason == "finish"
    assert out.degraded is False
    assert out.roundup == "# R"
    assert out.turns == 2
    assert ("finish", {"roundup_markdown": "# R"}) in tools.dispatched


def test_finish_on_first_turn() -> None:
    prov = FakeProvider([_tool("finish", {"roundup_markdown": "# Done"})])
    out = _run(prov, FakeTools())
    assert out.stop_reason == "finish"
    assert out.turns == 1
    assert out.roundup == "# Done"


def test_tool_results_are_fed_back_to_the_model() -> None:
    prov = FakeProvider([_tool("query_items", {}), _tool("finish", {"roundup_markdown": "x"})])
    _run(prov, FakeTools())
    # the last provider call (turn 2) must have seen the tool_result from turn 1
    blocks = [b for m in prov.last_messages for b in m.content]
    assert any(isinstance(b, ToolResultBlock) for b in blocks)


def test_end_turn_without_finish_is_degraded() -> None:
    prov = FakeProvider([_text("Here is my prose roundup")])
    out = _run(prov, FakeTools())
    assert out.stop_reason == "end_turn"
    assert out.degraded is True
    assert "stopped early" in out.roundup
    assert "Here is my prose roundup" in out.roundup


def test_max_turns_stops_and_is_degraded() -> None:
    prov = FakeProvider(default=_tool("query_items", {}))  # never finishes
    out = _run(prov, FakeTools(), max_turns=3)
    assert out.stop_reason == "max_turns"
    assert out.turns == 3
    assert out.degraded is True
    assert prov.calls == 3


def test_token_budget_stops_after_the_turn_that_exceeds_it() -> None:
    prov = FakeProvider(default=_tool("query_items", {}, usage=(10, 5)))  # 15 tokens/turn
    out = _run(prov, FakeTools(), max_turns=20, token_budget=20)
    assert out.stop_reason == "budget"
    assert out.turns == 2  # turn1 total=15 (<20, continue); turn2 total=30 (>=20, stop)
    assert out.usage.total == 30
    assert out.degraded is True


def test_provider_error_yields_degraded_outcome() -> None:
    class Boom:
        model_id = "fake:boom"

        def complete(self, **kw: Any) -> AgentReply:
            raise ProviderError("api down")

    out = _run(Boom(), FakeTools())
    assert out.stop_reason == "provider_error"
    assert out.degraded is True
    assert "stopped early" in out.roundup


def test_provider_error_after_finish_keeps_the_roundup() -> None:
    tools = FakeTools()

    class FinishThenBoom:
        model_id = "fake"

        def __init__(self) -> None:
            self.n = 0

        def complete(self, **kw: Any) -> AgentReply:
            self.n += 1
            if self.n == 1:
                return _tool("finish", {"roundup_markdown": "# kept"})
            raise ProviderError("down")

    out = _run(FinishThenBoom(), tools)
    assert out.stop_reason == "finish"
    assert out.roundup == "# kept"


def test_usage_accumulates_across_turns() -> None:
    prov = FakeProvider(
        [_tool("query_items", {}, usage=(3, 2)), _tool("finish", {"roundup_markdown": "x"}, (1, 1))]
    )
    out = _run(prov, FakeTools())
    assert out.usage.input_tokens == 4
    assert out.usage.output_tokens == 3
    assert out.usage.total == 7
