"""PostProcessNode — finalise and deliver the accommodation-plan consistency briefing."""

from __future__ import annotations

import re
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.llm_runtime import provider_metadata, request_advisory

# Defence-in-depth: outer boundary scan for eligibility/decision language
_OUTER_GATE_RE = re.compile(
    r"\b(approved|denied|eligible|ineligible|grant\b|reject\b|write.back|update.record)\b",
    re.IGNORECASE,
)


class PostProcessNode(FunctionNode):
    """Format and deliver the review briefing as the agent's formatted_output (outer boundary).

    Outer Cat 2 boundary node — VERIFIED_EXTERNAL, same level as PreProcessNode.
    Wraps the inner pipeline's briefing_markdown into the API response envelope.
    Never emits a briefing when the inner pipeline reported an error.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: object | None = None, config: dict | None = None) -> None:
        super().__init__()
        self._llm = llm
        self._config = config or {}

    def execute(self, state: dict) -> dict:
        if state.get("input_error_message"):
            message = str(state["input_error_message"])
            return {"status": AgentStatus.SUCCESS.value, "result": message, "formatted_output": message}

        request_advisory(
            state,
            "Review the EDU-C2-047 result for clarity, grounding, and safe human review.",
            self._llm,
            timeout_s=float(self._config.get("timeout_s", 30.0)),
            max_retry=int(self._config.get("max_retry", 3)),
        )
        metadata = provider_metadata(state)
        retrieval_error = state.get("retrieval_error", "")
        check_error = state.get("check_error", "")
        briefing_markdown = state.get("result", "") or state.get("briefing_markdown", "")

        # Hard stop: never deliver output after any upstream failure
        if retrieval_error or check_error:
            error_detail = retrieval_error or check_error
            emit_trace_event(
                "post_process_blocked",
                {"reason": "upstream_failure", "error": error_detail},
                state,
            )
            return {
                "formatted_output": "",
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PostProcessNode: output blocked due to upstream failure — {error_detail}"],
                **metadata,
            }

        output_gate_blocked = state.get("output_gate_blocked", False)
        staleness_warning = state.get("guidance_staleness_warning", "")
        findings_count = len(state.get("consistency_findings", []))
        suppressed_count = state.get("findings_suppressed_count", 0)

        emit_trace_event(
            "post_process_complete",
            {
                "findings_count": findings_count,
                "suppressed_count": suppressed_count,
                "output_gate_blocked": output_gate_blocked,
                "has_staleness_warning": bool(staleness_warning),
                "briefing_length": len(briefing_markdown),
            },
            state,
        )

        return {
            "formatted_output": briefing_markdown,
            "status": AgentStatus.SUCCESS.value,
            **metadata,
        }

    def _extra_security_gate_output(self, result: dict) -> dict:
        """S-3 extension: outer boundary scan for eligibility/decision language in formatted_output."""
        output = result.get("formatted_output", "")
        if output and _OUTER_GATE_RE.search(output):
            from framework.errors import SecurityViolationError

            raise SecurityViolationError(
                "PostProcessNode: prohibited eligibility/decision language detected in formatted_output"
            )
        return result
