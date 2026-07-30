"""Bounded discovery tests for effective systemd unit load paths."""

from __future__ import annotations

import os
import shlex
import time
from pathlib import Path

import pytest


TRUSTED_SYSTEMD_ANALYZE = Path("/usr/bin/systemd-analyze")


def _mapped(root: Path, absolute: str) -> Path:
    return root / absolute.lstrip("/")


def _discover(tmp_path: Path, runner, *, environ: dict[str, str] | None = None):
    from gateway.active_host_fence import discover_ivd_cron_service_scopes

    root = tmp_path / "root"
    scopes = discover_ivd_cron_service_scopes(
        "systemd",
        target_path=_mapped(root, "/home/test/.config/systemd/user/hermes-gateway.service"),
        scope_root=root,
        home=Path("/home/test"),
        environ={} if environ is None else environ,
        uid=1000,
        systemd_analyze_path=TRUSTED_SYSTEMD_ANALYZE,
        systemd_analyze_runner=runner,
    )
    return root, scopes


def test_dynamic_discovery_adds_snap_path_and_runs_each_mode_once(tmp_path):
    calls: list[bool] = []

    def runner(**kwargs):
        calls.append(kwargs["user"])
        assert kwargs["binary_path"] == TRUSTED_SYSTEMD_ANALYZE
        assert kwargs["timeout_seconds"] <= 2.0
        assert kwargs["max_output_bytes"] == 64 * 1024
        if kwargs["user"]:
            return b"/var/lib/snapd/desktop/systemd/user\n/usr/lib/systemd/user\n"
        return b"/etc/systemd/system\n/usr/lib/systemd/system\n"

    root, scopes = _discover(tmp_path, runner)

    assert calls == [False, True]
    assert _mapped(root, "/var/lib/snapd/desktop/systemd/user") in scopes
    assert Path("/var/lib/snapd/desktop/systemd/user") not in scopes


def test_dynamic_discovery_uses_fixed_binary_despite_path_fake(tmp_path):
    fake_dir = tmp_path / "fake-bin"
    fake_dir.mkdir()
    (fake_dir / "systemd-analyze").write_text("not executable", encoding="utf-8")
    seen: list[Path] = []

    def runner(**kwargs):
        seen.append(kwargs["binary_path"])
        return b"/usr/lib/systemd/user\n" if kwargs["user"] else b"/usr/lib/systemd/system\n"

    _discover(tmp_path, runner, environ={"PATH": os.fspath(fake_dir)})
    assert seen == [TRUSTED_SYSTEMD_ANALYZE, TRUSTED_SYSTEMD_ANALYZE]


@pytest.mark.parametrize(
    "payload",
    [
        b"relative/systemd/user\n",
        b"/valid/path\n/valid/path\n",
        b"/valid/path\x00suffix\n",
        b"/not/canonical/../path\n",
        b"/" + b"a" * 4097 + b"\n",
        b"\xff\n",
        b"".join(f"/opt/unit-{index}\n".encode() for index in range(65)),
        b"x" * (64 * 1024 + 1),
    ],
)
def test_dynamic_discovery_rejects_malicious_output(tmp_path, payload):
    from gateway.active_host_fence import IvdCronServiceDiscoveryError

    def runner(**kwargs):
        return payload

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_unit_paths_(invalid|output_limit)",
    ):
        _discover(tmp_path, runner)


@pytest.mark.parametrize("kind", ["nonroot", "writable", "symlink"])
def test_systemd_analyze_binary_must_be_trusted(tmp_path, kind):
    from gateway.active_host_fence import (
        IvdCronServiceDiscoveryError,
        validate_systemd_analyze_binary,
    )

    binary = tmp_path / "systemd-analyze"
    binary.write_text("binary", encoding="utf-8")
    binary.chmod(0o755 if kind == "nonroot" else 0o777)
    candidate = binary
    if kind == "symlink":
        candidate = tmp_path / "systemd-analyze-link"
        candidate.symlink_to(binary)
    with pytest.raises(IvdCronServiceDiscoveryError, match="systemd_analyze_binary_untrusted"):
        validate_systemd_analyze_binary(candidate)


def _write_tool(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_systemd_analyze_runner_times_out_and_kills_process_group(tmp_path):
    from gateway.active_host_fence import (
        IvdCronServiceDiscoveryError,
        run_systemd_analyze_unit_paths,
    )

    tool = _write_tool(tmp_path / "slow-tool", "sleep 30 &\nwait\n")
    started = time.monotonic()
    with pytest.raises(IvdCronServiceDiscoveryError, match="systemd_unit_paths_timeout"):
        run_systemd_analyze_unit_paths(
            binary_path=tool,
            user=False,
            timeout_seconds=0.1,
            max_output_bytes=64 * 1024,
            environ={},
        )
    assert time.monotonic() - started < 1.5


def test_systemd_analyze_timeout_kills_child_after_parent_exits(tmp_path):
    from gateway.active_host_fence import (
        IvdCronServiceDiscoveryError,
        run_systemd_analyze_unit_paths,
    )

    marker = tmp_path / "child-survived"
    tool = _write_tool(
        tmp_path / "orphan-tool",
        f"(sleep 0.3; touch {shlex.quote(os.fspath(marker))}) &\nexit 0\n",
    )
    with pytest.raises(IvdCronServiceDiscoveryError, match="systemd_unit_paths_timeout"):
        run_systemd_analyze_unit_paths(
            binary_path=tool,
            user=False,
            timeout_seconds=0.05,
            max_output_bytes=64 * 1024,
            environ={},
        )
    time.sleep(0.4)
    assert not marker.exists()


def test_systemd_analyze_runner_rejects_large_stdout(tmp_path):
    from gateway.active_host_fence import (
        IvdCronServiceDiscoveryError,
        run_systemd_analyze_unit_paths,
    )

    tool = _write_tool(tmp_path / "large-tool", "head -c 70000 /dev/zero\n")
    with pytest.raises(IvdCronServiceDiscoveryError, match="systemd_unit_paths_output_limit"):
        run_systemd_analyze_unit_paths(
            binary_path=tool,
            user=False,
            timeout_seconds=2.0,
            max_output_bytes=64 * 1024,
            environ={},
        )


def test_systemd_analyze_runner_fails_closed_on_nonzero_exit(tmp_path):
    from gateway.active_host_fence import (
        IvdCronServiceDiscoveryError,
        run_systemd_analyze_unit_paths,
    )

    tool = _write_tool(tmp_path / "failed-tool", "echo ignored >&2\nexit 7\n")
    with pytest.raises(IvdCronServiceDiscoveryError, match="systemd_unit_paths_command_failed"):
        run_systemd_analyze_unit_paths(
            binary_path=tool,
            user=True,
            timeout_seconds=2.0,
            max_output_bytes=64 * 1024,
            environ={},
        )


def test_static_fallback_and_launchd_do_not_call_runner(tmp_path):
    from gateway.active_host_fence import discover_ivd_cron_service_scopes

    def forbidden_runner(**kwargs):
        raise AssertionError("runner must not be called")

    root = tmp_path / "root"
    systemd_scopes = discover_ivd_cron_service_scopes(
        "systemd",
        target_path=_mapped(root, "/home/test/.config/systemd/user/hermes-gateway.service"),
        scope_root=root,
        home=Path("/home/test"),
        environ={},
        uid=1000,
        systemd_analyze_path=None,
        systemd_analyze_runner=forbidden_runner,
    )
    assert _mapped(root, "/etc/systemd/system") in systemd_scopes

    launchd_scopes = discover_ivd_cron_service_scopes(
        "launchd",
        target_path=_mapped(root, "/Users/test/Library/LaunchAgents/hermes.plist"),
        scope_root=root,
        home=Path("/Users/test"),
        environ={},
        uid=501,
        systemd_analyze_path=TRUSTED_SYSTEMD_ANALYZE,
        systemd_analyze_runner=forbidden_runner,
    )
    assert _mapped(root, "/Library/LaunchDaemons") in launchd_scopes


@pytest.mark.parametrize(
    ("platform", "kind", "expected"),
    [
        ("linux", "systemd", TRUSTED_SYSTEMD_ANALYZE),
        ("darwin", "systemd", None),
        ("linux", "launchd", None),
    ],
)
def test_gateway_selects_systemd_analyze_only_for_linux_systemd(
    monkeypatch, platform, kind, expected
):
    import hermes_cli.gateway as gateway

    monkeypatch.setattr(gateway.sys, "platform", platform)
    assert gateway._ivd_systemd_analyze_path(kind) == expected


def test_gateway_contract_passes_fixed_binary_only_to_systemd(monkeypatch, tmp_path):
    import gateway.active_host_fence as fence
    import hermes_cli.gateway as gateway

    calls = []

    def fake_assert(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(fence, "assert_embedded_ivd_cron_service_contract", fake_assert)
    monkeypatch.setattr(gateway.sys, "platform", "linux")
    monkeypatch.setattr(gateway, "_ivd_service_scope_root", lambda: tmp_path)
    gateway._enforce_embedded_ivd_cron_contract(
        "systemd", tmp_path / "hermes-gateway.service"
    )
    gateway._enforce_embedded_ivd_cron_contract(
        "launchd", tmp_path / "com.example.hermes.plist"
    )

    assert calls[0]["systemd_analyze_path"] == TRUSTED_SYSTEMD_ANALYZE
    assert calls[1]["systemd_analyze_path"] is None
