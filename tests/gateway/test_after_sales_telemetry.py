import json

from gateway.after_sales_telemetry import append_runtime_event, build_runtime_event


def test_runtime_event_is_sanitized_and_versioned(tmp_path):
    event = build_runtime_event(
        platform="weixin",
        session_key="weixin:dm:secret-user",
        product_scope="NIFTY",
        route_id="static:nifty-parameter",
        route_version="2026-07-29",
        fast_path=True,
        elapsed_seconds=1.25,
        api_calls=1,
        tool_names=["read_file"],
        source_paths=["knowledge-base/reference/nifty-troubleshooting-tree.md"],
        validation_status="pass",
        answer_text="患者姓名和完整回答不得写入事件",
    )

    assert event["schema_version"] == 1
    assert event["session_hash"] != "weixin:dm:secret-user"
    assert event["product_scope"] == "NIFTY"
    assert event["fast_path"] is True
    assert event["tool_count"] == 1
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
