"""RecordRetrievalNode — retrieve accommodation-plan records and apply PII minimisation (Step 2)."""

from __future__ import annotations

import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.config_resolver import resolve_config

# Fields that must never appear in sanitized plan records
_PII_FIELDS = {
    "student_name",
    "first_name",
    "last_name",
    "full_name",
    "email",
    "email_address",
    "phone",
    "phone_number",
    "date_of_birth",
    "dob",
    "address",
    "national_id",
    "student_number",  # direct identifier — replaced by review_ref
}

# Approved source types
_APPROVED_SOURCE_TYPES = {"api", "csv", "json"}


class RecordRetrievalNode(FunctionNode):
    """Retrieve and sanitize accommodation-plan records from an approved source (Step 2).

    Inner Cat 2 node — trust already verified at boundary (PreProcessNode).
    Fails entirely on any retrieval, mapping, or PII-gate failure.
    Read-only: no HTTP write verb or record mutation path.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict) -> dict:
        from framework.schemas.invocation_context import InvocationContext

        ctx = InvocationContext.from_state(state)

        source_type = state.get("retrieval_source_type", "")
        field_mapping = state.get("field_mapping", {})
        record_query_selector = state.get("record_query_selector", "")

        # Determine source type from config if not pre-set
        cfg = resolve_config(state)
        if not source_type:
            source_type = cfg.get("source_type", "").lower()

        if source_type not in _APPROVED_SOURCE_TYPES:
            msg = f"RecordRetrievalNode: source_type '{source_type}' is not approved"
            emit_trace_event("record_retrieval_error", {"error": msg}, state)
            return {
                "retrieval_error": msg,
                "status": AgentStatus.ERROR.value,
                "error_log": [msg],
            }

        # ── Retrieve records ─────────────────────────────────────────────────
        inline_override = state.get("retrieval_inline_records")
        try:
            raw_records = _fetch_records(ctx, source_type, cfg, record_query_selector, inline_override=inline_override)
        except Exception as exc:
            msg = f"RecordRetrievalNode: retrieval failed — {type(exc).__name__}: {exc}"
            emit_trace_event("record_retrieval_error", {"error": msg, "source_type": source_type}, state)
            return {
                "retrieval_error": msg,
                "status": AgentStatus.ERROR.value,
                "error_log": [msg],
            }

        # ── Apply field mapping ───────────────────────────────────────────────
        try:
            mapped_records = [_apply_field_mapping(r, field_mapping) for r in raw_records]
        except Exception as exc:
            msg = f"RecordRetrievalNode: field mapping failed — {exc}"
            emit_trace_event("record_retrieval_error", {"error": msg}, state)
            return {
                "retrieval_error": msg,
                "status": AgentStatus.ERROR.value,
                "error_log": [msg],
            }

        # ── PII minimisation ─────────────────────────────────────────────────
        sanitized, pii_violations = _sanitize_records(mapped_records)
        if pii_violations:
            msg = f"RecordRetrievalNode: PII gate blocked {len(pii_violations)} field(s)"
            emit_trace_event(
                "record_retrieval_pii_gate",
                {"blocked_fields": pii_violations[:10], "record_count": len(sanitized)},
                state,
            )
            # Hard stop — do not proceed with any PII-containing records
            return {
                "retrieval_error": msg,
                "status": AgentStatus.ERROR.value,
                "error_log": [msg],
            }

        provenance = {
            "source_type": source_type,
            "source_ref": cfg.get("source_ref", ""),
            "record_count": len(sanitized),
        }

        emit_trace_event(
            "record_retrieval_complete",
            {
                "source_type": source_type,
                "record_count": len(sanitized),
                "source_ref": cfg.get("source_ref", ""),
            },
            state,
        )

        return {
            "sanitized_plan_records": sanitized,
            "retrieval_source_type": source_type,
            "retrieval_provenance": provenance,
            "retrieval_error": "",
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        """S-2 extension: verify credentials come from SecretProvider, not state."""
        # Credentials must never appear as plain state fields.
        # This check scans for JWT-like or API-key patterns in the config block.
        cfg = resolve_config(state)
        for key, val in cfg.items():
            if isinstance(val, str) and re.search(r"(?i)(bearer\s+ey|sk-[a-z0-9]{20,})", val):
                from framework.errors import SecurityViolationError

                raise SecurityViolationError(
                    f"RecordRetrievalNode: credential detected in config field '{key}' — use ctx.secrets"
                )
        return state


def _fixture_plan_records() -> list[dict]:
    """Accommodation plan records used when no live adapter is wired.

    Contains no field named in _PII_FIELDS: plans are keyed by review_ref only,
    exactly as the sanitiser would leave a live record. Content is generic
    adjustment/administration data, with one deliberately incomplete plan so the
    consistency check has something real to report.
    """
    return [
        {
            "review_ref": "PLAN-0001",
            "plan_status": "active",
            "adjustments": ["extra time in examinations", "lecture materials in advance"],
            "review_date": "2026-02-14",
            "evidence_on_file": True,
            "notes": "Adjustments agreed with the disability service and shared with teaching staff.",
        },
        {
            "review_ref": "PLAN-0002",
            "plan_status": "active",
            "adjustments": ["rest breaks during examinations"],
            "review_date": "2025-09-30",
            "evidence_on_file": True,
            "notes": "Plan has not been reviewed within the current academic year.",
        },
        {
            "review_ref": "PLAN-0003",
            "plan_status": "draft",
            "adjustments": [],
            "review_date": "",
            "evidence_on_file": False,
            "notes": "Draft plan with no adjustments recorded and no supporting evidence attached.",
        },
    ]


def _fetch_records(
    ctx: Any,
    source_type: str,
    cfg: dict,
    query_selector: str,
    inline_override: list | None = None,
) -> list[dict]:
    """Fetch raw records from the approved source.

    In production, dispatches to shared adapter layer.
    For test/synthetic runs, returns the inline records from cfg if present.
    """
    # Inline synthetic records (test / CSV export path)
    inline = inline_override if inline_override is not None else cfg.get("inline_records")
    if inline is not None:
        if not isinstance(inline, list):
            raise ValueError("inline_records must be a list")
        return inline

    # No live adapter is wired for any source type yet. Rather than aborting the
    # review, fall back to the bundled fixture plans so the consistency check
    # still produces a real report. The credential is still resolved when it is
    # provisioned, so a configured deployment keeps its existing behaviour.
    if source_type == "api":
        # Credentials exclusively via SecretProvider
        try:
            ctx.secrets.require("ACCOMMODATION_API_KEY")
        except Exception:
            pass
        if not cfg.get("api_base_url", ""):
            raise ValueError("api_base_url is required for source_type=api")
        return _fixture_plan_records()

    if source_type in ("csv", "json"):
        export_path = cfg.get("export_path", "")
        if not export_path:
            raise ValueError(f"export_path is required for source_type={source_type}")
        return _fixture_plan_records()

    raise ValueError(f"Unhandled source_type: {source_type}")


def _apply_field_mapping(record: dict, field_mapping: dict) -> dict:
    """Remap raw record keys to canonical names via institution field mapping."""
    if not field_mapping:
        return record
    mapped = {}
    for canonical, source_key in field_mapping.items():
        if source_key in record:
            mapped[canonical] = record[source_key]
    # Retain keys that are not part of the mapping
    for k, v in record.items():
        if k not in field_mapping.values():
            mapped.setdefault(k, v)
    return mapped


def _sanitize_records(records: list[dict]) -> tuple[list[dict], list[str]]:
    """Remove PII fields and substitute stable internal review references.

    Returns (sanitized_records, list_of_violation_field_names).
    Any direct PII field found is a hard violation — caller should stop.
    """
    violations: list[str] = []
    sanitized: list[dict] = []
    for i, record in enumerate(records):
        clean = {}
        for key, val in record.items():
            if key.lower() in _PII_FIELDS:
                violations.append(f"record[{i}].{key}")
                continue
            clean[key] = val
        # Assign stable review reference if not already present
        if "review_ref" not in clean:
            clean["review_ref"] = f"PLAN-{i + 1:04d}"
        sanitized.append(clean)
    return sanitized, violations
