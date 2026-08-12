"""Request-only context projection for IVD after-sales conversations.

The persisted transcript remains the audit record. This module only builds a
smaller provider request after a completed tool exchange has a validated,
rebuildable evidence receipt.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


DEFAULT_POLICY: dict[str, int] = {
    "soft_limit_tokens": 40_000,
    "hard_limit_tokens": 45_000,
    "target_tokens": 35_000,
    "min_reclaim_tokens": 4_000,
    "min_tool_result_chars": 8_000,
}

_IVD_PROFILES = frozenset({"ivd", "ivd-after-sales"})


@dataclass(frozen=True)
class ProjectionResult:
    messages: list[dict[str, Any]]
    projected: bool
    estimated_reclaim_tokens: int = 0
    receipt_count: int = 0


def _message_text_size(message: Mapping[str, Any]) -> int:
    content = message.get("content", "")
    if isinstance(content, str):
        return len(content)
    try:
        return len(json.dumps(content, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError):
        return len(str(content))


def _receipt_text(
    call_id: str,
    receipt: Mapping[str, Any],
    *,
    session_revision: int,
) -> str:
    payload = {
        "call_id": call_id,
        "evidence_ids": sorted(
            str(item) for item in (receipt.get("evidence_ids") or ()) if item
        ),
        "session_revision": int(session_revision),
    }
    source = str(receipt.get("source") or "").strip()
    if source:
        payload["source"] = source
    for key in ("effect_disposition", "idempotency_id", "result_locator"):
        value = str(receipt.get(key) or "").strip()
        if value:
            payload[key] = value
    return "[IVD validated tool receipt]\n" + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _completed_receipt_groups(
    messages: Sequence[Mapping[str, Any]],
    receipts: Mapping[str, Mapping[str, Any]],
    *,
    min_tool_result_chars: int,
    session_revision: int,
) -> list[tuple[tuple[int, ...], dict[int, str], int]]:
    """Return atomic assistant-call/result groups eligible for projection."""
    last_user = max(
        (index for index, message in enumerate(messages) if message.get("role") == "user"),
        default=len(messages),
    )
    tool_results: dict[str, int] = {}
    for index, message in enumerate(messages[:last_user]):
        if message.get("role") == "tool" and message.get("tool_call_id"):
            tool_results[str(message["tool_call_id"])] = index

    groups: list[tuple[tuple[int, ...], dict[int, str], int]] = []
    for assistant_index, message in enumerate(messages[:last_user]):
        if message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls") or ()
        call_ids = [str(call.get("id") or call.get("call_id") or "") for call in calls]
        if not call_ids or any(not call_id for call_id in call_ids):
            continue
        # One assistant tool-call block and every referenced result are atomic.
        if any(call_id not in tool_results or call_id not in receipts for call_id in call_ids):
            continue
        if any(
            str(receipts[call_id].get("effect_disposition") or "") == "unknown_effect"
            for call_id in call_ids
        ):
            continue
        result_indices = tuple(tool_results[call_id] for call_id in call_ids)
        if any(index <= assistant_index for index in result_indices):
            continue
        if not any(
            _message_text_size(messages[index]) >= min_tool_result_chars
            for index in result_indices
        ):
            continue

        replacements: dict[int, str] = {}
        reclaim_chars = 0
        for call_id, result_index in zip(call_ids, result_indices):
            replacement = _receipt_text(
                call_id,
                receipts[call_id],
                session_revision=session_revision,
            )
            replacements[result_index] = replacement
            reclaim_chars += max(
                0,
                _message_text_size(messages[result_index]) - len(replacement),
            )
        groups.append((result_indices, replacements, reclaim_chars // 4))
    return groups


def project_ivd_context(
    messages: list[dict[str, Any]],
    *,
    policy: Mapping[str, int] | None = None,
    profile: str = "ivd-after-sales",
    receipts: Mapping[str, Mapping[str, Any]] | None = None,
    active_constraints: Sequence[str] = (),
    estimated_tokens: int | None = None,
    estimated_reclaim_tokens: int | None = None,
    session_revision: int = 0,
) -> ProjectionResult:
    """Build a deterministic IVD request view without changing history."""
    if profile not in _IVD_PROFILES:
        return ProjectionResult(messages=messages, projected=False)

    resolved_policy = dict(DEFAULT_POLICY)
    if policy:
        resolved_policy.update(
            {str(key): int(value) for key, value in policy.items() if value is not None}
        )
    if estimated_tokens is None:
        estimated_tokens = sum(_message_text_size(message) for message in messages) // 4
    if estimated_tokens < resolved_policy["soft_limit_tokens"]:
        return ProjectionResult(messages=messages, projected=False)

    groups = _completed_receipt_groups(
        messages,
        receipts or {},
        min_tool_result_chars=resolved_policy["min_tool_result_chars"],
        session_revision=session_revision,
    )
    calculated_reclaim = sum(group[2] for group in groups)
    reclaim = int(estimated_reclaim_tokens) if estimated_reclaim_tokens is not None else calculated_reclaim
    if not groups or reclaim < resolved_policy["min_reclaim_tokens"]:
        return ProjectionResult(messages=messages, projected=False)

    projected = copy.deepcopy(messages)
    replacement_count = 0
    for _, replacements, _ in groups:
        for index, content in replacements.items():
            projected[index]["content"] = content
            replacement_count += 1

    constraints = [str(item).strip() for item in active_constraints if str(item).strip()]
    if constraints:
        constraint_message = {
            "role": "system",
            "content": "[IVD current constraints]\n" + "\n".join(constraints),
        }
        insert_at = 1 if projected and projected[0].get("role") == "system" else 0
        projected.insert(insert_at, constraint_message)

    return ProjectionResult(
        messages=projected,
        projected=True,
        estimated_reclaim_tokens=calculated_reclaim,
        receipt_count=replacement_count,
    )
