"""PreProcessNode — validate citizen-developer configuration and build session-scoped guidance index."""

from __future__ import annotations

import re
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.config_resolver import resolve_config

# Approved guidance source schemes
_APPROVED_SCHEMES = {"https", "s3", "gs"}
# Prompt-injection sentinel: rejects config values that look like instructions
_INJECTION_RE = re.compile(r"(ignore (previous|all|above)|system prompt|you are now|act as)", re.IGNORECASE)
# Accepted output formats
_ACCEPTED_OUTPUT_FORMATS = {"markdown"}

# Validation messages are recorded with a "<NodeName>: " prefix so they stay
# traceable in the audit event. That prefix is an implementation detail: the
# caller sees these strings in chat, so it is stripped at the render boundary.
_NODE_PREFIX_RE = re.compile(r"^[A-Za-z]+Node:\s*")


def _caller_safe_errors(errors: list[str]) -> str:
    """Join validation errors with the internal node-name prefix removed."""
    return "; ".join(_NODE_PREFIX_RE.sub("", str(item)).strip() for item in errors)


class PreProcessNode(FunctionNode):
    """Validate configuration and build session-scoped approved-guidance index (Step 1).

    Outer Cat 2 boundary node — S-1 trust gate enforced at VERIFIED_EXTERNAL.
    Fails closed on any malformed or unauthorized configuration input.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        # Marketplace chat has no input_context["config"] slot, so the config may
        # arrive in the message text instead (see src/services/config_resolver).
        cfg = resolve_config(state)

        # ── Validate required configuration fields ───────────────────────────
        errors = []

        guidance_sources = cfg.get("guidance_sources", [])
        if not guidance_sources:
            errors.append("PreProcessNode: guidance_sources is required and must be non-empty")

        institution_id = cfg.get("institution_id", "").strip()
        if not institution_id:
            errors.append("PreProcessNode: institution_id is required")

        review_period = cfg.get("review_period", "").strip()
        if not review_period:
            errors.append("PreProcessNode: review_period is required (e.g. '2025-01')")

        output_format = cfg.get("output_format", "markdown").strip().lower()
        if output_format not in _ACCEPTED_OUTPUT_FORMATS:
            errors.append(f"PreProcessNode: output_format '{output_format}' is not approved; use 'markdown'")

        field_mapping = cfg.get("field_mapping", {})
        if not isinstance(field_mapping, dict):
            errors.append("PreProcessNode: field_mapping must be a dict")
            field_mapping = {}

        record_query_selector = cfg.get("record_query_selector", "").strip()
        if not record_query_selector:
            errors.append("PreProcessNode: record_query_selector is required")

        freshness_threshold_days = cfg.get("freshness_threshold_days", 90)
        if not isinstance(freshness_threshold_days, int) or freshness_threshold_days < 1:
            errors.append("PreProcessNode: freshness_threshold_days must be a positive integer")
            freshness_threshold_days = 90

        if errors:
            emit_trace_event("pre_process_config_error", {"errors": errors}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                # Errors carry a "PreProcessNode:" prefix so they are traceable
                # in the audit event above; that prefix is an implementation
                # detail and must not appear in the caller's chat window.
                "input_error_message": (
                    "The accommodation review configuration is incomplete: " + _caller_safe_errors(errors)
                ),
                "input_error_guidance": [
                    "Provide input_context.config with guidance_sources, institution_id, review_period, and record_query_selector.",
                    "Use markdown as output_format and a positive freshness_threshold_days value.",
                ],
            }

        # ── Validate guidance sources (scheme + injection guard) ─────────────
        for src in guidance_sources:
            src_ref = src.get("ref", "") if isinstance(src, dict) else str(src)
            scheme = src_ref.split("://")[0] if "://" in src_ref else ""
            if scheme not in _APPROVED_SCHEMES:
                errors.append(f"PreProcessNode: guidance source '{src_ref}' uses unapproved scheme '{scheme}'")
            # Reject prompt-injection-like text in config values
            if _INJECTION_RE.search(src_ref):
                errors.append(f"PreProcessNode: guidance source '{src_ref}' contains prohibited instruction-like text")

        # Check injection in institution_id, review_period
        for field_name, value in [("institution_id", institution_id), ("review_period", review_period)]:
            if _INJECTION_RE.search(value):
                errors.append(f"PreProcessNode: {field_name} contains prohibited instruction-like text")

        if errors:
            emit_trace_event("pre_process_source_validation_error", {"errors": errors}, state)
            if any("prohibited instruction-like text" in error for error in errors):
                return {"status": AgentStatus.ERROR.value, "error_log": errors}
            return {
                "status": AgentStatus.SUCCESS.value,
                "input_error_message": (
                    "The accommodation review configuration failed validation: " + _caller_safe_errors(errors)
                ),
                "input_error_guidance": ["Use https, s3, or gs references for every guidance source."],
            }

        # ── Build session-scoped guidance index ──────────────────────────────
        # Index contains provenance metadata only — no credential or raw content in state.
        guidance_index_ref, guidance_version, guidance_last_updated = _build_guidance_index(guidance_sources)

        # ── Staleness check ──────────────────────────────────────────────────
        staleness_warning = _check_staleness(guidance_last_updated, freshness_threshold_days)

        emit_trace_event(
            "pre_process_config_loaded",
            {
                "institution_id": institution_id,
                "review_period": review_period,
                "guidance_version": guidance_version,
                "guidance_last_updated": guidance_last_updated,
                "source_count": len(guidance_sources),
                "staleness_warning": bool(staleness_warning),
            },
            state,
        )

        return {
            "validated_input": state.get("user_input", ""),
            "guidance_index_ref": guidance_index_ref,
            "guidance_version": guidance_version,
            "guidance_last_updated": guidance_last_updated,
            "guidance_staleness_warning": staleness_warning,
            "review_period": review_period,
            "institution_id": institution_id,
            "output_format": output_format,
            "field_mapping": field_mapping,
            "record_query_selector": record_query_selector,
            "freshness_threshold_days": freshness_threshold_days,
            # Forward retrieval config into flat state so inner nodes can access
            # them without needing input_context (which is not forwarded to inner graph).
            "retrieval_source_type": cfg.get("source_type", "").lower(),
            "retrieval_inline_records": cfg.get("inline_records"),
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        """S-2 extension: reject direct student PII in the config payload."""
        # Resolve via the same path as execute() so the check also covers a
        # config supplied through the marketplace chat message.
        cfg = resolve_config(state)
        raw_query = cfg.get("record_query_selector", "")
        # Reject patterns that look like direct student identifiers embedded in the query
        if re.search(r"\b\d{7,10}\b", raw_query):
            from framework.errors import SecurityViolationError

            raise SecurityViolationError("PreProcessNode: record_query_selector must not embed raw student ID numbers")
        return state


def _build_guidance_index(guidance_sources: list) -> tuple[str, str, str]:
    """Build a session-scoped opaque index reference from the approved sources list.

    Returns (index_ref, version, last_updated_iso).
    In production, this would load chunks from the document store;
    here we derive provenance metadata from the source descriptors.
    """
    import hashlib
    import json

    refs = []
    latest_version = "unknown"
    latest_date = "1970-01-01"

    for src in guidance_sources:
        if isinstance(src, dict):
            ref = src.get("ref", "")
            version = src.get("version", "unknown")
            last_updated = src.get("last_updated", "1970-01-01")
            if last_updated > latest_date:
                latest_date = last_updated
                latest_version = version
        else:
            ref = str(src)
        refs.append(ref)

    index_ref = hashlib.sha256(json.dumps(sorted(refs)).encode()).hexdigest()[:16]
    return index_ref, latest_version, latest_date


def _check_staleness(last_updated_iso: str, threshold_days: int) -> str:
    """Return a warning string when guidance is older than threshold_days, else empty string."""
    try:
        from datetime import date

        last = date.fromisoformat(last_updated_iso)
        delta = (date.today() - last).days
        if delta > threshold_days:
            return (
                f"Guidance last updated {last_updated_iso} ({delta} days ago) "
                f"exceeds the configured freshness threshold of {threshold_days} days. "
                "Review guidance currency before relying on findings."
            )
    except (ValueError, TypeError):
        pass
    return ""
