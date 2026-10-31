"""Answer-link watchdog: alert on regressions, never on correct bypasses.

The watchdog's textless rule used to read "no user text" as "entered the
business chain".  Every correctly bypassed media turn records a row with the
recorder's defaults (`route_id="standard"`, empty `route_version`), so the rule
fired on the exact behaviour it was meant to protect.  These tests pin the
routing fact instead: a textless turn is only news when the governed engine
handled it.
"""

import importlib.util
from pathlib import Path

import pytest

from gateway.after_sales_telemetry import build_runtime_event

WATCHDOG = Path(__file__).resolve().parents[2] / "scripts" / "hermes_answer_link_watchdog.py"


@pytest.fixture(scope="module")
def watchdog():
    spec = importlib.util.spec_from_file_location("answer_link_watchdog", WATCHDOG)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _row(*, text=None, engine=None, route_id="standard", route_version="",
         validation_status="not_applicable", fingerprint="", preview="",
         api_calls=1):
    row = {
        "event_type": "ivd_answer_turn",
        "platform": "weixin",
        "route_id": route_id,
        "route_version": route_version,
        "validation_status": validation_status,
        "api_calls": api_calls,
        "question_preview": preview,
        "question_fingerprint": fingerprint,
    }
    if text is not None:
        row["question_has_user_text"] = text
    if engine is not None:
        row["ivd_engine_entered"] = engine
    return row


def _filler(count, **kwargs):
    return [
        _row(text=True, engine=True, route_id="clarification",
             route_version="2026-08-20", preview=f"第{n}个质控问题",
             fingerprint=f"filler-{n}", **kwargs)
        for n in range(count)
    ]


def test_bypassed_media_turns_do_not_alert(watchdog):
    # A caption-less screenshot: no user text, engine bypassed, agent answered.
    rows = _filler(4) + [
        _row(text=False, engine=False),
        _row(text=False, engine=False),
    ]

    assert watchdog.diagnose(rows) == []


def test_legacy_bypass_rows_without_field_do_not_alert(watchdog):
    # Rows written before the gateway recorded `ivd_engine_entered`.
    rows = _filler(4) + [
        _row(text=False),
        _row(text=False),
    ]

    assert watchdog.diagnose(rows) == []


def test_textless_turn_inside_engine_alerts(watchdog):
    rows = _filler(4) + [
        _row(text=False, engine=True),
        _row(text=False, engine=True),
    ]

    alerts = watchdog.diagnose(rows)

    assert len(alerts) == 1
    assert "没有用户正文" in alerts[0]


def test_legacy_textless_engine_row_alerts_on_route_evidence(watchdog):
    rows = _filler(4) + [
        _row(text=False, route_id="clarification", route_version="2026-08-20"),
        _row(text=False, validation_status="preflight_blocked", api_calls=0),
    ]

    alerts = watchdog.diagnose(rows)

    assert any("没有用户正文" in alert for alert in alerts)


def test_blocked_ratio_still_alerts(watchdog, tmp_path):
    rows = [
        _row(text=True, route_id="clarification", route_version="2026-08-20",
             validation_status="preflight_blocked", api_calls=0, fingerprint=f"f{n}")
        for n in range(6)
    ]

    alerts = watchdog.diagnose(rows)

    assert any("0 次 API 调用" in alert for alert in alerts)


def test_repeat_rule_ignores_scaffolding_previews(watchdog):
    rows = [
        _row(text=True, route_id="clarification", route_version="2026-08-20",
             preview="请补充产品名称", fingerprint="same")
        for _ in range(3)
    ] + [
        _row(text=False, route_id="clarification", route_version="2026-08-20",
             preview="[Gateway verified sender identity]",
             fingerprint="scaffold")
        for _ in range(5)
    ]

    alerts = watchdog.diagnose(rows)

    assert any("同一澄清追问重复 3 次" in alert for alert in alerts)
    assert not any("Gateway verified" in alert for alert in alerts)


def test_runtime_event_records_engine_entry_decision():
    entered = build_runtime_event(
        platform="weixin",
        session_key="weixin:dm:someone",
        product_scope="",
        route_id="standard",
        route_version="",
        fast_path=False,
        elapsed_seconds=1.0,
        api_calls=0,
        tool_names=(),
        source_paths=(),
        validation_status="not_applicable",
        question_has_user_text=False,
        ivd_engine_entered=False,
    )
    legacy = build_runtime_event(
        platform="weixin",
        session_key="weixin:dm:someone",
        product_scope="",
        route_id="standard",
        route_version="",
        fast_path=False,
        elapsed_seconds=1.0,
        api_calls=0,
        tool_names=(),
        source_paths=(),
        validation_status="not_applicable",
        question_has_user_text=False,
    )

    assert entered["ivd_engine_entered"] is False
    assert "ivd_engine_entered" not in legacy
