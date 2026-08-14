import asyncio
import importlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.config import PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import GatewayRunner
from gateway.session import (
    SessionSource,
    build_session_key,
    is_shared_multi_user_session,
)
from tools import clarify_gateway


def _runner(*, group_sessions_per_user: bool = True) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.session_store = None
    runner.config = SimpleNamespace(
        group_sessions_per_user=group_sessions_per_user,
        thread_sessions_per_user=False,
        multiplex_profiles=False,
    )
    return runner


def _qq_group_source(group: str, member: str) -> SessionSource:
    return SessionSource(
        platform=Platform.QQBOT,
        chat_id=group,
        chat_type="group",
        user_id=member,
        user_name=f"member-{member}",
    )


@pytest.mark.parametrize(
    "platform",
    [Platform.QQBOT, Platform.WECOM, Platform.WEIXIN],
)
def test_ivd_groups_share_context_while_private_chats_stay_isolated(platform):
    first_group_member = SessionSource(
        platform=platform,
        chat_id="group-1",
        chat_type="group",
        user_id="member-a",
    )
    second_group_member = SessionSource(
        platform=platform,
        chat_id="group-1",
        chat_type="group",
        user_id="member-b",
    )
    first_private_chat = SessionSource(
        platform=platform,
        chat_id="private-member-a",
        chat_type="dm",
        user_id="member-a",
    )
    second_private_chat = SessionSource(
        platform=platform,
        chat_id="private-member-b",
        chat_type="dm",
        user_id="member-b",
    )

    first_group_key = build_session_key(
        first_group_member,
        group_sessions_per_user=True,
    )
    second_group_key = build_session_key(
        second_group_member,
        group_sessions_per_user=True,
    )

    assert first_group_key == second_group_key
    assert is_shared_multi_user_session(
        first_group_member,
        group_sessions_per_user=True,
    ) is True
    assert build_session_key(first_private_chat) != build_session_key(
        second_private_chat
    )
    assert is_shared_multi_user_session(first_private_chat) is False


def test_qq_clarify_timeout_is_bounded_without_changing_other_platforms():
    from gateway.run import _clarify_timeout_for_platform

    assert _clarify_timeout_for_platform(Platform.QQBOT, 600) == 120
    assert _clarify_timeout_for_platform(Platform.WECOM, 600) == 600
    assert _clarify_timeout_for_platform(Platform.QQBOT, 30) == 30


def test_qq_group_members_share_one_session_without_losing_sender_identity():
    runner = _runner(group_sessions_per_user=True)
    member_a = _qq_group_source("group-1", "member-a")
    member_b = _qq_group_source("group-1", "member-b")
    other_group = _qq_group_source("group-2", "member-a")

    key_a = runner._session_key_for_source(member_a)
    key_b = runner._session_key_for_source(member_b)

    assert key_a == "agent:main:qqbot:group:group-1"
    assert key_b == key_a
    assert runner._session_key_for_source(other_group) != key_a
    assert member_a.user_id == "member-a"
    assert member_b.user_id == "member-b"


def test_gateway_created_qq_adapter_is_bound_to_epoch_owner(monkeypatch):
    from gateway.platforms.qqbot import QQAdapter
    from tests.gateway.test_qqbot import _make_config

    runner = _runner(group_sessions_per_user=True)
    monkeypatch.setattr(
        "gateway.platforms.qqbot.check_qq_requirements", lambda: True
    )

    adapter = runner._create_adapter(
        Platform.QQBOT,
        _make_config(app_id="a", client_secret="b"),
    )

    assert isinstance(adapter, QQAdapter)
    assert adapter.gateway_runner is runner


@pytest.mark.parametrize(
    "platform",
    [Platform.QQBOT, Platform.WECOM, Platform.WEIXIN],
)
def test_ivd_group_members_do_not_share_clarification_state(platform):
    runner = _runner(group_sessions_per_user=True)
    first_member = SessionSource(
        platform=platform,
        chat_id="group-1",
        chat_type="group",
        user_id="member-a",
    )
    second_member = SessionSource(
        platform=platform,
        chat_id="group-1",
        chat_type="group",
        user_id="member-b",
    )
    key_a = runner._effect_session_key_for_source(
        first_member, effect="clarification"
    )
    key_b = runner._effect_session_key_for_source(
        second_member, effect="clarification"
    )
    entry = clarify_gateway.register(
        clarify_id="clarify-qq-group",
        session_key=key_a,
        question="请确认故障发生在哪一步？",
        choices=["建任务", "数据导入"],
    )
    clarify_gateway.mark_awaiting_text(entry.clarify_id)
    try:
        assert key_a != key_b
        assert clarify_gateway.resolve_text_response_for_session(key_b, "2") is False
        assert entry.response is None
    finally:
        clarify_gateway.clear_session(key_a)


@pytest.mark.asyncio
async def test_qq_message_dedup_keeps_new_group_messages(monkeypatch):
    from gateway.platforms.qqbot.adapter import QQAdapter
    from tests.gateway.test_qqbot import _make_config

    adapter = QQAdapter(_make_config(app_id="a", client_secret="b"))
    seen = []

    async def capture_group(payload, msg_id, content, author, timestamp):
        seen.append((msg_id, content, author.get("member_openid")))

    monkeypatch.setattr(adapter, "_handle_group_message", capture_group)
    base = {
        "group_openid": "group-1",
        "content": "继续原任务",
        "author": {"member_openid": "member-a"},
        "timestamp": "2026-07-31T11:00:00+08:00",
    }

    await adapter._on_message("GROUP_AT_MESSAGE_CREATE", {**base, "id": "msg-1"})
    await adapter._on_message("GROUP_AT_MESSAGE_CREATE", {**base, "id": "msg-1"})
    await adapter._on_message(
        "GROUP_AT_MESSAGE_CREATE",
        {**base, "id": "msg-2", "author": {"member_openid": "member-b"}},
    )

    assert seen == [
        ("msg-1", "继续原任务", "member-a"),
        ("msg-2", "继续原任务", "member-b"),
    ]


@pytest.mark.asyncio
async def test_qq_active_group_routes_same_member_reply_to_clarify_not_busy_handler():
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.platforms.qqbot.adapter import QQAdapter
    from tests.gateway.test_qqbot import _make_config

    adapter = QQAdapter(_make_config(app_id="a", client_secret="b"))
    adapter.config.extra["group_sessions_per_user"] = True
    adapter._message_handler = AsyncMock(return_value="")
    adapter._busy_session_handler = AsyncMock(return_value=True)
    runner = _runner(group_sessions_per_user=True)
    adapter.gateway_runner = runner
    session_key = GatewayRunner._effect_session_key_for_source(
        runner,
        _qq_group_source("group-1", "member-a"),
        effect="clarification",
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    entry = clarify_gateway.register(
        clarify_id="clarify-active-qq-group",
        session_key=session_key,
        question="卡在哪一步？",
        choices=["建任务", "数据导入"],
    )
    clarify_gateway.mark_awaiting_text(entry.clarify_id)
    event = MessageEvent(
        text="2",
        message_type=MessageType.TEXT,
        source=_qq_group_source("group-1", "member-a"),
        message_id="msg-clarify-answer",
    )
    try:
        await adapter.handle_message(event)
    finally:
        clarify_gateway.clear_session(session_key)

    adapter._message_handler.assert_awaited_once_with(event)
    adapter._busy_session_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_qq_group_member_b_cannot_consume_member_a_clarification(monkeypatch):
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.platforms.qqbot.adapter import QQAdapter
    from tests.gateway.test_qqbot import _make_config

    runner = _runner(group_sessions_per_user=True)
    adapter = QQAdapter(_make_config(app_id="a", client_secret="b"))
    adapter.gateway_runner = runner
    adapter._message_handler = AsyncMock(return_value="")
    base_handler = AsyncMock(return_value=None)
    monkeypatch.setattr(BasePlatformAdapter, "handle_message", base_handler)
    key_a = runner._effect_session_key_for_source(
        _qq_group_source("group-1", "member-a"), effect="clarification"
    )
    entry = clarify_gateway.register(
        clarify_id="clarify-member-a",
        session_key=key_a,
        question="卡在哪一步？",
        choices=["建任务", "数据导入"],
    )
    clarify_gateway.mark_awaiting_text(entry.clarify_id)
    event_b = MessageEvent(
        text="2",
        message_type=MessageType.TEXT,
        source=_qq_group_source("group-1", "member-b"),
        message_id="msg-member-b",
    )
    try:
        await adapter.handle_message(event_b)
        assert entry.response is None
    finally:
        clarify_gateway.clear_session(key_a)

    adapter._message_handler.assert_not_awaited()
    base_handler.assert_awaited_once_with(event_b)


@pytest.mark.asyncio
async def test_qq_reply_from_old_epoch_is_not_consumed_after_reset(monkeypatch):
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.platforms.qqbot.adapter import QQAdapter
    from tests.gateway.test_qqbot import _make_config

    runner = _runner(group_sessions_per_user=True)
    source = _qq_group_source("group-1", "member-a")
    conversation_key = runner._session_key_for_source(source)
    old_key = runner._effect_session_key_for_source(
        source, effect="clarification"
    )
    entry = clarify_gateway.register(
        clarify_id="clarify-old-epoch",
        session_key=old_key,
        question="卡在哪一步？",
        choices=["建任务", "数据导入"],
    )
    clarify_gateway.mark_awaiting_text(entry.clarify_id)
    runner._clear_conversation_scope(conversation_key, reason="reset")
    current_key = runner._effect_session_key_for_source(
        source, effect="clarification"
    )
    adapter = QQAdapter(_make_config(app_id="a", client_secret="b"))
    adapter.gateway_runner = runner
    adapter._message_handler = AsyncMock(return_value="")
    base_handler = AsyncMock(return_value=None)
    monkeypatch.setattr(BasePlatformAdapter, "handle_message", base_handler)
    event = MessageEvent(
        text="2",
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg-after-reset",
    )
    try:
        await adapter.handle_message(event)
        assert current_key != old_key
        assert entry.response is None
    finally:
        clarify_gateway.clear_session(old_key)
        clarify_gateway.clear_session(current_key)

    adapter._message_handler.assert_not_awaited()
    base_handler.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_qq_reply_routes_to_current_epoch_after_reset(monkeypatch):
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.platforms.qqbot.adapter import QQAdapter
    from tests.gateway.test_qqbot import _make_config

    runner = _runner(group_sessions_per_user=True)
    source = _qq_group_source("group-1", "member-a")
    conversation_key = runner._session_key_for_source(source)
    old_key = runner._effect_session_key_for_source(
        source, effect="clarification"
    )
    runner._clear_conversation_scope(conversation_key, reason="reset")
    current_key = runner._effect_session_key_for_source(
        source, effect="clarification"
    )
    entry = clarify_gateway.register(
        clarify_id="clarify-current-epoch",
        session_key=current_key,
        question="卡在哪一步？",
        choices=["建任务", "数据导入"],
    )
    clarify_gateway.mark_awaiting_text(entry.clarify_id)
    adapter = QQAdapter(_make_config(app_id="a", client_secret="b"))
    adapter.gateway_runner = runner
    adapter._message_handler = AsyncMock(return_value="")
    base_handler = AsyncMock(return_value=None)
    monkeypatch.setattr(BasePlatformAdapter, "handle_message", base_handler)
    event = MessageEvent(
        text="2",
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg-current-epoch",
    )
    try:
        await adapter.handle_message(event)
        assert current_key != old_key
    finally:
        clarify_gateway.clear_session(old_key)
        clarify_gateway.clear_session(current_key)

    adapter._message_handler.assert_awaited_once_with(event)
    base_handler.assert_not_awaited()


def test_qq_busy_ack_does_not_expose_english_internal_template():
    from gateway.run import _qq_busy_ack_message

    messages = {
        _qq_busy_ack_message(steer=True, redirect=False, queue=False),
        _qq_busy_ack_message(steer=False, redirect=True, queue=False),
        _qq_busy_ack_message(steer=False, redirect=False, queue=True),
        _qq_busy_ack_message(steer=False, redirect=False, queue=False),
    }
    assert len(messages) == 4
    assert all("Interrupting" not in message for message in messages)
    assert all("current task" not in message for message in messages)


@pytest.mark.asyncio
async def test_qq_clarify_numbered_fallback_is_plain_chinese(monkeypatch):
    from gateway.platforms.base import SendResult
    from gateway.platforms.qqbot.adapter import QQAdapter
    from tests.gateway.test_qqbot import _make_config

    adapter = QQAdapter(_make_config(app_id="a", client_secret="b"))
    sent = []

    async def capture_send(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)
        return SendResult(success=True, message_id="clarify-message")

    monkeypatch.setattr(adapter, "send", capture_send)
    entry = clarify_gateway.register(
        clarify_id="clarify-chinese",
        session_key="agent:main:qqbot:group:group-1",
        question="HALOS 当前卡在哪一步？",
        choices=["建任务", "数据导入"],
    )
    try:
        result = await adapter.send_clarify(
            chat_id="group-1",
            question=entry.question,
            choices=entry.choices,
            clarify_id=entry.clarify_id,
            session_key=entry.session_key,
        )
    finally:
        clarify_gateway.clear_session(entry.session_key)

    assert result.success is True
    assert sent == [
        "❓ HALOS 当前卡在哪一步？\n\n"
        "1. 建任务\n"
        "2. 数据导入\n\n"
        "请回复序号、选项文字，或直接说明实际情况。"
    ]
    assert "Reply with" not in sent[0]
    assert "own answer" not in sent[0]


@pytest.mark.asyncio
async def test_qq_expired_group_reply_anchor_falls_back_once_without_anchor(monkeypatch):
    from gateway.platforms.qqbot.adapter import QQAdapter
    from tests.gateway.test_qqbot import _make_config

    adapter = QQAdapter(_make_config(app_id="a", client_secret="b"))
    adapter._chat_type_map["group-1"] = "group"
    attempts = []

    async def send_group(chat_id, content, reply_to=None, keyboard=None):
        attempts.append(reply_to)
        if reply_to is not None:
            raise RuntimeError("QQ Bot API error [400]: 回复消息msg_id已过期")
        return SendResult(success=True, message_id="fresh-message")

    monkeypatch.setattr(adapter, "_send_group_text", send_group)

    result = await adapter._send_chunk(
        "group-1",
        "继续原任务",
        reply_to="expired-message",
    )

    assert result.success is True
    assert result.message_id == "fresh-message"
    assert attempts == ["expired-message", None]


class _ClarifyCaptureAdapter(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.QQBOT)
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(content)
        return SendResult(success=True, message_id=f"m-{len(self.sent)}")

    async def send_clarify(
        self, chat_id, question, choices, clarify_id, session_key, metadata=None,
    ) -> SendResult:
        clarify_gateway.mark_awaiting_text(clarify_id)
        return await self.send(chat_id, f"❓ {question}\n\n请直接回复。")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class _BlockingClarifyAgent:
    runner = None
    session_key = ""

    def __init__(self, **kwargs):
        self.tools = []
        self.clarify_callback = None
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        if self.runner is not None and self.session_key:
            self.runner._running_agents[self.session_key] = self

    def get_activity_summary(self):
        return {
            "api_call_count": 1,
            "max_iterations": 10,
            "current_tool": "clarify",
            "last_activity_desc": "clarify",
        }

    def run_conversation(self, message, conversation_history=None, task_id=None):
        response = self.clarify_callback("请确认当前步骤", ["建任务", "数据导入"])
        return {"final_response": response, "messages": [], "api_calls": 1}


@pytest.mark.asyncio
async def test_qq_clarify_wait_state_is_immediate_and_independent_of_heartbeat(
    monkeypatch, tmp_path,
):
    adapter = _ClarifyCaptureAdapter()
    runner = _runner(group_sessions_per_user=True)
    runner.adapters = {Platform.QQBOT: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner._ivd_prepared_contracts = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config.stt_enabled = False

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _BlockingClarifyAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    # The waiting state belongs to clarify delivery, not the long-running
    # heartbeat.  A heartbeat interval beyond the whole clarify lifetime must
    # not suppress the explicit waiting state.
    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INTERVAL", "999")
    monkeypatch.setattr(clarify_gateway, "get_clarify_timeout", lambda: 0.12)

    source = _qq_group_source("group-1", "member-a")
    session_key = runner._session_key_for_source(source)
    _BlockingClarifyAgent.runner = runner
    _BlockingClarifyAgent.session_key = session_key
    result = await runner._run_agent(
        message="继续刚才的问题",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-qq-clarify",
        session_key=session_key,
    )
    _BlockingClarifyAgent.runner = None
    _BlockingClarifyAgent.session_key = ""

    assert "user did not respond" in result["final_response"]
    assert sum("等待回复" in message for message in adapter.sent) == 1, adapter.sent
    assert all("Working" not in message for message in adapter.sent)
    assert all("clarify" not in message.lower() for message in adapter.sent)
    assert clarify_gateway.has_pending(session_key) is False
