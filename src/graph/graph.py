"""Outer Graph for EDU-C2-047 — Cat 2 AgentBaseGraph."""

from __future__ import annotations

from typing import Any, ClassVar, cast

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.graph.base_graph import BaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State

# Validated fields PreProcessNode writes to outer state that inner nodes read from
# flat state (briefing metadata, retrieval config). Forwarded explicitly because the
# inner graph constructs its own initial State.
_FORWARDED_TO_SUBGRAPH = (
    "institution_id",
    "review_period",
    "output_format",
    "field_mapping",
    "record_query_selector",
    "freshness_threshold_days",
    "guidance_index_ref",
    "guidance_version",
    "guidance_last_updated",
    "guidance_staleness_warning",
    "retrieval_source_type",
    "retrieval_inline_records",
)


class AccommodationPlanWorkflowNode(GraphNode):
    """GraphNode wrapper for the inner accommodation-plan consistency workflow.

    Assigned to the `main` slot of the outer AgentBaseGraph.
    Delegates all four domain steps to DomainWorkflowGraph.
    """

    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        llm: Any | None = None,
        **kwargs: Any,
    ) -> None:
        """Keep the parent config and injectable client at the graph boundary."""
        self._config = dict(config or {})
        self._llm = llm if llm is not None else self._config.get("llm")
        super().__init__(**kwargs)

    def get_subgraph(self) -> BaseGraph:
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        subgraph_config = dict(self._config)
        subgraph_config["llm"] = self._llm
        return DomainWorkflowGraph(config=subgraph_config)

    def extract_input(self, state: AgentState) -> str:
        return state.get("validated_input", state.get("user_input", ""))

    def execute(self, state: AgentState) -> dict:
        """Forward outer input_context into the inner graph so domain nodes can read config."""
        if state.get("input_error_message"):
            return {"status": AgentStatus.SUCCESS.value}

        from framework.schemas.invocation_context import InvocationContext

        subgraph = self.get_subgraph()
        user_input = self.extract_input(state)
        ctx = InvocationContext.from_state(state)
        # The inner graph builds a fresh State from user_input + input_context, so
        # PreProcessNode's validated fields do not cross the boundary on their own.
        # BaseGraph.invoke() takes no extra state kwargs, so hand them over through
        # the subclass hook the framework provides for exactly this
        # (_extra_initial_state) — the briefing metadata and retrieval config read
        # them from flat state.
        subgraph.set_forwarded_state(
            {key: state.get(key) for key in _FORWARDED_TO_SUBGRAPH if state.get(key) is not None}
        )
        sub_result = subgraph.invoke(
            user_input,
            session_id=ctx.session_id,
            ctx=ctx,
            input_context=state.get("input_context", {}),
        )
        from framework.errors import SubgraphError

        if sub_result.get("status") == "error":
            if self.error_strategy == "propagate":
                raise SubgraphError(
                    agent_name=subgraph.name,
                    error_log=sub_result.get("error_log", []),
                    trace_id=sub_result.get("trace_id", ""),
                )
        return self.merge_output(state, sub_result)

    def merge_output(self, state: AgentState, sub_result: dict) -> dict:
        """Map inner graph output fields back into outer state (changed keys only)."""
        return {
            "result": sub_result.get("briefing_markdown", ""),
            "consistency_findings": sub_result.get("consistency_findings", []),
            "findings_suppressed_count": sub_result.get("findings_suppressed_count", 0),
            "guidance_staleness_warning": sub_result.get("guidance_staleness_warning", ""),
            "retrieval_provenance": sub_result.get("retrieval_provenance", {}),
            "output_gate_blocked": sub_result.get("output_gate_blocked", False),
            "output_gate_reason": sub_result.get("output_gate_reason", ""),
            "retrieval_error": sub_result.get("retrieval_error", ""),
            "check_error": sub_result.get("check_error", ""),
            "status": sub_result.get("status"),
            "error_log": sub_result.get("error_log", []),
        }


class Graph(AgentBaseGraph):
    """Cat 2 outer graph for EDU-C2-047 — Student Accommodation Plan Consistency Agent.

    Backbone: initialize → pre_process → main → post_process → finalize (fixed).
    Domain logic lives inside AccommodationPlanWorkflowNode (main slot).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    @property
    def name(self) -> str:
        return "EDU-C2-047"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = AccommodationPlanWorkflowNode(
            config=self.config,
            llm=self.config.get("llm"),
        )
        self._nodes["post_process"] = PostProcessNode(
            llm=self.config.get("llm"),
            config=self.config,
        )

    def get_output(self, state: AgentState) -> dict[str, Any]:
        output = cast(dict[str, Any], super().get_output(state))
        output["generation_mode"] = state.get("generation_mode")
        output["provider_error_message"] = state.get("provider_error_message")
        _set_marketplace_guidance(output, state, "Accommodation plan consistency request")
        return output

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


def _set_marketplace_guidance(output: dict[str, Any], state: AgentState, subject: str) -> None:
    context = state.get("input_context")
    message = state.get("input_error_message")
    if not (isinstance(context, dict) and "conversation_history" in context and message):
        return
    lines = [f"{subject} could not be processed.", "", f"Reason: {message}"]
    guidance = state.get("input_error_guidance")
    if isinstance(guidance, list) and guidance:
        lines.extend(["", "How to continue:"])
        lines.extend(f"- {item}" for item in guidance)
    output["output"] = "\n".join(lines)
