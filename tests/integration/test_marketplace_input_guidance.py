from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Graph


def test_missing_marketplace_config_returns_readable_guidance():
    graph = Graph(config={})
    graph.compile()
    result = graph.invoke("Hello", ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL), input_context={"conversation_history": []})
    assert result["status"] == "success"
    assert result["output"].startswith("Accommodation plan consistency request could not be processed.")
    assert "configuration is incomplete" in result["output"]
