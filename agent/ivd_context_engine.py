"""IVD request projection proxy for an existing context engine."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from agent.ivd_context_projection import DEFAULT_POLICY, project_ivd_context


class IVDContextEngineProxy:
    """Add request-only IVD projection while preserving delegate compaction."""

    def __init__(
        self,
        delegate: Any,
        *,
        policy: Mapping[str, int] | None = None,
        receipts: Mapping[str, Mapping[str, Any]] | None = None,
        active_constraints: Sequence[str] = (),
        session_revision: int = 0,
    ) -> None:
        self.delegate = delegate
        self.policy = dict(DEFAULT_POLICY)
        if policy:
            self.policy.update({str(key): int(value) for key, value in policy.items()})
        self.receipts: dict[str, dict[str, Any]] = {
            str(call_id): dict(receipt)
            for call_id, receipt in (receipts or {}).items()
        }
        self.active_constraints = tuple(active_constraints)
        self.session_revision = int(session_revision)
        self.last_projection = None

    @property
    def name(self) -> str:
        return f"ivd-projection({getattr(self.delegate, 'name', 'context-engine')})"

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def update_projection_context(
        self,
        *,
        active_constraints: Sequence[str] | None = None,
        session_revision: int | None = None,
    ) -> None:
        if active_constraints is not None:
            self.active_constraints = tuple(active_constraints)
        if session_revision is not None:
            self.session_revision = int(session_revision)

    def add_receipts(self, receipts: Mapping[str, Mapping[str, Any]]) -> None:
        for call_id, receipt in receipts.items():
            if call_id:
                self.receipts[str(call_id)] = dict(receipt)

    def select_context(self, request_messages, **kwargs):
        selected = None
        delegate_hook = getattr(self.delegate, "select_context", None)
        if callable(delegate_hook):
            selected = delegate_hook(request_messages, **kwargs)
        base_messages = selected if isinstance(selected, list) and selected else request_messages
        current_request_tokens = sum(
            len(str(message.get("content") or ""))
            for message in base_messages
            if isinstance(message, dict)
        ) // 4
        estimated_tokens = max(
            int(getattr(self.delegate, "last_prompt_tokens", 0) or 0),
            current_request_tokens,
        )
        result = project_ivd_context(
            base_messages,
            policy=self.policy,
            receipts=self.receipts,
            active_constraints=self.active_constraints,
            estimated_tokens=estimated_tokens,
            session_revision=self.session_revision,
        )
        self.last_projection = result
        return result.messages if result.projected else selected

    def on_turn_complete(self, messages, usage=None, **kwargs):
        hook = getattr(self.delegate, "on_turn_complete", None)
        if callable(hook):
            return hook(messages, usage=usage, **kwargs)
        return None


def ensure_ivd_context_engine(
    engine: Any,
    *,
    enabled: bool,
    policy: Mapping[str, int] | None = None,
    active_constraints: Sequence[str] = (),
    session_revision: int = 0,
) -> Any:
    """Attach/update the proxy only for an explicitly enabled IVD turn."""
    if not enabled:
        return engine
    if isinstance(engine, IVDContextEngineProxy):
        engine.update_projection_context(
            active_constraints=active_constraints,
            session_revision=session_revision,
        )
        return engine
    return IVDContextEngineProxy(
        engine,
        policy=policy,
        active_constraints=active_constraints,
        session_revision=session_revision,
    )
