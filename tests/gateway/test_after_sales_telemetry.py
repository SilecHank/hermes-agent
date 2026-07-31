import json

import pytest

from gateway.after_sales_telemetry import append_runtime_event, build_runtime_event


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
