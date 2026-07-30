"""Regressions for instantiated systemd activation dependency chains."""

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


def _link(path: Path, target: str) -> Path:
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


def _safe_timer_template(scope: Path) -> None:
    _write(scope / "backup@.timer", "[Timer]\nOnCalendar=daily\n")
    _write(
        scope / "backup@.service",
        "[Service]\nExecStart=/usr/bin/backup\n",
    )


def _link_source_instance(
    scope: Path,
    *,
    dependency: str = "wants",
    unit_name: str = "host@site.service",
    template_name: str = "host@.service",
) -> None:
    _link(
        scope / f"owner.target.{dependency}" / unit_name,
        f"../{template_name}",
    )


def _expect_blocked(root: Path, target: Path) -> None:
    from hermes_cli.ivd_cron_service_contract import IndependentIvdCronServiceError

    with pytest.raises(IndependentIvdCronServiceError):
        _assert_systemd(root, target)


def test_dependency_link_source_instance_expands_percent_i(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "host@.service",
        "[Unit]\nWants=backup@ivd-%i.timer\n",
    )
    _link_source_instance(scope)
    target = tmp_path / "target"
    target.mkdir()

    _expect_blocked(root, target)


@pytest.mark.parametrize("dependency", ("requires", "upholds"))
def test_other_dependency_dirs_instantiate_source_for_percent_i(
    tmp_path, dependency
):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "host@.service",
        "[Unit]\nWants=backup@ivd-%i.timer\n",
    )
    _link_source_instance(scope, dependency=dependency)
    target = tmp_path / "target"
    target.mkdir()

    _expect_blocked(root, target)


@pytest.mark.parametrize("unit_suffix", UNIT_SUFFIXES)
def test_dependency_links_instantiate_all_legal_unit_suffixes(
    tmp_path, unit_suffix
):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / f"host@.{unit_suffix}",
        "[Unit]\nWants=backup@ivd-%i.timer\n",
    )
    _link_source_instance(
        scope,
        unit_name=f"host@site.{unit_suffix}",
        template_name=f"host@.{unit_suffix}",
    )
    target = tmp_path / "target"
    target.mkdir()

    _expect_blocked(root, target)


def test_instance_specific_source_dropin_participates_in_chain(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(scope / "host@.service", "[Unit]\nDescription=host\n")
    _write(
        scope / "host@site.service.d" / "20-dependency.conf",
        "[Unit]\nWants=backup@ivd-%i.timer\n",
    )
    _link_source_instance(scope)
    target = tmp_path / "target"
    target.mkdir()

    _expect_blocked(root, target)


@pytest.mark.parametrize(
    "middle_suffix, section, key",
    (("path", "Path", "Unit"), ("socket", "Socket", "Service")),
)
def test_two_hop_activation_chain_reaches_timer(
    tmp_path, middle_suffix, section, key
):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "entry.service",
        f"[Unit]\nWants=middle@site.{middle_suffix}\n",
    )
    _write(
        scope / f"middle@.{middle_suffix}",
        f"[{section}]\n{key}=backup@ivd-%i.timer\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    _expect_blocked(root, target)


def test_cycles_and_duplicate_edges_are_deduplicated(tmp_path, monkeypatch):
    import hermes_cli.ivd_cron_service_contract as contract

    monkeypatch.setattr(contract, "MAX_SYSTEMD_ACTIVATION_EDGES", 2)
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _write(
        scope / "host@.service",
        "[Unit]\nWants=peer@%i.path peer@%i.path\n",
    )
    _write(scope / "peer@.path", "[Unit]\nWants=host@%i.service\n")
    _link_source_instance(scope)
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


def test_uninstantiated_timer_template_does_not_fall_back_from_percent_i(
    tmp_path,
):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _write(
        scope / "schedule@.timer",
        "[Timer]\nOnCalendar=daily\nUnit=worker@%i.service\n",
    )
    _write(
        scope / "schedule@.service",
        "[Service]\nExecStart=/usr/bin/ivd-maintenance\n",
    )
    _write(
        scope / "worker@.service",
        "[Service]\nExecStart=/usr/bin/worker\n",
    )
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


def test_instantiated_timer_expands_configured_service_percent_i(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _write(
        scope / "schedule@.timer",
        "[Timer]\nOnCalendar=daily\nUnit=worker@%i.service\n",
    )
    _write(
        scope / "worker@.service",
        "[Service]\nExecStart=/usr/bin/ivd-maintenance\n",
    )
    _link(
        scope / "timers.target.wants" / "schedule@site.timer",
        "../schedule@.timer",
    )
    target = tmp_path / "target"
    target.mkdir()

    _expect_blocked(root, target)


def test_percent_upper_i_unescapes_source_instance(tmp_path):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "host@.service",
        "[Unit]\nWants=backup@ivd-%I.timer\n",
    )
    _link_source_instance(
        scope,
        unit_name=r"host@site\x2deast.service",
    )
    target = tmp_path / "target"
    target.mkdir()

    _expect_blocked(root, target)


def test_double_percent_is_literal_and_not_a_second_specifier(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "host@.service",
        "[Unit]\nWants=backup@ivd-%%i.timer\n",
    )
    _link_source_instance(scope)
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_timer_instance_invalid",
    ):
        _assert_systemd(root, target)


def test_unknown_specifier_in_instantiated_source_fails_closed(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _safe_timer_template(scope)
    _write(
        scope / "host@.service",
        "[Unit]\nWants=backup@ivd-%N.timer\n",
    )
    _link_source_instance(scope)
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="systemd_dependency_specifier_invalid",
    ):
        _assert_systemd(root, target)


def test_template_without_concrete_instance_does_not_expand_percent_i(tmp_path):
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


def test_activation_depth_is_bounded(tmp_path, monkeypatch):
    import hermes_cli.ivd_cron_service_contract as contract

    monkeypatch.setattr(contract, "MAX_SYSTEMD_ACTIVATION_DEPTH", 2, raising=False)
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    for index in range(4):
        _write(
            scope / f"node{index}@.service",
            f"[Unit]\nWants=node{index + 1}@%i.service\n",
        )
    _write(scope / "node4@.service", "[Unit]\nDescription=end\n")
    _link_source_instance(
        scope,
        unit_name="node0@site.service",
        template_name="node0@.service",
    )
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        contract.IvdCronServiceDiscoveryError,
        match="systemd_activation_depth_limit",
    ):
        _assert_systemd(root, target)


def test_activation_edge_count_is_bounded(tmp_path, monkeypatch):
    import hermes_cli.ivd_cron_service_contract as contract

    monkeypatch.setattr(contract, "MAX_SYSTEMD_ACTIVATION_EDGES", 1, raising=False)
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _write(
        scope / "host@.service",
        "[Unit]\nWants=one@%i.path two@%i.path\n",
    )
    _write(scope / "one@.path", "[Path]\nPathExists=/tmp/one\n")
    _write(scope / "two@.path", "[Path]\nPathExists=/tmp/two\n")
    _link_source_instance(scope)
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        contract.IvdCronServiceDiscoveryError,
        match="systemd_activation_edge_limit",
    ):
        _assert_systemd(root, target)


def test_activation_node_count_is_bounded(tmp_path, monkeypatch):
    import hermes_cli.ivd_cron_service_contract as contract

    monkeypatch.setattr(contract, "MAX_SYSTEMD_ACTIVATION_NODES", 3, raising=False)
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _write(scope / "worker@.service", "[Unit]\nDescription=worker\n")
    for instance in ("one", "two", "three", "four"):
        _link(
            scope / "owner.target.wants" / f"worker@{instance}.service",
            "../worker@.service",
        )
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        contract.IvdCronServiceDiscoveryError,
        match="systemd_activation_node_limit",
    ):
        _assert_systemd(root, target)
