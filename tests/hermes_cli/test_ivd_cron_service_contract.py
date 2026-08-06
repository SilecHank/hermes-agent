"""Installer integration tests for the embedded-only IVD cron contract."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from hermes_cli import gateway as gateway_cli


SAFE_SYSTEMD = """[Unit]
Description=Hermes Gateway
[Service]
ExecStart=/usr/bin/hermes gateway run
"""
INDEPENDENT_SYSTEMD = """[Unit]
Description=IVD daily cron
[Service]
ExecStart=/usr/bin/python3 /opt/ivd/scripts/hermes_daily_maintenance_runner.py
[Timer]
OnUnitActiveSec=1h
"""


def _plist(label: str, arguments: list[str], **extra: object) -> str:
    payload: dict[str, object] = {
        "Label": label,
        "ProgramArguments": arguments,
        "RunAtLoad": True,
    }
    payload.update(extra)
    return plistlib.dumps(
        payload,
        fmt=plistlib.FMT_XML,
    ).decode("utf-8")


SAFE_LAUNCHD = _plist("com.nous.hermes.gateway", ["/usr/bin/hermes", "gateway", "run"])
INDEPENDENT_LAUNCHD = _plist(
    "com.example.ivd.sync",
    ["/usr/bin/python3", "/opt/ivd/scripts/hermes_daily_maintenance_runner.py"],
    StartInterval=3600,
)


def _systemd_mocks(
    monkeypatch,
    target: Path,
    generated: str = SAFE_SYSTEMD,
    *,
    contract_required: bool = True,
):
    scope_root = target.parent.parent / "scope-root"
    scope_root.mkdir(exist_ok=True)
    if contract_required:
        monkeypatch.setenv("IVD_CRON_SERVICE_CONTRACT_REQUIRED", "true")
    else:
        monkeypatch.delenv("IVD_CRON_SERVICE_CONTRACT_REQUIRED", raising=False)
        monkeypatch.delenv("IVD_ACTIVE_HOST_FENCE_REQUIRED", raising=False)
    monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: target)
    monkeypatch.setattr(gateway_cli, "has_legacy_hermes_units", lambda: False)
    monkeypatch.setattr(gateway_cli, "generate_systemd_unit", lambda **kwargs: generated)
    monkeypatch.setattr(gateway_cli, "_ensure_linger_enabled", lambda: None)
    monkeypatch.setattr(gateway_cli, "print_systemd_scope_conflict_warning", lambda: None)
    monkeypatch.setattr(gateway_cli, "print_legacy_unit_warning", lambda: None)
    monkeypatch.setattr(gateway_cli, "_run_systemctl", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_cli, "_ivd_service_scope_root", lambda: scope_root, raising=False)
    monkeypatch.setattr(gateway_cli, "_ivd_service_contract_home", lambda: Path("/home/test"), raising=False)
    monkeypatch.setattr(gateway_cli, "_ivd_service_contract_environ", lambda: {}, raising=False)
    monkeypatch.setattr(gateway_cli, "_ivd_service_contract_uid", lambda: 1000, raising=False)


def _launchd_mocks(
    monkeypatch,
    target: Path,
    generated: str = SAFE_LAUNCHD,
    *,
    contract_required: bool = True,
):
    scope_root = target.parent.parent / "scope-root"
    scope_root.mkdir(exist_ok=True)
    if contract_required:
        monkeypatch.setenv("IVD_CRON_SERVICE_CONTRACT_REQUIRED", "true")
    else:
        monkeypatch.delenv("IVD_CRON_SERVICE_CONTRACT_REQUIRED", raising=False)
        monkeypatch.delenv("IVD_ACTIVE_HOST_FENCE_REQUIRED", raising=False)
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: target)
    monkeypatch.setattr(gateway_cli, "generate_launchd_plist", lambda: generated)
    monkeypatch.setattr(gateway_cli, "_launchctl_bootstrap", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_cli, "_clear_launchd_unsupported_marker", lambda: None)
    monkeypatch.setattr(gateway_cli, "_ivd_service_scope_root", lambda: scope_root, raising=False)
    monkeypatch.setattr(gateway_cli, "_ivd_service_contract_home", lambda: Path("/Users/test"), raising=False)
    monkeypatch.setattr(gateway_cli, "_ivd_service_contract_environ", lambda: {}, raising=False)
    monkeypatch.setattr(gateway_cli, "_ivd_service_contract_uid", lambda: 501, raising=False)


def test_systemd_install_blocks_existing_independent_ivd_cron(monkeypatch, tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    target = tmp_path / "systemd" / "hermes-gateway.service"
    target.parent.mkdir()
    (target.parent / "ivd-sync.timer").write_text(
        "[Unit]\nDescription=IVD sync timer\n[Timer]\nOnCalendar=hourly\nUnit=ivd-sync.service\n",
        encoding="utf-8",
    )
    _systemd_mocks(monkeypatch, target)
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        gateway_cli.systemd_install(force=True)
    assert not target.exists()


def test_default_systemd_install_skips_opt_in_contract(monkeypatch, tmp_path):
    import hermes_cli.ivd_cron_service_contract as contract

    target = tmp_path / "systemd" / "hermes-gateway.service"
    target.parent.mkdir()
    _systemd_mocks(monkeypatch, target, contract_required=False)
    monkeypatch.setattr(
        contract,
        "find_trusted_systemd_analyze",
        lambda: pytest.fail("disabled install resolved systemd-analyze"),
    )
    gateway_cli.systemd_install(force=True)
    assert target.read_text(encoding="utf-8") == SAFE_SYSTEMD


def test_default_launchd_install_skips_bad_plist_scan(monkeypatch, tmp_path):
    target = tmp_path / "LaunchAgents" / "com.nous.hermes.gateway.plist"
    target.parent.mkdir()
    (target.parent / "com.example.generic.plist").write_text(
        "not a plist", encoding="utf-8"
    )
    _launchd_mocks(monkeypatch, target, contract_required=False)
    gateway_cli.launchd_install(force=True)
    assert target.read_text(encoding="utf-8") == SAFE_LAUNCHD


def test_systemd_refresh_blocks_existing_independent_ivd_cron(monkeypatch, tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    target = tmp_path / "systemd" / "hermes-gateway.service"
    target.parent.mkdir()
    target.write_text("old gateway", encoding="utf-8")
    (target.parent / "ivd-maintenance.timer").write_text(
        "[Unit]\nDescription=IVD cron\n[Timer]\nOnCalendar=daily\n",
        encoding="utf-8",
    )
    _systemd_mocks(monkeypatch, target)
    monkeypatch.setattr(gateway_cli, "systemd_unit_is_current", lambda system=False: False)
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        gateway_cli.refresh_systemd_unit_if_needed()
    assert target.read_text(encoding="utf-8") == "old gateway"


def test_systemd_install_blocks_proposed_independent_ivd_cron(monkeypatch, tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    target = tmp_path / "systemd" / "hermes-gateway.service"
    target.parent.mkdir()
    _systemd_mocks(monkeypatch, target, generated=INDEPENDENT_SYSTEMD)
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        gateway_cli.systemd_install(force=True)
    assert not target.exists()


def test_launchd_install_blocks_existing_independent_ivd_cron(monkeypatch, tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    target = tmp_path / "LaunchAgents" / "com.nous.hermes.gateway.plist"
    target.parent.mkdir()
    (target.parent / "com.silechank.ivd.cron.plist").write_text(INDEPENDENT_LAUNCHD, encoding="utf-8")
    _launchd_mocks(monkeypatch, target)
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        gateway_cli.launchd_install(force=True)
    assert not target.exists()


def test_launchd_refresh_blocks_existing_independent_ivd_cron(monkeypatch, tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    target = tmp_path / "LaunchAgents" / "com.nous.hermes.gateway.plist"
    target.parent.mkdir()
    target.write_text(SAFE_LAUNCHD, encoding="utf-8")
    (target.parent / "com.silechank.ivd.maintenance.plist").write_text(INDEPENDENT_LAUNCHD, encoding="utf-8")
    _launchd_mocks(monkeypatch, target)
    monkeypatch.setattr(gateway_cli, "launchd_plist_is_current", lambda: False)
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        gateway_cli.refresh_launchd_plist_if_needed()
    assert target.read_text(encoding="utf-8") == SAFE_LAUNCHD


def test_launchd_install_blocks_proposed_independent_ivd_cron(monkeypatch, tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    target = tmp_path / "LaunchAgents" / "com.nous.hermes.gateway.plist"
    target.parent.mkdir()
    _launchd_mocks(monkeypatch, target, generated=INDEPENDENT_LAUNCHD)
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        gateway_cli.launchd_install(force=True)
    assert not target.exists()


def test_installers_allow_embedded_gateway_and_unrelated_system_cron(monkeypatch, tmp_path):
    systemd_target = tmp_path / "systemd" / "hermes-gateway.service"
    systemd_target.parent.mkdir()
    (systemd_target.parent / "backup.timer").write_text(
        "[Unit]\nDescription=Nightly backup\n[Timer]\nOnCalendar=daily\n",
        encoding="utf-8",
    )
    _systemd_mocks(monkeypatch, systemd_target)
    gateway_cli.systemd_install(force=True)
    assert systemd_target.read_text(encoding="utf-8") == SAFE_SYSTEMD

    launchd_target = tmp_path / "LaunchAgents" / "com.nous.hermes.gateway.plist"
    launchd_target.parent.mkdir()
    (launchd_target.parent / "com.example.backup.plist").write_text(
        _plist("com.example.backup", ["/usr/bin/backup", "--daily"]),
        encoding="utf-8",
    )
    _launchd_mocks(monkeypatch, launchd_target)
    gateway_cli.launchd_install(force=True)
    assert launchd_target.read_text(encoding="utf-8") == SAFE_LAUNCHD


def test_launchd_install_allows_governed_telegram_release_follower(monkeypatch, tmp_path):
    target = tmp_path / "LaunchAgents" / "com.nous.hermes.gateway.plist"
    target.parent.mkdir()
    telegram_home = "/Users/test/.hermes/profiles/telegram"
    telegram_runtime = f"{telegram_home}/telegram-runtime"
    follower = {
        "Label": "ai.hermes.telegram-release-sync",
        "ProgramArguments": [
            "/Users/test/.hermes/hermes-agent-ivd/venv/bin/python",
            "-B",
            f"{telegram_runtime}/current/knowledgehub/scripts/hermes-telegram-release-sync",
            "--runtime-root",
            telegram_runtime,
            "--ivd-remote",
            "/Users/test/.local/bin/ivd-remote",
            "--ivd-wsl",
            "/Users/test/.local/bin/ivd-wsl",
        ],
        "EnvironmentVariables": {"HERMES_HOME": telegram_home},
        "StartInterval": 900,
    }
    (target.parent / "ai.hermes.telegram-release-sync.plist").write_bytes(
        plistlib.dumps(follower)
    )
    _launchd_mocks(monkeypatch, target)

    gateway_cli.launchd_install(force=True)

    assert target.read_text(encoding="utf-8") == SAFE_LAUNCHD


def test_launchd_install_blocks_spoofed_telegram_release_follower(monkeypatch, tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    target = tmp_path / "LaunchAgents" / "com.nous.hermes.gateway.plist"
    target.parent.mkdir()
    spoofed = _plist(
        "ai.hermes.telegram-release-sync",
        ["/usr/bin/python3", "/opt/ivd/scripts/hermes_daily_maintenance_runner.py"],
        StartInterval=900,
    )
    (target.parent / "ai.hermes.telegram-release-sync.plist").write_text(
        spoofed, encoding="utf-8"
    )
    _launchd_mocks(monkeypatch, target)

    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        gateway_cli.launchd_install(force=True)


def test_manual_ivd_services_without_periodic_configuration_are_allowed(tmp_path):
    from hermes_cli.ivd_cron_service_contract import assert_embedded_ivd_cron_service_contract

    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    (systemd_dir / "ivd-maintenance.service").write_text(
        "[Service]\nExecStart=/opt/ivd/manual-maintenance\n",
        encoding="utf-8",
    )
    assert assert_embedded_ivd_cron_service_contract(
        kind="systemd",
        service_dir=systemd_dir,
        target_path=systemd_dir / "hermes-gateway.service",
        scope_root=tmp_path / "empty-root",
    ).allowed

    launchd_dir = tmp_path / "LaunchAgents"
    launchd_dir.mkdir()
    (launchd_dir / "com.example.ivd.sync.plist").write_text(
        plistlib.dumps(
            {
                "Label": "com.example.ivd.sync",
                "ProgramArguments": ["/opt/ivd/manual-sync"],
                "KeepAlive": True,
            },
            fmt=plistlib.FMT_XML,
        ).decode("utf-8"),
        encoding="utf-8",
    )
    assert assert_embedded_ivd_cron_service_contract(
        kind="launchd",
        service_dir=launchd_dir,
        target_path=launchd_dir / "com.nous.hermes.gateway.plist",
        scope_root=tmp_path / "empty-root",
    ).allowed


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("systemd", "ivd-sync.timer"),
        ("launchd", "com.silechank.ivd.maintenance.plist"),
    ],
)
def test_contract_blocks_suspicious_service_symlink_without_following(kind, name, tmp_path):
    from hermes_cli.ivd_cron_service_contract import (
        IndependentIvdCronServiceError,
        IvdCronServiceDiscoveryError,
        assert_embedded_ivd_cron_service_contract,
    )

    service_dir = tmp_path / "services"
    service_dir.mkdir()
    outside = tmp_path / "outside-definition"
    outside.write_text("untrusted", encoding="utf-8")
    (service_dir / name).symlink_to(outside)
    target_name = "hermes-gateway.service" if kind == "systemd" else "com.nous.hermes.gateway.plist"
    expected_error = (
        IvdCronServiceDiscoveryError
        if kind == "launchd"
        else IndependentIvdCronServiceError
    )
    expected_reason = (
        "launchd_plist_unreadable"
        if kind == "launchd"
        else "independent_ivd_cron_forbidden"
    )
    with pytest.raises(expected_error, match=expected_reason):
        assert_embedded_ivd_cron_service_contract(
            kind=kind,
            service_dir=service_dir,
            target_path=service_dir / target_name,
            scope_root=tmp_path / "empty-root",
        )


def test_launchd_contract_parses_binary_plist_with_program_only(tmp_path):
    from hermes_cli.ivd_cron_service_contract import (
        IndependentIvdCronServiceError,
        assert_embedded_ivd_cron_service_contract,
    )

    service_dir = tmp_path / "LaunchAgents"
    service_dir.mkdir()
    (service_dir / "com.example.background-job.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": "com.example.background-job",
                "Program": "/opt/ivd/scripts/hermes_daily_maintenance_runner.py",
                "StartInterval": 3600,
            },
            fmt=plistlib.FMT_BINARY,
        )
    )
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        assert_embedded_ivd_cron_service_contract(
            kind="launchd",
            service_dir=service_dir,
            target_path=service_dir / "com.nous.hermes.gateway.plist",
            scope_root=tmp_path / "empty-root",
        )


def test_launchd_contract_rejects_malformed_candidate(tmp_path):
    from hermes_cli.ivd_cron_service_contract import (
        IvdCronServiceDiscoveryError,
        assert_embedded_ivd_cron_service_contract,
    )

    service_dir = tmp_path / "LaunchAgents"
    service_dir.mkdir()
    with pytest.raises(IvdCronServiceDiscoveryError, match="launchd_plist_invalid"):
        assert_embedded_ivd_cron_service_contract(
            kind="launchd",
            service_dir=service_dir,
            target_path=service_dir / "com.nous.hermes.gateway.plist",
            candidate_definition=(
                "<plist>--replace\n<key>HERMES_HOME</key>"
                "<string>/Users/alice/.hermes</string></plist>"
            ),
            scope_root=tmp_path / "empty-root",
        )


def _mapped(root: Path, absolute: str) -> Path:
    return root / absolute.lstrip("/")


def test_systemd_discovers_ivd_timer_outside_target_parent(tmp_path):
    from hermes_cli.ivd_cron_service_contract import (
        IndependentIvdCronServiceError,
        assert_embedded_ivd_cron_service_contract,
    )

    root = tmp_path / "root"
    scope = _mapped(root, "/usr/lib/systemd/user")
    scope.mkdir(parents=True)
    (scope / "ivd-sync.timer").write_text(
        "[Timer]\nOnUnitActiveSec=15m\nUnit=ivd-sync.service\n",
        encoding="utf-8",
    )
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        assert_embedded_ivd_cron_service_contract(
            kind="systemd",
            service_dir=target_dir,
            target_path=target_dir / "hermes-gateway.service",
            scope_root=root,
            home=Path("/home/test"),
            environ={},
            uid=1000,
        )


def _assert_mapped_systemd_contract(
    root: Path,
    target_dir: Path,
    *,
    environ: dict[str, str] | None = None,
):
    from hermes_cli.ivd_cron_service_contract import assert_embedded_ivd_cron_service_contract

    return assert_embedded_ivd_cron_service_contract(
        kind="systemd",
        service_dir=target_dir,
        target_path=target_dir / "hermes-gateway.service",
        scope_root=root,
        home=Path("/home/test"),
        environ={} if environ is None else environ,
        uid=1000,
    )


def test_systemd_timer_links_explicit_unit_to_ivd_service(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    scope.mkdir(parents=True)
    (scope / "nightly.timer").write_text(
        "[Timer]\nOnCalendar=daily\nUnit=after-sales-sync.service\n",
        encoding="utf-8",
    )
    (scope / "after-sales-sync.service").write_text(
        "[Service]\nExecStart=/opt/after-sales/sync\n",
        encoding="utf-8",
    )
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        _assert_mapped_systemd_contract(root, target_dir)


def test_systemd_timer_links_default_basename_to_ivd_service(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    scope.mkdir(parents=True)
    (scope / "nightly.timer").write_text(
        "[Timer]\nOnUnitActiveSec=1h\n",
        encoding="utf-8",
    )
    (scope / "nightly.service").write_text(
        "[Service]\nExecStart=/opt/ivd/sync\n",
        encoding="utf-8",
    )
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        _assert_mapped_systemd_contract(root, target_dir)


def test_systemd_timer_links_service_across_user_scopes(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    timer_scope = _mapped(root, "/etc/systemd/user")
    service_scope = _mapped(root, "/usr/lib/systemd/user")
    timer_scope.mkdir(parents=True)
    service_scope.mkdir(parents=True)
    (timer_scope / "nightly.timer").write_text(
        "[Timer]\nOnCalendar=hourly\nUnit=worker.service\n",
        encoding="utf-8",
    )
    (service_scope / "worker.service").write_text(
        "[Service]\nExecStart=/opt/ivd/worker\n",
        encoding="utf-8",
    )
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        _assert_mapped_systemd_contract(root, target_dir)


def test_systemd_service_override_uses_highest_priority_same_name(tmp_path):
    root = tmp_path / "root"
    high_scope = _mapped(root, "/etc/systemd/user")
    low_scope = _mapped(root, "/usr/lib/systemd/user")
    high_scope.mkdir(parents=True)
    low_scope.mkdir(parents=True)
    (high_scope / "nightly.timer").write_text(
        "[Timer]\nOnCalendar=daily\n",
        encoding="utf-8",
    )
    (high_scope / "nightly.service").write_text(
        "[Service]\nExecStart=/usr/bin/backup\n",
        encoding="utf-8",
    )
    (low_scope / "nightly.service").write_text(
        "[Service]\nExecStart=/opt/ivd/sync\n",
        encoding="utf-8",
    )
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    assert _assert_mapped_systemd_contract(root, target_dir).allowed


def test_systemd_timer_override_uses_highest_priority_same_name(tmp_path):
    root = tmp_path / "root"
    high_scope = _mapped(root, "/etc/systemd/user")
    low_scope = _mapped(root, "/usr/lib/systemd/user")
    high_scope.mkdir(parents=True)
    low_scope.mkdir(parents=True)
    (high_scope / "nightly.timer").write_text(
        "[Timer]\nOnCalendar=daily\nUnit=backup.service\n",
        encoding="utf-8",
    )
    (high_scope / "backup.service").write_text(
        "[Service]\nExecStart=/usr/bin/backup\n",
        encoding="utf-8",
    )
    (low_scope / "nightly.timer").write_text(
        "[Unit]\nDescription=IVD nightly sync\n[Timer]\nOnCalendar=daily\n",
        encoding="utf-8",
    )
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    assert _assert_mapped_systemd_contract(root, target_dir).allowed


def test_systemd_timer_does_not_cross_user_and_system_domains(tmp_path):
    root = tmp_path / "root"
    user_scope = _mapped(root, "/etc/systemd/user")
    system_scope = _mapped(root, "/etc/systemd/system")
    user_scope.mkdir(parents=True)
    system_scope.mkdir(parents=True)
    (user_scope / "nightly.timer").write_text(
        "[Timer]\nOnCalendar=daily\n",
        encoding="utf-8",
    )
    (system_scope / "nightly.service").write_text(
        "[Service]\nExecStart=/opt/ivd/sync\n",
        encoding="utf-8",
    )
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    assert _assert_mapped_systemd_contract(root, target_dir).allowed


def test_systemd_timer_allows_missing_or_non_ivd_linked_service(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    scope.mkdir(parents=True)
    (scope / "missing.timer").write_text(
        "[Timer]\nOnCalendar=daily\nUnit=ivd-missing.service\n",
        encoding="utf-8",
    )
    (scope / "backup.timer").write_text(
        "[Timer]\nOnCalendar=daily\n",
        encoding="utf-8",
    )
    (scope / "backup.service").write_text(
        "[Service]\nExecStart=/usr/bin/backup\n",
        encoding="utf-8",
    )
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    assert _assert_mapped_systemd_contract(root, target_dir).allowed


def test_systemd_generic_timer_does_not_pair_unrelated_ivd_service(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    scope.mkdir(parents=True)
    (scope / "backup.timer").write_text(
        "[Timer]\nOnCalendar=daily\n",
        encoding="utf-8",
    )
    (scope / "ivd-sync.service").write_text(
        "[Service]\nExecStart=/opt/ivd/sync\n",
        encoding="utf-8",
    )
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    assert _assert_mapped_systemd_contract(root, target_dir).allowed


def test_systemd_timer_allows_linked_hermes_gateway_service(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    scope.mkdir(parents=True)
    (scope / "heartbeat.timer").write_text(
        "[Timer]\nOnUnitActiveSec=5m\nUnit=hermes-gateway.service\n",
        encoding="utf-8",
    )
    (scope / "hermes-gateway.service").write_text(
        "[Unit]\nDescription=IVD Hermes Gateway\n"
        "[Service]\nExecStart=/usr/bin/hermes gateway run\n",
        encoding="utf-8",
    )
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    assert _assert_mapped_systemd_contract(root, target_dir).allowed


def test_systemd_timer_rejects_unit_path_escape(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    scope.mkdir(parents=True)
    (scope / "nightly.timer").write_text(
        "[Timer]\nOnCalendar=daily\nUnit=../../outside/ivd-sync.service\n",
        encoding="utf-8",
    )
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    with pytest.raises(IvdCronServiceDiscoveryError, match="systemd_timer_unit_invalid"):
        _assert_mapped_systemd_contract(root, target_dir)


def test_systemd_timer_resolves_safe_service_alias(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    scope.mkdir(parents=True)
    (scope / "nightly.timer").write_text(
        "[Timer]\nOnCalendar=daily\n",
        encoding="utf-8",
    )
    (scope / "ivd-worker.service").write_text(
        "[Service]\nExecStart=/opt/ivd/worker\n",
        encoding="utf-8",
    )
    (scope / "nightly.service").symlink_to("ivd-worker.service")
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        _assert_mapped_systemd_contract(root, target_dir)


def test_systemd_timer_resolves_mapped_absolute_cross_scope_alias(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    high_scope = _mapped(root, "/etc/systemd/user")
    low_scope = _mapped(root, "/usr/lib/systemd/user")
    high_scope.mkdir(parents=True)
    low_scope.mkdir(parents=True)
    (high_scope / "nightly.timer").write_text(
        "[Timer]\nOnCalendar=daily\n",
        encoding="utf-8",
    )
    (low_scope / "ivd-worker.service").write_text(
        "[Service]\nExecStart=/opt/ivd/worker\n",
        encoding="utf-8",
    )
    (high_scope / "nightly.service").symlink_to(
        "/usr/lib/systemd/user/ivd-worker.service"
    )
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        _assert_mapped_systemd_contract(root, target_dir)


def test_systemd_timer_rejects_service_alias_escape(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    scope.mkdir(parents=True)
    outside = tmp_path / "outside.service"
    outside.write_text("[Service]\nExecStart=/opt/ivd/sync\n", encoding="utf-8")
    (scope / "nightly.timer").write_text(
        "[Timer]\nOnCalendar=daily\n",
        encoding="utf-8",
    )
    (scope / "nightly.service").symlink_to(outside)
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    with pytest.raises(IvdCronServiceDiscoveryError, match="systemd_unit_alias_escape"):
        _assert_mapped_systemd_contract(root, target_dir)


def test_systemd_timer_fails_closed_for_unreadable_linked_service(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    scope.mkdir(parents=True)
    (scope / "nightly.timer").write_text(
        "[Timer]\nOnCalendar=daily\n",
        encoding="utf-8",
    )
    service = scope / "nightly.service"
    service.write_text("[Service]\nExecStart=/opt/ivd/sync\n", encoding="utf-8")
    service.chmod(0)
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    try:
        with pytest.raises(IvdCronServiceDiscoveryError, match="service_definition_unreadable"):
            _assert_mapped_systemd_contract(root, target_dir)
    finally:
        service.chmod(0o600)


def test_launchd_discovers_periodic_ivd_job_outside_target_parent(tmp_path):
    from hermes_cli.ivd_cron_service_contract import (
        IndependentIvdCronServiceError,
        assert_embedded_ivd_cron_service_contract,
    )

    root = tmp_path / "root"
    scope = _mapped(root, "/Library/LaunchDaemons")
    scope.mkdir(parents=True)
    (scope / "com.example.ivd.sync.plist").write_text(
        _plist("com.example.ivd.sync", ["/opt/bin/sync"]),
        encoding="utf-8",
    )
    payload = plistlib.loads((scope / "com.example.ivd.sync.plist").read_bytes())
    payload["StartCalendarInterval"] = {"Minute": 15}
    (scope / "com.example.ivd.sync.plist").write_bytes(plistlib.dumps(payload))
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        assert_embedded_ivd_cron_service_contract(
            kind="launchd",
            service_dir=target_dir,
            target_path=target_dir / "com.nous.hermes.gateway.plist",
            scope_root=root,
            home=Path("/Users/test"),
            environ={},
            uid=501,
        )


def test_scope_discovery_is_complete_mapped_and_deduplicated(tmp_path):
    from hermes_cli.ivd_cron_service_contract import discover_ivd_cron_service_scopes

    root = tmp_path / "root"
    scopes = discover_ivd_cron_service_scopes(
        "systemd",
        target_path=_mapped(root, "/home/test/.config/systemd/user/hermes-gateway.service"),
        scope_root=root,
        home=Path("/home/test"),
        environ={
            "XDG_CONFIG_HOME": "/home/test/.config",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        },
        uid=1000,
    )
    expected = {
        _mapped(root, path)
        for path in (
            "/home/test/.config/systemd/user.control",
            "/run/user/1000/systemd/user.control",
            "/run/user/1000/systemd/transient",
            "/run/user/1000/systemd/generator.early",
            "/home/test/.config/systemd/user",
            "/etc/xdg/systemd/user",
            "/etc/systemd/user",
            "/run/user/1000/systemd/user",
            "/run/systemd/user",
            "/run/user/1000/systemd/generator",
            "/home/test/.local/share/systemd/user",
            "/usr/local/share/systemd/user",
            "/usr/share/systemd/user",
            "/usr/local/lib/systemd/user",
            "/usr/lib/systemd/user",
            "/run/user/1000/systemd/generator.late",
            "/etc/systemd/system.control",
            "/run/systemd/system.control",
            "/run/systemd/transient",
            "/run/systemd/generator.early",
            "/etc/systemd/system",
            "/etc/systemd/system.attached",
            "/run/systemd/system",
            "/run/systemd/system.attached",
            "/run/systemd/generator",
            "/usr/local/lib/systemd/system",
            "/usr/lib/systemd/system",
            "/run/systemd/generator.late",
        )
    }
    assert set(scopes) == expected
    assert len(scopes) == len(set(scopes))

    launchd_scopes = discover_ivd_cron_service_scopes(
        "launchd",
        target_path=_mapped(root, "/Users/test/Library/LaunchAgents/com.nous.hermes.gateway.plist"),
        scope_root=root,
        home=Path("/Users/test"),
        environ={},
        uid=501,
    )
    assert set(launchd_scopes) == {
        _mapped(root, path)
        for path in (
            "/Users/test/Library/LaunchAgents",
            "/Library/LaunchAgents",
            "/Library/LaunchDaemons",
            "/System/Library/LaunchAgents",
            "/System/Library/LaunchDaemons",
        )
    }


def test_systemd_scans_reviewer_xdg_config_dirs_counterexample(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/opt/custom/systemd/user")
    scope.mkdir(parents=True)
    (scope / "nightly.timer").write_text(
        "[Timer]\nOnCalendar=daily\n",
        encoding="utf-8",
    )
    (scope / "nightly.service").write_text(
        "[Service]\nExecStart=/opt/ivd/sync\n",
        encoding="utf-8",
    )
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        _assert_mapped_systemd_contract(
            root,
            target_dir,
            environ={"XDG_CONFIG_DIRS": "/opt/custom/systemd/user"},
        )


def test_systemd_discovers_xdg_data_home_and_dirs(tmp_path):
    from hermes_cli.ivd_cron_service_contract import discover_ivd_cron_service_scopes

    root = tmp_path / "root"
    scopes = discover_ivd_cron_service_scopes(
        "systemd",
        target_path=_mapped(root, "/home/test/.config/systemd/user/hermes-gateway.service"),
        scope_root=root,
        home=Path("/home/test"),
        environ={
            "XDG_DATA_HOME": "/srv/user-data",
            "XDG_DATA_DIRS": "/opt/data-a:/opt/data-b",
        },
        uid=1000,
    )
    assert _mapped(root, "/srv/user-data/systemd/user") in scopes
    assert _mapped(root, "/opt/data-a/systemd/user") in scopes
    assert _mapped(root, "/opt/data-b/systemd/user") in scopes


def test_systemd_discovers_runtime_transient_and_generators(tmp_path):
    from hermes_cli.ivd_cron_service_contract import discover_ivd_cron_service_scopes

    root = tmp_path / "root"
    scopes = discover_ivd_cron_service_scopes(
        "systemd",
        target_path=_mapped(root, "/home/test/.config/systemd/user/hermes-gateway.service"),
        scope_root=root,
        home=Path("/home/test"),
        environ={"XDG_RUNTIME_DIR": "/run/user/1000"},
        uid=1000,
    )
    for path in (
        "/run/user/1000/systemd/user",
        "/run/user/1000/systemd/transient",
        "/run/user/1000/systemd/generator.early",
        "/run/user/1000/systemd/generator",
        "/run/user/1000/systemd/generator.late",
    ):
        assert _mapped(root, path) in scopes


def test_systemd_discovers_system_control_attached_and_generators(tmp_path):
    from hermes_cli.ivd_cron_service_contract import discover_ivd_cron_service_scopes

    root = tmp_path / "root"
    scopes = discover_ivd_cron_service_scopes(
        "systemd",
        target_path=_mapped(root, "/home/test/.config/systemd/user/hermes-gateway.service"),
        scope_root=root,
        home=Path("/home/test"),
        environ={},
        uid=1000,
    )
    for path in (
        "/etc/systemd/system.control",
        "/run/systemd/system.control",
        "/run/systemd/transient",
        "/run/systemd/generator.early",
        "/etc/systemd/system.attached",
        "/run/systemd/system.attached",
        "/run/systemd/generator",
        "/run/systemd/generator.late",
    ):
        assert _mapped(root, path) in scopes


def test_systemd_unit_path_overrides_and_trailing_empty_appends_defaults(tmp_path):
    from hermes_cli.ivd_cron_service_contract import discover_ivd_cron_service_scopes

    root = tmp_path / "root"
    target = _mapped(root, "/home/test/.config/systemd/user/hermes-gateway.service")
    scopes = discover_ivd_cron_service_scopes(
        "systemd",
        target_path=target,
        scope_root=root,
        home=Path("/home/test"),
        environ={
            "SYSTEMD_USER_UNIT_PATH": "/opt/user-a::/opt/user-b:",
            "SYSTEMD_UNIT_PATH": "/opt/system-only",
        },
        uid=1000,
    )
    assert scopes[0:2] == (
        _mapped(root, "/opt/user-a"),
        _mapped(root, "/opt/user-b"),
    )
    assert _mapped(root, "/home/test/.config/systemd/user") in scopes
    assert _mapped(root, "/opt/system-only") in scopes
    assert _mapped(root, "/etc/systemd/system") not in scopes


def test_systemd_empty_unit_path_override_removes_domain_defaults(tmp_path):
    from hermes_cli.ivd_cron_service_contract import discover_ivd_cron_service_scopes

    root = tmp_path / "root"
    scopes = discover_ivd_cron_service_scopes(
        "systemd",
        target_path=_mapped(root, "/home/test/.config/systemd/user/hermes-gateway.service"),
        scope_root=root,
        home=Path("/home/test"),
        environ={"SYSTEMD_UNIT_PATH": ""},
        uid=1000,
    )
    assert _mapped(root, "/home/test/.config/systemd/user") in scopes
    assert _mapped(root, "/etc/systemd/system") not in scopes
    assert _mapped(root, "/usr/lib/systemd/system") not in scopes


@pytest.mark.parametrize(
    ("environ", "reason"),
    [
        ({"XDG_CONFIG_DIRS": "relative/path"}, "service_scope_env_invalid"),
        ({"SYSTEMD_UNIT_PATH": "/opt/ok:relative/path"}, "service_scope_env_invalid"),
        (
            {"SYSTEMD_USER_UNIT_PATH": ":".join(f"/opt/unit-{index}" for index in range(65))},
            "service_scope_limit",
        ),
    ],
)
def test_systemd_rejects_malicious_or_excessive_dynamic_paths(tmp_path, environ, reason):
    from hermes_cli.ivd_cron_service_contract import (
        IvdCronServiceDiscoveryError,
        discover_ivd_cron_service_scopes,
    )

    root = tmp_path / "root"
    with pytest.raises(IvdCronServiceDiscoveryError, match=reason):
        discover_ivd_cron_service_scopes(
            "systemd",
            target_path=_mapped(root, "/home/test/.config/systemd/user/hermes-gateway.service"),
            scope_root=root,
            home=Path("/home/test"),
            environ=environ,
            uid=1000,
        )


def test_systemd_default_dynamic_paths_stay_mapped_and_bounded(tmp_path):
    from hermes_cli.ivd_cron_service_contract import discover_ivd_cron_service_scopes

    root = tmp_path / "root"
    scopes = discover_ivd_cron_service_scopes(
        "systemd",
        target_path=_mapped(root, "/home/test/.config/systemd/user/hermes-gateway.service"),
        scope_root=root,
        home=Path("/home/test"),
        environ={},
        uid=1000,
    )
    assert len(scopes) <= 64
    assert len(scopes) == len(set(scopes))
    assert all(scope.is_relative_to(root) for scope in scopes)
    for path in (
        "/home/test/.config/systemd/user",
        "/etc/xdg/systemd/user",
        "/home/test/.local/share/systemd/user",
        "/usr/local/share/systemd/user",
        "/usr/share/systemd/user",
        "/etc/systemd/system",
        "/usr/lib/systemd/system",
    ):
        assert _mapped(root, path) in scopes


def test_systemd_allows_declared_scope_alias_without_leaving_mapped_root(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    alias_scope = _mapped(root, "/etc/xdg/systemd/user")
    target_scope = _mapped(root, "/etc/systemd/user")
    alias_scope.parent.mkdir(parents=True)
    target_scope.mkdir(parents=True)
    alias_scope.symlink_to("../../systemd/user", target_is_directory=True)
    (target_scope / "nightly.timer").write_text(
        "[Timer]\nOnCalendar=daily\n",
        encoding="utf-8",
    )
    (target_scope / "nightly.service").write_text(
        "[Service]\nExecStart=/opt/ivd/sync\n",
        encoding="utf-8",
    )
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        _assert_mapped_systemd_contract(root, target_dir)


def test_scope_mapping_normalizes_parent_segments_inside_temporary_root(tmp_path):
    from hermes_cli.ivd_cron_service_contract import discover_ivd_cron_service_scopes

    root = tmp_path / "root"
    scopes = discover_ivd_cron_service_scopes(
        "systemd",
        target_path=tmp_path / "target" / "hermes-gateway.service",
        scope_root=root,
        home=Path("/home/test"),
        environ={"XDG_CONFIG_HOME": "/home/test/../../etc"},
        uid=1000,
    )
    mapped_scopes = [scope for scope in scopes if scope != tmp_path / "target"]
    assert _mapped(root, "/etc/systemd/user") in mapped_scopes
    assert all(".." not in scope.parts for scope in mapped_scopes)


@pytest.mark.parametrize("unsafe_kind", ["symlink", "unreadable"])
def test_scope_scan_fails_closed_for_unsafe_scope(tmp_path, unsafe_kind):
    from hermes_cli.ivd_cron_service_contract import (
        IvdCronServiceDiscoveryError,
        assert_embedded_ivd_cron_service_contract,
    )

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/system")
    scope.parent.mkdir(parents=True)
    if unsafe_kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        scope.symlink_to(outside, target_is_directory=True)
        reason = "service_scope_symlink"
    else:
        scope.mkdir()
        scope.chmod(0)
        reason = "service_scope_unreadable"
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    try:
        with pytest.raises(IvdCronServiceDiscoveryError, match=reason):
            assert_embedded_ivd_cron_service_contract(
                kind="systemd",
                service_dir=target_dir,
                target_path=target_dir / "hermes-gateway.service",
                scope_root=root,
                home=Path("/home/test"),
                environ={},
                uid=1000,
            )
    finally:
        if unsafe_kind == "unreadable":
            scope.chmod(0o700)


def test_scope_scan_has_entry_limit(tmp_path):
    from hermes_cli.ivd_cron_service_contract import (
        IvdCronServiceDiscoveryError,
        assert_embedded_ivd_cron_service_contract,
    )

    target_dir = tmp_path / "target"
    target_dir.mkdir()
    for index in range(3):
        (target_dir / f"unrelated-{index}.service").write_text(SAFE_SYSTEMD, encoding="utf-8")
    with pytest.raises(IvdCronServiceDiscoveryError, match="service_scope_entry_limit"):
        assert_embedded_ivd_cron_service_contract(
            kind="systemd",
            service_dir=target_dir,
            target_path=target_dir / "hermes-gateway.service",
            scope_root=tmp_path / "empty-root",
            max_entries_per_scope=2,
        )
