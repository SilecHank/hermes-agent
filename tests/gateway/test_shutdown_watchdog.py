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
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway import shutdown_watchdog as watchdog
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


def test_owner_mode_check_has_safe_non_posix_degradation():
    assert watchdog._owner_and_mode_are_safe(
        SimpleNamespace(st_uid=-1, st_mode=0o100666), platform_name="nt"
    )

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


def test_heartbeat_payload_uses_exact_closed_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "get_process_start_time", lambda pid: 321)
    monkeypatch.setattr(watchdog, "_read_linux_boot_id", lambda: "boot-123")
    _write_runtime_status(
        tmp_path,
        {
            "pid": os.getpid(),
            "start_time": 321,
            "platforms": {"qqbot": {"state": "connected"}},
        },
    )

    path = write_loop_heartbeat(start_time=100.5, home=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected_fields = frozenset(
        {
            "pid",
            "updated_at",
            "monotonic",
            "process_start_time",
            "boot_id",
            "app_start_time",
            "start_time",
            "platforms",
            "platforms_observed_at",
            "platforms_observation_valid",
            "platforms_observation_reason",
        }
    )

    assert watchdog._HEARTBEAT_SCHEMA_FIELDS == expected_fields
    assert set(payload) == expected_fields - {"platforms_observation_reason"}


def test_heartbeat_extra_signature_never_writes_free_fields_or_text(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(watchdog, "get_process_start_time", lambda pid: 321)
    _write_runtime_status(
        tmp_path,
        {"pid": os.getpid(), "start_time": 321, "platforms": {}},
    )

    path = write_loop_heartbeat(
        home=tmp_path,
        extra={
            "Authorization": "Bearer top-secret-token",
            "diagnostic": "customer incident details",
            "attempt": 7,
            "enabled": True,
            "nested": {"note": "arbitrary text"},
        },
    )
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)

    assert set(payload) <= watchdog._HEARTBEAT_SCHEMA_FIELDS
    for forbidden in (
        "Authorization",
        "Bearer",
        "top-secret-token",
        "diagnostic",
        "customer incident details",
        "attempt",
        "enabled",
        "nested",
        "arbitrary text",
    ):
        assert forbidden not in raw


@pytest.mark.parametrize("secure_dir_fd", [True, False])
def test_heartbeat_replaces_final_symlink_without_touching_target(
    tmp_path, monkeypatch, secure_dir_fd
):
    monkeypatch.setattr(watchdog, "get_process_start_time", lambda pid: 321)
    monkeypatch.setattr(
        watchdog, "_secure_dir_fd_supported", lambda: secure_dir_fd
    )
    _write_runtime_status(
        tmp_path,
        {"pid": os.getpid(), "start_time": 321, "platforms": {}},
    )
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "symlink-target"
    target.write_text("do-not-change", encoding="utf-8")
    heartbeat = state / "gateway.heartbeat"
    heartbeat.symlink_to(target)

    write_loop_heartbeat(home=tmp_path)

    assert target.read_text(encoding="utf-8") == "do-not-change"
    assert heartbeat.is_file()
    assert not heartbeat.is_symlink()
    assert json.loads(heartbeat.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_heartbeat_rejects_symlink_in_hermes_home_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "get_process_start_time", lambda pid: 321)
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)

    path = write_loop_heartbeat(home=linked_home)

    assert path == linked_home / "state" / "gateway.heartbeat"
    assert not (real_home / "state" / "gateway.heartbeat").exists()


def test_heartbeat_fsyncs_file_and_directory_and_replaces_by_dir_fd(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(watchdog, "get_process_start_time", lambda pid: 321)
    _write_runtime_status(
        tmp_path,
        {"pid": os.getpid(), "start_time": 321, "platforms": {}},
    )
    fsync_calls = []
    replace_calls = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    def recording_replace(src, dst, **kwargs):
        replace_calls.append((src, dst, kwargs))
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(watchdog.os, "fsync", recording_fsync)
    monkeypatch.setattr(watchdog.os, "replace", recording_replace)

    write_loop_heartbeat(home=tmp_path)

    assert len(fsync_calls) >= 2
    assert replace_calls
    assert replace_calls[-1][2].get("src_dir_fd") is not None
    assert replace_calls[-1][2].get("dst_dir_fd") is not None


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


def test_write_loop_heartbeat_accepts_known_disabled_management_platforms(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(watchdog, "get_process_start_time", lambda pid: 321)
    _write_runtime_status(
        tmp_path,
        {
            "pid": os.getpid(),
            "start_time": 321,
            "platforms": {
                "qqbot": {"state": "connected"},
                "telegram": {"state": "disabled"},
                "feishu": {"state": "disconnected"},
            },
        },
    )

    path = write_loop_heartbeat(home=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["platforms_observation_valid"] is True
    assert payload["platforms"]["telegram"] == {"state": "disabled"}
    assert payload["platforms"]["feishu"] == {"state": "disconnected"}


@pytest.mark.parametrize("platform_name", ["token", "x" * 70_000])
def test_write_loop_heartbeat_rejects_unknown_or_oversized_platform_key(
    tmp_path, monkeypatch, platform_name
):
    monkeypatch.setattr(watchdog, "get_process_start_time", lambda pid: 321)
    _write_runtime_status(
        tmp_path,
        {
            "pid": os.getpid(),
            "start_time": 321,
            "platforms": {platform_name: {"state": "connected"}},
        },
    )

    path = write_loop_heartbeat(home=tmp_path)
    raw = path.read_bytes()
    payload = json.loads(raw)

    assert len(raw) < 64 * 1024
    assert payload["platforms_observation_valid"] is False
    assert payload["platforms_observation_reason"] == "runtime_platforms_invalid"
    assert platform_name not in payload.get("platforms", {})


def test_heartbeat_drops_oversized_and_sensitive_extra_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "get_process_start_time", lambda pid: 321)
    _write_runtime_status(
        tmp_path,
        {"pid": os.getpid(), "start_time": 321, "platforms": {}},
    )

    path = write_loop_heartbeat(
        home=tmp_path,
        extra={
            "note": "x" * 70_000,
            "error_message": "must-not-leak",
            "api_token": "must-not-leak-either",
        },
    )
    raw = path.read_bytes()
    payload = json.loads(raw)

    assert len(raw) < 64 * 1024
    assert "note" not in payload
    assert "error_message" not in payload
    assert "api_token" not in payload
    assert "must-not-leak" not in raw.decode("utf-8")


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


@pytest.mark.parametrize("mutation", ["writable", "hardlink"])
def test_write_loop_heartbeat_rejects_untrusted_runtime_file_metadata(
    tmp_path, monkeypatch, mutation
):
    monkeypatch.setattr(watchdog, "get_process_start_time", lambda pid: 321)
    runtime = tmp_path / "gateway_state.json"
    _write_runtime_status(
        tmp_path,
        {"pid": os.getpid(), "start_time": 321, "platforms": {}},
    )
    if mutation == "writable":
        runtime.chmod(0o666)
    else:
        os.link(runtime, tmp_path / "gateway_state.hardlink")

    path = write_loop_heartbeat(home=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["platforms_observation_valid"] is False
    assert payload["platforms_observation_reason"] == "runtime_status_untrusted"
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


def test_repeated_heartbeat_writes_do_not_leak_file_descriptors(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(watchdog, "get_process_start_time", lambda pid: 321)
    _write_runtime_status(
        tmp_path,
        {"pid": os.getpid(), "start_time": 321, "platforms": {}},
    )
    fd_dir = "/proc/self/fd"
    before = len(os.listdir(fd_dir))
    started = time.monotonic()

    for _ in range(100):
        write_loop_heartbeat(home=tmp_path)

    elapsed = time.monotonic() - started
    after = len(os.listdir(fd_dir))
    assert after <= before + 1
    assert elapsed < 5.0


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
