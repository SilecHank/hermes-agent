from agent.ivd_context_engine import IVDContextEngineProxy, ensure_ivd_context_engine


class Delegate:
    name = "delegate"
    context_length = 100000
    last_prompt_tokens = 50000

    def select_context(self, request_messages, **kwargs):
        return None

    def on_turn_complete(self, messages, usage=None, **kwargs):
        self.observed = list(messages)


def test_proxy_delegates_properties_and_projects_request_only():
    delegate = Delegate()
    proxy = IVDContextEngineProxy(delegate, receipts={"c": {"evidence_ids": ["ev-a"]}})
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "tool_calls": [{"id": "c", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c", "content": "X" * 50000},
        {"role": "user", "content": "next"},
    ]
    selected = proxy.select_context(messages, budget_tokens=100000)
    assert proxy.context_length == 100000
    assert "ev-a" in str(selected)
    assert "X" * 100 in str(messages)


def test_gateway_attachment_is_explicit_and_never_nests_proxy():
    delegate = Delegate()
    assert ensure_ivd_context_engine(delegate, enabled=False) is delegate

    proxy = ensure_ivd_context_engine(
        delegate,
        enabled=True,
        active_constraints=["产品=WES"],
        session_revision=3,
    )
    updated = ensure_ivd_context_engine(
        proxy,
        enabled=True,
        active_constraints=["产品=CNV-seq"],
        session_revision=4,
    )

    assert updated is proxy
    assert updated.delegate is delegate
    assert updated.active_constraints == ("产品=CNV-seq",)
    assert updated.session_revision == 4


def test_new_large_tool_result_can_trigger_before_usage_counter_catches_up():
    delegate = Delegate()
    delegate.last_prompt_tokens = 1_000
    proxy = IVDContextEngineProxy(delegate, receipts={"c": {"evidence_ids": ["ev-a"]}})
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "tool_calls": [{"id": "c", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c", "content": "X" * 180_000},
        {"role": "user", "content": "next"},
    ]

    selected = proxy.select_context(messages, budget_tokens=100000)

    assert "ev-a" in str(selected)
    assert "X" * 100 not in str(selected)


def test_proxy_budget_counts_tools_and_calibrates_from_provider_usage():
    delegate = Delegate()
    delegate.last_prompt_tokens = 0
    tools = [{
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "检索" * 10_000,
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    proxy = IVDContextEngineProxy(
        delegate,
        tool_schemas=tools,
        answer_shape="scalar_lookup",
        max_output_tokens=8_000,
    )
    messages = [{"role": "system", "content": "规则"}, {"role": "user", "content": "参数"}]

    proxy.select_context(messages)
    first = proxy.last_request_budget
    proxy.on_turn_complete(messages, usage={"prompt_tokens": first.raw_estimated_tokens * 2})
    proxy.select_context(messages)

    assert first.tool_schema_tokens > 0
    assert proxy.last_request_budget.calibration_factor > 1
    assert proxy.tool_budget().turn_budget == 16_000


def test_gateway_update_refreshes_shape_and_tool_schemas_without_nesting():
    delegate = Delegate()
    proxy = ensure_ivd_context_engine(
        delegate,
        enabled=True,
        answer_shape="diagnostic",
        tool_schemas=[{"function": {"name": "one"}}],
    )

    updated = ensure_ivd_context_engine(
        proxy,
        enabled=True,
        answer_shape="direct_fact",
        tool_schemas=[{"function": {"name": "two"}}],
    )

    assert updated is proxy
    assert updated.answer_shape == "direct_fact"
    assert updated.tool_schemas[0]["function"]["name"] == "two"


def test_ivd_hard_limit_requests_existing_compression_path():
    delegate = Delegate()
    delegate.last_prompt_tokens = 0
    delegate.should_compress = lambda prompt_tokens=None: False
    delegate.should_compress_info = lambda prompt_tokens=None: (False, None)
    proxy = IVDContextEngineProxy(delegate)

    assert proxy.should_compress(44_999) is False
    assert proxy.should_compress(45_000) is True
    assert proxy.should_compress_info(45_000) == (True, "ivd_hard_limit")


def test_provider_window_can_lower_ivd_hard_limit():
    delegate = Delegate()
    delegate.context_length = 48_000
    proxy = IVDContextEngineProxy(delegate, max_output_tokens=8_000)
    proxy.select_context([{"role": "user", "content": "问题"}])

    assert proxy.last_request_budget.hard_limit_tokens == 38_000
    assert proxy.should_compress(38_000) is True
