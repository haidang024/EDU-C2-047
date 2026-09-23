"""Unit coverage for invocation-scoped Azure OpenAI runtime behavior."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

from src.services.llm_runtime import complete_text, request_advisory


class _CompleteClient:
    def complete(self, messages: list[dict[str, str]]) -> dict[str, str]:
        assert messages
        return {"content": "Grounded review complete."}


class _FailingClient:
    def complete(self, messages: list[dict[str, str]]) -> str:
        raise RuntimeError("provider unavailable")


def test_complete_text_supports_injected_client() -> None:
    assert complete_text({}, [{"role": "user", "content": "Review"}], _CompleteClient()) == "Grounded review complete."


def test_advisory_provider_failure_never_raises() -> None:
    state = {}
    assert request_advisory(state, "Review safely", _FailingClient()) is None
    assert state["generation_mode"] == "deterministic_fallback"
    assert "failed or timed out" in state["provider_error_message"]


def test_resolve_azure_llm_uses_three_invocation_secrets(monkeypatch: Any) -> None:
    from src.services import llm_runtime

    requested: list[str] = []

    class _Secrets:
        def require(self, name: str) -> str:
            requested.append(name)
            return f"value-for-{name}"

    class _Context:
        secrets = _Secrets()

    captured: dict[str, Any] = {}

    class _AzureClient:
        def __init__(self, config: dict[str, Any]) -> None:
            captured.update(config)

    monkeypatch.setattr(llm_runtime.InvocationContext, "from_state", lambda state: _Context())
    monkeypatch.setitem(
        sys.modules,
        "shared.services.llm.azure_openai_client",
        SimpleNamespace(AzureOpenAIClient=_AzureClient),
    )

    client = llm_runtime.resolve_azure_llm({}, timeout_s=12.5, max_retry=1)
    assert isinstance(client, _AzureClient)
    assert requested == [
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
    ]
    assert captured["azure_endpoint"] == "value-for-AZURE_OPENAI_ENDPOINT"
    assert captured["timeout"] == 12.5
    assert captured["max_retries"] == 1


def test_graph_exposes_provider_status() -> None:
    from src.graph.graph import Graph

    graph = Graph(config={})
    output = graph.get_output(
        {
            "status": "success",
            "formatted_output": "ok",
            "generation_mode": "deterministic_fallback",
            "provider_error_message": "provider unavailable",
        }
    )

    assert output["generation_mode"] == "deterministic_fallback"
    assert output["provider_error_message"] == "provider unavailable"
