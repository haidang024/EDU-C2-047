"""BriefingGenerationNode — assemble review-ready Markdown briefing with output gate (Step 4)."""

from __future__ import annotations

import re
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_DISCLAIMER = (
    "> **RESTRICTED — Review Aid Only.** "
    "This briefing is generated to assist human reviewers and does not determine "
    "accommodation outcomes, confer eligibility, or trigger any action or notification. "
    "All findings require human confirmation before any further step."
)

# Patterns the output gate strips/blocks
_PII_ID_RE = re.compile(r"\b\d{7,10}\b")  # raw student ID numbers
_CREDENTIAL_RE = re.compile(r"(?i)(bearer\s+ey[a-z0-9]+|sk-[a-z0-9]{20,}|api[_-]?key\s*[:=]\s*\S+)")
_DECISION_RE = re.compile(
    r"\b(approved|denied|eligible|ineligible|grant\b|reject\b|remediat|"
    r"notify|send\s+email|write.back|update.record)\b",
    re.IGNORECASE,
)


class BriefingGenerationNode(FunctionNode):
    """Assemble Markdown briefing and apply restricted-output gate (Step 4).

    Inner Cat 2 node — trust already verified at boundary.
    Never emits a briefing after any upstream retrieval or config failure.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict) -> dict:
        # ── Hard stop on any upstream failure ────────────────────────────────
        if state.get("retrieval_error"):
            msg = "BriefingGenerationNode: skipping briefing due to retrieval failure"
            emit_trace_event("briefing_skipped", {"reason": "retrieval_error"}, state)
            return {
                "briefing_markdown": "",
                "output_gate_blocked": False,
                "output_gate_reason": "",
                "status": AgentStatus.ERROR.value,
                "error_log": [msg],
            }

        if state.get("check_error"):
            msg = "BriefingGenerationNode: skipping briefing due to consistency-check failure"
            emit_trace_event("briefing_skipped", {"reason": "check_error"}, state)
            return {
                "briefing_markdown": "",
                "output_gate_blocked": False,
                "output_gate_reason": "",
                "status": AgentStatus.ERROR.value,
                "error_log": [msg],
            }

        # ── Assemble briefing ────────────────────────────────────────────────
        findings = state.get("consistency_findings", [])
        suppressed = state.get("findings_suppressed_count", 0)
        staleness_warning = state.get("guidance_staleness_warning", "")
        institution_id = state.get("institution_id", "")
        review_period = state.get("review_period", "")
        guidance_version = state.get("guidance_version", "")
        guidance_last_updated = state.get("guidance_last_updated", "")
        provenance = state.get("retrieval_provenance", {})

        md = _render_briefing(
            institution_id=institution_id,
            review_period=review_period,
            guidance_version=guidance_version,
            guidance_last_updated=guidance_last_updated,
            staleness_warning=staleness_warning,
            provenance=provenance,
            findings=findings,
            suppressed_count=suppressed,
        )

        # ── Output gate ───────────────────────────────────────────────────────
        clean_md, gate_triggered, gate_reason = _apply_output_gate(md)

        emit_trace_event(
            "briefing_generation_complete",
            {
                "findings_count": len(findings),
                "suppressed_count": suppressed,
                "output_gate_triggered": gate_triggered,
                "institution_id": institution_id,
                "review_period": review_period,
            },
            state,
        )

        return {
            "briefing_markdown": clean_md,
            "output_gate_blocked": gate_triggered,
            "output_gate_reason": gate_reason,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_output(self, result: dict) -> dict:
        """S-3 extension: final scan on the assembled Markdown for PII and credentials."""
        md = result.get("briefing_markdown", "")
        if _PII_ID_RE.search(md):
            from framework.errors import SecurityViolationError

            raise SecurityViolationError(
                "BriefingGenerationNode: raw numeric student identifier detected in briefing output"
            )
        if _CREDENTIAL_RE.search(md):
            from framework.errors import SecurityViolationError

            raise SecurityViolationError("BriefingGenerationNode: credential pattern detected in briefing output")
        return result


def _render_briefing(
    institution_id: str,
    review_period: str,
    guidance_version: str,
    guidance_last_updated: str,
    staleness_warning: str,
    provenance: dict,
    findings: list[dict],
    suppressed_count: int,
) -> str:
    lines = [
        "# Accommodation Plan Consistency Review Briefing",
        "",
        _DISCLAIMER,
        "",
        "## Review Metadata",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Institution | `{institution_id}` |",
        f"| Review Period | `{review_period}` |",
        f"| Guidance Version | `{guidance_version}` (last updated {guidance_last_updated}) |",
        f"| Records Reviewed | {provenance.get('record_count', 0)} |",
        f"| Source Type | {provenance.get('source_type', '')} |",
        "",
    ]

    if staleness_warning:
        lines += [
            "## ⚠ Guidance Freshness Warning",
            "",
            f"> {staleness_warning}",
            "",
        ]

    lines += ["## Findings Summary", ""]

    high = [f for f in findings if f.get("severity") == "high"]
    medium = [f for f in findings if f.get("severity") == "medium"]
    low = [f for f in findings if f.get("severity") == "low"]

    if not findings:
        lines.append(
            "_No findings requiring human review were identified for this period. "
            "This does not constitute a determination of compliance._"
        )
        lines.append("")
    else:
        for severity_label, group in [("High", high), ("Medium", medium), ("Low", low)]:
            if group:
                lines += [f"### {severity_label} Severity", ""]
                for f in group:
                    lines += [
                        f"**Plan Reference:** `{f.get('review_ref', 'N/A')}`  ",
                        f"**Field:** `{f.get('plan_field', 'N/A')}`  ",
                        f"**Guidance:** {f.get('guidance_source', '')} {f.get('guidance_section', '')} "
                        f"(version: {f.get('guidance_version', 'N/A')})  ",
                        f"**Rationale:** {f.get('rationale', '')}  ",
                        "> ⚠ **Human confirmation required before any action.**",
                        "",
                    ]

    if suppressed_count:
        lines += [
            f"_{suppressed_count} potential finding(s) were suppressed due to "
            "insufficient evidence or missing citation._",
            "",
        ]

    lines += [
        "---",
        "",
        "_End of briefing. For retrieval/audit trace, refer to the correlation ID in the invocation response._",
    ]

    return "\n".join(lines)


def _apply_output_gate(md: str) -> tuple[str, bool, str]:
    """Scan and clean the Markdown; return (cleaned_md, gate_triggered, reason)."""
    reasons = []

    # Remove raw numeric IDs
    clean = _PII_ID_RE.sub("[ID-REDACTED]", md)
    if clean != md:
        reasons.append("raw numeric student identifier removed")
        md = clean

    # Remove credential patterns
    clean = _CREDENTIAL_RE.sub("[CREDENTIAL-REDACTED]", md)
    if clean != md:
        reasons.append("credential pattern removed")
        md = clean

    # Flag (but do not suppress) prohibited decision language — output gate logs it
    decision_matches = _DECISION_RE.findall(md)
    if decision_matches:
        unique = list(dict.fromkeys(m.lower() for m in decision_matches))
        reasons.append(f"prohibited decision/action language detected: {unique}")

    triggered = bool(reasons)
    reason = "; ".join(reasons) if reasons else ""
    return md, triggered, reason
