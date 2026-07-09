import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.review_approval_commands import (
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


def test_review_approval_config_defaults_disabled(monkeypatch):
    monkeypatch.setattr(
        "gateway.review_approval_commands.read_raw_config",
        lambda: {},
    )

    cfg = review_approval_config()

    assert cfg.enabled is False


def test_weixin_dm_approval_command_matches_when_enabled(monkeypatch):
    monkeypatch.setattr(
        "gateway.review_approval_commands.read_raw_config",
        lambda: {
            "review_approval_commands": {
                "enabled": True,
                "platform": "weixin",
                "chat_type": "dm",
                "command": "python3 scripts/merge-review-candidates.py --handle-command {message}",
                "cwd": "/home/slim/IVD-KnowledgeHub",
            }
        },
    )

    cfg = review_approval_config()

    assert is_review_approval_command(_event("N"), cfg)
    assert is_review_approval_command(_event("A1 S2 Q3"), cfg)
    assert is_review_approval_command(_event("D5"), cfg)


def test_non_weixin_or_normal_text_does_not_match(monkeypatch):
    monkeypatch.setattr(
        "gateway.review_approval_commands.read_raw_config",
        lambda: {
            "review_approval_commands": {
                "enabled": True,
                "platform": "weixin",
                "chat_type": "dm",
                "command": "python3 scripts/merge-review-candidates.py --handle-command {message}",
                "cwd": "/home/slim/IVD-KnowledgeHub",
            }
        },
    )

    cfg = review_approval_config()

    assert not is_review_approval_command(_event("N", Platform.QQBOT), cfg)
    assert not is_review_approval_command(_event("N", Platform.WEIXIN, "group"), cfg)
    assert not is_review_approval_command(_event("NIFTY灰区报告怎么处理"), cfg)


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
        lambda: {
            "review_approval_commands": {
                "enabled": True,
                "platform": "weixin",
                "chat_type": "dm",
                "command": "unused",
                "cwd": "/tmp",
            }
        },
    )

    async def fake_run(_event, _cfg=None):
        return "第 2/2 页"

    monkeypatch.setattr("gateway.review_approval_commands.run_review_approval_command", fake_run)

    result = await _runner()._handle_message(_event("N"))

    assert result == "第 2/2 页"
