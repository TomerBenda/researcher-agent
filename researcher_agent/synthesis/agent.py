"""The synthesis agent loop — provider-agnostic, bounded, and degraded-finish-safe.

The loop owns the conversation and the stopping rules; a `SynthesisProvider` only
does one model call at a time, returning a normalized `AgentReply`. This keeps the
loop fully testable with a fake provider (no network/LLM) and lets the real
Anthropic/Gemini providers be thin translators.

Stopping criteria (whichever first): the model calls `finish`, the turn limit is
reached, the token budget is exhausted, or the provider errors. Any stop other
than a clean `finish` yields a *degraded* roundup rather than nothing, so an
unattended weekly run always writes something useful.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from researcher_agent.llm.base import ProviderError
from researcher_agent.synthesis.tools import ToolSpec

# --- normalized conversation + reply shapes ------------------------------------


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResultBlock:
    tool_use_id: str
    content: str  # JSON-encoded tool result


Block = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass(frozen=True)
class AgentMessage:
    role: Literal["user", "assistant"]
    content: list[Block]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True)
class AgentReply:
    """One provider turn, normalized."""

    text: str | None
    tool_calls: list[ToolCall]
    usage: TokenUsage = field(default_factory=TokenUsage)
    stop_reason: str = ""


@runtime_checkable
class SynthesisProvider(Protocol):
    """A backend that runs one tool-using turn and returns a normalized reply."""

    model_id: str

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[AgentMessage],
        tools: Sequence[ToolSpec],
    ) -> AgentReply: ...


@runtime_checkable
class ToolRunner(Protocol):
    """What the loop needs from the tool layer (SynthesisTools satisfies this)."""

    finished_roundup: str | None

    def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]: ...


StopReason = Literal["finish", "end_turn", "max_turns", "budget", "provider_error"]


@dataclass(frozen=True)
class AgentOutcome:
    roundup: str
    stop_reason: StopReason
    turns: int
    usage: TokenUsage
    degraded: bool  # True unless the model cleanly called finish()


def _degraded_roundup(last_text: str | None, stop_reason: StopReason) -> str:
    """Build a roundup when the agent didn't cleanly finish, so we still write something."""
    note = f"> _Synthesis stopped early ({stop_reason}); this roundup may be incomplete._"
    body = (last_text or "").strip()
    return f"{note}\n\n{body}".rstrip() if body else note


def run_agent(
    provider: SynthesisProvider,
    tools: ToolRunner,
    *,
    system_prompt: str,
    initial_user_text: str,
    tool_specs: Sequence[ToolSpec],
    max_turns: int = 20,
    token_budget: int | None = None,
) -> AgentOutcome:
    """Drive the tool-using agent to a (possibly degraded) roundup.

    `tools.dispatch` must never raise (it returns `{ok, ...}` results); the loop
    serializes each result back to the model as a tool_result block.
    """
    messages: list[AgentMessage] = [AgentMessage("user", [TextBlock(initial_user_text)])]
    usage = TokenUsage()
    last_text: str | None = None

    for turn in range(1, max_turns + 1):
        try:
            reply = provider.complete(system=system_prompt, messages=messages, tools=tool_specs)
        except ProviderError:
            return AgentOutcome(
                roundup=tools.finished_roundup or _degraded_roundup(last_text, "provider_error"),
                stop_reason="provider_error",
                turns=turn - 1,
                usage=usage,
                degraded=True,
            )

        usage = usage + reply.usage
        if reply.text:
            last_text = reply.text

        # Record the assistant turn (text + any tool_use requests) in history.
        assistant_blocks: list[Block] = []
        if reply.text:
            assistant_blocks.append(TextBlock(reply.text))
        assistant_blocks.extend(
            ToolUseBlock(tc.id, tc.name, tc.arguments) for tc in reply.tool_calls
        )
        messages.append(AgentMessage("assistant", assistant_blocks))

        if not reply.tool_calls:
            # The model stopped talking without calling finish — treat its text as
            # an implicit (degraded) roundup unless it already recorded one.
            if tools.finished_roundup is not None:
                return AgentOutcome(
                    tools.finished_roundup, "finish", turn, usage, degraded=False
                )
            return AgentOutcome(
                _degraded_roundup(last_text, "end_turn"), "end_turn", turn, usage, degraded=True
            )

        # Execute every requested tool and feed the results back.
        result_blocks: list[Block] = []
        for tc in reply.tool_calls:
            result = tools.dispatch(tc.name, tc.arguments)
            result_blocks.append(ToolResultBlock(tc.id, json.dumps(result, ensure_ascii=False)))
        messages.append(AgentMessage("user", result_blocks))

        if tools.finished_roundup is not None:
            return AgentOutcome(tools.finished_roundup, "finish", turn, usage, degraded=False)

        if token_budget is not None and usage.total >= token_budget:
            return AgentOutcome(
                tools.finished_roundup or _degraded_roundup(last_text, "budget"),
                "budget",
                turn,
                usage,
                degraded=True,
            )

    return AgentOutcome(
        tools.finished_roundup or _degraded_roundup(last_text, "max_turns"),
        "max_turns",
        max_turns,
        usage,
        degraded=tools.finished_roundup is None,
    )
