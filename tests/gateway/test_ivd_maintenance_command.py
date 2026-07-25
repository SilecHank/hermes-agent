from unittest.mock import patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    return runner


def _make_event(text: str, platform: Platform = Platform.WEIXIN) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=platform,
            user_id="u1",
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

    with patch("gateway.run._hermes_home", tmp_path):
        first = await runner._handle_ivd_command(_make_event("/ivd sync --scope kb-update-20260725"))
        second = await runner._handle_ivd_command(
            _make_event("/ivd sync --scope kb-update-20260725", platform=Platform.QQBOT)
        )

    assert "已接收统一维护命令" in first
    assert "只会执行一次" in first
    assert "已有执行记录" in second
    assert "不会重复执行" in second
