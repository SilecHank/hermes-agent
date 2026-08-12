import pytest

from gateway.ivd_task_checkpoint import (
    build_checkpoint_result,
    CheckpointConflict,
    CheckpointScope,
    IVDTaskCheckpointService,
    message_allows_history_search,
    wants_task_continuation,
)
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
    return CheckpointScope(**values)


def _payload(**overrides):
    values = {
        "active_constraints": ["产品=WES", "版本=V5"],
        "unfinished_steps": ["核对建库质控"],
        "evidence_ids": ["ev-a", "ev-a"],
        "adopted_facts": [{"fact_key": "dna_input", "evidence_id": "ev-a"}],
        "approvals": [],
        "side_effects": [],
    }
    values.update(overrides)
    return values


def test_state_machine_and_stale_revision_rejection(tmp_path):
    service = IVDTaskCheckpointService(SessionDB(db_path=tmp_path / "state.db"))
    created = service.save(
        task_id="task-1",
        scope=_scope(),
        state="active",
        source_session_id="session-a",
        payload=_payload(),
    )
    assert created["revision"] == 1
    waiting = service.save(
        task_id="task-1",
        scope=_scope(),
        state="waiting_approval",
        source_session_id="session-a",
        payload=_payload(),
        expected_revision=1,
    )
    assert waiting["revision"] == 2
    with pytest.raises(CheckpointConflict):
        service.save(
            task_id="task-1",
            scope=_scope(),
            state="completed",
            source_session_id="session-a",
            payload=_payload(),
            expected_revision=1,
        )
    completed = service.save(
        task_id="task-1",
        scope=_scope(),
        state="completed",
        source_session_id="session-a",
        payload=_payload(),
        expected_revision=2,
    )
    with pytest.raises(CheckpointConflict):
        service.save(
            task_id="task-1",
            scope=_scope(),
            state="active",
            source_session_id="session-a",
            payload=_payload(),
            expected_revision=completed["revision"],
        )


@pytest.mark.parametrize("terminal", ["blocked", "abandoned", "superseded"])
def test_supported_non_happy_states_are_persisted(tmp_path, terminal):
    service = IVDTaskCheckpointService(SessionDB(db_path=tmp_path / f"{terminal}.db"))
    created = service.save(
        task_id=f"task-{terminal}", scope=_scope(), state="active",
        source_session_id="s", payload=_payload(),
    )
    saved = service.save(
        task_id=created["task_id"], scope=_scope(), state=terminal,
        source_session_id="s", payload=_payload(), expected_revision=1,
    )
    assert saved["state"] == terminal


def test_scope_isolates_group_users_and_private_chat(tmp_path):
    service = IVDTaskCheckpointService(SessionDB(db_path=tmp_path / "state.db"))
    service.save(
        task_id="task-1", scope=_scope(), state="active",
        source_session_id="s", payload=_payload(),
    )
    assert len(service.find_resumable(_scope())) == 1
    assert service.find_resumable(_scope(user_id="user-b")) == []
    assert service.find_resumable(
        _scope(chat_type="dm", chat_id="user-a", user_id="user-a")
    ) == []


def test_multiple_resumable_tasks_require_short_clarification(tmp_path):
    service = IVDTaskCheckpointService(SessionDB(db_path=tmp_path / "state.db"))
    for task_id in ("task-a", "task-b"):
        service.save(
            task_id=task_id, scope=_scope(), state="active",
            source_session_id=task_id, payload=_payload(),
        )
    resolution = service.resolve_continuation(_scope())
    assert resolution["action"] == "clarify"
    assert "task-a" in resolution["message"] and "task-b" in resolution["message"]
    assert len(resolution["message"]) < 120


def test_live_lease_rejected_and_expired_lease_taken_over(tmp_path):
    service = IVDTaskCheckpointService(SessionDB(db_path=tmp_path / "state.db"))
    service.save(
        task_id="task-1", scope=_scope(), state="active",
        source_session_id="s", payload=_payload(),
    )
    first = service.acquire_lease("task-1", owner_id="worker-a", ttl_seconds=30, now=100)
    assert first["generation"] == 1
    renewed = service.acquire_lease("task-1", owner_id="worker-a", ttl_seconds=30, now=105)
    assert renewed["generation"] == 1
    assert renewed["lease_until"] == 135
    assert service.acquire_lease("task-1", owner_id="worker-b", ttl_seconds=30, now=110) is None
    takeover = service.acquire_lease("task-1", owner_id="worker-b", ttl_seconds=30, now=136)
    assert takeover["generation"] == 2


def test_payload_is_bounded_deduplicated_and_allowlisted(tmp_path):
    service = IVDTaskCheckpointService(SessionDB(db_path=tmp_path / "state.db"))
    saved = service.save(
        task_id="task-1", scope=_scope(), state="active", source_session_id="s",
        payload=_payload(unknown="must-not-persist"),
    )
    assert saved["payload"]["evidence_ids"] == ["ev-a"]
    assert "unknown" not in saved["payload"]


def test_active_checkpoint_can_advance_without_changing_state(tmp_path):
    service = IVDTaskCheckpointService(SessionDB(db_path=tmp_path / "state.db"))
    service.save(
        task_id="task-1", scope=_scope(), state="active",
        source_session_id="s", payload=_payload(),
    )
    updated = service.save(
        task_id="task-1", scope=_scope(), state="active",
        source_session_id="s", payload=_payload(unfinished_steps=["下一步"]),
        expected_revision=1,
    )
    assert updated["revision"] == 2
    assert updated["payload"]["unfinished_steps"] == ["下一步"]


def test_continuation_intent_does_not_hijack_a_new_full_question():
    assert wants_task_continuation("继续") is True
    assert wants_task_continuation("批准 task-123") is True
    assert wants_task_continuation("123通过、456不通过") is True
    assert wants_task_continuation("携带者筛查DNA起始投入量是多少") is False
    assert message_allows_history_search("请核对上次回复的原话") is True
    assert message_allows_history_search("继续") is False


def test_multiple_task_clarification_is_a_zero_model_result():
    result = build_checkpoint_result("发现多个可继续任务：task-a、task-b。请回复任务编号。")
    assert result["api_calls"] == 0
    assert result["completed"] is True
    assert "task-a" in result["final_response"]


def test_authorized_participants_survive_normal_checkpoint_update(tmp_path):
    service = IVDTaskCheckpointService(SessionDB(db_path=tmp_path / "state.db"))
    service.save(
        task_id="task-1", scope=_scope(), state="active", source_session_id="s",
        payload=_payload(), authorized_participants=["user-b"],
    )
    service.save(
        task_id="task-1", scope=_scope(), state="active", source_session_id="s",
        payload=_payload(unfinished_steps=["下一步"]), expected_revision=1,
    )
    assert len(service.find_resumable(_scope(user_id="user-b"))) == 1
