"""Quality regressions for the opt-in IVD service installation contract."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest


def _mapped(root: Path, absolute: str) -> Path:
    return root / absolute.lstrip("/")


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _assert_systemd(root: Path, target: Path):
    from hermes_cli.ivd_cron_service_contract import (
        assert_embedded_ivd_cron_service_contract,
    )

    return assert_embedded_ivd_cron_service_contract(
        kind="systemd",
        service_dir=target,
        target_path=target / "hermes-gateway.service",
        scope_root=root,
        home=Path("/home/test"),
        environ={},
        uid=1000,
        systemd_analyze_path=None,
    )


def _base_timer(scope: Path, *, schedule: str = "OnCalendar=daily") -> None:
    _write(scope / "nightly.timer", f"[Timer]\n{schedule}\n")
    _write(scope / "nightly.service", "[Service]\nExecStart=/usr/bin/backup\n")


def test_owner_fence_module_has_no_installer_contract_surface():
    import gateway.active_host_fence as owner

    forbidden = {
        "IndependentIvdCronServiceError",
        "IvdCronServiceDiscoveryError",
        "assert_embedded_ivd_cron_service_contract",
        "discover_ivd_cron_service_scopes",
        "run_systemd_analyze_unit_paths",
        "validate_systemd_analyze_binary",
    }
    assert forbidden.isdisjoint(vars(owner))


def test_cold_owner_and_gateway_run_imports_do_not_load_installer_contract():
    code = (
        "import sys; import gateway.active_host_fence; import gateway.run; "
        "assert 'hermes_cli.ivd_cron_service_contract' not in sys.modules"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr


def test_default_disabled_gateway_contract_skips_import_and_io(monkeypatch, tmp_path):
    import hermes_cli.gateway as gateway

    monkeypatch.delenv("IVD_ACTIVE_HOST_FENCE_REQUIRED", raising=False)
    monkeypatch.delenv("IVD_CRON_SERVICE_CONTRACT_REQUIRED", raising=False)
    sys.modules.pop("hermes_cli.ivd_cron_service_contract", None)
    gateway._enforce_embedded_ivd_cron_contract(
        "systemd", tmp_path / "hermes-gateway.service"
    )
    assert "hermes_cli.ivd_cron_service_contract" not in sys.modules


@pytest.mark.parametrize(
    "switch", ["IVD_ACTIVE_HOST_FENCE_REQUIRED", "IVD_CRON_SERVICE_CONTRACT_REQUIRED"]
)
def test_explicit_contract_switch_enables_lazy_installer_scan(
    monkeypatch, tmp_path, switch
):
    import hermes_cli.gateway as gateway
    import hermes_cli.ivd_cron_service_contract as contract

    calls = []
    monkeypatch.delenv("IVD_ACTIVE_HOST_FENCE_REQUIRED", raising=False)
    monkeypatch.delenv("IVD_CRON_SERVICE_CONTRACT_REQUIRED", raising=False)
    monkeypatch.setenv(switch, "true")
    monkeypatch.setattr(
        contract,
        "assert_embedded_ivd_cron_service_contract",
        lambda **kwargs: calls.append(kwargs),
    )
    gateway._enforce_embedded_ivd_cron_contract(
        "systemd", tmp_path / "hermes-gateway.service"
    )
    assert len(calls) == 1


def test_trusted_candidate_search_supports_bin_symlink_and_nixos(monkeypatch):
    import hermes_cli.ivd_cron_service_contract as contract

    assert contract.find_trusted_systemd_analyze(
        candidates=(Path("/bin/systemd-analyze"),)
    ) == Path("/usr/bin/systemd-analyze")

    nix = Path("/run/current-system/sw/bin/systemd-analyze")
    monkeypatch.setattr(
        contract,
        "validate_systemd_analyze_binary",
        lambda path: Path("/nix/store/trusted-systemd-analyze")
        if path == nix
        else (_ for _ in ()).throw(
            contract.IvdCronServiceDiscoveryError(
                "systemd_analyze_binary_untrusted", path
            )
        ),
    )
    assert contract.find_trusted_systemd_analyze(candidates=(nix,)) == Path(
        "/nix/store/trusted-systemd-analyze"
    )


def test_trusted_binary_rejects_user_mutable_symlink_parent(tmp_path):
    from hermes_cli.ivd_cron_service_contract import (
        IvdCronServiceDiscoveryError,
        validate_systemd_analyze_binary,
    )

    redirect = tmp_path / "mutable-bin"
    redirect.symlink_to("/usr/bin", target_is_directory=True)
    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_analyze_binary_untrusted",
    ):
        validate_systemd_analyze_binary(redirect / "systemd-analyze")


def test_enabled_contract_fails_closed_without_trusted_systemd_analyze(
    monkeypatch, tmp_path
):
    import hermes_cli.gateway as gateway
    import hermes_cli.ivd_cron_service_contract as contract

    monkeypatch.setenv("IVD_CRON_SERVICE_CONTRACT_REQUIRED", "true")
    monkeypatch.setattr(contract, "SYSTEMD_ANALYZE_CANDIDATES", ())
    with pytest.raises(
        contract.IvdCronServiceDiscoveryError,
        match="systemd_analyze_binary_untrusted",
    ):
        gateway._enforce_embedded_ivd_cron_contract(
            "systemd", tmp_path / "hermes-gateway.service"
        )


def test_timer_schedule_in_unit_specific_dropin_is_blocked(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _base_timer(scope, schedule="")
    _write(scope / "nightly.service", "[Service]\nExecStart=/opt/ivd/sync\n")
    _write(
        scope / "nightly.timer.d" / "10-schedule.conf",
        "[Timer]\nOnCalendar=daily\n",
    )
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(IndependentIvdCronServiceError):
        _assert_systemd(root, target)


@pytest.mark.parametrize("trigger", ["OnClockChange=yes", "OnTimezoneChange=yes"])
def test_additional_systemd_timer_triggers_are_blocked(tmp_path, trigger):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _base_timer(scope, schedule="")
    _write(scope / "nightly.service", "[Service]\nExecStart=/opt/ivd/sync\n")
    _write(
        scope / "nightly.timer.d" / "10-trigger.conf",
        f"[Timer]\n{trigger}\n",
    )
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(IndependentIvdCronServiceError):
        _assert_systemd(root, target)


def test_ivd_execstart_in_service_dropin_is_blocked(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _base_timer(scope)
    _write(
        scope / "nightly.service.d" / "20-worker.conf",
        "[Service]\nExecStart=\nExecStart=/opt/ivd/sync\n",
    )
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(IndependentIvdCronServiceError):
        _assert_systemd(root, target)


def test_type_wide_dropins_and_line_continuations_are_effective(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _base_timer(scope, schedule="")
    _write(scope / "timer.d" / "10-schedule.conf", "[Timer]\nOnBootSec=5m\n")
    _write(
        scope / "service.d" / "20-worker.conf",
        "[Service]\nExecStart=\nExecStart=/usr/bin/python3 \\\n+ /opt/ivd/worker.py\n",
    )
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(IndependentIvdCronServiceError):
        _assert_systemd(root, target)


def test_high_priority_same_name_dropin_reset_removes_lower_schedule(tmp_path):
    root = tmp_path / "root"
    high = _mapped(root, "/etc/systemd/user")
    low = _mapped(root, "/usr/lib/systemd/user")
    _base_timer(low, schedule="")
    _write(low / "nightly.service", "[Service]\nExecStart=/opt/ivd/sync\n")
    _write(
        low / "nightly.timer.d" / "10-schedule.conf",
        "[Timer]\nOnCalendar=daily\n",
    )
    _write(
        high / "nightly.timer.d" / "10-schedule.conf",
        "[Timer]\nOnCalendar=\n",
    )
    target = tmp_path / "target"
    target.mkdir()
    assert _assert_systemd(root, target).allowed


def test_dropin_lexical_reset_removes_ivd_execstart_and_timer_unit_override(
    tmp_path,
):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _base_timer(scope, schedule="")
    _write(
        scope / "nightly.timer.d" / "10-schedule.conf",
        "[Timer]\nOnCalendar=daily\nUnit=ivd-worker.service\n",
    )
    _write(
        scope / "nightly.timer.d" / "20-reset.conf",
        "[Timer]\nOnCalendar=\nUnit=\nUnit=nightly.service\n",
    )
    _write(
        scope / "nightly.service.d" / "10-ivd.conf",
        "[Service]\nExecStart=\nExecStart=/opt/ivd/sync\n",
    )
    _write(
        scope / "nightly.service.d" / "20-reset.conf",
        "[Service]\nExecStart=\nExecStart=/usr/bin/backup\n",
    )
    target = tmp_path / "target"
    target.mkdir()
    assert _assert_systemd(root, target).allowed


@pytest.mark.parametrize("unsafe", ["symlink", "unreadable", "overflow"])
def test_systemd_dropins_fail_closed_for_unsafe_or_excessive_entries(
    tmp_path, unsafe
):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _base_timer(scope)
    dropin = scope / "nightly.timer.d"
    if unsafe == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        dropin.symlink_to(outside, target_is_directory=True)
    else:
        dropin.mkdir()
        if unsafe == "unreadable":
            dropin.chmod(0)
        else:
            for index in range(3):
                _write(dropin / f"{index:02d}.conf", "[Timer]\nOnCalendar=daily\n")
    target = tmp_path / "target"
    target.mkdir()
    try:
        with pytest.raises(IvdCronServiceDiscoveryError):
            from hermes_cli.ivd_cron_service_contract import (
                assert_embedded_ivd_cron_service_contract,
            )

            assert_embedded_ivd_cron_service_contract(
                kind="systemd",
                service_dir=target,
                target_path=target / "hermes-gateway.service",
                scope_root=root,
                home=Path("/home/test"),
                environ={},
                uid=1000,
                max_entries_per_scope=2,
                systemd_analyze_path=None,
            )
    finally:
        if unsafe == "unreadable":
            dropin.chmod(0o700)


def _launchd_payload(*, periodic: bool = False) -> bytes:
    payload = {
        "Label": "com.example.generic",
        "ProgramArguments": ["/opt/ivd/sync"],
    }
    if periodic:
        payload["StartInterval"] = 60
    return plistlib.dumps(payload)


@pytest.mark.parametrize(
    "kind", ["invalid", "schema", "oversize", "symlink", "unreadable"]
)
def test_enabled_launchd_scan_fails_closed_for_any_bad_plist(tmp_path, kind):
    from hermes_cli.ivd_cron_service_contract import (
        IvdCronServiceDiscoveryError,
        assert_embedded_ivd_cron_service_contract,
    )

    root = tmp_path / "root"
    scope = _mapped(root, "/Library/LaunchDaemons")
    scope.mkdir(parents=True)
    plist = scope / "com.example.generic.plist"
    if kind == "invalid":
        plist.write_bytes(b"not a plist")
    elif kind == "schema":
        plist.write_bytes(
            plistlib.dumps(
                {
                    "Label": "com.example.generic",
                    "ProgramArguments": "/opt/ivd/sync",
                    "StartInterval": 60,
                }
            )
        )
    elif kind == "oversize":
        plist.write_bytes(b"x" * (256 * 1024 + 1))
    elif kind == "symlink":
        outside = tmp_path / "outside.plist"
        outside.write_bytes(_launchd_payload())
        plist.symlink_to(outside)
    else:
        plist.write_bytes(_launchd_payload())
        plist.chmod(0)
    target = tmp_path / "target"
    target.mkdir()
    try:
        with pytest.raises(IvdCronServiceDiscoveryError):
            assert_embedded_ivd_cron_service_contract(
                kind="launchd",
                service_dir=target,
                target_path=target / "com.nous.hermes.gateway.plist",
                scope_root=root,
                home=Path("/Users/test"),
                environ={},
                uid=501,
            )
    finally:
        if kind == "unreadable":
            plist.chmod(0o600)


def test_launchd_injected_privileged_reader_handles_root_0600_simulation(tmp_path):
    from hermes_cli.ivd_cron_service_contract import (
        IndependentIvdCronServiceError,
        assert_embedded_ivd_cron_service_contract,
    )

    root = tmp_path / "root"
    scope = _mapped(root, "/Library/LaunchDaemons")
    scope.mkdir(parents=True)
    plist = scope / "com.example.generic.plist"
    plist.write_bytes(_launchd_payload(periodic=True))
    plist.chmod(0)
    target = tmp_path / "target"
    target.mkdir()
    try:
        with pytest.raises(IndependentIvdCronServiceError):
            assert_embedded_ivd_cron_service_contract(
                kind="launchd",
                service_dir=target,
                target_path=target / "com.nous.hermes.gateway.plist",
                scope_root=root,
                home=Path("/Users/test"),
                environ={},
                uid=501,
                definition_reader=lambda path: _launchd_payload(periodic=True),
            )
    finally:
        plist.chmod(0o600)
