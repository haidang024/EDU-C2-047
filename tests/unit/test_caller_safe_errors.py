"""Validation messages must not carry the internal node-name prefix.

Errors are recorded as "PreProcessNode: institution_id is required" so they stay
traceable in the audit event. That prefix used to be rendered straight into the
caller's chat window, repeated once per missing field.
"""

from __future__ import annotations

from src.nodes.pre_process_node import _caller_safe_errors


def test_node_prefix_is_stripped() -> None:
    joined = _caller_safe_errors(
        [
            "PreProcessNode: institution_id is required",
            "PreProcessNode: review_period is required (e.g. '2025-01')",
        ]
    )

    assert "PreProcessNode" not in joined
    assert "institution_id is required" in joined
    assert "review_period is required (e.g. '2025-01')" in joined


def test_messages_without_a_prefix_are_unchanged() -> None:
    assert _caller_safe_errors(["guidance_sources is required"]) == "guidance_sources is required"


def test_errors_are_separated_readably() -> None:
    assert _caller_safe_errors(["PreProcessNode: one", "PostProcessNode: two"]) == "one; two"


def test_unrelated_colons_are_preserved() -> None:
    """Only a real "<Name>Node: " prefix is stripped, not any colon."""
    assert _caller_safe_errors(["review_period is required (e.g. '2025-01')"]) == (
        "review_period is required (e.g. '2025-01')"
    )
