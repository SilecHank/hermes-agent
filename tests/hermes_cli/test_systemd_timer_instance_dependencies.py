"""Regressions for direct unit references to template timer instances."""

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


def _safe_timer_template(scope: Path, prefix: str = "backup") -> None:
    _write(scope / f"{prefix}@.timer", "[Timer]\nOnCalendar=daily\n")
    _write(
        scope / f"{prefix}@.service",
        "[Service]\nExecStart=/usr/bin/backup\n",
    )


def _expect_blocked(root: Path, target: Path) -> None:
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    with pytest.raises(IndependentIvdCronServiceError):
        _assert_systemd(root, target)


def test_unit_wants_discovers_timer_instance(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(scope / "host.service", "[Unit]\nWants=backup@ivd-site.timer\n")
    target = tmp_path / "target"
    target.mkdir()

    _expect_blocked(root, target)


@pytest.mark.parametrize("directive", ("Requires", "Upholds"))
def test_other_unit_dependency_lists_discover_timer_instance(tmp_path, directive):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "host.service",
        f"[Unit]\n{directive}=backup@ivd-site.timer\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    _expect_blocked(root, target)


@pytest.mark.parametrize("directive", ("BindsTo", "OnFailure", "OnSuccess"))
def test_other_activating_unit_fields_discover_timer_instance(tmp_path, directive):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "host.service",
        f"[Unit]\n{directive}=backup@ivd-site.timer\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    _expect_blocked(root, target)


@pytest.mark.parametrize(
    "source_name, section, directive",
    (
        ("watch.path", "Path", "Unit"),
        ("listener.socket", "Socket", "Service"),
        ("scheduler.timer", "Timer", "Unit"),
    ),
)
def test_type_specific_activation_fields_discover_timer_instance(
    tmp_path, source_name, section, directive
):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / source_name,
        f"[{section}]\n{directive}=backup@ivd-site.timer\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    _expect_blocked(root, target)


def test_dependency_list_supports_repeated_assignments_and_multiple_targets(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "host.service",
        "[Unit]\n"
        "Wants=ordinary.service other.target\n"
        "Wants=backup@ivd-site.timer final.socket\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    _expect_blocked(root, target)


def test_empty_assignment_resets_previous_dependency_list(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "host.service",
        "[Unit]\nWants=backup@ivd-site.timer\nWants=\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


def test_scalar_activation_field_uses_last_assignment(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "watch.path",
        "[Path]\nUnit=backup@ivd-site.timer\nUnit=ordinary.service\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


def test_dependency_in_source_dropin_is_effective(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(scope / "host.service", "[Unit]\nDescription=host\n")
    _write(
        scope / "host.service.d" / "10-dependency.conf",
        "[Unit]\nWants=backup@ivd-site.timer\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    _expect_blocked(root, target)


def test_dependency_list_supports_line_continuation(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "host.service",
        "[Unit]\nWants=ordinary.service \\\n backup@ivd-site.timer\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    _expect_blocked(root, target)


@pytest.mark.parametrize("directive", ("Before", "After", "Conflicts"))
def test_non_activating_unit_relationships_are_ignored(tmp_path, directive):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "host.service",
        f"[Unit]\n{directive}=backup@ivd-site.timer\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


def test_ordinary_non_timer_references_are_ignored(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "host.service",
        "[Unit]\nWants=ivd-worker.service ivd-target.target\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


def test_timer_instance_reference_without_template_is_ignored(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _write(scope / "host.service", "[Unit]\nWants=missing@ivd-site.timer\n")
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


@pytest.mark.parametrize(
    "reference",
    (
        "backup@@ivd-site.timer",
        "../backup@ivd-site.timer",
        f"backup@{'x' * 260}.timer",
    ),
)
def test_malformed_timer_instance_reference_fails_closed(tmp_path, reference):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(scope / "host.service", f"[Unit]\nWants={reference}\n")
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_timer_instance_invalid",
    ):
        _assert_systemd(root, target)


def test_percent_i_is_expanded_for_concrete_source_instance(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "host@site.service",
        "[Unit]\nWants=backup@ivd-%i.timer\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    _expect_blocked(root, target)


def test_unresolved_percent_i_on_source_template_is_ignored(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "host@.service",
        "[Unit]\nWants=backup@ivd-%i.timer\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


def test_unsupported_timer_specifier_fails_closed(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "host.service",
        "[Unit]\nWants=backup@ivd-%N.timer\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_dependency_specifier_invalid",
    ):
        _assert_systemd(root, target)


def test_global_source_unit_count_is_bounded(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    high = _mapped(root, "/etc/systemd/user")
    low = _mapped(root, "/usr/lib/systemd/user")
    _safe_timer_template(high)
    _write(high / "one.path", "[Path]\nUnit=ordinary.service\n")
    _write(low / "two.socket", "[Socket]\nService=ordinary.service\n")
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_source_unit_limit",
    ):
        _assert_systemd(root, target, max_entries=3)


def test_dependency_reference_count_is_bounded(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "host.service",
        "[Unit]\nWants=one.service two.service three.service four.service\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_dependency_reference_limit",
    ):
        _assert_systemd(root, target, max_entries=3)


def test_unreadable_source_unit_fails_closed(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    source = _write(scope / "host.path", "[Path]\nUnit=ordinary.service\n")
    source.chmod(0)
    target = tmp_path / "target"
    target.mkdir()

    try:
        with pytest.raises(
            IvdCronServiceDiscoveryError,
            match="service_definition_unreadable",
        ):
            _assert_systemd(root, target)
    finally:
        source.chmod(0o600)


def test_malformed_source_unit_name_fails_closed(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _write(scope / "host@@node.service", "[Unit]\nWants=ordinary.service\n")
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_source_unit_invalid",
    ):
        _assert_systemd(root, target)
