from agent.ivd_request_budget import IVDRequestBudget, estimate_ivd_request_budget
from agent.tool_executor import _budget_for_agent
from tools.budget_config import BudgetConfig


def _tool_schema(description: str):
    return [{
        "type": "function",
        "function": {
            "name": "search_files",
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }]


def test_complete_request_budget_counts_tool_schemas():
    messages = [{"role": "system", "content": "规则"}, {"role": "user", "content": "问题"}]

    without_tools = estimate_ivd_request_budget(messages, tools=[])
    with_tools = estimate_ivd_request_budget(
        messages,
        tools=_tool_schema("检索工具" * 4_000),
    )

    assert with_tools.tool_schema_tokens > 0
    assert with_tools.estimated_input_tokens > without_tools.estimated_input_tokens


def test_provider_usage_calibrates_later_estimates_without_replacing_current_shape():
    budget = IVDRequestBudget()
    messages = [{"role": "user", "content": "A" * 4_000}]
    first = budget.estimate(messages, tools=[])
    budget.observe_provider_usage(prompt_tokens=first.raw_estimated_tokens * 2)

    calibrated = budget.estimate(messages, tools=[])

    assert calibrated.estimated_input_tokens >= first.raw_estimated_tokens * 1.9
    assert calibrated.calibration_factor > 1


def test_effective_limits_reserve_output_and_provider_safety_margin():
    result = estimate_ivd_request_budget(
        [{"role": "user", "content": "问题"}],
        tools=[],
        context_length=48_000,
        max_output_tokens=8_000,
        safety_margin_tokens=2_000,
    )

    assert result.hard_limit_tokens == 38_000
    assert result.target_tokens < result.soft_limit_tokens < result.hard_limit_tokens


def test_tool_budget_is_shape_aware_but_never_crosses_ivd_caps():
    budget = IVDRequestBudget()
    budget.estimate(
        [{"role": "user", "content": "排查异常"}],
        tools=[],
        context_length=100_000,
        max_output_tokens=8_000,
    )

    scalar = budget.tool_budget("scalar_lookup")
    diagnostic = budget.tool_budget("diagnostic")

    assert scalar.turn_budget == 16_000
    assert diagnostic.turn_budget == 36_000
    assert diagnostic.resolve_threshold("read_file") == 12_000
    assert diagnostic.resolve_threshold("search_files") == 8_000
    assert diagnostic.resolve_threshold("session_search") == 2_000


def test_tool_executor_prefers_explicit_ivd_budget_and_keeps_legacy_fallback():
    ivd_budget = BudgetConfig(turn_budget=16_000)

    class IVDContext:
        context_length = 100_000

        def tool_budget(self):
            return ivd_budget

    class Agent:
        context_compressor = IVDContext()

    assert _budget_for_agent(Agent()) is ivd_budget

    class LegacyContext:
        context_length = 100_000

    Agent.context_compressor = LegacyContext()
    assert _budget_for_agent(Agent()).turn_budget != 16_000
