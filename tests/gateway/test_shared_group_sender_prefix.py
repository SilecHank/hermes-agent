import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner(config: GatewayConfig) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    return runner


@pytest.mark.asyncio
async def test_preprocess_prefixes_sender_for_shared_non_thread_group_session():
    runner = _make_runner(
        GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake"),
            },
            group_sessions_per_user=False,
        )
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1002285219667",
        chat_name="Test Group",
        chat_type="group",
        user_name="Alice",
    )
    event = MessageEvent(text="hello", source=source)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert result == "[Alice] hello"


@pytest.mark.asyncio
async def test_preprocess_keeps_plain_text_for_default_group_sessions():
    runner = _make_runner(
        GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake"),
            },
        )
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1002285219667",
        chat_name="Test Group",
        chat_type="group",
        user_name="Alice",
    )
    event = MessageEvent(text="hello", source=source)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert result == "hello"


@pytest.mark.asyncio
async def test_preprocess_includes_slack_author_mention_for_shared_thread():
    """Shared Slack threads expose the current author's verifiable user ID
    next to the display name so 'mention me again' requests can bind the
    mention to the CURRENT speaker (#17916)."""
    runner = _make_runner(
        GatewayConfig(
            platforms={
                Platform.SLACK: PlatformConfig(enabled=True, token="fake"),
            },
        )
    )
    source = SessionSource(
        platform=Platform.SLACK,
        chat_id="C123",
        chat_name="team-channel",
        chat_type="group",
        user_id="U123",
        user_name="Alice",
        thread_id="171.000",
    )
    event = MessageEvent(text="mention me again", source=source)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert result == "[Alice | Slack user <@U123>] mention me again"


@pytest.mark.asyncio
async def test_preprocess_slack_shared_thread_without_user_id_keeps_name_only():
    """No user_id on the source → fall back to the plain name prefix."""
    runner = _make_runner(
        GatewayConfig(
            platforms={
                Platform.SLACK: PlatformConfig(enabled=True, token="fake"),
            },
        )
    )
    source = SessionSource(
        platform=Platform.SLACK,
        chat_id="C123",
        chat_name="team-channel",
        chat_type="group",
        user_name="Alice",
        thread_id="171.000",
    )
    event = MessageEvent(text="hello", source=source)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert result == "[Alice] hello"


@pytest.mark.asyncio
async def test_preprocess_injects_exact_verified_sender_alias():
    runner = _make_runner(
        GatewayConfig.from_dict(
            {
                "group_sessions_per_user": False,
                "identity_aliases": {
                    "qqbot": {
                        "owner-id": {
                            "display_name": "斯霖",
                            "preferred_address": "老板",
                        },
                        "colleague-id": {
                            "display_name": "我是海",
                            "preferred_address": "我是海",
                        },
                    }
                },
            }
        )
    )
    source = SessionSource(
        platform=Platform.QQBOT,
        chat_id="group-id",
        chat_type="group",
        user_id="colleague-id",
    )
    event = MessageEvent(text="你叫我什么", source=source)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert 'display name="我是海"' in result
    assert 'preferred address="我是海"' in result
    assert "Use this identity only for the current sender" in result
    assert result.endswith("你叫我什么")
    assert "老板" not in result


@pytest.mark.asyncio
async def test_preprocess_blocks_global_profile_alias_for_unknown_group_member():
    runner = _make_runner(
        GatewayConfig.from_dict(
            {
                "group_sessions_per_user": False,
                "identity_aliases": {
                    "qqbot": {
                        "owner-id": {
                            "display_name": "斯霖",
                            "preferred_address": "老板",
                        }
                    }
                },
            }
        )
    )
    source = SessionSource(
        platform=Platform.QQBOT,
        chat_id="group-id",
        chat_type="group",
        user_id="unknown-id",
    )
    event = MessageEvent(text="hello", source=source)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert "no address alias is configured" in result
    assert "Do not infer the current sender's name or form of address" in result
    assert "老板" not in result
    assert result.endswith("hello")


@pytest.mark.asyncio
async def test_preprocess_does_not_cross_match_alias_between_platforms():
    runner = _make_runner(
        GatewayConfig.from_dict(
            {
                "identity_aliases": {
                    "qqbot": {
                        "shared-id": {
                            "display_name": "斯霖",
                            "preferred_address": "老板",
                        }
                    }
                }
            }
        )
    )
    source = SessionSource(
        platform=Platform.WEIXIN,
        chat_id="shared-id",
        chat_type="dm",
        user_id="shared-id",
    )
    event = MessageEvent(text="hello", source=source)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert "no address alias is configured" in result
    assert "老板" not in result
