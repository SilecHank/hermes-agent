"""Shared maintenance-command ledger for messaging gateways.

This module is deliberately small and deterministic.  It lets any configured
messaging platform claim the same maintenance command while ensuring the
backend runner executes it once per explicit scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

REQUIRED_IVD_PLATFORMS = {"weixin", "wecom", "qqbot"}

_SYNC_PHRASES = (
    "识别调用这次的全部更新",
    "调用这次的全部更新",
    "识别这次的全部更新",
    "执行知识库维护",
    "知识库维护",
    "同步更新",
)
_STATUS_PHRASES = ("维护状态", "查看维护状态", "ivd status")


@dataclass(frozen=True)
class MaintenanceClaim:
    command_id: str
    normalized_command: str
    should_execute: bool
    status: str
    notify_platforms: tuple[str, ...]


def classify_maintenance_command(text: str) -> str | None:
    raw = str(text or "").strip()
    normalized = raw.casefold()
    normalized = normalized.lstrip("/!").strip()
    if normalized in {"ivd sync", "ivd update", "ivd maintenance", "maintenance sync"}:
        return "ivd_maintenance_sync"
    if normalized.startswith(("ivd sync ", "ivd update ", "ivd maintenance ", "maintenance sync ")):
        return "ivd_maintenance_sync"
    if normalized in {"ivd status", "maintenance status"}:
        return "ivd_maintenance_status"
    if normalized.startswith(("ivd status ", "maintenance status ")):
        return "ivd_maintenance_status"
    if any(phrase in raw for phrase in _SYNC_PHRASES):
        return "ivd_maintenance_sync"
    if any(phrase in raw for phrase in _STATUS_PHRASES):
        return "ivd_maintenance_status"
    return None


def _command_id_for(fingerprint: str) -> str:
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]
    return f"ivd-{digest}"


class MaintenanceCommandLedger:
    def __init__(self, path: str | Path, *, lock_timeout_seconds: float = 5.0) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.lock_timeout_seconds = lock_timeout_seconds

    def claim(
        self,
        text: str,
        *,
        origin_platform: str,
        origin_chat_id: str,
        origin_user_id: str | None = None,
        scope: str = "default",
    ) -> MaintenanceClaim | None:
        normalized_command = classify_maintenance_command(text)
        if normalized_command is None:
            return None
        fingerprint = f"{normalized_command}:{scope}"
        command_id = _command_id_for(fingerprint)
        notify_platforms = tuple(sorted(REQUIRED_IVD_PLATFORMS - {origin_platform}))

        with self._locked():
            state = self._read_state()
            commands = state.setdefault("commands", {})
            fingerprints = state.setdefault("fingerprints", {})
            existing_id = fingerprints.get(fingerprint)
            if existing_id and existing_id in commands:
                item = commands[existing_id]
                item.setdefault("sources", []).append(
                    self._source_record(origin_platform, origin_chat_id, origin_user_id)
                )
                self._write_state(state)
                return MaintenanceClaim(
                    command_id=existing_id,
                    normalized_command=str(item.get("normalized_command") or normalized_command),
                    should_execute=False,
                    status=str(item.get("status") or "queued"),
                    notify_platforms=notify_platforms,
                )

            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            commands[command_id] = {
                "command_id": command_id,
                "fingerprint": fingerprint,
                "normalized_command": normalized_command,
                "scope": scope,
                "status": "queued",
                "created_at": now,
                "updated_at": now,
                "origin_platform": origin_platform,
                "origin_chat_id": origin_chat_id,
                "sources": [self._source_record(origin_platform, origin_chat_id, origin_user_id)],
            }
            fingerprints[fingerprint] = command_id
            self._write_state(state)

        return MaintenanceClaim(
            command_id=command_id,
            normalized_command=normalized_command,
            should_execute=True,
            status="queued",
            notify_platforms=notify_platforms,
        )

    def mark_running(self, command_id: str) -> None:
        self._set_status(command_id, "running")

    def mark_completed(self, command_id: str, *, artifact: str | None = None) -> None:
        self._set_status(command_id, "completed", artifact=artifact)

    def mark_failed(self, command_id: str, *, error: str | None = None, artifact: str | None = None) -> None:
        self._set_status(command_id, "failed", error=error, artifact=artifact)

    def format_status_summary(self, command_id: str) -> str:
        state = self._read_state()
        item = state.get("commands", {}).get(command_id)
        if not isinstance(item, dict):
            return f"维护命令 `{command_id}`：未找到记录。"
        labels = {
            "queued": "已排队",
            "running": "正在执行",
            "completed": "已完成",
            "failed": "执行失败",
        }
        status = str(item.get("status") or "queued")
        label = labels.get(status, status)
        suffix = f"；产物 `{item['artifact']}`" if item.get("artifact") else ""
        return f"维护命令 `{command_id}`：{label}；范围 `{item.get('scope', '-')}`{suffix}。"

    def format_recent_summary(self, *, limit: int = 5) -> str:
        commands = self.list_recent(limit=limit)
        if not commands:
            return "最近维护命令：暂无记录。"
        lines = ["最近维护命令："]
        for item in commands:
            lines.append(
                f"- `{item.get('command_id')}`：{item.get('status', 'queued')}；"
                f"范围 `{item.get('scope', '-')}`；更新时间 {item.get('updated_at', '-')}"
            )
        return "\n".join(lines)

    def list_recent(self, *, limit: int = 5) -> list[dict[str, Any]]:
        state = self._read_state()
        commands = [
            item for item in state.get("commands", {}).values()
            if isinstance(item, dict)
        ]
        commands.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return commands[: max(limit, 0)]

    def recover_stale_running(self, *, max_age_seconds: int = 3600, now_epoch: float | None = None) -> int:
        now = now_epoch if now_epoch is not None else time.time()
        recovered = 0
        with self._locked():
            state = self._read_state()
            for item in state.get("commands", {}).values():
                if not isinstance(item, dict) or item.get("status") != "running":
                    continue
                updated = _parse_epoch(str(item.get("updated_at") or item.get("created_at") or ""))
                if updated is None or now - updated <= max_age_seconds:
                    continue
                item["status"] = "failed"
                item["error"] = "stale_running_recovered"
                item["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
                recovered += 1
            if recovered:
                self._write_state(state)
        return recovered

    def prune(self, *, max_age_seconds: int = 7 * 24 * 3600, now_epoch: float | None = None) -> int:
        now = now_epoch if now_epoch is not None else time.time()
        with self._locked():
            state = self._read_state()
            commands = state.setdefault("commands", {})
            fingerprints = state.setdefault("fingerprints", {})
            to_remove: list[str] = []
            for command_id, item in list(commands.items()):
                if not isinstance(item, dict):
                    continue
                if item.get("status") in {"queued", "running"}:
                    continue
                updated = _parse_epoch(str(item.get("updated_at") or item.get("created_at") or ""))
                if updated is not None and now - updated > max_age_seconds:
                    to_remove.append(command_id)
            for command_id in to_remove:
                item = commands.pop(command_id, {})
                fingerprint = item.get("fingerprint") if isinstance(item, dict) else None
                if fingerprint:
                    fingerprints.pop(fingerprint, None)
            if to_remove:
                self._write_state(state)
        return len(to_remove)

    def _set_status(self, command_id: str, status: str, **extra: Any) -> None:
        with self._locked():
            state = self._read_state()
            item = state.setdefault("commands", {}).get(command_id)
            if not isinstance(item, dict):
                return
            item["status"] = status
            item["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for key, value in extra.items():
                if value is not None:
                    item[key] = value
            self._write_state(state)

    def _read_state(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"commands": {}, "fingerprints": {}}
        except Exception:
            return {"commands": {}, "fingerprints": {}}
        return loaded if isinstance(loaded, dict) else {"commands": {}, "fingerprints": {}}

    def _write_state(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def _source_record(
        self,
        origin_platform: str,
        origin_chat_id: str,
        origin_user_id: str | None,
    ) -> dict[str, str]:
        record = {
            "platform": origin_platform,
            "chat_id": origin_chat_id,
            "seen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if origin_user_id:
            record["user_id"] = origin_user_id
        return record

    @contextmanager
    def _locked(self) -> Iterator[None]:
        deadline = time.monotonic() + self.lock_timeout_seconds
        fd: int | None = None
        while fd is None:
            try:
                self.lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for maintenance ledger lock: {self.lock_path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            os.close(fd)
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass


def _parse_epoch(value: str) -> float | None:
    if not value:
        return None
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None
