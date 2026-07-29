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
"""


def _plist(label: str, arguments: list[str]) -> str:
    return plistlib.dumps(
        {"Label": label, "ProgramArguments": arguments, "RunAtLoad": True},
        fmt=plistlib.FMT_XML,
    ).decode("utf-8")


SAFE_LAUNCHD = _plist("com.nous.hermes.gateway", ["/usr/bin/hermes", "gateway", "run"])
INDEPENDENT_LAUNCHD = _plist(
    "com.silechank.ivd.daily-maintenance",
    ["/usr/bin/python3", "/opt/ivd/scripts/hermes_daily_maintenance_runner.py"],
)


def _systemd_mocks(monkeypatch, target: Path, generated: str = SAFE_SYSTEMD):
    monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: target)
    monkeypatch.setattr(gateway_cli, "has_legacy_hermes_units", lambda: False)
    monkeypatch.setattr(gateway_cli, "generate_systemd_unit", lambda **kwargs: generated)
    monkeypatch.setattr(gateway_cli, "_ensure_linger_enabled", lambda: None)
    monkeypatch.setattr(gateway_cli, "print_systemd_scope_conflict_warning", lambda: None)
    monkeypatch.setattr(gateway_cli, "print_legacy_unit_warning", lambda: None)
    monkeypatch.setattr(gateway_cli, "_run_systemctl", lambda *args, **kwargs: None)


def _launchd_mocks(monkeypatch, target: Path, generated: str = SAFE_LAUNCHD):
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: target)
    monkeypatch.setattr(gateway_cli, "generate_launchd_plist", lambda: generated)
    monkeypatch.setattr(gateway_cli, "_launchctl_bootstrap", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_cli, "_clear_launchd_unsupported_marker", lambda: None)


def test_systemd_install_blocks_existing_independent_ivd_cron(monkeypatch, tmp_path):
    from gateway.active_host_fence import IndependentIvdCronServiceError

    target = tmp_path / "systemd" / "hermes-gateway.service"
    target.parent.mkdir()
    (target.parent / "ivd-daily-cron.service").write_text(INDEPENDENT_SYSTEMD, encoding="utf-8")
    _systemd_mocks(monkeypatch, target)
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        gateway_cli.systemd_install(force=True)
    assert not target.exists()


def test_systemd_refresh_blocks_existing_independent_ivd_cron(monkeypatch, tmp_path):
    from gateway.active_host_fence import IndependentIvdCronServiceError

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
    from gateway.active_host_fence import IndependentIvdCronServiceError

    target = tmp_path / "systemd" / "hermes-gateway.service"
    target.parent.mkdir()
    _systemd_mocks(monkeypatch, target, generated=INDEPENDENT_SYSTEMD)
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        gateway_cli.systemd_install(force=True)
    assert not target.exists()


def test_launchd_install_blocks_existing_independent_ivd_cron(monkeypatch, tmp_path):
    from gateway.active_host_fence import IndependentIvdCronServiceError

    target = tmp_path / "LaunchAgents" / "com.nous.hermes.gateway.plist"
    target.parent.mkdir()
    (target.parent / "com.silechank.ivd.cron.plist").write_text(INDEPENDENT_LAUNCHD, encoding="utf-8")
    _launchd_mocks(monkeypatch, target)
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        gateway_cli.launchd_install(force=True)
    assert not target.exists()


def test_launchd_refresh_blocks_existing_independent_ivd_cron(monkeypatch, tmp_path):
    from gateway.active_host_fence import IndependentIvdCronServiceError

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
    from gateway.active_host_fence import IndependentIvdCronServiceError

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


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("systemd", "ivd-daily-cron.service"),
        ("launchd", "com.silechank.ivd.maintenance.plist"),
    ],
)
def test_contract_blocks_suspicious_service_symlink_without_following(kind, name, tmp_path):
    from gateway.active_host_fence import (
        IndependentIvdCronServiceError,
        assert_embedded_ivd_cron_service_contract,
    )

    service_dir = tmp_path / "services"
    service_dir.mkdir()
    outside = tmp_path / "outside-definition"
    outside.write_text("untrusted", encoding="utf-8")
    (service_dir / name).symlink_to(outside)
    target_name = "hermes-gateway.service" if kind == "systemd" else "com.nous.hermes.gateway.plist"
    with pytest.raises(IndependentIvdCronServiceError, match="independent_ivd_cron_forbidden"):
        assert_embedded_ivd_cron_service_contract(
            kind=kind,
            service_dir=service_dir,
            target_path=service_dir / target_name,
        )


def test_launchd_contract_parses_binary_plist_with_program_only(tmp_path):
    from gateway.active_host_fence import (
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
        )
