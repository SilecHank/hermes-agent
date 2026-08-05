"""Regressions for timer instances linked from systemd dependency directories."""

from __future__ import annotations

from pathlib import Path

import pytest


UNIT_SUFFIXES = (
    "service",
    "socket",
    "device",
    "mount",
    "automount",
    "swap",
    "target",
    "path",
    "timer",
    "slice",
    "scope",
)


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


def _safe_template(scope: Path) -> None:
    _write(scope / "backup@.timer", "[Timer]\nOnCalendar=daily\n")
    _write(
        scope / "backup@.service",
        "[Service]\nExecStart=/usr/bin/backup\n",
    )


def _instance_link(
    scope: Path,
    directory: str,
    instance: str = "ivd-site",
    link_target: str = "../backup@.timer",
) -> None:
    _link(
        scope / directory / f"backup@{instance}.timer",
        link_target,
    )


def test_service_wants_dependency_discovers_template_timer_instance(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_template(scope)
    _instance_link(scope, "host.service.wants")
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(IndependentIvdCronServiceError):
        _assert_systemd(root, target)


@pytest.mark.parametrize("dependency", ("requires", "upholds"))
def test_other_dependency_types_discover_template_timer_instance(
    tmp_path, dependency
):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_template(scope)
    _instance_link(scope, f"host.service.{dependency}")
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(IndependentIvdCronServiceError):
        _assert_systemd(root, target)


@pytest.mark.parametrize("unit_suffix", UNIT_SUFFIXES)
def test_all_systemd_source_unit_types_are_scanned(tmp_path, unit_suffix):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_template(scope)
    _instance_link(scope, f"source.{unit_suffix}.wants")
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(IndependentIvdCronServiceError):
        _assert_systemd(root, target)


@pytest.mark.parametrize(
    "source_unit",
    ("source@.service", "source@node.service"),
)
def test_legal_template_and_instance_source_units_are_scanned(tmp_path, source_unit):
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_template(scope)
    _instance_link(scope, f"{source_unit}.requires")
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(IndependentIvdCronServiceError):
        _assert_systemd(root, target)


@pytest.mark.parametrize(
    "directory_name",
    (
        "host.invalid.wants",
        "host.service.extra.requires",
        "@host.service.wants",
        "host@@node.service.upholds",
        "host@bad@.service.wants",
    ),
)
def test_malformed_dependency_directory_fails_closed(tmp_path, directory_name):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_template(scope)
    _instance_link(scope, directory_name)
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_wants_directory_invalid",
    ):
        _assert_systemd(root, target)


@pytest.mark.parametrize(
    "directory_name",
    (
        "../host.service.wants",
        f"{'x' * 250}.service.wants",
        "host@.wants",
        "\udcff.service.wants",
    ),
)
def test_dependency_directory_parser_rejects_path_overlong_and_bad_template(
    directory_name,
):
    import hermes_cli.ivd_cron_service_contract as contract

    parser = getattr(contract, "_parse_systemd_dependency_directory", None)
    assert parser is not None, "dependency directory parser is required"
    with pytest.raises(
        contract.IvdCronServiceDiscoveryError,
        match="systemd_wants_directory_invalid",
    ):
        parser(directory_name, Path(directory_name))


def test_nonstandard_wanted_by_directory_is_not_scanned(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_template(scope)
    _instance_link(scope, "host.service.wantedBy")
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


def test_unrelated_symlink_in_dependency_directory_is_ignored(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_template(scope)
    _link(
        scope / "host.service.requires" / "unrelated.service",
        "../missing.service",
    )
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


def test_dependency_directories_are_not_scanned_recursively(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_template(scope)
    _link(
        scope
        / "host.service.wants"
        / "nested"
        / "backup@ivd-site.timer",
        "../../backup@.timer",
    )
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


@pytest.mark.parametrize("dependency", ("requires", "upholds"))
def test_dependency_directory_entry_count_is_bounded(tmp_path, dependency):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_template(scope)
    for instance in ("a", "b", "c", "d"):
        _instance_link(
            scope,
            f"host.service.{dependency}",
            instance,
            "/etc/systemd/user/backup@.timer",
        )
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_wants_entry_limit",
    ):
        _assert_systemd(root, target, max_entries=3)


def test_dependency_instances_are_bounded_by_the_global_scan_limit(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    logical_scopes = (
        "/etc/systemd/user",
        "/run/systemd/user",
        "/usr/local/lib/systemd/user",
        "/usr/lib/systemd/user",
    )
    scopes = [_mapped(root, item) for item in logical_scopes]
    _safe_template(scopes[0])
    dependencies = ("wants", "requires", "upholds", "wants")
    for instance, dependency, scope in zip(
        ("a", "b", "c", "d"),
        dependencies,
        scopes,
        strict=True,
    ):
        _instance_link(
            scope,
            f"host.service.{dependency}",
            instance,
            "/etc/systemd/user/backup@.timer",
        )
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_wants_entry_limit",
    ):
        _assert_systemd(root, target, max_entries=3)


def test_dependency_entries_share_a_global_scan_limit(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    logical_scopes = (
        "/etc/systemd/user",
        "/run/systemd/user",
        "/usr/local/lib/systemd/user",
        "/usr/lib/systemd/user",
    )
    scopes = [_mapped(root, item) for item in logical_scopes]
    _safe_template(scopes[0])
    for scope in scopes:
        _link(
            scope / "host.service.requires" / "unrelated.service",
            "../missing.service",
        )
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_wants_entry_limit",
    ):
        _assert_systemd(root, target, max_entries=3)
