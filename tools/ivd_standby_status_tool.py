"""Telegram-admin-only reader for the sanitized Mac standby receipt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import stat
from typing import Any, Mapping


MAX_RECEIPT_BYTES = 16 * 1024
RECEIPT_FIELDS = {
    "schema_version",
    "status",
    "tag",
    "mac_identity_decrypt",
    "recovery_identity_decrypt",
    "verified_at",
    "freshness",
}

TOOL_SCHEMA = {
    "name": "ivd_standby_status",
    "description": "查看最近一次 Hermes 灾备验收的公开结果。",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


@dataclass(frozen=True)
class StandbyStatusPolicy:
    enabled: bool
    profile: str
    receipt_path: Path


@dataclass(frozen=True)
class StandbyStatusSession:
    platform: str
    profile: str
    gateway_admin: bool


def load_policy(config: Mapping[str, Any] | None = None) -> StandbyStatusPolicy:
    if config is None:
        from hermes_cli.config import load_config

        config = load_config() or {}
    raw = config.get("ivd_standby_status") if isinstance(config, Mapping) else None
    if not isinstance(raw, Mapping) or set(raw) != {"enabled", "profile", "receipt_path"}:
        raise ValueError("invalid standby status policy")
    path = Path(str(raw.get("receipt_path") or "")).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("unsafe standby receipt path")
    return StandbyStatusPolicy(
        enabled=raw.get("enabled") is True,
        profile=str(raw.get("profile") or ""),
        receipt_path=path,
    )


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp missing timezone")
    return parsed.astimezone(timezone.utc)


def _load_receipt(path: Path, *, now: datetime) -> dict[str, Any]:
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size <= 0
        or metadata.st_size > MAX_RECEIPT_BYTES
    ):
        raise ValueError("unsafe receipt")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != RECEIPT_FIELDS:
        raise ValueError("invalid receipt fields")
    if payload["schema_version"] != 1:
        raise ValueError("unsupported receipt schema")
    if payload["status"] not in {"ready", "blocked"}:
        raise ValueError("invalid receipt status")
    if payload["mac_identity_decrypt"] not in {"ready", "blocked"}:
        raise ValueError("invalid Mac identity state")
    if payload["recovery_identity_decrypt"] not in {"ready", "blocked"}:
        raise ValueError("invalid recovery identity state")
    if not isinstance(payload["tag"], str) or (payload["tag"] and not payload["tag"].startswith("standby-")):
        raise ValueError("invalid receipt tag")
    if not isinstance(payload["verified_at"], str):
        raise ValueError("invalid verification time")
    verified_at = _parse_time(payload["verified_at"])
    if verified_at > now:
        raise ValueError("future verification time")
    payload["freshness"] = "fresh" if (now - verified_at).total_seconds() <= 48 * 3600 else "stale"
    return payload


def read_standby_status(
    args: Mapping[str, Any] | None,
    policy: StandbyStatusPolicy,
    session: StandbyStatusSession,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if args:
        return {"status": "blocked", "message_zh": "此状态工具不接受任何参数。"}
    if not (
        policy.enabled
        and policy.profile == "telegram"
        and session.platform == "telegram"
        and session.profile == "telegram"
        and session.gateway_admin
    ):
        return {"status": "blocked", "message_zh": "当前会话无权查看灾备验收状态。"}
    try:
        payload = _load_receipt(policy.receipt_path, now=now or datetime.now(timezone.utc))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"status": "blocked", "message_zh": "尚无可用的灾备验收结果。"}
    if payload["status"] == "ready":
        freshness = "这是最新有效结果。" if payload["freshness"] == "fresh" else "这是历史结果，建议你重新手工验收。"
        message = f"灾备验收通过。最新产物：{payload['tag']}；Mac 身份和离线恢复身份均可解密。{freshness}"
    else:
        message = "最近一次灾备验收未通过，请在 Mac Terminal 中重新手工执行验收。"
    return {**{key: payload[key] for key in RECEIPT_FIELDS if key != "schema_version"}, "message_zh": message}


def _handle_ivd_standby_status(args, **_kwargs) -> str:
    from gateway.session_context import get_session_env

    try:
        policy = load_policy()
    except Exception:
        return json.dumps({"status": "blocked", "message_zh": "灾备状态策略未就绪。"}, ensure_ascii=False)
    session = StandbyStatusSession(
        platform=get_session_env("HERMES_SESSION_PLATFORM"),
        profile=get_session_env("HERMES_SESSION_PROFILE"),
        gateway_admin=get_session_env("HERMES_SESSION_IVD_ADMIN") == "1",
    )
    return json.dumps(read_standby_status(args, policy, session), ensure_ascii=False)


def _check_requirements() -> bool:
    try:
        policy = load_policy()
        return policy.enabled and policy.profile == "telegram"
    except Exception:
        return False


from tools.registry import registry

registry.register(
    name="ivd_standby_status",
    toolset="ivd_standby_status",
    schema=TOOL_SCHEMA,
    handler=_handle_ivd_standby_status,
    check_fn=_check_requirements,
    description="查看最近一次灾备验收公开结果",
    emoji="🧯",
    max_result_size_chars=2_000,
)
