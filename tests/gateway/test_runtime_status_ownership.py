"""Ownership handling for ``gateway_state.json``.

Regression coverage for a release-blocking symptom: a fully connected gateway
reported ``gateway_state=starting`` with an empty ``platforms`` map, making
health gates reject it until the process was restarted.

The cause was ``write_runtime_status`` rebuilding the record from defaults
whenever the PID/start-time pair did not match byte-for-byte.  A start-time
fingerprint read that drifted (a transient ``/proc`` failure falling back to a
psutil value in different units) was enough to look like "this file belongs to
another process", and the rebuild discarded both the lifecycle state and every
connected platform.  Because adapters announce ``connected`` only once, nothing
repaired the record afterwards.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

import gateway.status as status_mod


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _status_path():
    return status_mod._get_runtime_status_path()


def _healthy_running_record(platforms=("wecom", "weixin", "qqbot")):
    """Drive the real startup sequence: starting -> connected -> running."""
    status_mod.write_runtime_status(gateway_state="starting", exit_reason=None)
    for platform in platforms:
        status_mod.write_runtime_status(
            platform=platform,
            platform_state="connected",
            error_code=None,
            error_message=None,
        )
    status_mod.write_runtime_status(
        gateway_state="running", exit_reason=None, restart_requested=False,
        active_agents=0,
    )
    record = _read(_status_path())
    assert record["gateway_state"] == "running"
    assert sorted(record["platforms"]) == sorted(platforms)
    return record


def test_healthy_startup_sequence_persists_lifecycle_and_platforms():
    record = _healthy_running_record()
    assert record["platforms"]["weixin"]["state"] == "connected"


def test_turn_boundary_write_keeps_lifecycle_and_platforms():
    """``_persist_active_agents`` passes only active_agents — it must merge."""
    _healthy_running_record()
    status_mod.write_runtime_status(active_agents=1)
    record = _read(_status_path())
    assert record["gateway_state"] == "running"
    assert sorted(record["platforms"]) == ["qqbot", "wecom", "weixin"]
    assert record["active_agents"] == 1


def test_drifted_start_time_for_our_own_pid_does_not_reset_the_record():
    """The regression: our PID with a drifted fingerprint is still our file."""
    record = _healthy_running_record()
    record["start_time"] = int(record["start_time"] or 0) + 1
    _status_path().write_text(json.dumps(record), encoding="utf-8")

    status_mod.write_runtime_status(active_agents=1)

    after = _read(_status_path())
    assert after["gateway_state"] == "running"
    assert sorted(after["platforms"]) == ["qqbot", "wecom", "weixin"]
    assert after["platforms"]["wecom"]["state"] == "connected"


def test_platform_write_after_drift_keeps_whole_map():
    record = _healthy_running_record()
    record["start_time"] = int(record["start_time"] or 0) + 1
    _status_path().write_text(json.dumps(record), encoding="utf-8")

    status_mod.write_runtime_status(
        platform="weixin", platform_state="connected",
        error_code=None, error_message=None,
    )

    after = _read(_status_path())
    assert after["gateway_state"] == "running"
    assert sorted(after["platforms"]) == ["qqbot", "wecom", "weixin"]


def test_live_foreign_writer_takeover_preserves_observations(monkeypatch):
    """A second live writer in the same home must not blank the record."""
    record = _healthy_running_record()
    foreign_pid = 424242
    record["pid"] = foreign_pid
    _status_path().write_text(json.dumps(record), encoding="utf-8")

    monkeypatch.setattr(status_mod, "_pid_exists", lambda pid: pid == foreign_pid)
    monkeypatch.setattr(
        status_mod, "_get_process_start_time", lambda pid: record["start_time"]
    )
    monkeypatch.setattr(status_mod, "_OWNERSHIP_CHANGE_LOGGED", False)

    status_mod.write_runtime_status(active_agents=2)

    after = _read(_status_path())
    assert after["pid"] != foreign_pid
    assert after["gateway_state"] == "running"
    assert sorted(after["platforms"]) == ["qqbot", "wecom", "weixin"]
    assert after["active_agents"] == 2


def test_dead_writer_record_starts_fresh(monkeypatch):
    """An ordinary restart after a crash inherits nothing."""
    record = _healthy_running_record()
    record["pid"] = 424243
    _status_path().write_text(json.dumps(record), encoding="utf-8")

    monkeypatch.setattr(status_mod, "_pid_exists", lambda pid: False)

    status_mod.write_runtime_status(gateway_state="starting", exit_reason=None)

    after = _read(_status_path())
    assert after["gateway_state"] == "starting"
    assert after["platforms"] == {}


def test_recycled_pid_record_starts_fresh(monkeypatch):
    """Same PID, different start time = a reused PID, not our file."""
    record = _healthy_running_record()
    record["pid"] = 424244
    record["start_time"] = 1000
    _status_path().write_text(json.dumps(record), encoding="utf-8")

    monkeypatch.setattr(status_mod, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(status_mod, "_get_process_start_time", lambda pid: 2000)

    status_mod.write_runtime_status(gateway_state="starting", exit_reason=None)

    after = _read(_status_path())
    assert after["gateway_state"] == "starting"
    assert after["platforms"] == {}


def test_unknown_start_time_is_not_a_mismatch(monkeypatch):
    record = _healthy_running_record()
    record["pid"] = 424245
    record["start_time"] = None
    _status_path().write_text(json.dumps(record), encoding="utf-8")

    monkeypatch.setattr(status_mod, "_pid_exists", lambda pid: pid == 424245)
    monkeypatch.setattr(status_mod, "_get_process_start_time", lambda pid: 777)
    monkeypatch.setattr(status_mod, "_OWNERSHIP_CHANGE_LOGGED", False)

    status_mod.write_runtime_status(active_agents=1)

    after = _read(_status_path())
    assert after["gateway_state"] == "running"
    assert sorted(after["platforms"]) == ["qqbot", "wecom", "weixin"]


def test_linux_fingerprint_never_falls_back_to_psutil(monkeypatch, tmp_path):
    """Units must not be mixed: /proc ticks vs psutil centiseconds."""
    monkeypatch.setattr(status_mod, "_PROC_STAT_DIR", tmp_path)  # exists, no <pid>/stat
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        types.SimpleNamespace(Process=lambda pid: pytest.fail("psutil used on Linux")),
    )
    assert status_mod._get_process_start_time(999999) is None


def test_startup_reconcile_reasserts_connected_adapters(monkeypatch):
    """A gateway whose platform writes were lost repairs itself at startup."""
    from gateway.run import GatewayRunner

    # Simulate the lost writes: record exists but carries no platform states.
    status_mod.write_runtime_status(gateway_state="running", exit_reason=None)
    assert _read(_status_path())["platforms"] == {}

    class _Adapter:
        def __init__(self, running):
            self._running = running
            self.has_fatal_error = False

    class _Platform:
        def __init__(self, value):
            self.value = value

    class _Runner:
        # Bind the real methods onto a stub so the production code path runs.
        _update_platform_runtime_status = (
            GatewayRunner._update_platform_runtime_status
        )
        _reconcile_platform_runtime_status = (
            GatewayRunner._reconcile_platform_runtime_status
        )

        def __init__(self, adapters):
            self.adapters = adapters

    runner = _Runner({
        _Platform("wecom"): _Adapter(True),
        _Platform("weixin"): _Adapter(True),
        _Platform("qqbot"): _Adapter(False),
    })
    # Bind the real method onto the stub so the production code path runs.
    GatewayRunner._reconcile_platform_runtime_status(runner)

    after = _read(_status_path())
    assert after["gateway_state"] == "running"
    assert after["platforms"]["wecom"]["state"] == "connected"
    assert after["platforms"]["weixin"]["state"] == "connected"
    assert after["platforms"]["qqbot"]["state"] == "disconnected"
