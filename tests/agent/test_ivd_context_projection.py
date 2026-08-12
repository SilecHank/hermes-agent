import copy

from agent.ivd_context_projection import DEFAULT_POLICY, project_ivd_context


def _tool_exchange(size=50000):
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "read_file", "content": "X" * size},
    ]


def test_projection_keeps_system_and_active_constraints():
    messages = [{"role": "system", "content": "system-rule"}, *_tool_exchange(), {"role": "user", "content": "当前问题"}]
    result = project_ivd_context(
        messages,
        policy=DEFAULT_POLICY,
        active_constraints=["产品=WES", "版本=V5"],
        receipts={"call-1": {"evidence_ids": ["ev-a"], "source": "knowledge-base/products/WES.md"}},
        estimated_tokens=50000,
        estimated_reclaim_tokens=10000,
    )
    text = str(result.messages)
    assert "system-rule" in text
    assert "产品=WES" in text and "版本=V5" in text


def test_projection_replaces_old_large_tool_block_with_receipt_and_keeps_pair():
    messages = [{"role": "system", "content": "sys"}, *_tool_exchange(), {"role": "user", "content": "next"}]
    result = project_ivd_context(
        messages,
        policy=DEFAULT_POLICY,
        receipts={"call-1": {"evidence_ids": ["ev-a"], "source": "knowledge-base/products/WES.md"}},
        estimated_tokens=50000,
        estimated_reclaim_tokens=10000,
    )
    assert "X" * 100 not in str(result.messages)
    assert "ev-a" in str(result.messages)
    ids = [m.get("tool_call_id") for m in result.messages if m.get("role") == "tool"]
    calls = [tc["id"] for m in result.messages for tc in m.get("tool_calls", [])]
    assert set(ids) == set(calls)


def test_projection_does_not_mutate_history_and_is_deterministic():
    messages = [{"role": "system", "content": "sys"}, *_tool_exchange(), {"role": "user", "content": "next"}]
    original = copy.deepcopy(messages)
    kwargs = dict(
        policy=DEFAULT_POLICY,
        receipts={"call-1": {"evidence_ids": ["ev-a"]}},
        estimated_tokens=50000,
        estimated_reclaim_tokens=10000,
        session_revision=7,
    )
    first = project_ivd_context(messages, **kwargs)
    second = project_ivd_context(messages, **kwargs)
    assert messages == original
    assert first.messages == second.messages


def test_below_soft_limit_or_reclaim_hysteresis_is_noop():
    messages = [{"role": "system", "content": "sys"}, *_tool_exchange()]
    assert project_ivd_context(messages, policy=DEFAULT_POLICY, estimated_tokens=1000).messages is messages
    assert project_ivd_context(
        messages,
        policy=DEFAULT_POLICY,
        estimated_tokens=41000,
        estimated_reclaim_tokens=3000,
    ).messages is messages


def test_non_ivd_profile_is_byte_identical_noop():
    messages = [{"role": "system", "content": "sys"}, *_tool_exchange()]
    assert project_ivd_context(
        messages,
        policy=DEFAULT_POLICY,
        profile="default",
        estimated_tokens=50000,
        estimated_reclaim_tokens=10000,
    ).messages is messages
