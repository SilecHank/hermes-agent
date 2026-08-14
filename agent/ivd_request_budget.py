"""Complete-request budgets for the opt-in IVD after-sales profile."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from agent.model_metadata import (
    _estimate_tools_tokens_rough,
    estimate_messages_tokens_rough,
)
from tools.budget_config import BudgetConfig


DEFAULT_POLICY = {
    "target_tokens": 35_000,
    "soft_limit_tokens": 40_000,
    "hard_limit_tokens": 45_000,
    "safety_margin_tokens": 2_000,
    "default_max_output_tokens": 8_000,
    "min_limit_gap_tokens": 1_000,
}

_TOOL_OVERRIDES = {
    "read_file": 12_000,
    "search_files": 8_000,
    "session_search": 2_000,
    "terminal": 8_000,
}
_SIMPLE_SHAPES = frozenset(
    {"scalar_lookup", "scoped_scalar", "ambiguous_scalar", "direct_fact"}
)


@dataclass(frozen=True)
class RequestBudgetResult:
    raw_estimated_tokens: int
    estimated_input_tokens: int
    message_tokens: int
    tool_schema_tokens: int
    calibration_factor: float
    target_tokens: int
    soft_limit_tokens: int
    hard_limit_tokens: int
    pressure: str


def _effective_limits(
    policy: Mapping[str, int],
    *,
    context_length: int | None,
    max_output_tokens: int | None,
    safety_margin_tokens: int | None,
) -> tuple[int, int, int]:
    hard = int(policy["hard_limit_tokens"])
    reserve = int(
        max_output_tokens
        if max_output_tokens is not None
        else policy["default_max_output_tokens"]
    )
    margin = int(
        safety_margin_tokens
        if safety_margin_tokens is not None
        else policy["safety_margin_tokens"]
    )
    if context_length and int(context_length) > 0:
        hard = min(hard, max(1, int(context_length) - reserve - margin))
    gap = int(policy["min_limit_gap_tokens"])
    soft = min(int(policy["soft_limit_tokens"]), max(1, hard - gap))
    target = min(int(policy["target_tokens"]), max(1, soft - gap))
    return target, soft, hard


class IVDRequestBudget:
    def __init__(self, policy: Mapping[str, int] | None = None) -> None:
        self.policy = dict(DEFAULT_POLICY)
        if policy:
            self.policy.update(
                {str(key): int(value) for key, value in policy.items() if value is not None}
            )
        self._calibration_factor = 1.0
        self._last_raw_estimate = 0
        self._last_result: RequestBudgetResult | None = None

    def observe_provider_usage(self, *, prompt_tokens: int) -> None:
        observed = int(prompt_tokens or 0)
        if observed <= 0 or self._last_raw_estimate <= 0:
            return
        ratio = observed / self._last_raw_estimate
        # Never under-estimate from calibration and cap one anomalous provider
        # report so it cannot permanently starve later requests.
        self._calibration_factor = max(1.0, min(3.0, ratio))

    def estimate(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        context_length: int | None = None,
        max_output_tokens: int | None = None,
        safety_margin_tokens: int | None = None,
    ) -> RequestBudgetResult:
        message_tokens = estimate_messages_tokens_rough(list(messages))
        tool_schema_tokens = _estimate_tools_tokens_rough(list(tools or ()))
        raw = message_tokens + tool_schema_tokens
        estimated = max(raw, int(round(raw * self._calibration_factor)))
        target, soft, hard = _effective_limits(
            self.policy,
            context_length=context_length,
            max_output_tokens=max_output_tokens,
            safety_margin_tokens=safety_margin_tokens,
        )
        if estimated >= hard:
            pressure = "hard"
        elif estimated >= soft:
            pressure = "soft"
        else:
            pressure = "normal"
        result = RequestBudgetResult(
            raw_estimated_tokens=raw,
            estimated_input_tokens=estimated,
            message_tokens=message_tokens,
            tool_schema_tokens=tool_schema_tokens,
            calibration_factor=self._calibration_factor,
            target_tokens=target,
            soft_limit_tokens=soft,
            hard_limit_tokens=hard,
            pressure=pressure,
        )
        self._last_raw_estimate = raw
        self._last_result = result
        return result

    def tool_budget(self, answer_shape: str) -> BudgetConfig:
        turn_budget = 16_000 if answer_shape in _SIMPLE_SHAPES else 36_000
        if self._last_result is not None:
            remaining_tokens = max(
                0,
                self._last_result.soft_limit_tokens
                - self._last_result.estimated_input_tokens,
            )
            # Remaining input space is shared with future assistant/tool turns.
            turn_budget = min(turn_budget, max(8_000, remaining_tokens * 2))
        per_result = 12_000 if answer_shape not in _SIMPLE_SHAPES else 8_000
        return BudgetConfig(
            default_result_size=per_result,
            turn_budget=turn_budget,
            preview_size=1_500,
            tool_overrides=dict(_TOOL_OVERRIDES),
            honor_pinned_thresholds=False,
        )


def estimate_ivd_request_budget(
    messages: Sequence[Mapping[str, Any]],
    *,
    tools: Sequence[Mapping[str, Any]] | None = None,
    context_length: int | None = None,
    max_output_tokens: int | None = None,
    safety_margin_tokens: int | None = None,
    policy: Mapping[str, int] | None = None,
) -> RequestBudgetResult:
    return IVDRequestBudget(policy).estimate(
        messages,
        tools=tools,
        context_length=context_length,
        max_output_tokens=max_output_tokens,
        safety_margin_tokens=safety_margin_tokens,
    )
