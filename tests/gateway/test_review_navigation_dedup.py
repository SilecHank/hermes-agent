import pytest

from gateway.config import PlatformConfig
from gateway.platforms.weixin import WeixinAdapter


def _adapter() -> WeixinAdapter:
    return WeixinAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "account_id": "bot-account",
                "dm_policy": "pairing",
            },
        )
    )


@pytest.mark.asyncio
async def test_next_page_without_message_id_is_not_swallowed_by_content_dedup(monkeypatch):
    adapter = _adapter()
    adapter._poll_session = object()
    seen = []

    async def capture(event):
        seen.append(event.text)

    monkeypatch.setattr(adapter, "handle_message", capture)
    message = {
        "from_user_id": "u1",
        "item_list": [{"type": 1, "text_item": {"text": "N"}}],
    }

    await adapter._process_message(dict(message))
    await adapter._process_message(dict(message))

    assert seen == ["N", "N"]


@pytest.mark.asyncio
async def test_normal_duplicate_content_without_message_id_remains_suppressed(monkeypatch):
    adapter = _adapter()
    adapter._poll_session = object()
    seen = []

    def capture(event):
        seen.append(event.text)

    monkeypatch.setattr(adapter, "_enqueue_text_event", capture)
    message = {
        "from_user_id": "u1",
        "item_list": [{"type": 1, "text_item": {"text": "普通问题"}}],
    }

    await adapter._process_message(dict(message))
    await adapter._process_message(dict(message))

    assert seen == ["普通问题"]
