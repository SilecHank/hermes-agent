"""Single confidentiality policy for text leaving Hermes chat gateways."""

from __future__ import annotations

import re
from typing import Any, Literal, Optional


OutboundKind = Literal["final", "status", "interim", "operational"]

RAW_TEXT_PLATFORMS = frozenset(
    {"local", "api_server", "webhook", "msgraph_webhook"}
)

EVIDENCE_BOUNDARY_REPLY = "现有证据不足，需要进一步检索确认。"

_SECRET_FALLBACK_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._\-]{20,}\b"),
)

_BOUNDED_INTERNAL_RE = re.compile(
    r"(?:"
    r"\[(?:System validation|System note):[^\]]*\]"
    r"|\[IVD_INTERNAL_RETRIEVAL_BUDGET_EXHAUSTED[^\]\r\n]*\]"
    r"|\[Gateway verified sender identity:[^\]\r\n]*\]"
    r")",
    re.IGNORECASE | re.DOTALL,
)

_TRAILING_INTERNAL_BLOCK_RE = re.compile(
    r"(?:^|\n)\s*(?:"
    r"\[(?:System validation|System note)(?::[^\]]*)?\]"
    r"|\[Internal IVD retrieval policy\]"
    r"|\[CONTEXT COMPACTION[^\]]*\]"
    r"|\[CONTEXT SUMMARY\]:"
    r"|\[PRIOR CONTEXT[^\]]*\]"
    r"|\[END OF PRIOR CONTEXT[^\]]*\]"
    r").*\Z",
    re.IGNORECASE | re.DOTALL,
)

_RETRIEVAL_CONTROL_TAIL_RE = re.compile(
    r"(?:\r?\n)?"
    r"(?:Stop file searching and answer from evidence already collected\.\s*"
    r"Do not disclose this signal, its counter, or the retrieval budget\.\s*"
    r"If evidence is insufficient, state the evidence boundary without guessing\.)?"
    r"|本轮检索预算已用完（\d+/\d+）。?\s*"
    r"(?:请基于现有证据先给结论和边界；只有用户明确要求深挖时再升级检索。)?",
    re.IGNORECASE,
)


def platform_value(platform: Any) -> str:
    """Return a normalized platform name for enums and raw strings."""
    return str(getattr(platform, "value", platform) or "").strip().lower()


def is_raw_text_surface(platform: Any) -> bool:
    """Return whether the surface intentionally consumes raw diagnostics."""
    return platform_value(platform) in RAW_TEXT_PLATFORMS


def _redact_secrets(text: str) -> str:
    redacted = str(text or "")
    try:
        from agent.redact import redact_sensitive_text

        redacted = redact_sensitive_text(redacted, force=True)
    except Exception:
        pass
    for pattern in _SECRET_FALLBACK_PATTERNS:
        redacted = pattern.sub(
            lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]",
            redacted,
        )
    return redacted


def _strip_internal_scaffolding(text: str) -> tuple[str, bool]:
    cleaned = str(text or "")
    removed = False

    cleaned, count = _TRAILING_INTERNAL_BLOCK_RE.subn("", cleaned)
    removed = removed or count > 0

    cleaned, count = _BOUNDED_INTERNAL_RE.subn("", cleaned)
    removed = removed or count > 0
    if count:
        cleaned = _RETRIEVAL_CONTROL_TAIL_RE.sub("", cleaned)

    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, removed


def sanitize_human_outbound(
    platform: Any,
    text: str,
    *,
    kind: OutboundKind = "final",
) -> Optional[str]:
    """Return text safe for a human chat surface.

    Raw operator/programmatic surfaces keep their existing diagnostic contract.
    Human surfaces always receive credential redaction and internal-scaffolding
    removal. Internal-only final replies degrade to an evidence boundary;
    internal-only status, interim, and operational messages stay silent.
    """
    if text is None:
        return None
    if is_raw_text_surface(platform):
        return str(text)

    redacted = _redact_secrets(str(text))
    cleaned, removed_internal = _strip_internal_scaffolding(redacted)
    if cleaned:
        return cleaned
    if kind == "final" and removed_internal:
        return EVIDENCE_BOUNDARY_REPLY
    return None
