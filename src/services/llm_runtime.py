"""Invocation-scoped Azure OpenAI access shared by agent nodes."""

from __future__ import annotations

from typing import Any

from framework.schemas.invocation_context import InvocationContext
from src.services.progress_events import emit_progress


def resolve_azure_llm(
    state: dict[str, Any],
    injected: Any | None = None,
    *,
    max_tokens: int = 1200,
    timeout_s: float = 30.0,
    max_retry: int = 3,
) -> Any:
    """Build an Azure client from the current invocation's secret context."""
    if injected is not None:
        return injected
    from shared.services.llm.azure_openai_client import AzureOpenAIClient

    ctx = InvocationContext.from_state(state)
    return AzureOpenAIClient(
        {
            "api_key": ctx.secrets.require("AZURE_OPENAI_API_KEY"),
            "azure_endpoint": ctx.secrets.require("AZURE_OPENAI_ENDPOINT"),
            "azure_deployment": ctx.secrets.require("AZURE_OPENAI_DEPLOYMENT"),
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "timeout": timeout_s,
            "max_retries": max_retry,
        }
    )


def complete_text(
    state: dict[str, Any],
    messages: list[dict[str, str]],
    injected: Any | None = None,
    *,
    max_tokens: int = 1200,
    timeout_s: float = 30.0,
    max_retry: int = 3,
) -> str:
    """Complete chat messages and reject empty or credential-bearing output."""
    emit_progress("Contacting Azure OpenAI.", {"provider": "azure_openai", "stage": "llm_request"})
    client = resolve_azure_llm(
        state,
        injected,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
        max_retry=max_retry,
    )
    if hasattr(client, "complete"):
        response = client.complete(messages)
    elif hasattr(client, "generate"):
        system = next((item["content"] for item in messages if item.get("role") == "system"), "")
        user = "\n\n".join(item["content"] for item in messages if item.get("role") == "user")
        try:
            response = client.generate(system_prompt=system, user_prompt=user)
        except TypeError:
            response = client.generate(user)
    else:
        raise TypeError("LLM client must provide complete() or generate()")
    content = response.get("content", "") if isinstance(response, dict) else response
    text = str(content).strip()
    if not text:
        raise RuntimeError("Azure OpenAI returned an empty response")
    from shared.security import detect_credentials

    if detect_credentials(text):
        raise RuntimeError("Azure OpenAI response failed credential safety validation")
    emit_progress("Azure OpenAI response received.", {"provider": "azure_openai", "stage": "llm_request"})
    return text


def request_advisory(
    state: dict[str, Any],
    purpose: str,
    injected: Any | None = None,
    *,
    timeout_s: float = 30.0,
    max_retry: int = 3,
) -> str | None:
    """Exercise LLM reasoning without making provider availability a graph failure."""
    try:
        advisory = complete_text(
            state,
            [
                {
                    "role": "system",
                    "content": (
                        "You are a read-only quality reviewer. Return one short, non-sensitive sentence. "
                        "Do not include credentials, personal data, or instructions that change external systems."
                    ),
                },
                {"role": "user", "content": purpose[:1000]},
            ],
            injected,
            max_tokens=120,
            timeout_s=timeout_s,
            max_retry=max_retry,
        )
        state["generation_mode"] = "azure_openai"
        state["provider_error_message"] = None
        return advisory
    except Exception as exc:
        # Azure/provider failures are advisory and must never turn a valid
        # deterministic workflow into Graph status='error'.
        warning = "Azure OpenAI advisory failed or timed out; deterministic workflow output was used."
        state["generation_mode"] = "deterministic_fallback"
        state["provider_error_message"] = warning
        emit_progress(
            warning,
            {"provider": "azure_openai", "status": "fallback", "error_type": type(exc).__name__},
        )
        return None


def provider_metadata(state: dict[str, Any]) -> dict[str, str | None]:
    """Return checkpoint-safe provider status after an advisory attempt."""
    mode = str(state.get("generation_mode") or "deterministic_fallback")
    warning = str(state.get("provider_error_message") or "").strip()
    return {
        "generation_mode": mode,
        "provider_error_message": warning or None,
    }
