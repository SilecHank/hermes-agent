"""Explicit, authorized handoff of a minimal IVD case summary."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Mapping


CASE_ID_PATTERN = r"case-[0-9]{8}-[0-9]{3,6}"
CASE_HANDOFF_RE = re.compile(
    rf"^(?:继续处理|接续处理|接手处理)\s+({CASE_ID_PATTERN})$"
)
MAX_HANDOFF_CONTEXT_BYTES = 8 * 1024


@dataclass(frozen=True)
class CaseHandoffRequest:
    case_id: str


@dataclass(frozen=True)
class CaseHandoffResult:
    status: str
    reason: str
    case_id: str
    context: str = ""


def parse_case_handoff(message: str) -> CaseHandoffRequest | None:
    """Parse only an explicit case handoff command; never infer continuity."""
    match = CASE_HANDOFF_RE.fullmatch(str(message or "").strip())
    if match is None:
        return None
    return CaseHandoffRequest(case_id=match.group(1))


def _bounded_context(case_id: str, summary: str) -> str:
    prefix = f"[IVD跨平台Case接续]\nCase ID：{case_id}\n摘要："
    prefix_bytes = prefix.encode("utf-8")
    remaining = MAX_HANDOFF_CONTEXT_BYTES - len(prefix_bytes)
    if remaining <= 0:
        return prefix_bytes[:MAX_HANDOFF_CONTEXT_BYTES].decode("utf-8", errors="ignore")
    summary_bytes = summary.encode("utf-8")[:remaining]
    return prefix + summary_bytes.decode("utf-8", errors="ignore")


def resolve_case_handoff(
    request: CaseHandoffRequest | None,
    *,
    actor: str,
    authorize: Callable[[str, str], bool],
    load_summary: Callable[[str], Mapping[str, Any] | None] | None = None,
) -> CaseHandoffResult:
    """Authorize a request and load only its bounded, minimal summary."""
    if request is None:
        return CaseHandoffResult("invalid", "explicit_case_required", "")
    try:
        allowed = bool(authorize(str(actor or ""), request.case_id))
    except Exception:
        allowed = False
    if not allowed:
        return CaseHandoffResult(
            "denied",
            "case_access_denied",
            request.case_id,
        )
    if load_summary is None:
        return CaseHandoffResult(
            "unavailable",
            "case_summary_unavailable",
            request.case_id,
        )
    try:
        payload = load_summary(request.case_id)
    except Exception:
        payload = None
    if not isinstance(payload, Mapping):
        return CaseHandoffResult(
            "unavailable",
            "case_summary_unavailable",
            request.case_id,
        )
    returned_case_id = str(payload.get("case_id") or request.case_id)
    summary = str(payload.get("summary") or "").strip()
    if returned_case_id != request.case_id or not summary:
        return CaseHandoffResult(
            "unavailable",
            "case_summary_unavailable",
            request.case_id,
        )
    return CaseHandoffResult(
        "ready",
        "authorized_case_summary_loaded",
        request.case_id,
        _bounded_context(request.case_id, summary),
    )
