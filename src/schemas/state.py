"""State schema for EDU-C2-047 Student Accommodation Plan Consistency Agent."""

from __future__ import annotations

# ADR-005: flat TypedDict only — no Pydantic, no dataclass, no credentials.
# LangGraph checkpoints use msgpack; only plain serializable fields allowed.

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Agent state for the Student Accommodation Plan Consistency Agent.

    Extends AgentState with EDU-C2-047-specific fields.
    All fields are plain primitives or JSON-serializable types (msgpack-safe).
    No secrets, tokens, raw student identifiers, or InvocationContext here.
    """

    # ── Step 1: Configuration & guidance-load ────────────────────────────────
    guidance_index_ref: str  # Opaque reference to the loaded guidance index
    guidance_version: str  # e.g. "2024-Q4-v3"
    guidance_last_updated: str  # ISO-8601 date string
    guidance_staleness_warning: str  # Non-empty when freshness threshold exceeded
    review_period: str  # e.g. "2025-01" (YYYY-MM)
    institution_id: str  # Opaque institution identifier (no PII)
    output_format: str  # "markdown" (only accepted value)
    field_mapping: dict  # Approved field-name mappings from config
    record_query_selector: str  # Record query filter expression
    freshness_threshold_days: int  # Max age of guidance before warning

    # ── Step 2: Record retrieval & PII minimisation ──────────────────────────
    sanitized_plan_records: list  # List of sanitized plan dicts (no direct PII)
    retrieval_source_type: str  # "api" | "csv" | "json"
    retrieval_inline_records: list  # Synthetic/test records forwarded from config (no PII)
    retrieval_provenance: dict  # {source_ref, fetched_at, record_count}
    retrieval_error: str  # Non-empty on retrieval failure

    # ── Step 3: Consistency check & flagging ─────────────────────────────────
    consistency_findings: list  # List of structured finding dicts
    findings_suppressed_count: int  # Findings dropped for missing citation
    check_error: str  # Non-empty on analysis failure

    # ── Step 4: Briefing generation & output gate ────────────────────────────
    briefing_markdown: str  # Final Markdown briefing text
    output_gate_blocked: bool  # True when output gate removed/blocked content
    output_gate_reason: str  # Non-empty when gate triggered

    # ── Audit ─────────────────────────────────────────────────────────────────
    audit_ids: list  # Trace/correlation IDs collected during run
    input_error_message: str | None
    input_error_guidance: list[str]
    generation_mode: str | None
    provider_error_message: str | None
