import json
import threading
import time

import pytest

import gateway.after_sales_telemetry as after_sales_telemetry
from gateway.after_sales_telemetry import (
    append_runtime_event,
    build_runtime_event,
    build_turn_span_event,
    default_runtime_event_path,
)


def test_default_runtime_event_path_uses_ivd_live_data(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

    assert default_runtime_event_path() == (
        tmp_path / "hermes/ivd-live-data/telemetry/runtime-events.jsonl"
    )


def test_runtime_event_is_sanitized_and_versioned(tmp_path):
    event = build_runtime_event(
        platform="weixin",
        session_key="weixin:dm:secret-user",
        product_scope="NIFTY",
        product_variant="全因",
        route_id="static:nifty-parameter",
        route_version="2026-07-29",
        fast_path=True,
        elapsed_seconds=1.25,
        api_calls=1,
        tool_names=["read_file"],
        source_paths=["knowledge-base/reference/nifty-troubleshooting-tree.md"],
        validation_status="pass",
        answer_text="患者姓名和完整回答不得写入事件",
        retrieval_snapshot={
            "profile": "direct",
            "stages": [],
            "searches": 0,
            "signature_count": 1,
            "formal_source_count": 0,
            "no_gain_streak": 0,
            "stop_reason": "direct",
        },
    )

    assert event["schema_version"] == 2
    assert event["session_hash"] != "weixin:dm:secret-user"
    assert event["product_scope"] == "NIFTY"
    assert event["product_variant"] == "全因"
    assert event["fast_path"] is True
    assert event["tool_count"] == 1
    assert event["retrieval_profile"] == "direct"
    assert event["retrieval_stages"] == []
    assert event["retrieval_searches"] == 0
    assert event["retrieval_signature_count"] == 1
    assert event["retrieval_stop_reason"] == "direct"
    assert "answer" not in event
    assert "患者" not in json.dumps(event, ensure_ascii=False)

    path = tmp_path / "runtime-events.jsonl"
    append_runtime_event(path, event)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == event


def test_runtime_event_never_infers_missing_product_scope():
    event = build_runtime_event(
        platform="qqbot",
        session_key="session",
        product_scope="",
        route_id="standard",
        route_version="",
        fast_path=False,
        elapsed_seconds=5,
        api_calls=2,
        tool_names=[],
        source_paths=[],
        validation_status="not_applicable",
        answer_text="这是 NIFTY 问题",
    )

    assert event["product_scope"] == ""


def test_active_turn_span_has_no_competing_outcome_fields():
    event = build_turn_span_event(
        turn_id="ivd-turn-1",
        elapsed_seconds=1.25,
        api_calls=1,
        tool_names=["read_file"],
        retrieval_snapshot={
            "profile": "direct",
            "searches": 0,
            "signature_count": 1,
            "formal_source_count": 1,
            "stop_reason": "formal_source_found",
        },
    )

    assert event["event_type"] == "ivd_turn_span"
    assert event["turn_id"] == "ivd-turn-1"
    assert event["span_name"] == "runtime"
    assert event["counters"]["model_calls"] == 1
    assert event["counters"]["skill_calls"] == 1
    for forbidden in (
        "route_id",
        "route_version",
        "validation_status",
        "source_paths",
        "answer_text",
        "product_scope",
    ):
        assert forbidden not in event


def test_runtime_event_records_sanitized_preflight_gate_decision():
    event = build_runtime_event(
        platform="qqbot",
        session_key="session",
        product_scope="",
        route_id="blocked-test",
        route_version="test-v1",
        fast_path=True,
        elapsed_seconds=0,
        api_calls=0,
        tool_names=[],
        source_paths=[],
        validation_status="preflight_blocked",
        preflight_decision="block",
        preflight_action="stop_before_answer_generation",
        preflight_issues=["pending_candidate_source_used"],
    )

    assert event["pre_answer_budget_gate"] == {
        "decision": "block",
        "pipeline_action": "stop_before_answer_generation",
        "issues": ["pending_candidate_source_used"],
    }


def test_runtime_event_records_sanitized_retrieval_miss_preview():
    event = build_runtime_event(
        platform="weixin",
        session_key="session",
        product_scope="NIFTY",
        route_id="standard",
        route_version="",
        fast_path=False,
        elapsed_seconds=12,
        api_calls=2,
        tool_names=["search_files"],
        source_paths=[],
        validation_status="not_applicable",
        question_text=(
            "患者姓名：张三，手机13800138000，样本号 SAMPLE-20260730-001，"
            "NIFTY 胎儿浓度低怎么排查？"
        ),
        retrieval_snapshot={
            "profile": "evidence_supplement",
            "formal_source_count": 0,
            "stop_reason": "no_gain",
        },
    )

    serialized = json.dumps(event, ensure_ascii=False)
    assert event["schema_version"] == 2
    assert event["retrieval_outcome"] == "miss"
    assert len(event["question_preview"]) <= 120
    assert len(event["question_fingerprint"]) == 16
    assert "NIFTY 胎儿浓度低怎么排查" in event["question_preview"]
    assert "张三" not in serialized
    assert "13800138000" not in serialized
    assert "SAMPLE-20260730-001" not in serialized
    assert "question_text" not in event


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        ({"profile": "direct", "formal_source_count": 0, "stop_reason": "direct"}, "not_needed"),
        ({"profile": "index_fallback", "formal_source_count": 1, "stop_reason": "formal_source_found"}, "hit"),
        ({"profile": "evidence_supplement", "formal_source_count": 1, "stop_reason": "duplicate_intent"}, "partial"),
        ({"profile": "index_fallback", "formal_source_count": 0, "stop_reason": "profile_limit"}, "miss"),
        ({"profile": "complex_diagnosis", "formal_source_count": 0, "stop_reason": "hard_limit"}, "miss"),
        ({"profile": "evidence_supplement", "formal_source_count": 0, "stop_reason": "duplicate"}, "partial"),
    ],
)
def test_runtime_event_classifies_retrieval_outcomes(snapshot, expected):
    event = build_runtime_event(
        platform="weixin",
        session_key="session",
        product_scope="NIFTY",
        route_id="standard",
        route_version="",
        fast_path=False,
        elapsed_seconds=1,
        api_calls=1,
        tool_names=[],
        source_paths=[],
        validation_status="not_applicable",
        question_text="NIFTY 怎么处理？",
        retrieval_snapshot=snapshot,
    )

    assert event["retrieval_outcome"] == expected


def test_runtime_event_fails_closed_when_question_redaction_breaks(monkeypatch):
    import agent.redact

    def fail_redaction(*_args, **_kwargs):
        raise RuntimeError("redaction unavailable")

    monkeypatch.setattr(agent.redact, "redact_sensitive_text", fail_redaction)
    event = build_runtime_event(
        platform="weixin",
        session_key="session",
        product_scope="NIFTY",
        route_id="standard",
        route_version="",
        fast_path=False,
        elapsed_seconds=1,
        api_calls=1,
        tool_names=[],
        source_paths=[],
        validation_status="not_applicable",
        question_text="患者姓名：张三，NIFTY 怎么处理？",
        retrieval_snapshot={"formal_source_count": 0, "stop_reason": "no_gain"},
    )

    assert event["question_preview"] == "[content redacted]"
    assert event["question_fingerprint"] == ""


def test_shadow_replay_returns_served_answer_without_waiting_or_replacing_it():
    replay_started = threading.Event()
    release_replay = threading.Event()
    served_answer = {"text": "served answer", "citations": ["source-a"]}
    recorded_events = []
    submitter = after_sales_telemetry.ShadowReplaySubmitter(
        recorder=recorded_events.append,
        max_workers=1,
        queue_capacity=1,
    )

    def replay():
        replay_started.set()
        release_replay.wait(timeout=2)
        return {
            "comparison_status": "different",
            "exact_match": False,
            "answer_text": "different shadow answer",
        }

    try:
        started_at = time.monotonic()
        returned_answer = submitter.submit(
            served_answer,
            replay,
            comparison_metadata={
                "shadow_route_id": "unified:v2",
                "comparison_status": "prefilled",
                "exact_match": True,
            },
        )

        assert returned_answer is served_answer
        assert time.monotonic() - started_at < 0.25
        assert replay_started.wait(timeout=1)
        assert recorded_events == []

        release_replay.set()
        assert submitter.wait_for_idle(timeout=1)
        assert recorded_events[0]["event_type"] == "ivd_shadow_replay_comparison"
        assert recorded_events[0]["outcome"] == "completed"
        assert recorded_events[0]["comparison_metadata"] == {
            "shadow_route_id": "unified:v2",
            "comparison_status": "different",
            "exact_match": False,
        }
        assert "served answer" not in json.dumps(recorded_events)
        assert "different shadow answer" not in json.dumps(recorded_events)
    finally:
        release_replay.set()
        submitter.close()


def test_shadow_replay_queue_is_bounded_and_rejection_does_not_change_turn():
    release_replay = threading.Event()
    recorded_events = []
    submitter = after_sales_telemetry.ShadowReplaySubmitter(
        recorder=recorded_events.append,
        max_workers=1,
        queue_capacity=0,
    )
    first_answer = object()
    rejected_answer = object()

    try:
        assert submitter.submit(
            first_answer,
            lambda: release_replay.wait(timeout=2),
        ) is first_answer
        assert submitter.submit(rejected_answer, lambda: None) is rejected_answer

        assert submitter.stats.submitted == 1
        assert submitter.stats.rejected == 1
        release_replay.set()
        assert submitter.wait_for_idle(timeout=1)
        assert {
            "schema_version": 2,
            "event_type": "ivd_shadow_replay_isolation",
            "outcome": "queue_full",
        } in recorded_events
    finally:
        release_replay.set()
        submitter.close()


def test_shadow_replay_failure_is_isolated_counted_and_sanitized():
    recorded_events = []
    submitter = after_sales_telemetry.ShadowReplaySubmitter(
        recorder=recorded_events.append,
        max_workers=1,
        queue_capacity=1,
    )
    served_answer = "the served response must remain unchanged"

    def failing_replay():
        raise RuntimeError(
            "患者姓名：张三 email user@example.com token=sk-abcdefghijklmnop"
        )

    try:
        assert submitter.submit(
            served_answer,
            failing_replay,
            comparison_metadata={
                "comparison_status": "failed",
                "note": "患者姓名：李四，手机13800138000",
                "exact_match": False,
                "score_delta": 0.25,
                "shadow_route_id": "unified:v2",
                "answer_text": "完整答案不得记录",
                "session_key": "weixin:dm:secret-user",
                "api_key": "sk-1234567890abcdef",
            },
        ) == served_answer
        assert submitter.wait_for_idle(timeout=1)

        assert submitter.stats.failed == 1
        serialized = json.dumps(recorded_events, ensure_ascii=False)
        assert recorded_events[0]["event_type"] == "ivd_shadow_replay_isolation"
        assert recorded_events[0]["outcome"] == "execution_failed"
        assert recorded_events[0]["error_type"] == "RuntimeError"
        assert recorded_events[0]["comparison_metadata"] == {
            "comparison_status": "failed",
            "exact_match": False,
            "score_delta": 0.25,
            "shadow_route_id": "unified:v2",
        }
        for secret in (
            "张三",
            "李四",
            "13800138000",
            "user@example.com",
            "sk-abcdefghijklmnop",
            "完整答案不得记录",
            "weixin:dm:secret-user",
            "sk-1234567890abcdef",
            served_answer,
        ):
            assert secret not in serialized
    finally:
        submitter.close()


def test_shadow_replay_close_is_idempotent_and_rejects_later_work():
    recorded_events = []
    submitter = after_sales_telemetry.ShadowReplaySubmitter(
        recorder=recorded_events.append,
        max_workers=1,
        queue_capacity=1,
    )
    submitter.close()
    submitter.close()

    served_answer = object()
    assert submitter.submit(served_answer, lambda: None) is served_answer
    assert submitter.stats.rejected == 1
    assert submitter.wait_for_idle(timeout=0.1)


def test_shadow_replay_recorder_failure_never_changes_served_turn():
    def failing_recorder(_event):
        raise OSError("telemetry unavailable")

    submitter = after_sales_telemetry.ShadowReplaySubmitter(
        recorder=failing_recorder,
        max_workers=1,
        queue_capacity=0,
    )
    served_answer = object()
    try:
        assert submitter.submit(served_answer, lambda: None) is served_answer
        assert submitter.wait_for_idle(timeout=1)
        assert submitter.stats.completed == 1
    finally:
        submitter.close()

    assert not any(
        thread.name.startswith("hermes-shadow-replay") and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_shadow_replay_rejections_never_wait_for_blocking_recorder(monkeypatch):
    replay_release = threading.Event()
    recorder_started = threading.Event()
    recorder_release = threading.Event()

    def blocking_recorder(_event):
        recorder_started.set()
        recorder_release.wait(timeout=2)

    submitter = after_sales_telemetry.ShadowReplaySubmitter(
        recorder=blocking_recorder,
        max_workers=1,
        queue_capacity=0,
    )
    try:
        submitter.submit(object(), lambda: replay_release.wait(timeout=2))
        started_at = time.monotonic()
        served_answer = object()
        assert submitter.submit(served_answer, lambda: None) is served_answer
        assert time.monotonic() - started_at < 0.25
        assert recorder_started.wait(timeout=1)

        recorder_release.set()
        replay_release.set()
        assert submitter.wait_for_idle(timeout=1)

        recorder_started.clear()
        recorder_release.clear()
        monkeypatch.setattr(
            submitter._executor,
            "submit",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("executor unavailable")
            ),
        )
        started_at = time.monotonic()
        served_answer = object()
        assert submitter.submit(served_answer, lambda: None) is served_answer
        assert time.monotonic() - started_at < 0.25
        assert recorder_started.wait(timeout=1)

        submitter.close(wait_for_tasks=False)
        started_at = time.monotonic()
        served_answer = object()
        assert submitter.submit(served_answer, lambda: None) is served_answer
        assert time.monotonic() - started_at < 0.25
    finally:
        recorder_release.set()
        replay_release.set()
        submitter.close(wait_for_tasks=True)


def test_blocking_recorder_does_not_hold_shadow_worker_capacity():
    recorder_started = threading.Event()
    recorder_release = threading.Event()
    second_replay_started = threading.Event()

    def blocking_recorder(_event):
        recorder_started.set()
        recorder_release.wait(timeout=2)

    submitter = after_sales_telemetry.ShadowReplaySubmitter(
        recorder=blocking_recorder,
        max_workers=1,
        queue_capacity=0,
    )
    try:
        submitter.submit(object(), lambda: {"comparison_status": "first"})
        assert recorder_started.wait(timeout=1)

        served_answer = object()
        assert submitter.submit(
            served_answer,
            lambda: second_replay_started.set(),
        ) is served_answer
        assert second_replay_started.wait(timeout=1)
        assert submitter.stats.submitted == 2
    finally:
        recorder_release.set()
        submitter.close(wait_for_tasks=True)


def test_non_waiting_close_cancels_queued_replays_and_later_waits_for_shutdown():
    replay_started = threading.Event()
    replay_release = threading.Event()
    submitter = after_sales_telemetry.ShadowReplaySubmitter(
        recorder=lambda _event: None,
        max_workers=1,
        queue_capacity=2,
    )

    def blocking_replay():
        replay_started.set()
        replay_release.wait(timeout=2)

    submitter.submit(object(), blocking_replay)
    assert replay_started.wait(timeout=1)
    submitter.submit(object(), lambda: None)
    submitter.submit(object(), lambda: None)

    started_at = time.monotonic()
    submitter.close(wait_for_tasks=False)
    assert time.monotonic() - started_at < 0.25
    assert submitter.stats.submitted == 3
    assert submitter.stats.cancelled == 2

    replay_release.set()
    submitter.close(wait_for_tasks=True)
    stats = submitter.stats
    assert stats.submitted == stats.completed + stats.failed + stats.cancelled
    assert not any(
        thread.name.startswith("hermes-shadow-replay") and thread.is_alive()
        for thread in threading.enumerate()
    )
