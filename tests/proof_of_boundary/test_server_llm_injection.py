"""Proof that Azure OpenAI clients are created per invocation, never at server import."""

from __future__ import annotations

import importlib


def test_server_keeps_llm_invocation_scoped() -> None:
    import src.api.server as server

    importlib.reload(server)
    assert server._llm is None
    assert server._namespace == "agent1000"


def test_server_chains_environment_secrets_first() -> None:
    import src.api.server as server

    providers = server._secrets_provider._providers
    assert providers
    assert providers[0].__class__.__name__ == "EnvProvider"
