"""Single confidentiality policy for text leaving Hermes chat gateways."""

from __future__ import annotations

import re
from typing import Any, Literal, Optional


OutboundKind = Literal["final", "status", "interim", "operational"]

RAW_TEXT_PLATFORMS = frozenset(
    {"local", "api_server", "webhook", "msgraph_webhook"}
)

IVD_PLAIN_TEXT_PLATFORMS = frozenset({"weixin", "wecom", "qqbot", "telegram"})

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


def _plain_text_table(lines: list[str], start: int) -> tuple[list[str], int] | None:
    """Convert one Markdown table to compact Chinese plain-text rows."""

    if start + 2 >= len(lines):
        return None

    def cells(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip().strip("|").split("|")]

    header = cells(lines[start])
    separator = cells(lines[start + 1])
    if len(header) < 2 or len(separator) != len(header):
        return None
    if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
        return None

    converted: list[str] = []
    cursor = start + 2
    row_number = 0
    while cursor < len(lines) and "|" in lines[cursor]:
        row = cells(lines[cursor])
        if len(row) != len(header):
            break
        row_number += 1
        title = row[0] or f"第{row_number}项"
        converted.append(f"{row_number}. {title}")
        converted.extend(
            f"{name}：{value}"
            for name, value in zip(header[1:], row[1:])
            if name and value
        )
        converted.append("")
        cursor += 1
    if not converted:
        return None
    return converted, cursor


def _ivd_copy_friendly_plain_text(text: str) -> str:
    """Remove presentation Markdown without changing technical content."""

    protected: list[str] = []

    def protect(value: str) -> str:
        token = f"\ue000{len(protected)}\ue001"
        protected.append(value)
        return token

    def protect_fence(match: re.Match[str]) -> str:
        body = match.group(1).strip("\n")
        return protect(body)

    plain = re.sub(r"```[^\n]*\n(.*?)```", protect_fence, text, flags=re.DOTALL)
    plain = re.sub(r"`([^`\n]+)`", lambda match: protect(match.group(1)), plain)

    lines = plain.splitlines()
    converted_lines: list[str] = []
    cursor = 0
    while cursor < len(lines):
        table = _plain_text_table(lines, cursor)
        if table is not None:
            table_lines, cursor = table
            converted_lines.extend(table_lines)
            continue
        converted_lines.append(lines[cursor])
        cursor += 1
    plain = "\n".join(converted_lines)

    plain = re.sub(
        r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
        lambda match: f"{match.group(1)}：{match.group(2)}",
        plain,
    )
    plain = re.sub(
        r"\s*[（(]\s*已审核\s+reference\s*:\s*[A-Za-z0-9._/-]+\s*[）)]",
        "",
        plain,
        flags=re.IGNORECASE,
    )
    plain = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", plain)
    plain = re.sub(r"(?m)^\s*>\s?", "", plain)
    plain = re.sub(r"(?m)^\s*[-*+]\s+", "• ", plain)
    plain = re.sub(r"(?m)^```[^\n]*\n?", "", plain)
    plain = plain.replace("`", "")
    plain = plain.replace("**", "").replace("~~", "")
    plain = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", plain)
    plain = re.sub(r"(?<![\w/])_([^_\n]+)_(?!\w)", r"\1", plain)

    for index, value in enumerate(protected):
        plain = plain.replace(f"\ue000{index}\ue001", value)
    plain = re.sub(r"[ \t]+\n", "\n", plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    return plain.strip()


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
    if cleaned and platform_value(platform) in IVD_PLAIN_TEXT_PLATFORMS:
        cleaned = _ivd_copy_friendly_plain_text(cleaned)
    if cleaned:
        return cleaned
    if kind == "final" and removed_internal:
        return EVIDENCE_BOUNDARY_REPLY
    return None
