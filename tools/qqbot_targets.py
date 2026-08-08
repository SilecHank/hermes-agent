"""Shared parsing boundary for explicit QQBot proactive targets."""

import re
from typing import Optional, Tuple


QQBOT_OPENID_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")
_QQBOT_TYPED_TARGET_RE = re.compile(
    r"^(group|direct):([A-Za-z0-9_-]{32})$"
)
_MALFORMED_TYPED_TARGET_ERROR = (
    "Malformed QQBot typed target. Expected "
    "'group:<32-character-openid>' or "
    "'direct:<32-character-openid>'."
)


def parse_qqbot_typed_target(value: str) -> Optional[Tuple[str, str]]:
    """Return ``(kind, openid)`` or reject any invalid colon-bearing target."""
    target = str(value).strip()
    match = _QQBOT_TYPED_TARGET_RE.fullmatch(target)
    if match:
        return match.group(1), match.group(2)
    if ":" in target:
        raise ValueError(_MALFORMED_TYPED_TARGET_ERROR)
    return None


def is_qqbot_openid(value: str) -> bool:
    """Return whether *value* is a bare 32-character QQ openid."""
    return bool(QQBOT_OPENID_RE.fullmatch(str(value).strip()))


def canonical_qqbot_target_id(value: str) -> str:
    """Return the bare openid used for QQ session identity and deduplication."""
    target = str(value).strip()
    typed_target = parse_qqbot_typed_target(target)
    return typed_target[1] if typed_target else target


def format_qqbot_typed_target(openid: str, chat_type: object) -> Optional[str]:
    """Format a typed QQ target only when both id and chat type are reliable."""
    target_id = str(openid).strip()
    if not is_qqbot_openid(target_id):
        return None

    normalized_type = str(chat_type or "").strip().lower()
    if normalized_type == "group":
        return f"qqbot:group:{target_id}"
    if normalized_type in {"dm", "c2c", "direct"}:
        return f"qqbot:direct:{target_id}"
    return None
