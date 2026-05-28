"""Tests for the Gemini + Ollama providers and the provider factory.

The SDK clients are injected (fakes), so these exercise the wire format + error
handling without any network. The real API path is covered only by the golden
eval, which is opt-in.
"""

from __future__ import annotations

from typing import Any

import pytest

from researcher_agent.config import ClassifierConfig
from researcher_agent.llm.base import ClassifierInput, ProviderError
from researcher_agent.llm.factory import build_classifier_provider
from researcher_agent.llm.gemini import GeminiProvider
from researcher_agent.llm.ollama import OllamaProvider

INPUTS = [ClassifierInput(id="1", title="t", url="https://e.com/1")]
GOOD_JSON = '[{"id":"1","topic":"tooling","score":5,"rationale":"r"}]'


# --- Gemini --------------------------------------------------------------------


class _FakeGeminiClient:
    def __init__(self, text: str, record: dict[str, Any]) -> None:
        self.models = self._Models(text, record)

    class _Models:
        def __init__(self, text: str, record: dict[str, Any]) -> None:
            self._text = text
            self._record = record

        def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
            self._record.update(model=model, contents=contents, config=config)
            return type("R", (), {"text": self._text})()


def test_gemini_classify_parses() -> None:
    p = GeminiProvider(model="gemini-2.5-flash", client=_FakeGeminiClient(GOOD_JSON, {}))
    out = p.classify("SYS", INPUTS, temperature=0.2)
    assert out["1"].topic == "tooling"
    assert p.model_id == "gemini:gemini-2.5-flash"


def test_gemini_passes_system_and_temperature() -> None:
    rec: dict[str, Any] = {}
    p = GeminiProvider(client=_FakeGeminiClient(GOOD_JSON, rec))
    p.classify("MY SYSTEM", INPUTS, temperature=0.7)
    assert rec["config"].system_instruction == "MY SYSTEM"
    assert rec["config"].temperature == 0.7
    assert rec["config"].response_mime_type == "application/json"


def test_gemini_error_is_wrapped() -> None:
    class _Boom:
        def generate_content(self, **kw: Any) -> Any:
            raise RuntimeError("network down")

    client = type("C", (), {"models": _Boom()})()
    p = GeminiProvider(client=client)
    with pytest.raises(ProviderError):
        p.classify("S", INPUTS, temperature=0.0)


# --- Ollama --------------------------------------------------------------------


def test_ollama_parses_dict_response() -> None:
    p = OllamaProvider(model="llama3.1", client=lambda **kw: {"message": {"content": GOOD_JSON}})
    out = p.classify("S", INPUTS, temperature=0.1)
    assert out["1"].topic == "tooling"
    assert p.model_id == "ollama:llama3.1"


def test_ollama_parses_object_response() -> None:
    resp = type("Resp", (), {"message": type("Msg", (), {"content": GOOD_JSON})()})()
    p = OllamaProvider(client=lambda **kw: resp)
    out = p.classify("S", INPUTS, temperature=0.0)
    assert out["1"].topic == "tooling"


def test_ollama_passes_format_and_temperature() -> None:
    rec: dict[str, Any] = {}

    def fake_chat(**kw: Any) -> Any:
        rec.update(kw)
        return {"message": {"content": GOOD_JSON}}

    p = OllamaProvider(client=fake_chat)
    p.classify("SYS", INPUTS, temperature=0.3)
    assert rec["format"] == "json"
    assert rec["options"]["temperature"] == 0.3
    assert rec["messages"][0]["role"] == "system"
    assert rec["messages"][0]["content"] == "SYS"


def test_ollama_error_is_wrapped() -> None:
    def boom(**kw: Any) -> Any:
        raise RuntimeError("connection refused")

    p = OllamaProvider(client=boom)
    with pytest.raises(ProviderError):
        p.classify("S", INPUTS, temperature=0.0)


# --- factory -------------------------------------------------------------------


def _cfg(provider: str, model: str = "m") -> ClassifierConfig:
    return ClassifierConfig(provider=provider, model=model)  # type: ignore[arg-type]


def test_factory_builds_ollama() -> None:
    p = build_classifier_provider(_cfg("ollama", "llama3.1"))
    assert isinstance(p, OllamaProvider)


def test_factory_gemini_without_key_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ProviderError):
        build_classifier_provider(_cfg("gemini", "gemini-2.5-flash"))


def test_factory_anthropic_not_supported() -> None:
    with pytest.raises(ProviderError):
        build_classifier_provider(_cfg("anthropic", "claude-sonnet-4-5"))
