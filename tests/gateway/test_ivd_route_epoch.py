import pytest
import ast
import inspect
from datetime import datetime, timedelta

from gateway.ivd_route_epoch import (
    IVDRouteEpochService,
    RouteScope,
    route_epoch_enabled,
)
from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.session import SessionSource, SessionStore
from gateway import run as gateway_run
from hermes_state import SessionDB


def _scope(**overrides):
    values = {
        "profile": "ivd",
        "platform": "qqbot",
        "chat_type": "group",
        "chat_id": "group-a",
        "user_id": "user-a",
    }
    values.update(overrides)
    return RouteScope(**values)


def _session(db, session_id, started_at, end_reason=None):
    db.create_session(session_id=session_id, source="qqbot")
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ?, end_reason = ? WHERE id = ?",
        (started_at, started_at + 1 if end_reason else None, end_reason, session_id),
    )
    db._conn.commit()


@pytest.mark.parametrize(
    "reason",
    ["idle", "prompt_tokens", "user_new", "task_completed", "explicit_reset"],
)
def test_nonrecoverable_boundaries_advance_epoch_and_reject_old_session(tmp_path, reason):
    db = SessionDB(db_path=tmp_path / f"{reason}.db")
    _session(db, "old", 90, "restart_timeout")
    service = IVDRouteEpochService(db)
    first = service.bind(_scope(), session_id="old", now=90)
    advanced = service.advance_boundary(
        _scope(), new_session_id="new", reason=reason, now=100,
        expected_epoch=first["route_epoch"],
    )
    assert advanced["route_epoch"] == first["route_epoch"] + 1
    assert service.can_recover(_scope(), session_id="old") is False


def test_compression_continuation_keeps_epoch(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    _session(db, "parent", 90, "compression")
    _session(db, "child", 101)
    service = IVDRouteEpochService(db)
    first = service.bind(_scope(), session_id="parent", now=90)
    child = service.bind_compression_child(
        _scope(), parent_session_id="parent", child_session_id="child",
        expected_epoch=first["route_epoch"], now=101,
    )
    assert child["route_epoch"] == first["route_epoch"]
    assert child["session_id"] == "child"


def test_only_current_epoch_abnormal_crash_can_recover(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    _session(db, "crashed", 110, "restart_timeout")
    service = IVDRouteEpochService(db)
    service.bind(_scope(), session_id="crashed", now=100)
    assert service.can_recover(_scope(), session_id="crashed") is True

    _session(db, "clean", 120, "session_reset")
    service.bind(_scope(), session_id="clean", now=120, expected_epoch=1)
    assert service.can_recover(_scope(), session_id="clean") is False


def test_old_active_ancestor_and_cross_scope_are_rejected(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    _session(db, "ancestor", 80)
    _session(db, "current", 110, "shutdown_timeout")
    service = IVDRouteEpochService(db)
    service.bind(_scope(), session_id="current", now=100)
    assert service.can_recover(_scope(), session_id="ancestor") is False
    assert service.can_recover(_scope(user_id="user-b"), session_id="current") is False


def test_progressive_reconcile_binds_then_advances_only_with_exact_previous(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    _session(db, "old", 90, "session_reset")
    _session(db, "new", 110)
    service = IVDRouteEpochService(db)
    first = service.reconcile_current(_scope(), session_id="old", now=90)
    assert first["route_epoch"] == 1
    assert service.reconcile_current(
        _scope(), session_id="new", now=110,
        previous_session_id="wrong", boundary_reason="explicit_reset",
    ) is None
    advanced = service.reconcile_current(
        _scope(), session_id="new", now=110,
        previous_session_id="old", boundary_reason="explicit_reset",
    )
    assert advanced["route_epoch"] == 2


def test_reconcile_repairs_interrupted_boundary_from_persisted_metadata(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    _session(db, "old", 90, "idle")
    _session(db, "new", 110)
    service = IVDRouteEpochService(db)
    initial = service.bind(_scope(), session_id="old", now=90)

    repaired = service.reconcile_current(
        _scope(),
        session_id="new",
        previous_session_id="old",
        boundary_reason="idle",
        now=110,
    )

    assert repaired["session_id"] == "new"
    assert repaired["route_epoch"] == initial["route_epoch"] + 1


def test_session_store_idle_reset_advances_existing_route_epoch(tmp_path, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig(
        default_reset_policy=SessionResetPolicy(mode="idle", idle_minutes=1)
    )
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    source = SessionSource(
        platform=Platform.QQBOT,
        chat_id="group-a",
        chat_type="group",
        user_id="user-a",
        profile="ivd",
    )
    first = store.get_or_create_session(source)
    service = IVDRouteEpochService(store._db)
    initial = service.bind(_scope(), session_id=first.session_id, now=90)
    first.updated_at = datetime.now() - timedelta(minutes=5)
    store._save()

    current = store.get_or_create_session(source)

    binding = service.get(_scope())
    assert current.session_id != first.session_id
    assert binding["session_id"] == current.session_id
    assert binding["route_epoch"] == initial["route_epoch"] + 1
    assert binding["boundary_reason"] == "idle"


def test_route_epoch_enablement_is_independent_of_task_checkpoints():
    config = {
        "enabled": True,
        "route_epoch_enabled": True,
        "task_checkpoint_enabled": False,
        "platforms": ["qqbot"],
    }

    assert route_epoch_enabled(config, "qqbot") is True
    assert route_epoch_enabled(config, "wecom") is False


def test_task_completion_advances_epoch_without_changing_live_session(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    _session(db, "current", 90)
    service = IVDRouteEpochService(db)
    initial = service.bind(_scope(), session_id="current", now=90)

    completed = service.mark_task_completed(
        _scope(), session_id="current", now=100
    )

    assert completed["session_id"] == "current"
    assert completed["route_epoch"] == initial["route_epoch"] + 1
    assert completed["boundary_reason"] == "task_completed"
    db.end_session("current", "restart_timeout")
    assert service.can_recover(_scope(), session_id="current") is False


def test_new_activity_after_task_completion_can_recover_from_later_crash(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    _session(db, "current", 90)
    service = IVDRouteEpochService(db)
    service.bind(_scope(), session_id="current", now=90)
    service.mark_task_completed(_scope(), session_id="current", now=100)
    db.append_message("current", role="user", content="new task", timestamp=110)
    db.end_session("current", "restart_timeout")

    assert service.can_recover(_scope(), session_id="current") is True


def test_gateway_marks_route_boundary_when_resumed_checkpoint_completes():
    tree = ast.parse(inspect.getsource(gateway_run.GatewayRunner))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "mark_task_completed" in calls
