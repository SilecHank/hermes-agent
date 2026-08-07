"""Shutdown watchdog + loop heartbeat coverage for #66892.

The drain path is asyncio-based; a frozen loop makes every asyncio timeout
structurally unable to fire. These tests pin the out-of-loop backstop
(thread watchdog) and the loop-liveness heartbeat file contract.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from unittest.mock import patch

import pytest

from gateway.shutdown_watchdog import (
    DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S,
    _read_linux_boot_id,
    arm_shutdown_watchdog,
    get_loop_heartbeat_path,
    get_shutdown_watchdog_dump_path,
    loop_heartbeat_forever,
    resolve_shutdown_watchdog_delay,
    write_loop_heartbeat,
)


def test_read_linux_boot_id_rejects_symlink(tmp_path):
    target = tmp_path / "boot-id.txt"
    target.write_text("12345678-1234-1234-1234-123456789abc\n", encoding="ascii")
    link = tmp_path / "boot-id-link"
    link.symlink_to(target)

    assert _read_linux_boot_id(str(link)) is None

def test_resolve_shutdown_watchdog_delay_adds_grace():
    assert resolve_shutdown_watchdog_delay(180) == 180 + DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    assert resolve_shutdown_watchdog_delay(0) == DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    assert resolve_shutdown_watchdog_delay("bad") == DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    assert resolve_shutdown_watchdog_delay(10, grace_s=5) == 15.0


def test_write_loop_heartbeat_atomic_json(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "gateway.shutdown_watchdog.get_process_start_time", lambda pid: 777
    )
    monkeypatch.setattr(
        "gateway.shutdown_watchdog._read_linux_boot_id", lambda: "boot-123"
    )
    path = write_loop_heartbeat(pid=4242, start_time=100.5, home=tmp_path)
    assert path == tmp_path / "state" / "gateway.heartbeat"
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["pid"] == 4242
    assert data["process_start_time"] == 777
    assert data["app_start_time"] == 100.5
    assert data["start_time"] == 100.5
    assert data["boot_id"] == "boot-123"
    assert "updated_at" in data
    assert "monotonic" in data
    assert get_loop_heartbeat_path(tmp_path) == path


def _write_runtime_status(home, payload):
    (home / "gateway_state.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_write_loop_heartbeat_projects_only_live_platform_states(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gateway.shutdown_watchdog.get_process_start_time", lambda pid: 321
    )
    _write_runtime_status(
        tmp_path,
        {
            "pid": os.getpid(),
            "start_time": 321,
            "platforms": {
                "qqbot": {
                    "state": "connected",
                    "error_message": "token=must-not-leak",
                    "channel_id": "customer-room",
                },
                "wecom": {"state": "retrying", "credential": "secret"},
            },
        },
    )

    path = write_loop_heartbeat(start_time=100.5, home=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["platforms_observation_valid"] is True
    assert payload["platforms"] == {
        "qqbot": {"state": "connected"},
        "wecom": {"state": "retrying"},
    }
    assert "platforms_observed_at" in payload
    serialized = json.dumps(payload)
    assert "must-not-leak" not in serialized
    assert "customer-room" not in serialized
    assert "secret" not in serialized


def test_write_loop_heartbeat_rejects_unbounded_platform_state_text(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "gateway.shutdown_watchdog.get_process_start_time", lambda pid: 321
    )
    _write_runtime_status(
        tmp_path,
        {
            "pid": os.getpid(),
            "start_time": 321,
            "platforms": {"qqbot": {"state": "token=must-not-leak"}},
        },
    )

    path = write_loop_heartbeat(home=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["platforms_observation_valid"] is False
    assert payload["platforms_observation_reason"] == "runtime_platforms_invalid"
    assert "platforms" not in payload
    assert "must-not-leak" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("runtime_payload", "reason"),
    [
        (None, "runtime_status_missing"),
        ("not-json", "runtime_status_invalid"),
        ({"pid": 999999, "start_time": 321, "platforms": {}}, "runtime_process_mismatch"),
        (
            {"pid": os.getpid(), "start_time": 999, "platforms": {}},
            "runtime_process_mismatch",
        ),
    ],
)
def test_write_loop_heartbeat_fails_closed_for_untrusted_runtime(
    tmp_path, monkeypatch, runtime_payload, reason
):
    monkeypatch.setattr(
        "gateway.shutdown_watchdog.get_process_start_time", lambda pid: 321
    )
    if isinstance(runtime_payload, dict):
        _write_runtime_status(tmp_path, runtime_payload)
    elif isinstance(runtime_payload, str):
        (tmp_path / "gateway_state.json").write_text(
            runtime_payload, encoding="utf-8"
        )

    path = write_loop_heartbeat(start_time=100.5, home=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["pid"] == os.getpid()
    assert payload["platforms_observation_valid"] is False
    assert payload["platforms_observation_reason"] == reason
    assert "platforms" not in payload
    assert "platforms_observed_at" not in payload


def test_write_loop_heartbeat_rejects_symlinked_runtime_status(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gateway.shutdown_watchdog.get_process_start_time", lambda pid: 321
    )
    target = tmp_path / "foreign.json"
    _write_runtime_status(
        tmp_path,
        {"pid": os.getpid(), "start_time": 321, "platforms": {}},
    )
    (tmp_path / "gateway_state.json").replace(target)
    (tmp_path / "gateway_state.json").symlink_to(target)

    path = write_loop_heartbeat(home=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["platforms_observation_valid"] is False
    assert payload["platforms_observation_reason"] == "runtime_status_symlink"
    assert "platforms" not in payload


def test_extra_cannot_override_heartbeat_identity_or_platform_observation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "gateway.shutdown_watchdog.get_process_start_time", lambda pid: 321
    )
    _write_runtime_status(
        tmp_path,
        {
            "pid": os.getpid(),
            "start_time": 321,
            "platforms": {"qqbot": {"state": "connected"}},
        },
    )

    path = write_loop_heartbeat(
        home=tmp_path,
        extra={
            "pid": 1,
            "process_start_time": 999,
            "platforms": {"fake": {"state": "connected", "token": "secret"}},
            "platforms_observation_valid": False,
        },
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["pid"] == os.getpid()
    assert payload["process_start_time"] == 321
    assert payload["platforms"] == {"qqbot": {"state": "connected"}}
    assert payload["platforms_observation_valid"] is True
    assert "secret" not in json.dumps(payload)


def test_arm_shutdown_watchdog_disarm_before_fire(tmp_path):
    done = threading.Event()
    exited = []

    def fake_exit(code):
        exited.append(code)
        raise _ExitCalled(code)

    with patch("gateway.shutdown_watchdog.os._exit", side_effect=fake_exit):
        arm_shutdown_watchdog(
            0.4,
            done_event=done,
            dump_path=tmp_path / "dump.log",
            exit_code=7,
        )
        time.sleep(0.1)
        done.set()
        time.sleep(0.5)

    assert exited == []


def test_arm_shutdown_watchdog_fires_with_dump_and_exit(tmp_path):
    done = threading.Event()
    fired = threading.Event()
    dump = tmp_path / "logs" / "watchdog.log"
    snapshot_calls = []
    exit_codes = []

    def snapshot():
        snapshot_calls.append(1)
        return {"active_agents": 1, "draining": True}

    def fake_exit(code):
        exit_codes.append(code)
        fired.set()

    with patch("gateway.shutdown_watchdog.os._exit", side_effect=fake_exit):
        arm_shutdown_watchdog(
            0.15,
            done_event=done,
            snapshot_fn=snapshot,
            dump_path=dump,
            exit_code=9,
        )
        assert fired.wait(timeout=5.0), "watchdog did not fire"

    assert exit_codes == [9]
    assert snapshot_calls == [1]
    assert dump.is_file()
    text = dump.read_text(encoding="utf-8")
    assert "shutdown_watchdog_fired" in text
    assert "faulthandler dump" in text
    assert get_shutdown_watchdog_dump_path(tmp_path).name == "gateway-shutdown-watchdog.log"


@pytest.mark.asyncio
async def test_loop_heartbeat_rewrites_until_cancelled(tmp_path):
    from gateway.status import get_process_start_time

    process_start_time = get_process_start_time(os.getpid())
    _write_runtime_status(
        tmp_path,
        {
            "pid": os.getpid(),
            "start_time": process_start_time,
            "platforms": {"qqbot": {"state": "connected"}},
        },
    )
    path = get_loop_heartbeat_path(tmp_path)
    task = asyncio.create_task(
        loop_heartbeat_forever(
            interval_s=0.05,
            start_time=12.0,
            home=tmp_path,
        )
    )
    try:
        # First write is immediate.
        for _ in range(50):
            if path.is_file():
                break
            await asyncio.sleep(0.02)
        assert path.is_file()
        first = path.read_text(encoding="utf-8")
        first_payload = json.loads(first)
        assert first_payload["app_start_time"] == 12.0
        assert first_payload["platforms_observation_valid"] is True
        first_observed_at = first_payload["platforms_observed_at"]

        # Poll until a refresh lands (monotonic / updated_at change).
        second = first
        for _ in range(100):
            await asyncio.sleep(0.03)
            second = path.read_text(encoding="utf-8")
            if second != first:
                break
        assert second != first
        second_payload = json.loads(second)
        assert second_payload["app_start_time"] == 12.0
        assert second_payload["platforms_observation_valid"] is True
        assert second_payload["platforms_observed_at"] != first_observed_at
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_gateway_runner_exposes_shutdown_watchdog_state():
    """Attrs used by stop()/start() exist after normal construction hooks."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._shutdown_watchdog_done = threading.Event()
    runner._loop_heartbeat_task = None
    runner._gateway_started_at = time.time()
    assert not runner._shutdown_watchdog_done.is_set()
    runner._shutdown_watchdog_done.set()
    assert runner._shutdown_watchdog_done.is_set()
    assert runner._loop_heartbeat_task is None
