"""The "/sethome" tip is DM-shaped advice and must not leak into group chats."""

from __future__ import annotations

from types import SimpleNamespace

from gateway.config import Platform
from gateway.run import _should_offer_home_channel


def _source(platform=Platform.WECOM, chat_type="dm"):
    return SimpleNamespace(platform=platform, chat_type=chat_type)


def test_first_dm_turn_offers_the_tip():
    assert _should_offer_home_channel(_source("dm"), has_history=False) is True


def test_first_group_turn_never_offers_the_tip():
    # Regression (2026-11-01): the WeCom group's first message after the daily
    # reset answered with "/sethome" instead of the question.
    for chat_type in ("group", "forum", "channel"):
        assert _should_offer_home_channel(_source(chat_type=chat_type), has_history=False) is False


def test_existing_history_suppresses_the_tip():
    assert _should_offer_home_channel(_source("dm"), has_history=True) is False


def test_local_and_webhook_sources_never_offer_the_tip():
    assert _should_offer_home_channel(_source(Platform.LOCAL, ""), has_history=False) is False
    assert (
        _should_offer_home_channel(_source(Platform.WEBHOOK, ""), has_history=False) is False
    )
