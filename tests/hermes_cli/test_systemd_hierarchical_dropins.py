"""Regression tests for systemd hierarchical unit drop-ins."""

from __future__ import annotations

from pathlib import Path

import pytest


def _mapped(root: Path, absolute: str) -> Path:
    return root / absolute.lstrip("/")


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _assert_systemd(root: Path, target: Path, *, max_entries: int = 512):
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
        max_entries_per_scope=max_entries,
        systemd_analyze_path=None,
    )


def _timer_and_service(
    scope: Path,
    name: str,
    *,
    schedule: str = "",
    command: str = "/opt/ivd/sync",
) -> None:
    _write(scope / f"{name}.timer", f"[Timer]\n{schedule}\n")
    _write(scope / f"{name}.service", f"[Service]\nExecStart={command}\n")


def test_dash_prefix_dropin_supplies_periodic_schedule(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _timer_and_service(scope, "ivd-maintenance-daily", command="/usr/bin/backup")
    _write(
        scope / "ivd-maintenance-.timer.d" / "10-schedule.conf",
        "[Timer]\nOnCalendar=daily\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(IndependentIvdCronServiceError):
        _assert_systemd(root, target)


def test_more_specific_prefix_wins_same_named_dropin(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _timer_and_service(scope, "foo-bar-baz")
    _write(
        scope / "foo-.timer.d" / "10-policy.conf",
        "[Timer]\nOnCalendar=daily\n",
    )
    _write(
        scope / "foo-bar-.timer.d" / "10-policy.conf",
        "[Timer]\nOnCalendar=\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


def test_different_dropin_names_apply_in_global_lexical_order(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _timer_and_service(scope, "foo-bar-baz")
    _write(
        scope / "foo-.timer.d" / "10-schedule.conf",
        "[Timer]\nOnCalendar=daily\n",
    )
    _write(
        scope / "foo-bar-.timer.d" / "20-reset.conf",
        "[Timer]\nOnCalendar=\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


def test_instance_same_named_dropin_overrides_template(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _timer_and_service(scope, "foo-bar@instance")
    _write(
        scope / "foo-bar@.timer.d" / "10-policy.conf",
        "[Timer]\nOnCalendar=daily\n",
    )
    _write(
        scope / "foo-bar@instance.timer.d" / "10-policy.conf",
        "[Timer]\nOnCalendar=\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


def test_prefix_same_named_dropin_overrides_type_wide(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _timer_and_service(scope, "foo-bar-baz")
    _write(
        scope / "timer.d" / "10-policy.conf",
        "[Timer]\nOnCalendar=daily\n",
    )
    _write(
        scope / "foo-.timer.d" / "10-policy.conf",
        "[Timer]\nOnCalendar=\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


@pytest.mark.parametrize(
    "dropin_directory",
    ("foo-bar@instance.timer.d", "foo-bar@.timer.d", "foo-.timer.d"),
)
def test_instance_template_and_prefix_timer_dropins_are_applied(
    tmp_path, dropin_directory
):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _timer_and_service(scope, "foo-bar@instance")
    _write(
        scope / dropin_directory / "10-schedule.conf",
        "[Timer]\nOnUnitActiveSec=5m\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(IndependentIvdCronServiceError):
        _assert_systemd(root, target)


@pytest.mark.parametrize(
    "dropin_directory",
    ("foo-bar@.service.d", "foo-.service.d"),
)
def test_instance_timer_resolves_template_service_and_dropins(
    tmp_path, dropin_directory
):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _write(scope / "foo-bar@instance.timer", "[Timer]\nOnCalendar=daily\n")
    _write(
        scope / "foo-bar@.service",
        "[Service]\nExecStart=/usr/bin/backup\n",
    )
    _write(
        scope / dropin_directory / "10-command.conf",
        "[Service]\nExecStart=\nExecStart=/opt/ivd/sync\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(IndependentIvdCronServiceError):
        _assert_systemd(root, target)


def test_unrelated_dash_prefix_does_not_apply(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _timer_and_service(scope, "foo-bar-baz")
    _write(
        scope / "foo-baz-.timer.d" / "10-schedule.conf",
        "[Timer]\nOnCalendar=daily\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


def test_dropin_hierarchy_depth_is_bounded():
    from hermes_cli.ivd_cron_service_contract import (
        IvdCronServiceDiscoveryError,
        _systemd_dropin_directory_names,
    )

    unit_name = f"{'x-' * 64}x.timer"
    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_dropin_level_limit",
    ):
        _systemd_dropin_directory_names(unit_name)


@pytest.mark.parametrize(
    "unsafe_kind, expected_reason",
    (
        ("directory_symlink", "systemd_dropin_symlink"),
        ("entry_symlink", "systemd_dropin_unreadable"),
        ("unreadable", "systemd_dropin_unreadable"),
        ("excessive", "systemd_dropin_entry_limit"),
    ),
)
def test_hierarchical_dropins_fail_closed(tmp_path, unsafe_kind, expected_reason):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _timer_and_service(scope, "ivd-maintenance-daily", command="/usr/bin/backup")
    directory = scope / "ivd-maintenance-.timer.d"
    max_entries = 512
    if unsafe_kind == "directory_symlink":
        destination = tmp_path / "dropins"
        destination.mkdir()
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.symlink_to(destination, target_is_directory=True)
    elif unsafe_kind == "entry_symlink":
        destination = _write(tmp_path / "unsafe.conf", "[Timer]\nOnCalendar=daily\n")
        directory.mkdir(parents=True)
        (directory / "10-policy.conf").symlink_to(destination)
    elif unsafe_kind == "unreadable":
        _write(directory / "10-policy.conf", "[Timer]\nOnCalendar=daily\n")
        directory.chmod(0)
    else:
        _write(directory / "10-policy.conf", "[Timer]\nOnCalendar=daily\n")
        _write(directory / "20-policy.conf", "[Timer]\nOnBootSec=5m\n")
        _write(directory / "30-policy.conf", "[Timer]\nOnUnitActiveSec=5m\n")
        _write(directory / "40-policy.conf", "[Timer]\nOnUnitInactiveSec=5m\n")
        max_entries = 3
    target = tmp_path / "target"
    target.mkdir()

    try:
        with pytest.raises(IvdCronServiceDiscoveryError, match=expected_reason):
            _assert_systemd(root, target, max_entries=max_entries)
    finally:
        if unsafe_kind == "unreadable":
            directory.chmod(0o700)
