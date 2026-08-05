import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_runner(*, admin_user_id: str = "u1"):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.config = GatewayConfig(
        platforms={
            Platform.WEIXIN: PlatformConfig(enabled=True, extra={"allow_admin_from": [admin_user_id]}),
            Platform.QQBOT: PlatformConfig(enabled=True, extra={"allow_admin_from": [admin_user_id]}),
        }
    )
    return runner


def _make_event(text: str, platform: Platform = Platform.WEIXIN, *, user_id: str = "u1") -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=platform,
            user_id=user_id,
            chat_id="c1",
            user_name="tester",
            chat_type="dm",
        ),
    )


def test_ivd_is_registered_gateway_command():
    from hermes_cli.commands import resolve_command

    command = resolve_command("ivd")

    assert command is not None
    assert command.name == "ivd"
    assert command.gateway_only is True


@pytest.mark.asyncio
async def test_ivd_sync_handler_claims_once_with_chinese_reply(tmp_path):
    runner = _make_runner()

    with patch("gateway.run._hermes_home", tmp_path), \
         patch.object(runner, "_schedule_ivd_maintenance_worker") as schedule:
        first = await runner._handle_ivd_command(_make_event("/ivd sync --scope kb-update-20260725"))
        second = await runner._handle_ivd_command(
            _make_event("/ivd sync --scope kb-update-20260725", platform=Platform.QQBOT)
        )

    assert "已接收统一维护命令" in first
    assert "只会执行一次" in first
    assert "已有执行记录" in second
    assert "不会重复执行" in second
    schedule.assert_called_once()


@pytest.mark.asyncio
async def test_ivd_sync_sends_short_notice_to_configured_peer_home_channel(tmp_path):
    runner = _make_runner()
    qq_adapter = MagicMock()
    qq_adapter.send = AsyncMock()
    runner.adapters = {Platform.QQBOT: qq_adapter}
    runner.config.platforms[Platform.QQBOT] = PlatformConfig(
        enabled=True,
        home_channel=HomeChannel(Platform.QQBOT, "qq-home", "QQ home"),
    )

    with patch("gateway.run._hermes_home", tmp_path), \
         patch.object(runner, "_schedule_ivd_maintenance_worker"):
        result = await runner._handle_ivd_command(_make_event("/ivd sync --scope kb-update-20260725"))

    assert "已接收统一维护命令" in result
    qq_adapter.send.assert_awaited_once()
    args, kwargs = qq_adapter.send.call_args
    assert args[0] == "qq-home"
    assert "统一维护命令" in args[1]
    assert "kb-update-20260725" in args[1]


@pytest.mark.asyncio
async def test_ivd_status_without_id_lists_recent_commands(tmp_path):
    runner = _make_runner()

    status = {
        "status": "ready",
        "active_host": "wsl-primary",
        "active_generation": 10,
        "current_release": "a" * 64,
        "platform_health": {"status": "healthy", "platforms": {"weixin": "connected", "wecom": "connected", "qqbot": "connected"}},
        "cron": {"status": "healthy", "jobs": 4},
        "knowledge_release_digest": "b" * 64,
    }
    with patch("gateway.run._hermes_home", tmp_path), \
         patch("gateway.ivd_operator_control.read_ivd_operator_status", return_value=status), \
         patch.object(runner, "_schedule_ivd_maintenance_worker"):
        await runner._handle_ivd_command(_make_event("/ivd sync --scope kb-update-20260725"))
        result = await runner._handle_ivd_command(_make_event("/ivd status"))

    assert "生产主机：wsl-primary（generation 10）" in result
    assert "三平台：正常" in result
    assert "Cron：正常（4 项）" in result
    assert "无需操作" in result


@pytest.mark.asyncio
async def test_ivd_sync_requires_explicit_admin_even_if_user_command_allowed(tmp_path):
    runner = _make_runner(admin_user_id="admin")
    runner.config.platforms[Platform.WEIXIN].extra["user_allowed_commands"] = ["ivd"]

    with patch("gateway.run._hermes_home", tmp_path), \
         patch.object(runner, "_schedule_ivd_maintenance_worker") as schedule:
        result = await runner._handle_ivd_command(
            _make_event("/ivd sync --scope kb-update-20260725", user_id="guest")
        )

    assert "只有管理员可以执行 IVD 维护同步" in result
    schedule.assert_not_called()


@pytest.mark.asyncio
async def test_ivd_status_remains_readable_for_non_admin(tmp_path):
    runner = _make_runner(admin_user_id="admin")
    runner.config.platforms[Platform.WEIXIN].extra["user_allowed_commands"] = []

    status = {
        "status": "ready", "active_host": "wsl-primary", "active_generation": 10,
        "current_release": "a" * 64,
        "platform_health": {"status": "healthy", "platforms": {"weixin": "connected", "wecom": "connected", "qqbot": "connected"}},
        "cron": {"status": "healthy", "jobs": 1},
    }
    with patch("gateway.run._hermes_home", tmp_path), \
         patch("gateway.ivd_operator_control.read_ivd_operator_status", return_value=status), \
         patch.object(runner, "_schedule_ivd_maintenance_worker"):
        assert "只有管理员" in await runner._handle_ivd_command(
            _make_event("/ivd sync --scope kb-update-20260725", user_id="guest")
        )
        result = await runner._handle_ivd_command(_make_event("/ivd status", user_id="guest"))

    assert "生产主机：wsl-primary" in result


@pytest.mark.asyncio
async def test_ivd_repair_requires_one_chinese_confirmation_before_write(tmp_path):
    runner = _make_runner()
    status = {
        "status": "ready", "active_host": "wsl-primary", "active_generation": 10,
        "current_release": "a" * 64,
        "platform_health": {"status": "degraded", "platforms": {"weixin": "connected", "wecom": "disconnected", "qqbot": "connected"}},
        "cron": {"status": "healthy", "jobs": 1},
    }
    repaired = {"status": "ready", "reason": "restart_gateway", "operator_message": "已执行一次网关重启。"}
    with patch("gateway.run._hermes_home", tmp_path), \
         patch("gateway.ivd_operator_control.read_ivd_operator_status", return_value=status), \
         patch("gateway.ivd_operator_control.run_ivd_safe_repair", return_value=repaired) as repair:
        prompt = await runner._handle_ivd_command(_make_event("/ivd repair"))
        result = await runner._handle_ivd_command(_make_event("/ivd repair confirm"))

    assert "等待确认" in prompt
    assert "请回复 `/ivd repair confirm`" in prompt
    assert "已执行一次网关重启" in result
    repair.assert_called_once()


@pytest.mark.asyncio
async def test_ivd_repair_confirmation_is_scoped_to_member_and_generation(tmp_path):
    runner = _make_runner(admin_user_id="admin")
    status10 = {
        "status": "ready", "active_host": "wsl-primary", "active_generation": 10,
        "current_release": "a" * 64,
        "platform_health": {"status": "degraded", "platforms": {}}, "cron": {"status": "healthy", "jobs": 1},
    }
    status11 = {**status10, "active_generation": 11}
    with patch("gateway.run._hermes_home", tmp_path), \
         patch("gateway.ivd_operator_control.read_ivd_operator_status", side_effect=[status10, status11]), \
         patch("gateway.ivd_operator_control.run_ivd_safe_repair") as repair:
        await runner._handle_ivd_command(_make_event("/ivd repair", user_id="admin"))
        wrong_member = await runner._handle_ivd_command(_make_event("/ivd repair confirm", user_id="guest"))
        stale = await runner._handle_ivd_command(_make_event("/ivd repair confirm", user_id="admin"))

    assert "只有管理员" in wrong_member
    assert "系统版本已变化" in stale
    repair.assert_not_called()


def test_ivd_repair_is_classified_without_matching_arbitrary_text():
    from gateway.maintenance_command_bus import classify_maintenance_command

    assert classify_maintenance_command("/ivd repair") == "ivd_maintenance_repair"
    assert classify_maintenance_command("/ivd repair confirm") == "ivd_maintenance_repair"
    assert classify_maintenance_command("帮我执行 rm -rf") is None


def test_management_session_never_reuses_after_sales_case_key():
    from gateway.maintenance_command_bus import management_session_key

    key = management_session_key("telegram", "group-1", "admin-1")

    assert key == "ivd-admin:telegram:group-1:admin-1"
    assert key != "telegram:group-1"
    assert key != "case-20260805-003"


def test_confirmation_requires_same_admin_chat_and_unexpired_nonce():
    from gateway.maintenance_command_bus import (
        consume_management_confirmation,
        issue_management_confirmation,
    )

    pending = {}
    nonce = issue_management_confirmation(
        pending,
        platform="telegram",
        chat_id="group-1",
        user_id="admin-1",
        generation=10,
        now=100.0,
        ttl_seconds=60.0,
    )

    wrong_chat = consume_management_confirmation(
        pending,
        platform="telegram",
        chat_id="group-2",
        user_id="admin-1",
        generation=10,
        nonce=nonce,
        now=101.0,
    )
    accepted = consume_management_confirmation(
        pending,
        platform="telegram",
        chat_id="group-1",
        user_id="admin-1",
        generation=10,
        nonce=nonce,
        now=102.0,
    )
    replayed = consume_management_confirmation(
        pending,
        platform="telegram",
        chat_id="group-1",
        user_id="admin-1",
        generation=10,
        nonce=nonce,
        now=103.0,
    )

    assert wrong_chat == "confirmation_scope_mismatch"
    assert accepted == "ready"
    assert replayed == "confirmation_scope_mismatch"


@pytest.mark.asyncio
async def test_ivd_worker_completion_notice_is_sent_to_origin_and_peers():
    runner = _make_runner()
    origin_adapter = MagicMock()
    origin_adapter.send = AsyncMock()
    qq_adapter = MagicMock()
    qq_adapter.send = AsyncMock()
    runner.adapters = {Platform.WEIXIN: origin_adapter, Platform.QQBOT: qq_adapter}
    runner.config.platforms[Platform.WEIXIN].home_channel = HomeChannel(Platform.WEIXIN, "wx-home", "WX home")
    runner.config.platforms[Platform.QQBOT].home_channel = HomeChannel(Platform.QQBOT, "qq-home", "QQ home")

    await runner._send_ivd_maintenance_completion_notice(
        command_id="ivd-123",
        scope="kb-update-20260725",
        status="completed",
        artifact="/tmp/result.json",
        origin_platform=Platform.WEIXIN,
        origin_chat_id="origin-chat",
        notify_platforms=("qqbot",),
    )

    origin_adapter.send.assert_awaited_once()
    qq_adapter.send.assert_awaited_once()
    assert origin_adapter.send.call_args.args[0] == "origin-chat"
    assert qq_adapter.send.call_args.args[0] == "qq-home"
    assert "维护完成" in origin_adapter.send.call_args.args[1]
    assert "ivd-123" in qq_adapter.send.call_args.args[1]


@pytest.mark.asyncio
async def test_ivd_completion_notice_prefers_completed_artifact_over_stale_status(tmp_path):
    runner = _make_runner()
    origin_adapter = MagicMock()
    origin_adapter.send = AsyncMock()
    runner.adapters = {Platform.WEIXIN: origin_adapter}
    artifact = tmp_path / "ivd-result.json"
    artifact.write_text(
        json.dumps(
            {
                "status": "completed",
                "steps": [
                    {"name": "kb_conflict_detection", "returncode": 1, "allow_failure": True}
                ],
            }
        ),
        encoding="utf-8",
    )

    await runner._send_ivd_maintenance_completion_notice(
        command_id="ivd-123",
        scope="kb-update-20260725",
        status="failed",
        artifact=str(artifact),
        origin_platform=Platform.WEIXIN,
        origin_chat_id="origin-chat",
        notify_platforms=(),
    )

    text = origin_adapter.send.call_args.args[1]
    assert "维护完成" in text
    assert "维护失败" not in text
    assert "待确认" in text
