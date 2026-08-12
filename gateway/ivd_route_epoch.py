"""Transactional route epochs for safe IVD session recovery."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


ABNORMAL_END_REASONS = frozenset(
    {
        "restart_timeout",
        "shutdown_timeout",
        "restart_interrupted",
        "agent_close",
        "ws_orphan_reap",
    }
)
NONRECOVERABLE_BOUNDARIES = frozenset(
    {
        "idle",
        "daily",
        "prompt_tokens",
        "user_new",
        "task_completed",
        "explicit_reset",
        "session_reset",
        "session_switch",
        "resume_pending_expired",
        "suspended",
    }
)


def route_epoch_enabled(config: Any, platform: str) -> bool:
    """Return whether route epochs are enabled for this IVD platform."""
    if not isinstance(config, dict):
        return False
    platforms = config.get("platforms") or []
    if isinstance(platforms, str):
        platforms = [item.strip() for item in platforms.split(",") if item.strip()]
    return bool(
        config.get("enabled", False)
        and config.get("route_epoch_enabled", False)
        and platform in platforms
    )


@dataclass(frozen=True)
class RouteScope:
    profile: str
    platform: str
    chat_type: str
    chat_id: str
    user_id: str

    def _hash(self, label: str, value: str) -> str:
        return hashlib.sha256(
            f"ivd-route\0{label}\0{value}".encode("utf-8")
        ).hexdigest()

    @property
    def chat_id_hash(self) -> str:
        return self._hash(f"{self.platform}:{self.chat_type}:chat", self.chat_id)

    @property
    def user_id_hash(self) -> str:
        return self._hash(f"{self.platform}:{self.chat_type}:user", self.user_id)

    @property
    def scope_key(self) -> str:
        return self._hash(
            "scope",
            "\0".join(
                (
                    self.profile,
                    self.platform,
                    self.chat_type,
                    self.chat_id_hash,
                    self.user_id_hash,
                )
            ),
        )


class IVDRouteEpochService:
    def __init__(self, session_db: Any) -> None:
        self.db = session_db

    def get(self, scope: RouteScope) -> dict[str, Any] | None:
        return self.db.get_ivd_route_binding(scope.scope_key)

    def _record(self, scope: RouteScope, session_id: str, **extra: Any) -> dict[str, Any]:
        return {
            "scope_key": scope.scope_key,
            "profile": scope.profile,
            "platform": scope.platform,
            "chat_type": scope.chat_type,
            "chat_id_hash": scope.chat_id_hash,
            "user_id_hash": scope.user_id_hash,
            "session_id": str(session_id),
            **extra,
        }

    def bind(
        self,
        scope: RouteScope,
        *,
        session_id: str,
        now: float,
        expected_epoch: int | None = None,
    ) -> dict[str, Any]:
        result = self.db.upsert_ivd_route_binding(
            self._record(scope, session_id, updated_at=now),
            expected_epoch=expected_epoch,
        )
        if result is None:
            raise RuntimeError("route epoch conflict")
        return result

    def advance_boundary(
        self,
        scope: RouteScope,
        *,
        new_session_id: str,
        reason: str,
        now: float,
        expected_epoch: int,
    ) -> dict[str, Any]:
        if reason not in NONRECOVERABLE_BOUNDARIES:
            raise ValueError(f"not a non-recoverable boundary: {reason}")
        result = self.db.upsert_ivd_route_binding(
            self._record(
                scope,
                new_session_id,
                nonrecoverable_after=now,
                boundary_reason=reason,
                updated_at=now,
            ),
            expected_epoch=expected_epoch,
            advance_epoch=True,
        )
        if result is None:
            raise RuntimeError("route epoch conflict")
        return result

    def bind_compression_child(
        self,
        scope: RouteScope,
        *,
        parent_session_id: str,
        child_session_id: str,
        expected_epoch: int,
        now: float,
    ) -> dict[str, Any]:
        result = self.db.upsert_ivd_route_binding(
            self._record(scope, child_session_id, boundary_reason="compression", updated_at=now),
            expected_epoch=expected_epoch,
            expected_session_id=parent_session_id,
            advance_epoch=False,
        )
        if result is None:
            raise RuntimeError("compression route conflict")
        return result

    def mark_task_completed(
        self,
        scope: RouteScope,
        *,
        session_id: str,
        now: float,
    ) -> dict[str, Any]:
        binding = self.get(scope)
        if binding is None or str(binding["session_id"]) != str(session_id):
            raise RuntimeError("task completion route conflict")
        return self.advance_boundary(
            scope,
            new_session_id=session_id,
            reason="task_completed",
            now=now,
            expected_epoch=int(binding["route_epoch"]),
        )

    def can_recover(self, scope: RouteScope, *, session_id: str) -> bool:
        binding = self.get(scope)
        if binding is None or str(binding["session_id"]) != str(session_id):
            return False
        row = self.db.get_session(session_id)
        if row is None:
            return False
        started_at = float(row.get("started_at") or 0)
        boundary_at = float(binding.get("nonrecoverable_after") or 0)
        if started_at < boundary_at:
            if str(binding.get("boundary_reason") or "") != "task_completed":
                return False
            has_later_user_message = getattr(self.db, "has_user_message_after", None)
            if not callable(has_later_user_message) or not has_later_user_message(
                session_id, boundary_at
            ):
                return False
        return str(row.get("end_reason") or "") in ABNORMAL_END_REASONS

    def reconcile_current(
        self,
        scope: RouteScope,
        *,
        session_id: str,
        now: float,
        previous_session_id: str = "",
        boundary_reason: str = "",
    ) -> dict[str, Any] | None:
        """Progressively bind one live route without rewriting old history."""
        binding = self.get(scope)
        if binding is None:
            return self.bind(scope, session_id=session_id, now=now)
        if str(binding["session_id"]) == str(session_id):
            return binding
        if previous_session_id and str(binding["session_id"]) != str(previous_session_id):
            return None
        if boundary_reason == "compression":
            return self.bind_compression_child(
                scope,
                parent_session_id=previous_session_id,
                child_session_id=session_id,
                expected_epoch=int(binding["route_epoch"]),
                now=now,
            )
        if boundary_reason in NONRECOVERABLE_BOUNDARIES:
            return self.advance_boundary(
                scope,
                new_session_id=session_id,
                reason=boundary_reason,
                now=now,
                expected_epoch=int(binding["route_epoch"]),
            )
        return None
