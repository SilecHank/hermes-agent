"""Versioned, scope-safe task checkpoints for IVD continuation."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable


STATES = frozenset(
    {"active", "waiting_approval", "blocked", "completed", "abandoned", "superseded"}
)
RESUMABLE_STATES = ("active", "waiting_approval")
TERMINAL_STATES = frozenset({"blocked", "completed", "abandoned", "superseded"})
TRANSITIONS = {
    "active": STATES,
    "waiting_approval": STATES,
}
PAYLOAD_FIELDS = (
    "active_constraints",
    "unfinished_steps",
    "evidence_ids",
    "adopted_facts",
    "approvals",
    "failure_state",
    "side_effects",
)
MAX_PAYLOAD_BYTES = 64 * 1024
_CONTINUATION_RE = re.compile(
    r"^(?:继续|接着|恢复|确认|批准|通过|暂缓|拒绝|下一页|[ynYN]|"
    r"(?:继续|恢复|确认|批准|通过|暂缓|拒绝)\s*[\w.-]{1,80})[。！!？?\s]*$"
)
_BATCH_DECISION_RE = re.compile(
    r"^(?:[\w.-]{1,40}(?:通过|不通过|暂缓|拒绝)[,，、;；\s]*){1,20}$"
)
_HISTORY_QUOTE_RE = re.compile(r"原话|原文回复|逐字|上次.{0,8}(?:说|回复)|历史记录")


class CheckpointConflict(RuntimeError):
    """The expected revision or state transition no longer matches."""


@dataclass(frozen=True)
class CheckpointScope:
    profile: str
    platform: str
    chat_type: str
    chat_id: str
    user_id: str

    @staticmethod
    def _hash(label: str, value: str) -> str:
        return hashlib.sha256(f"ivd-checkpoint\0{label}\0{value}".encode("utf-8")).hexdigest()

    @property
    def chat_id_hash(self) -> str:
        return self._hash(f"{self.platform}:{self.chat_type}:chat", self.chat_id)

    @property
    def user_id_hash(self) -> str:
        return self._hash(f"{self.platform}:{self.chat_type}:user", self.user_id)


def _dedupe(items: Iterable[Any]) -> list[Any]:
    seen: set[str] = set()
    result = []
    for item in items:
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result


def sanitize_checkpoint_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for field in PAYLOAD_FIELDS:
        value = payload.get(field)
        if value in (None, "", [], {}):
            continue
        if field in {"active_constraints", "unfinished_steps", "evidence_ids"}:
            if not isinstance(value, list):
                raise ValueError(f"{field} must be a list")
            value = _dedupe(str(item)[:1000] for item in value if str(item).strip())
        elif field in {"adopted_facts", "approvals", "side_effects"}:
            if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
                raise ValueError(f"{field} must be a list of objects")
            value = _dedupe(value)
        elif field == "failure_state" and not isinstance(value, dict):
            raise ValueError("failure_state must be an object")
        cleaned[field] = value
    encoded = json.dumps(cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("checkpoint payload exceeds 64 KiB")
    return cleaned


def wants_task_continuation(message: str) -> bool:
    text = str(message or "").strip()
    return bool(_CONTINUATION_RE.fullmatch(text) or _BATCH_DECISION_RE.fullmatch(text))


def message_allows_history_search(message: str) -> bool:
    return bool(_HISTORY_QUOTE_RE.search(str(message or "")))


def build_checkpoint_result(message: str, *, user_message: str = "") -> dict[str, Any]:
    response = str(message or "").strip()
    messages = []
    if user_message:
        messages.append({"role": "user", "content": str(user_message)})
    messages.append({"role": "assistant", "content": response})
    return {
        "final_response": response,
        "messages": messages,
        "api_calls": 0,
        "completed": True,
        "partial": False,
        "interrupted": False,
        "error": None,
        "history_offset": 0,
        "agent_persisted": False,
        "checkpoint_clarification": True,
    }


class IVDTaskCheckpointService:
    def __init__(self, session_db: Any) -> None:
        self.db = session_db

    def _decode(self, record: dict[str, Any]) -> dict[str, Any]:
        result = dict(record)
        result["payload"] = json.loads(result.pop("payload_json", "{}") or "{}")
        result["authorized_participants"] = json.loads(
            result.pop("authorized_participants_json", "[]") or "[]"
        )
        return result

    def save(
        self,
        *,
        task_id: str,
        scope: CheckpointScope,
        state: str,
        source_session_id: str,
        payload: dict[str, Any],
        expected_revision: int | None = None,
        authorized_participants: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        if state not in STATES:
            raise ValueError(f"invalid checkpoint state: {state}")
        existing = self.get(task_id)
        if existing is not None:
            if (
                existing["profile"] != scope.profile
                or existing["platform"] != scope.platform
                or existing["chat_type"] != scope.chat_type
                or existing["chat_id_hash"] != scope.chat_id_hash
                or existing["user_id_hash"] != scope.user_id_hash
            ):
                raise CheckpointConflict("checkpoint scope cannot change")
            if existing["state"] in TERMINAL_STATES or state not in TRANSITIONS.get(existing["state"], set()):
                raise CheckpointConflict(
                    f"invalid transition {existing['state']} -> {state}"
                )
            if expected_revision != existing["revision"]:
                raise CheckpointConflict("stale checkpoint revision")
        elif expected_revision not in (None, 0):
            raise CheckpointConflict("checkpoint does not exist")

        if authorized_participants is None and existing is not None:
            participant_hashes = list(existing.get("authorized_participants") or [])
        else:
            participant_hashes = sorted(
                {
                    CheckpointScope._hash(
                        f"{scope.platform}:{scope.chat_type}:user", str(participant)
                    )
                    for participant in (authorized_participants or ())
                    if str(participant)
                }
            )
        cleaned = sanitize_checkpoint_payload(payload)
        record = self.db.save_ivd_task_checkpoint(
            {
                "task_id": str(task_id),
                "profile": scope.profile,
                "platform": scope.platform,
                "chat_type": scope.chat_type,
                "chat_id_hash": scope.chat_id_hash,
                "user_id_hash": scope.user_id_hash,
                "authorized_participants_json": json.dumps(participant_hashes),
                "state": state,
                "source_session_id": str(source_session_id),
                "payload_json": json.dumps(
                    cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            },
            expected_revision=expected_revision,
        )
        if record is None:
            raise CheckpointConflict("stale checkpoint revision")
        return self._decode(record)

    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self.db._conn.execute(
            "SELECT * FROM ivd_task_checkpoints WHERE task_id = ?", (task_id,)
        ).fetchone()
        return self._decode(dict(row)) if row is not None else None

    def find_resumable(self, scope: CheckpointScope) -> list[dict[str, Any]]:
        rows = self.db.find_ivd_task_checkpoints(
            profile=scope.profile,
            platform=scope.platform,
            chat_type=scope.chat_type,
            chat_id_hash=scope.chat_id_hash,
            states=RESUMABLE_STATES,
        )
        matches = []
        for row in rows:
            authorized = set(json.loads(row.get("authorized_participants_json") or "[]"))
            if row["user_id_hash"] == scope.user_id_hash or scope.user_id_hash in authorized:
                matches.append(self._decode(row))
        return matches

    def resolve_continuation(self, scope: CheckpointScope) -> dict[str, Any]:
        matches = self.find_resumable(scope)
        if not matches:
            return {"action": "none", "checkpoint": None, "message": ""}
        if len(matches) == 1:
            return {"action": "resume", "checkpoint": matches[0], "message": ""}
        task_ids = "、".join(item["task_id"] for item in matches[:5])
        return {
            "action": "clarify",
            "checkpoint": None,
            "message": f"发现多个可继续任务：{task_ids}。请回复任务编号。",
        }

    def acquire_lease(
        self,
        task_id: str,
        *,
        owner_id: str,
        ttl_seconds: float,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        timestamp = time.time() if now is None else float(now)
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        return self.db.acquire_ivd_task_lease(
            task_id,
            owner_id=str(owner_id),
            lease_until=timestamp + float(ttl_seconds),
            now=timestamp,
        )


def render_checkpoint_context(checkpoint: dict[str, Any]) -> str:
    """Render a compact, non-transcript continuation context."""
    payload = checkpoint.get("payload") or {}
    lines = [
        "[IVD scoped task checkpoint]",
        f"task_id={checkpoint.get('task_id', '')}",
        f"state={checkpoint.get('state', '')}",
        f"revision={checkpoint.get('revision', '')}",
    ]
    for field in ("active_constraints", "unfinished_steps", "evidence_ids"):
        values = payload.get(field) or []
        if values:
            lines.append(f"{field}=" + json.dumps(values, ensure_ascii=False))
    lines.append("Use this checkpoint before any broad session search.")
    return "\n".join(lines)
