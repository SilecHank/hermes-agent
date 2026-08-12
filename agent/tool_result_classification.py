"""Shared helpers for classifying tool result payloads."""

from __future__ import annotations

import json
import time
from typing import Any


FILE_MUTATING_TOOL_NAMES = frozenset({"write_file", "patch"})


# Tools whose interrupted/dangling execution is safe to discard because they
# cannot mutate either external state or Hermes session state. Unknown/plugin/
# MCP tools stay effect-capable by default.
NO_EFFECT_TOOL_NAMES = frozenset({
    "read_file", "search_files", "session_search", "skill_view", "skills_list",
    "web_extract", "web_search", "vision_analyze", "browser_snapshot",
    "browser_get_images", "browser_console", "read_terminal",
})


def tool_may_have_side_effect(tool_name: str) -> bool:
    return tool_name not in NO_EFFECT_TOOL_NAMES


EFFECT_DISPOSITIONS = frozenset(
    {"read_only", "effect_committed", "effect_failed", "unknown_effect"}
)


def build_effect_receipt(
    idempotency_id: str,
    tool_name: str,
    effect_disposition: str,
    result_locator: str,
    *,
    recorded_at: float | None = None,
) -> dict[str, Any]:
    """Build a bounded receipt without storing command arguments or secrets."""
    if effect_disposition not in EFFECT_DISPOSITIONS:
        raise ValueError(f"invalid effect disposition: {effect_disposition}")
    if not idempotency_id or not tool_name or not result_locator:
        raise ValueError("effect receipt identifiers cannot be empty")
    return {
        "idempotency_id": str(idempotency_id)[:200],
        "tool_name": str(tool_name)[:100],
        "effect_disposition": effect_disposition,
        "result_locator": str(result_locator)[:1000],
        "recorded_at": time.time() if recorded_at is None else float(recorded_at),
    }


def continuation_action(
    receipt: dict[str, Any],
    *,
    source_revision_matches: bool = True,
) -> str:
    disposition = str(receipt.get("effect_disposition") or "unknown_effect")
    if disposition == "read_only":
        return "read_again" if source_revision_matches else "revalidate_source"
    if disposition == "effect_committed":
        return "reuse_result"
    if disposition == "effect_failed":
        return "review_failure"
    return "verify_status"


def _explicit_effect_disposition(tool_name: str, result: Any) -> str:
    if not tool_may_have_side_effect(tool_name):
        return "read_only"
    if file_mutation_result_landed(tool_name, result):
        return "effect_committed"
    payload = None
    if isinstance(result, str):
        try:
            payload = json.loads(result.strip())
        except Exception:
            payload = None
    elif isinstance(result, dict):
        payload = result
    if isinstance(payload, dict):
        if payload.get("error") or payload.get("success") is False:
            return "effect_failed"
        status = str(payload.get("status") or "").strip().lower()
        if payload.get("success") is True or status in {
            "completed", "complete", "committed", "ready", "success", "succeeded"
        }:
            return "effect_committed"
        if status in {"failed", "error", "blocked", "rejected"}:
            return "effect_failed"
    return "unknown_effect"


def build_effect_receipts_from_messages(
    messages: list[dict[str, Any]],
    *,
    recorded_at: float | None = None,
) -> list[dict[str, Any]]:
    """Derive receipts from completed tool pairs without retaining arguments."""
    calls: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or ():
            call_id = str(call.get("id") or call.get("call_id") or "")
            function = call.get("function") or {}
            tool_name = str(function.get("name") or call.get("name") or "")
            if call_id and tool_name:
                calls[call_id] = tool_name
    receipts = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id") or "")
        tool_name = calls.get(call_id, "")
        if not tool_name:
            continue
        receipts.append(
            build_effect_receipt(
                call_id,
                tool_name,
                _explicit_effect_disposition(tool_name, message.get("content")),
                f"tool:{call_id}",
                recorded_at=recorded_at,
            )
        )
    return receipts


def file_mutation_result_landed(tool_name: str, result: Any) -> bool:
    """Return True when a file mutation result proves the write landed."""
    if tool_name not in FILE_MUTATING_TOOL_NAMES or not isinstance(result, str):
        return False
    try:
        data = json.loads(result.strip())
    except Exception:
        return False
    if not isinstance(data, dict) or data.get("error"):
        return False
    if tool_name == "write_file":
        return "bytes_written" in data
    if tool_name == "patch":
        return data.get("success") is True
    return False
