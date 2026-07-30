"""Regressions for template timer instances without concrete unit files."""

from __future__ import annotations

from pathlib import Path

import pytest


def _mapped(root: Path, absolute: str) -> Path:
    return root / absolute.lstrip("/")


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _link(path: Path, target: str | Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
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


def _template(scope: Path, prefix: str = "batch") -> None:
    _write(scope / f"{prefix}@.timer", "[Timer]\n")
    _write(
        scope / f"{prefix}@.service",
        "[Service]\nExecStart=/opt/ivd/sync\n",
    )


def test_dropin_only_instance_uses_timer_and_service_templates(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _template(scope)
    _write(
        scope / "batch@site-a.timer.d" / "10-schedule.conf",
        "[Timer]\nOnCalendar=daily\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(IndependentIvdCronServiceError):
        _assert_systemd(root, target)


def test_wants_only_instance_is_synthesized_from_template_target(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _write(scope / "backup@.timer", "[Timer]\nOnCalendar=daily\n")
    _write(
        scope / "backup@.service",
        "[Service]\nExecStart=/usr/bin/backup\n",
    )
    _link(
        scope / "timers.target.wants" / "backup@ivd-site.timer",
        "../backup@.timer",
    )
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(IndependentIvdCronServiceError):
        _assert_systemd(root, target)


def test_dropin_only_instance_without_template_is_ignored(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _write(
        scope / "missing@site-a.timer.d" / "10-schedule.conf",
        "[Timer]\nOnCalendar=daily\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


@pytest.mark.parametrize(
    "unsafe_kind, expected_reason",
    (
        ("symlink", "systemd_dropin_symlink"),
        ("unreadable", "systemd_dropin_unreadable"),
        ("excessive", "systemd_dropin_entry_limit"),
    ),
)
def test_instance_dropin_source_fails_closed(tmp_path, unsafe_kind, expected_reason):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _template(scope)
    directory = scope / "batch@site-a.timer.d"
    max_entries = 512
    if unsafe_kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        _link(directory, outside)
    elif unsafe_kind == "unreadable":
        _write(directory / "10-policy.conf", "[Timer]\nOnCalendar=daily\n")
        directory.chmod(0)
    else:
        for index in range(4):
            _write(
                directory / f"{index:02d}-policy.conf",
                "[Timer]\nOnCalendar=daily\n",
            )
        max_entries = 3
    target = tmp_path / "target"
    target.mkdir()

    try:
        with pytest.raises(IvdCronServiceDiscoveryError, match=expected_reason):
            _assert_systemd(root, target, max_entries=max_entries)
    finally:
        if unsafe_kind == "unreadable":
            directory.chmod(0o700)


@pytest.mark.parametrize(
    "directory_name",
    ("@site.timer.d", "batch@bad name.timer.d", "batch@@site.timer.d"),
)
def test_malformed_instance_dropin_directory_fails_closed(tmp_path, directory_name):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _write(scope / directory_name / "10-policy.conf", "[Timer]\nOnCalendar=daily\n")
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_timer_instance_invalid",
    ):
        _assert_systemd(root, target)


def test_overlong_template_instance_name_is_rejected():
    import hermes_cli.ivd_cron_service_contract as contract

    parser = getattr(contract, "_parse_systemd_template_timer_instance", None)
    assert parser is not None, "template instance parser is required"
    with pytest.raises(
        contract.IvdCronServiceDiscoveryError,
        match="systemd_timer_instance_invalid",
    ):
        parser(f"batch@{'x' * 260}.timer", Path("overlong.timer.d"))


@pytest.mark.parametrize("target_kind", ("escape", "dangling", "unrelated"))
def test_wants_template_instance_target_fails_closed(tmp_path, target_kind):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _template(scope, "backup")
    if target_kind == "escape":
        link_target = "../../../../../../outside/backup@.timer"
    elif target_kind == "dangling":
        link_target = "../missing@.timer"
    else:
        _write(scope / "other@.timer", "[Timer]\n")
        link_target = "../other@.timer"
    _link(
        scope / "timers.target.wants" / "backup@site-a.timer",
        link_target,
    )
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_wants_target_invalid",
    ):
        _assert_systemd(root, target)


def test_wants_instance_entry_must_be_a_symlink(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _template(scope, "backup")
    _write(
        scope / "timers.target.wants" / "backup@site-a.timer",
        "not a symlink",
    )
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_wants_entry_invalid",
    ):
        _assert_systemd(root, target)


def test_wants_directory_symlink_fails_closed(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _template(scope, "backup")
    outside = tmp_path / "outside"
    outside.mkdir()
    _link(scope / "timers.target.wants", outside)
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_wants_directory_invalid",
    ):
        _assert_systemd(root, target)


def test_duplicate_instances_across_scopes_are_deduplicated(tmp_path):
    root = tmp_path / "root"
    logical_scopes = (
        "/etc/systemd/user",
        "/run/systemd/user",
        "/usr/local/lib/systemd/user",
        "/usr/lib/systemd/user",
    )
    scopes = [_mapped(root, item) for item in logical_scopes]
    _write(scopes[0] / "batch@.timer", "[Timer]\n")
    _write(
        scopes[0] / "batch@.service",
        "[Service]\nExecStart=/usr/bin/backup\n",
    )
    for scope in scopes:
        (scope / "batch@site-a.timer.d").mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target, max_entries=3).allowed


def test_unique_template_instances_have_a_global_limit(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    logical_scopes = (
        "/etc/systemd/user",
        "/run/systemd/user",
        "/usr/local/lib/systemd/user",
        "/usr/lib/systemd/user",
    )
    scopes = [_mapped(root, item) for item in logical_scopes]
    _write(scopes[0] / "batch@.timer", "[Timer]\n")
    _write(
        scopes[0] / "batch@.service",
        "[Service]\nExecStart=/usr/bin/backup\n",
    )
    for instance, scope in zip(("a", "b", "c", "d"), scopes, strict=True):
        (scope / f"batch@{instance}.timer.d").mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_timer_instance_limit",
    ):
        _assert_systemd(root, target, max_entries=3)


def test_wants_directory_entry_count_is_bounded(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _write(scope / "backup@.timer", "[Timer]\n")
    _write(
        scope / "backup@.service",
        "[Service]\nExecStart=/usr/bin/backup\n",
    )
    for instance in ("a", "b", "c", "d"):
        _link(
            scope / "timers.target.wants" / f"backup@{instance}.timer",
            "../backup@.timer",
        )
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_wants_entry_limit",
    ):
        _assert_systemd(root, target, max_entries=3)


def test_unrelated_wants_directory_is_not_used_as_instance_source(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _write(scope / "backup@.timer", "[Timer]\nOnCalendar=daily\n")
    _write(
        scope / "backup@.service",
        "[Service]\nExecStart=/usr/bin/backup\n",
    )
    _link(
        scope / "misc.wants" / "backup@ivd-site.timer",
        "../backup@.timer",
    )
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


def test_explicit_ivd_template_remains_conservatively_blocked(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _write(
        scope / "ivd-maintenance@.timer",
        "[Timer]\nOnCalendar=daily\n",
    )
    _write(
        scope / "ivd-maintenance@.service",
        "[Service]\nExecStart=/usr/bin/backup\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(IndependentIvdCronServiceError):
        _assert_systemd(root, target)
