"""Per-platform final response policy for human-facing gateway replies."""

from __future__ import annotations

import dataclasses
import re
from typing import Any

from hermes_cli.config import cfg_get, read_raw_config


_CLOSING_INVITATION_RE = re.compile(
    r"(?m)^\s*(如果你需要|如需|需要的话|我也可以|我可以继续|你也可以告诉我).*$"
)


@dataclasses.dataclass(frozen=True)
class ResponsePolicy:
    enabled: bool = False
    max_chars: int = 0
    remove_closing_invitations: bool = False


def _platform_name(platform: Any) -> str:
    return str(getattr(platform, "value", platform) or "").strip().lower()


def response_policy_for_platform(platform: Any, raw_config: dict[str, Any] | None = None) -> ResponsePolicy:
    raw = read_raw_config() if raw_config is None else raw_config
    section = cfg_get(raw, "response_policies", _platform_name(platform), default={})
    if not isinstance(section, dict):
        section = {}
    try:
        max_chars = int(section.get("max_chars") or 0)
    except (TypeError, ValueError):
        max_chars = 0
    return ResponsePolicy(
        enabled=bool(section.get("enabled", False)),
        max_chars=max(0, max_chars),
        remove_closing_invitations=bool(section.get("remove_closing_invitations", False)),
    )


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return "…"[:max_chars]
    cut = text[: max_chars - 1].rstrip()
    sentence_candidates = [
        cut.rfind(mark)
        for mark in ("。", "！", "？", "\n")
        if cut.rfind(mark) >= max_chars // 2
    ]
    if sentence_candidates:
        cut = cut[: max(sentence_candidates) + 1].rstrip()
    return cut[: max_chars - 1].rstrip() + "…"


def apply_response_policy(platform: Any, text: str) -> str:
    if not text:
        return text
    policy = response_policy_for_platform(platform)
    if not policy.enabled:
        return text
    result = str(text).strip()
    if policy.remove_closing_invitations:
        result = _CLOSING_INVITATION_RE.sub("", result).strip()
    if policy.max_chars:
        result = _truncate_text(result, policy.max_chars)
    return result
