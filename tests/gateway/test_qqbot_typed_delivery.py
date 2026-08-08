"""Typed QQ proactive delivery tests with no optional Telegram dependency."""

import asyncio
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cron.scheduler import _deliver_result
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.delivery import DeliveryRouter, DeliveryTarget
from gateway.platforms.qqbot import QQAdapter
from tools.send_message_tool import _send_qqbot


GROUP_OPENID = "0123456789abcdef0123456789abcdef"
DIRECT_OPENID = "fedcba9876543210fedcba9876543210"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"


class _FakeQQResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeQQHttp:
    def __init__(self, send_statuses):
        self.send_statuses = list(send_statuses)
        self.calls = []

    def __call__(self, *_args, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "payload": json, "headers": headers})
        if url == TOKEN_URL:
            return _FakeQQResponse(200, {"access_token": "qq-access-token"})
        if not self.send_statuses:
            raise AssertionError(f"Unexpected QQBot endpoint probe: {url}")
        return _FakeQQResponse(
            self.send_statuses.pop(0),
            {"id": "qq-message-id"},
        )


def _qq_send_urls(fake):
    return [call["url"] for call in fake.calls if call["url"] != TOKEN_URL]


def _run_standalone_qq_send(monkeypatch, target, send_statuses):
    import httpx

    fake = _FakeQQHttp(send_statuses)
    monkeypatch.setattr(httpx, "AsyncClient", fake)
    pconfig = SimpleNamespace(token="qq-secret", extra={"app_id": "qq-app"})
    result = asyncio.run(_send_qqbot(pconfig, target, "hello qq"))
    return result, fake


def _connected_adapter(api_request):
    pconfig = PlatformConfig(
        enabled=True,
        extra={"app_id": "qq-app", "client_secret": "qq-secret"},
    )
    adapter = QQAdapter(pconfig)
    adapter._running = True
    adapter._ws = SimpleNamespace(closed=False)
    adapter._api_request = api_request
    return pconfig, adapter


def _run_coro_immediately(coro, _loop):
    future = Future()
    try:
        future.set_result(asyncio.run(coro))
    except BaseException as exc:
        future.set_exception(exc)
    return future


def test_standalone_typed_group_uses_only_group_endpoint(monkeypatch):
    result, fake = _run_standalone_qq_send(
        monkeypatch,
        f"group:{GROUP_OPENID}",
        [201],
    )

    assert result["success"] is True
    assert result["chat_id"] == GROUP_OPENID
    assert _qq_send_urls(fake) == [
        f"https://api.sgroup.qq.com/v2/groups/{GROUP_OPENID}/messages"
    ]


def test_standalone_typed_direct_uses_only_direct_endpoint(monkeypatch):
    result, fake = _run_standalone_qq_send(
        monkeypatch,
        f"direct:{DIRECT_OPENID}",
        [200],
    )

    assert result["success"] is True
    assert result["chat_id"] == DIRECT_OPENID
    assert _qq_send_urls(fake) == [
        f"https://api.sgroup.qq.com/v2/users/{DIRECT_OPENID}/messages"
    ]


@pytest.mark.parametrize(
    ("typed_target", "expected_url"),
    [
        (
            f"group:{GROUP_OPENID}",
            f"https://api.sgroup.qq.com/v2/groups/{GROUP_OPENID}/messages",
        ),
        (
            f"direct:{DIRECT_OPENID}",
            f"https://api.sgroup.qq.com/v2/users/{DIRECT_OPENID}/messages",
        ),
    ],
)
def test_standalone_failed_typed_send_does_not_cross_endpoints(
    monkeypatch,
    typed_target,
    expected_url,
):
    result, fake = _run_standalone_qq_send(monkeypatch, typed_target, [500])

    assert "error" in result
    assert _qq_send_urls(fake) == [expected_url]


def test_standalone_bare_openid_keeps_multi_endpoint_probe(monkeypatch):
    result, fake = _run_standalone_qq_send(
        monkeypatch,
        GROUP_OPENID,
        [404, 404, 201],
    )

    assert result["success"] is True
    assert _qq_send_urls(fake) == [
        f"https://api.sgroup.qq.com/channels/{GROUP_OPENID}/messages",
        f"https://api.sgroup.qq.com/v2/users/{GROUP_OPENID}/messages",
        f"https://api.sgroup.qq.com/v2/groups/{GROUP_OPENID}/messages",
    ]


@pytest.mark.parametrize(
    ("kind", "openid"),
    [("group", GROUP_OPENID), ("direct", DIRECT_OPENID)],
)
def test_delivery_target_parse_preserves_qq_typed_chat_id(kind, openid):
    target = DeliveryTarget.parse(f"qqbot:{kind}:{openid}")

    assert target.platform == Platform.QQBOT
    assert target.chat_id == f"{kind}:{openid}"
    assert target.thread_id is None
    assert target.to_string() == f"qqbot:{kind}:{openid}"


@pytest.mark.parametrize("kind", ["group", "direct"])
def test_delivery_target_parse_rejects_malformed_qq_typed_chat_id(kind):
    with pytest.raises(ValueError, match="Malformed QQBot typed target"):
        DeliveryTarget.parse(f"qqbot:{kind}:short")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "openid", "resource"),
    [
        ("group", GROUP_OPENID, "groups"),
        ("direct", DIRECT_OPENID, "users"),
    ],
)
async def test_live_delivery_router_preserves_qq_type(kind, openid, resource):
    paths = []

    async def fake_api_request(method, path, body):
        paths.append((method, path))
        return {"id": "live-message"}

    pconfig, adapter = _connected_adapter(fake_api_request)
    config = GatewayConfig(platforms={Platform.QQBOT: pconfig})
    router = DeliveryRouter(config, adapters={Platform.QQBOT: adapter})

    result = await router._deliver_to_platform(
        DeliveryTarget.parse(f"qqbot:{kind}:{openid}"),
        "live hello",
        metadata=None,
    )

    assert result.success is True
    assert paths == [("POST", f"/v2/{resource}/{openid}/messages")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "openid", "resource"),
    [
        ("group", GROUP_OPENID, "groups"),
        ("direct", DIRECT_OPENID, "users"),
    ],
)
async def test_live_delivery_failure_does_not_cross_qq_endpoints(
    kind,
    openid,
    resource,
):
    paths = []

    async def failing_api_request(method, path, body):
        paths.append((method, path))
        raise RuntimeError("bad request")

    pconfig, adapter = _connected_adapter(failing_api_request)
    config = GatewayConfig(platforms={Platform.QQBOT: pconfig})
    router = DeliveryRouter(config, adapters={Platform.QQBOT: adapter})

    with pytest.raises(RuntimeError, match="bad request"):
        await router._deliver_to_platform(
            DeliveryTarget.parse(f"qqbot:{kind}:{openid}"),
            "live hello",
            metadata=None,
        )

    assert paths == [("POST", f"/v2/{resource}/{openid}/messages")]


@pytest.mark.parametrize(
    ("kind", "openid", "resource"),
    [
        ("group", GROUP_OPENID, "groups"),
        ("direct", DIRECT_OPENID, "users"),
    ],
)
def test_cron_live_delivery_preserves_qq_type(kind, openid, resource):
    paths = []

    async def fake_api_request(method, path, body):
        paths.append((method, path))
        return {"id": "cron-message"}

    pconfig, adapter = _connected_adapter(fake_api_request)
    config = GatewayConfig(platforms={Platform.QQBOT: pconfig})
    loop = SimpleNamespace(is_running=lambda: True)
    job = {
        "id": "qq-cron",
        "name": "QQ Cron",
        "deliver": f"qqbot:{kind}:{openid}",
    }

    with patch("gateway.config.load_gateway_config", return_value=config), patch(
        "cron.scheduler.load_config",
        return_value={"cron": {"wrap_response": False}},
    ), patch(
        "agent.async_utils.safe_schedule_threadsafe",
        side_effect=_run_coro_immediately,
    ):
        error = _deliver_result(
            job,
            "cron hello",
            adapters={Platform.QQBOT: adapter},
            loop=loop,
        )

    assert error is None
    assert paths == [("POST", f"/v2/{resource}/{openid}/messages")]
