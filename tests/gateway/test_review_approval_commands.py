import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.review_approval_commands import (
    build_review_approval_shell_command,
    is_review_approval_command,
    review_approval_config,
)
from gateway.session import SessionSource


def _event(text: str, platform: Platform = Platform.WEIXIN, chat_type: str = "dm") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_id="m1",
        source=SessionSource(
            platform=platform,
            user_id="u1",
            chat_id="c1",
            user_name="tester",
            chat_type=chat_type,
        ),
    )


def _enabled_config() -> dict:
    return {
        "review_approval_commands": {
            "enabled": True,
            "platform": "weixin",
            "chat_type": "dm",
            "command": "python3 scripts/review-command-handler.py {message}",
            "cwd": "/home/slim/IVD-KnowledgeHub",
        }
    }


def test_review_approval_config_defaults_disabled(monkeypatch):
    monkeypatch.setattr(
        "gateway.review_approval_commands.read_raw_config",
        lambda: {},
    )

    cfg = review_approval_config()

    assert cfg.enabled is False


@pytest.mark.parametrize(
    "text",
    [
        "N",
        "n",
        "下一页",
        "下一页。",
        "下页",
        "下一頁",
        "P",
        "p",
        "上一页",
        "上一页。",
        "上页",
        "上一頁",
        "D5",
        "A1 S2 Q3",
        "123通过、456不通过",
        "1、2通过，3不通过",
    ],
)
def test_weixin_dm_review_commands_match_when_enabled(monkeypatch, text):
    monkeypatch.setattr(
        "gateway.review_approval_commands.read_raw_config",
        lambda: _enabled_config(),
    )

    cfg = review_approval_config()

    assert is_review_approval_command(_event(text), cfg)


def test_non_weixin_or_normal_text_does_not_match(monkeypatch):
    monkeypatch.setattr(
        "gateway.review_approval_commands.read_raw_config",
        lambda: _enabled_config(),
    )

    cfg = review_approval_config()

    assert not is_review_approval_command(_event("N", Platform.QQBOT), cfg)
    assert not is_review_approval_command(_event("N", Platform.WEIXIN, "group"), cfg)
    assert not is_review_approval_command(_event("NIFTY灰区报告怎么处理"), cfg)
    assert not is_review_approval_command(_event("下一页是什么操作"), cfg)
    assert not is_review_approval_command(_event("123通过是什么意思"), cfg)


def test_build_review_approval_command_quotes_message():
    cfg = review_approval_config(
        {
            "review_approval_commands": {
                "enabled": True,
                "command": "python3 scripts/review-command-handler.py {message}",
            }
        }
    )

    command = build_review_approval_shell_command(cfg, "A1 S2")

    assert command == "python3 scripts/review-command-handler.py 'A1 S2'"


async def _capture_should_not_run(*_args, **_kwargs):
    raise AssertionError("agent path should not run for review approval command")


def _runner():
    from gateway.config import GatewayConfig, PlatformConfig
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.WEIXIN: PlatformConfig(enabled=True)})
    runner.session_store = object()
    runner.pairing_store = object()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._update_prompt_pending = {}
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda _source: True
    runner._session_key_for_source = lambda _source: "agent:main:weixin:dm:c1"
    runner._handle_message_with_agent = _capture_should_not_run
    return runner


@pytest.mark.asyncio
async def test_runner_intercepts_review_approval_before_agent(monkeypatch):
    monkeypatch.setattr(
        "gateway.review_approval_commands.read_raw_config",
        lambda: _enabled_config(),
    )

    async def fake_run(_event, _cfg=None):
        return "第 2/5 页"

    monkeypatch.setattr("gateway.review_approval_commands.run_review_approval_command", fake_run)

    result = await _runner()._handle_message(_event("下一页。"))

    assert result == "第 2/5 页"
