"""IVD request projection proxy for an existing context engine."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from agent.ivd_context_projection import DEFAULT_POLICY, project_ivd_context
from agent.ivd_request_budget import IVDRequestBudget


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
        tool_schemas: Sequence[Mapping[str, Any]] = (),
        answer_shape: str = "diagnostic",
        max_output_tokens: int | None = None,
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
        self.tool_schemas = list(tool_schemas)
        self.answer_shape = str(answer_shape or "diagnostic")
        self.max_output_tokens = max_output_tokens
        self.request_budget = IVDRequestBudget(self.policy)
        self.last_request_budget = None
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
        tool_schemas: Sequence[Mapping[str, Any]] | None = None,
        answer_shape: str | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        if active_constraints is not None:
            self.active_constraints = tuple(active_constraints)
        if session_revision is not None:
            self.session_revision = int(session_revision)
        if tool_schemas is not None:
            self.tool_schemas = list(tool_schemas)
        if answer_shape is not None:
            self.answer_shape = str(answer_shape or "diagnostic")
        if max_output_tokens is not None:
            self.max_output_tokens = int(max_output_tokens)

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
        self.last_request_budget = self.request_budget.estimate(
            base_messages,
            tools=self.tool_schemas,
            context_length=int(getattr(self.delegate, "context_length", 0) or 0),
            max_output_tokens=self.max_output_tokens,
        )
        estimated_tokens = max(
            int(getattr(self.delegate, "last_prompt_tokens", 0) or 0),
            self.last_request_budget.estimated_input_tokens,
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
        if isinstance(usage, Mapping):
            self.request_budget.observe_provider_usage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0)
            )
        hook = getattr(self.delegate, "on_turn_complete", None)
        if callable(hook):
            return hook(messages, usage=usage, **kwargs)
        return None

    def tool_budget(self):
        return self.request_budget.tool_budget(self.answer_shape)

    def _hard_limit_tokens(self) -> int:
        if self.last_request_budget is not None:
            return int(self.last_request_budget.hard_limit_tokens)
        return int(self.request_budget.policy["hard_limit_tokens"])

    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        tokens = int(prompt_tokens or 0)
        if tokens >= self._hard_limit_tokens():
            return True
        delegate_hook = getattr(self.delegate, "should_compress", None)
        return bool(delegate_hook(prompt_tokens)) if callable(delegate_hook) else False

    def should_compress_info(
        self, prompt_tokens: int | None = None
    ) -> tuple[bool, str | None]:
        tokens = int(prompt_tokens or 0)
        if tokens >= self._hard_limit_tokens():
            return True, "ivd_hard_limit"
        delegate_hook = getattr(self.delegate, "should_compress_info", None)
        if callable(delegate_hook):
            return delegate_hook(prompt_tokens)
        return self.should_compress(prompt_tokens), None


def ensure_ivd_context_engine(
    engine: Any,
    *,
    enabled: bool,
    policy: Mapping[str, int] | None = None,
    active_constraints: Sequence[str] = (),
    session_revision: int = 0,
    tool_schemas: Sequence[Mapping[str, Any]] = (),
    answer_shape: str = "diagnostic",
    max_output_tokens: int | None = None,
) -> Any:
    """Attach/update the proxy only for an explicitly enabled IVD turn."""
    if not enabled:
        return engine
    if isinstance(engine, IVDContextEngineProxy):
        engine.update_projection_context(
            active_constraints=active_constraints,
            session_revision=session_revision,
            tool_schemas=tool_schemas,
            answer_shape=answer_shape,
            max_output_tokens=max_output_tokens,
        )
        return engine
    return IVDContextEngineProxy(
        engine,
        policy=policy,
        active_constraints=active_constraints,
        session_revision=session_revision,
        tool_schemas=tool_schemas,
        answer_shape=answer_shape,
        max_output_tokens=max_output_tokens,
    )
