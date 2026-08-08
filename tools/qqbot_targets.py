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
