"""conftest.py — framework/langgraph/shared test stubs for EDU-C2-047.

These stubs let the test-suite run without the real ``agenticstar-agentcore``
wheel installed (local dev, offline CI arms). They are installed **only when the
real framework is not importable** — in a CI arm that provisions the real wheel
this file is a no-op, so the stubs never shadow the production package.

The stubbed ``BaseNode.__call__`` reproduces the security pipeline exercised by
the proof-of-boundary tests:

    S-1 trust gate → S-4 node_start → S-2 _security_gate_input()
      → execute() → S-3 _security_gate_output() → S-4 node_complete
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from enum import Enum

ROOT = os.path.abspath(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _framework_installed() -> bool:
    try:
        return importlib.util.find_spec("framework") is not None
    except (ImportError, ValueError):
        return False


def _register_module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _install_stubs() -> None:
    # ── langgraph ────────────────────────────────────────────────────────
    langgraph = _register_module("langgraph")
    lg_graph = _register_module("langgraph.graph")
    lg_graph.START = "START"
    lg_graph.END = "END"
    langgraph.graph = lg_graph

    lg_types = _register_module("langgraph.types")
    lg_types.interrupt = lambda value: value  # HITL stub — returns immediately
    langgraph.types = lg_types

    lg_errors = _register_module("langgraph.errors")

    class GraphInterrupt(Exception):
        """Stub of langgraph.errors.GraphInterrupt (HITL bubble-up signal)."""

    class GraphBubbleUp(Exception):
        pass

    lg_errors.GraphInterrupt = GraphInterrupt
    lg_errors.GraphBubbleUp = GraphBubbleUp
    langgraph.errors = lg_errors

    # ── Enums ────────────────────────────────────────────────────────────
    class AgentStatus(str, Enum):
        PENDING = "pending"
        SUCCESS = "success"
        RETRY = "retry"
        ERROR = "error"
        TIMEOUT = "timeout"
        AWAITING_HUMAN = "awaiting_human"
        CANCELLED = "cancelled"

    class TrustLevel(int, Enum):
        ANONYMOUS = 0
        VERIFIED_EXTERNAL = 1
        INTERNAL = 2

    class HitlStatus(str, Enum):
        APPROVED = "approved"
        REJECTED = "rejected"
        CORRECTED = "corrected"

    _NAME2LEVEL = {"ANONYMOUS": 0, "VERIFIED_EXTERNAL": 1, "INTERNAL": 2}

    def _coerce_level(value) -> int:
        if isinstance(value, TrustLevel):
            return value.value
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            s = value.strip().upper()
            return int(s) if s.isdigit() else _NAME2LEVEL.get(s, 0)
        return 0

    # ── Errors ───────────────────────────────────────────────────────────
    class SecurityViolationError(Exception):
        pass

    class ConfigError(Exception):
        pass

    class MissingSecret(Exception):
        pass

    class SubgraphError(Exception):
        pass

    class SubgraphTimeoutError(Exception):
        pass

    class MissingNodeError(Exception):
        pass

    # ── Secrets ──────────────────────────────────────────────────────────
    class _StubSecretProvider:
        """Deterministic mock secret provider for tests (never a real secret)."""

        def require(self, key: str) -> str:
            return f"mock-secret-handle::{key}"

        def get(self, key: str, default=None):
            return f"mock-secret-handle::{key}"

        def require_at_compile(self, keys) -> None:
            return None

    class InvocationContext:
        def __init__(self, correlation_id="", session_id="", thread_id="",
                     caller_id="", caller_trust_level=TrustLevel.ANONYMOUS,
                     secrets=None, hitl_allowed=True, **kwargs) -> None:
            self.correlation_id = correlation_id
            self.session_id = session_id
            self.thread_id = thread_id
            self.caller_id = caller_id
            self.caller_trust_level = caller_trust_level
            self.secrets = secrets or _StubSecretProvider()
            self.hitl_allowed = hitl_allowed

        @classmethod
        def from_state(cls, state: dict) -> "InvocationContext":
            return cls(
                correlation_id=state.get("correlation_id", ""),
                session_id=state.get("session_id", ""),
                caller_id=state.get("caller_id", ""),
                caller_trust_level=state.get("caller_trust_level", TrustLevel.ANONYMOUS),
                secrets=_StubSecretProvider(),
                hitl_allowed=state.get("hitl_allowed", True),
            )

        @classmethod
        def for_internal(cls, caller_id="system") -> "InvocationContext":
            return cls(caller_id=caller_id, caller_trust_level=TrustLevel.INTERNAL)

    def bound_secrets(provider):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield provider

        return _cm()

    # ── Audit logger ─────────────────────────────────────────────────────
    def emit_trace_event(event_type: str, payload: dict, state: dict | None = None) -> None:
        # No-op stub. PB-1 patches the node module's binding; PB-6 patches
        # framework.nodes.base_node.emit_trace_event to observe call order.
        return None

    # ── BaseNode + security pipeline ─────────────────────────────────────
    base_node_mod = _register_module("framework.nodes.base_node")
    base_node_mod.emit_trace_event = emit_trace_event

    class BaseNode:
        required_trust_level = TrustLevel.ANONYMOUS

        def __init__(self) -> None:  # real FunctionNode takes no args
            pass

        def execute(self, state: dict) -> dict:
            raise NotImplementedError

        def _security_gate_input(self, state: dict) -> dict:
            if hasattr(self, "_extra_security_gate_input"):
                return self._extra_security_gate_input(state)
            return state

        def _security_gate_output(self, result: dict) -> dict:
            if hasattr(self, "_extra_security_gate_output"):
                return self._extra_security_gate_output(result)
            return result

        def __call__(self, state: dict) -> dict:
            import framework.nodes.base_node as _bn

            required = getattr(self, "required_trust_level", TrustLevel.ANONYMOUS)
            required_level = required.value if isinstance(required, TrustLevel) else _coerce_level(required)
            caller_level = _coerce_level(state.get("caller_trust_level"))
            if caller_level < required_level:
                _bn.emit_trace_event("s1_denied", {"required": required_level}, state)
                return {
                    "status": AgentStatus.ERROR.value,
                    "error": (
                        "S-1 trust gate: insufficient trust level "
                        f"(caller={caller_level} < required={required_level})"
                    ),
                }

            _bn.emit_trace_event("node_start", {}, state)
            try:
                state = self._security_gate_input(state)
                result = self.execute(state)
                result = self._security_gate_output(result)
            except SecurityViolationError as exc:
                _bn.emit_trace_event("node_error", {"error": str(exc)}, state)
                return {"status": AgentStatus.ERROR.value, "error": str(exc)}
            _bn.emit_trace_event("node_complete", {}, state)
            return result

    base_node_mod.BaseNode = BaseNode

    class FunctionNode(BaseNode):
        pass

    class GraphNode(BaseNode):
        error_strategy = "propagate"
        propagate_hitl = False

        def __init__(self, config: dict | None = None, **kwargs) -> None:
            super().__init__()
            self._config = config or {}

        def get_subgraph(self):
            raise NotImplementedError

        def extract_input(self, state: dict):
            raise NotImplementedError

        def merge_output(self, state: dict, sub_result: dict) -> dict:
            raise NotImplementedError

        def execute(self, state: dict) -> dict:
            sub = self.get_subgraph()
            payload = self.extract_input(state)
            ctx = InvocationContext.from_state(state)
            sub_result = sub.invoke(payload, session_id=ctx.session_id, ctx=ctx,
                                    input_context=state.get("input_context"))
            return self.merge_output(state, sub_result)

    class RemoteAgentNode(BaseNode):
        pass

    # ── Graphs ───────────────────────────────────────────────────────────
    class _StubStateGraph:
        def add_edge(self, _a, _b): pass
        def add_conditional_edges(self, _s, _fn, _m=None): pass

    class BaseGraph:
        def __init__(self, config: dict | None = None) -> None:
            self._config = config or {}
            self._nodes: dict = {}
            self._sg = _StubStateGraph()
            self._secrets_provider = _StubSecretProvider()
            self.register_nodes()
            self.add_edges()

        def register_nodes(self): pass
        def add_edges(self): pass
        def route(self, state): return "END"
        def get_output(self, state): return dict(state)
        def compile(self): return self
        def provision_secrets(self, provider) -> None:
            self._secrets_provider = provider

        def _execution_order(self):
            return list(self._nodes.items())

        def invoke(self, user_input, session_id: str = "", ctx=None,
                   input_context: dict | None = None, **kwargs) -> dict:
            state: dict = {}
            if isinstance(user_input, dict):
                state.update(user_input)
                state.setdefault("user_input", user_input)
            else:
                state["user_input"] = user_input
            if input_context:
                state["input_context"] = input_context
            if ctx is not None:
                state.update({
                    "session_id": getattr(ctx, "session_id", session_id or ""),
                    "correlation_id": getattr(ctx, "correlation_id", ""),
                    "caller_trust_level": getattr(getattr(ctx, "caller_trust_level", None), "value", 1),
                    "caller_id": getattr(ctx, "caller_id", ""),
                    "hitl_allowed": getattr(ctx, "hitl_allowed", True),
                })
            else:
                state.setdefault("caller_trust_level", TrustLevel.VERIFIED_EXTERNAL.value)
            for _key, node in self._execution_order():
                result = node(state)
                if isinstance(result, dict):
                    state.update(result)
            return self.get_output(state)

    class _InitializeNode(FunctionNode):
        def execute(self, state): return {}

    class _FinalizeNode(FunctionNode):
        def execute(self, state): return {}

    class AgentBaseGraph(BaseGraph):
        def register_nodes(self):
            self._nodes["initialize"] = _InitializeNode()
            self._nodes["finalize"] = _FinalizeNode()

        def _execution_order(self):
            order = ["initialize", "pre_process", "main", "post_process", "finalize"]
            items = [(k, self._nodes[k]) for k in order if k in self._nodes]
            for k, v in self._nodes.items():
                if k not in order:
                    items.append((k, v))
            return items

        def get_output(self, state):
            return {
                "output": state.get("formatted_output") or state.get("result"),
                "status": state.get("status"),
                "correlation_id": state.get("correlation_id"),
            }

    class AutonomousBaseGraph(BaseGraph):
        pass

    class AgentState(dict):
        pass

    class AutonomousState(dict):
        pass

    # ── Register framework.* module tree ─────────────────────────────────
    def _mod(name, **attrs):
        m = _register_module(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        return m

    framework = _register_module("framework")
    _register_module("framework.nodes")
    _register_module("framework.graph")
    _register_module("framework.schemas")
    _register_module("framework.secrets")
    _register_module("framework.utils")

    _mod("framework.nodes.function_node", FunctionNode=FunctionNode)
    _mod("framework.nodes.graph_node", GraphNode=GraphNode)
    _mod("framework.nodes.remote_agent_node", RemoteAgentNode=RemoteAgentNode)
    _mod("framework.graph.base_graph", BaseGraph=BaseGraph)
    _mod("framework.graph.agent_base_graph", AgentBaseGraph=AgentBaseGraph)
    _mod("framework.graph.autonomous_base_graph", AutonomousBaseGraph=AutonomousBaseGraph)
    _mod("framework.schemas.agent_state", AgentState=AgentState)
    _mod("framework.schemas.autonomous_state", AutonomousState=AutonomousState)
    _mod("framework.schemas.agent_status", AgentStatus=AgentStatus)
    _mod("framework.schemas.trust_level", TrustLevel=TrustLevel)
    _mod("framework.schemas.hitl_status", HitlStatus=HitlStatus)
    _mod("framework.schemas.invocation_context", InvocationContext=InvocationContext)
    _mod("framework.secrets.context", bound_secrets=bound_secrets)
    _mod("framework.secrets.base", SecretProvider=_StubSecretProvider, MissingSecret=MissingSecret)

    errors_mod = _mod(
        "framework.errors",
        SecurityViolationError=SecurityViolationError,
        ConfigError=ConfigError,
        MissingSecret=MissingSecret,
        SubgraphError=SubgraphError,
        SubgraphTimeoutError=SubgraphTimeoutError,
        MissingNodeError=MissingNodeError,
    )

    framework.errors = errors_mod
    framework.AgentBaseGraph = AgentBaseGraph
    framework.AutonomousBaseGraph = AutonomousBaseGraph
    framework.BaseGraph = BaseGraph
    framework.BaseNode = BaseNode
    framework.FunctionNode = FunctionNode
    framework.GraphNode = GraphNode
    framework.AgentState = AgentState
    framework.AgentStatus = AgentStatus
    framework.TrustLevel = TrustLevel
    framework.InvocationContext = InvocationContext

    # ── shared.* ─────────────────────────────────────────────────────────
    _register_module("shared")
    _register_module("shared.utils")
    _mod("shared.utils.audit_logger", emit_trace_event=emit_trace_event)

    def _secrets_factory(namespace: str = "", agent_name: str = "", **kwargs):
        return _StubSecretProvider()

    _register_module("shared.secrets")
    _mod("shared.secrets", factory=_secrets_factory)


if not _framework_installed():
    _install_stubs()
