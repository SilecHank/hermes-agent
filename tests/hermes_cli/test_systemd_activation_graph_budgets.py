"""Regressions for separate base-unit and dynamic activation budgets."""

from __future__ import annotations

import time
from pathlib import Path

import pytest


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


def _base_units(scope: Path, count: int) -> None:
    for index in range(count):
        _write(
            scope / f"ordinary-{index:03d}.service",
            "[Unit]\nDescription=ordinary worker\n",
        )


@pytest.mark.parametrize("count", (312, 512))
def test_base_units_do_not_consume_dynamic_node_budget(tmp_path, count):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _base_units(scope, count)
    target = tmp_path / "target"
    target.mkdir()

    decision = _assert_systemd(root, target)

    assert decision.allowed
    assert decision.reason == "embedded_cron_owned"


def test_513_base_units_still_fail_closed_at_existing_limit(tmp_path):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _base_units(scope, 513)
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(
        IvdCronServiceDiscoveryError,
        match="service_scope_entry_limit|systemd_source_unit_limit",
    ):
        _assert_systemd(root, target)


@pytest.mark.parametrize("count, allowed", ((256, True), (257, False)))
def test_dynamic_instances_have_an_independent_256_node_budget(
    tmp_path, count, allowed
):
    from hermes_cli.ivd_cron_service_contract import IvdCronServiceDiscoveryError

    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _write(scope / "worker@.service", "[Unit]\nDescription=worker\n")
    for index in range(count):
        _link(
            scope
            / "owner.target.wants"
            / f"worker@site-{index:03d}.service",
            "../worker@.service",
        )
    target = tmp_path / "target"
    target.mkdir()

    if allowed:
        assert _assert_systemd(root, target).allowed
    else:
        with pytest.raises(
            IvdCronServiceDiscoveryError,
            match="systemd_activation_node_limit",
        ):
            _assert_systemd(root, target)


def test_base_references_duplicates_and_cycles_do_not_consume_dynamic_budget(
    tmp_path, monkeypatch
):
    import hermes_cli.ivd_cron_service_contract as contract

    monkeypatch.setattr(contract, "MAX_SYSTEMD_ACTIVATION_NODES", 2)
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _base_units(scope, 312)
    _write(
        scope / "first@.service",
        "[Unit]\nWants=second@%i.path second@%i.path ordinary-000.service\n",
    )
    _write(
        scope / "second@.path",
        "[Unit]\nWants=first@%i.service ordinary-001.service\n",
    )
    _link(
        scope / "owner.target.requires" / "first@site.service",
        "../first@.service",
    )
    target = tmp_path / "target"
    target.mkdir()

    assert _assert_systemd(root, target).allowed


def test_production_sized_read_only_scan_records_elapsed_time(tmp_path, capsys):
    root = tmp_path / "root"
    scope = _mapped(root, "/etc/systemd/user")
    _base_units(scope, 312)
    target = tmp_path / "target"
    target.mkdir()

    started = time.perf_counter()
    decision = _assert_systemd(root, target)
    elapsed = time.perf_counter() - started
    print(f"production_read_only_scan_seconds={elapsed:.6f}")

    assert decision.allowed
    assert decision.reason == "embedded_cron_owned"
    assert elapsed < 5.0
    assert "production_read_only_scan_seconds=" in capsys.readouterr().out
