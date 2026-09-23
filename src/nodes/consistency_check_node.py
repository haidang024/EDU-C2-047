"""ConsistencyCheckNode — retrieve guidance passages and perform grounded consistency analysis (Step 3)."""

from __future__ import annotations

import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

# Pattern to detect prompt-injection-like text in retrieved guidance passages
_INJECTION_RE = re.compile(
    r"(ignore (previous|all|above)|system prompt|you are now|act as|disregard|override)",
    re.IGNORECASE,
)

# Required fields every finding must contain
_REQUIRED_FINDING_FIELDS = {
    "review_ref",
    "plan_field",
    "guidance_section",
    "guidance_source",
    "guidance_version",
    "severity",
    "rationale",
    "requires_human_confirmation",
}

# Autonomous decision / eligibility language to reject in LLM output
_PROHIBITED_OUTPUT_RE = re.compile(
    r"\b(approved|denied|eligible|ineligible|grant|reject|recommend|remediat|notify|"
    r"send|email|write.back|update.record)\b",
    re.IGNORECASE,
)


class ConsistencyCheckNode(FunctionNode):
    """Retrieve approved guidance passages and run grounded consistency analysis (Step 3).

    Inner Cat 2 node — trust already verified at boundary.
    Every finding must include a citation; unsupported findings are suppressed.
    No raw student identifiers enter the LLM prompt.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def __init__(self, llm: Any | None = None) -> None:
        super().__init__()
        self._llm = llm

    def execute(self, state: dict) -> dict:
        sanitized_records = state.get("sanitized_plan_records", [])
        guidance_index_ref = state.get("guidance_index_ref", "")
        guidance_version = state.get("guidance_version", "unknown")
        review_period = state.get("review_period", "")

        if not sanitized_records:
            emit_trace_event(
                "consistency_check_skipped",
                {"reason": "no sanitized records", "review_period": review_period},
                state,
            )
            return {
                "consistency_findings": [],
                "findings_suppressed_count": 0,
                "check_error": "",
                "status": AgentStatus.SUCCESS.value,
            }

        all_findings: list[dict] = []
        suppressed = 0

        for record in sanitized_records:
            review_ref = record.get("review_ref", "PLAN-????")

            # ── Retrieve bounded guidance passages ────────────────────────────
            passages = _retrieve_guidance_passages(guidance_index_ref, record)

            # ── Guard: reject instruction-like content in guidance ────────────
            clean_passages = []
            for p in passages:
                if _INJECTION_RE.search(p.get("text", "")):
                    suppressed += 1
                    emit_trace_event(
                        "consistency_check_injection_suppressed",
                        {"review_ref": review_ref, "passage_id": p.get("id", "")},
                        state,
                    )
                    continue
                clean_passages.append(p)

            if not clean_passages:
                continue

            # ── LLM-based grounded comparison ─────────────────────────────────
            try:
                raw_findings = _invoke_grounded_comparison(record, clean_passages, guidance_version)
            except Exception as exc:
                msg = f"ConsistencyCheckNode: LLM comparison failed for {review_ref} — {exc}"
                emit_trace_event("consistency_check_error", {"review_ref": review_ref, "error": msg}, state)
                return {
                    "check_error": msg,
                    "status": AgentStatus.ERROR.value,
                    "error_log": [msg],
                }

            # ── Validate and filter findings ──────────────────────────────────
            for finding in raw_findings:
                valid, reason = _validate_finding(finding)
                if not valid:
                    suppressed += 1
                    emit_trace_event(
                        "consistency_check_finding_suppressed",
                        {"review_ref": review_ref, "reason": reason},
                        state,
                    )
                    continue

                # Check for prohibited autonomous-decision language in rationale
                if _PROHIBITED_OUTPUT_RE.search(finding.get("rationale", "")):
                    suppressed += 1
                    emit_trace_event(
                        "consistency_check_finding_suppressed",
                        {"review_ref": review_ref, "reason": "prohibited decision language in rationale"},
                        state,
                    )
                    continue

                all_findings.append(finding)

        emit_trace_event(
            "consistency_check_complete",
            {
                "total_records": len(sanitized_records),
                "findings_count": len(all_findings),
                "suppressed_count": suppressed,
                "guidance_version": guidance_version,
            },
            state,
        )

        return {
            "consistency_findings": all_findings,
            "findings_suppressed_count": suppressed,
            "check_error": "",
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_output(self, result: dict) -> dict:
        """S-3 extension: scan findings for PII or prohibited decision language."""
        for finding in result.get("consistency_findings", []):
            rationale = finding.get("rationale", "")
            if _PROHIBITED_OUTPUT_RE.search(rationale):
                from framework.errors import SecurityViolationError

                raise SecurityViolationError(
                    "ConsistencyCheckNode: prohibited decision/action language detected in finding rationale"
                )
        return result


def _retrieve_guidance_passages(index_ref: str, record: dict) -> list[dict]:
    """Retrieve bounded guidance passages relevant to this plan record.

    In production, queries the vector/document store keyed by index_ref.
    Returns synthetic passages for testing.
    """
    # Synthetic stub — returns minimal passages keyed off accommodation_type
    accommodation_type = record.get("accommodation_type", "standard")
    return [
        {
            "id": f"{index_ref[:8]}-accommodation-type",
            "text": (
                f"Accommodation type '{accommodation_type}' requires documented approval "
                "within 30 days of application date per Policy §4.2."
            ),
            "section": "§4.2",
            "source": "Accommodation_Policy_2024.pdf",
            "version": record.get("guidance_version", "2024"),
        }
    ]


def _invoke_grounded_comparison(record: dict, passages: list[dict], guidance_version: str) -> list[dict]:
    """Invoke structured LLM comparison of a sanitized plan against guidance passages.

    No raw student identifiers may enter the prompt.
    Returns a list of raw finding dicts; caller validates schema.

    In production, calls the LLM via the shared LLM service.
    Returns synthetic findings for testing.
    """
    review_ref = record.get("review_ref", "PLAN-????")
    approval_date = record.get("approval_date", "")
    findings = []
    # Synthetic finding: flag if approval_date is missing
    if not approval_date:
        findings.append(
            {
                "review_ref": review_ref,
                "plan_field": "approval_date",
                "guidance_section": "§4.2",
                "guidance_source": "Accommodation_Policy_2024.pdf",
                "guidance_version": guidance_version,
                "severity": "high",
                "rationale": (
                    "The plan record does not include an approval_date. "
                    "Per Policy §4.2, documented approval is required within 30 days of application."
                ),
                "requires_human_confirmation": True,
            }
        )

    return findings


def _validate_finding(finding: dict) -> tuple[bool, str]:
    """Validate that a finding includes all required citation fields."""
    missing = _REQUIRED_FINDING_FIELDS - set(finding.keys())
    if missing:
        return False, f"missing required fields: {sorted(missing)}"
    if not finding.get("guidance_source"):
        return False, "guidance_source is empty"
    if not finding.get("guidance_section"):
        return False, "guidance_section is empty"
    if finding.get("requires_human_confirmation") is not True:
        return False, "requires_human_confirmation must be True"
    return True, ""
