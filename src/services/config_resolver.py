"""Resolve the citizen-developer review configuration from either entry point.

Two entry points deliver the configuration differently:

* HTTP ``/invoke`` (``src/api/server.py``) carries it as a dedicated request
  field and places it at ``input_context["config"]``.
* Marketplace chat (``cli.py`` → ``shared.bootstrap.marketplace_app``) builds
  ``input_context`` itself as ``{"conversation_history": [...]}`` and has no
  ``config`` slot at all, so the only channel a chat user can reach is the
  message text.

This module gives both paths one shape. ``input_context["config"]`` stays
authoritative; the chat message is parsed only as a fallback, so the HTTP path
behaves exactly as before.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Chat clients commonly wrap pasted JSON in a fenced code block.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def resolve_config(state: dict[str, Any]) -> dict[str, Any]:
    """Return the review configuration dict, or ``{}`` when none is supplied.

    Never raises: unparseable chat text yields ``{}`` so the caller reports the
    existing field-level validation guidance rather than a stack trace.
    """
    input_context = state.get("input_context")
    if isinstance(input_context, dict):
        cfg = input_context.get("config")
        if isinstance(cfg, dict) and cfg:
            return cfg

    return _parse_config_from_message(state.get("user_input", ""))


def _parse_config_from_message(message: Any) -> dict[str, Any]:
    """Extract a config dict from free-form chat text."""
    if not isinstance(message, str) or not message.strip():
        return {}

    for candidate in _candidates(message):
        parsed = _loads(candidate)
        if parsed is None:
            continue
        # A full HTTP request body pasted into the chat box: unwrap it so the
        # documented curl payload works verbatim in chat.
        if isinstance(parsed.get("config"), dict):
            return parsed["config"]
        if _looks_like_config(parsed):
            return parsed
    return {}


def _candidates(message: str) -> list[str]:
    """Yield progressively more forgiving slices of the message to try."""
    stripped = message.strip()
    out = [stripped]

    fenced = _FENCE_RE.search(message)
    if fenced:
        out.append(fenced.group(1).strip())

    # Surrounding prose ("Please review: {...}") — take the outermost braces.
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        out.append(stripped[start : end + 1])

    return out


def _loads(text: str) -> dict[str, Any] | None:
    """Parse ``text`` as a JSON object, tolerating one layer of over-quoting."""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    # A chat client may deliver the payload as a JSON-encoded string.
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (ValueError, TypeError):
            return None
    return parsed if isinstance(parsed, dict) else None


def _looks_like_config(parsed: dict[str, Any]) -> bool:
    """True when the object carries at least one recognised config field.

    Guards against treating unrelated JSON a user happens to paste as a
    configuration, which would replace the field-level guidance with a
    confusing downstream error.
    """
    known = {
        "guidance_sources",
        "institution_id",
        "review_period",
        "record_query_selector",
        "output_format",
        "field_mapping",
        "freshness_threshold_days",
        "source_type",
        "inline_records",
    }
    return bool(known & parsed.keys())
