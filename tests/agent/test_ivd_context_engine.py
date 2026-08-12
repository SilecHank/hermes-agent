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
