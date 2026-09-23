"""DomainWorkflowGraph — inner BaseGraph for the accommodation-plan consistency pipeline."""

from __future__ import annotations

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.record_retrieval_node import RecordRetrievalNode
from src.nodes.consistency_check_node import ConsistencyCheckNode
from src.nodes.briefing_generation_node import BriefingGenerationNode
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    """Inner four-step pipeline for EDU-C2-047.

    Pipeline (linear with failure short-circuit):
        START → record_retrieval → consistency_check → briefing_generation → END

    Failure at record_retrieval or consistency_check sets status=ERROR;
    briefing_generation self-guards against upstream failure (see its execute()).
    All inner nodes use TrustLevel.ANONYMOUS — trust verified at outer boundary.
    """

    @property
    def name(self) -> str:
        return "edu_c2_047_accommodation_plan_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        pass  # No mandatory config keys for the inner graph

    def set_forwarded_state(self, fields: dict) -> None:
        """Seed validated outer-state fields into this graph's initial State.

        BaseGraph.invoke() builds its initial State from user_input and
        input_context only, so fields PreProcessNode validated on the outer state
        would otherwise be lost at the boundary. Set immediately before invoke();
        the graph instance is constructed per invocation by
        AccommodationPlanWorkflowNode.get_subgraph(), so this holds no state across
        invocations.
        """
        self._forwarded_state = dict(fields)

    def _extra_initial_state(self) -> dict:
        """Framework hook — merge the forwarded fields into the initial State."""
        return dict(getattr(self, "_forwarded_state", {}))

    def register_nodes(self) -> None:
        # No super() call — BaseGraph.register_nodes() is abstract.
        # Do NOT register initialize / finalize (outer backbone concerns).
        self._nodes["record_retrieval"] = RecordRetrievalNode()
        self._nodes["consistency_check"] = ConsistencyCheckNode(llm=self.config.get("llm"))
        self._nodes["briefing_generation"] = BriefingGenerationNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "record_retrieval")
        self._sg.add_conditional_edges("record_retrieval", self.route)
        self._sg.add_conditional_edges("consistency_check", self.route)
        self._sg.add_edge("briefing_generation", END)

    def route(self, state: AgentState) -> str:
        """Short-circuit to END on retrieval or check failure; otherwise advance."""
        status = state.get("status")
        if status == AgentStatus.ERROR.value:
            return str(END)
        current = state.get("node_history", [])
        if current and current[-1] == "record_retrieval":
            return "consistency_check"
        return "briefing_generation"

    def get_output(self, state: AgentState) -> dict:
        """Shape the sub_result dict for DomainWorkflowGraphNode.merge_output()."""
        return {
            "briefing_markdown": state.get("briefing_markdown", ""),
            "consistency_findings": state.get("consistency_findings", []),
            "findings_suppressed_count": state.get("findings_suppressed_count", 0),
            "guidance_staleness_warning": state.get("guidance_staleness_warning", ""),
            "retrieval_provenance": state.get("retrieval_provenance", {}),
            "output_gate_blocked": state.get("output_gate_blocked", False),
            "output_gate_reason": state.get("output_gate_reason", ""),
            "retrieval_error": state.get("retrieval_error", ""),
            "check_error": state.get("check_error", ""),
            "status": state.get("status"),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
