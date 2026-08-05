from gateway.ivd_case_handoff import parse_case_handoff, resolve_case_handoff
from gateway.session import resolve_explicit_case_handoff


def test_ordinary_messages_never_cross_platform():
    assert parse_case_handoff("继续刚才的问题") is None


def test_explicit_case_requires_authorization():
    request = parse_case_handoff("继续处理 case-20260805-003")

    result = resolve_case_handoff(
        request,
        actor="other",
        authorize=lambda *_: False,
    )

    assert result.status == "denied"
    assert result.reason == "case_access_denied"
    assert result.context == ""


def test_authorized_handoff_loads_only_minimal_summary():
    result = resolve_case_handoff(
        parse_case_handoff("继续处理 case-20260805-003"),
        actor="owner",
        authorize=lambda *_: True,
        load_summary=lambda _: {
            "case_id": "case-20260805-003",
            "summary": "已确认报告生成失败",
            "raw_messages": ["不得加载"],
        },
    )

    assert result.status == "ready"
    assert result.case_id == "case-20260805-003"
    assert "已确认报告生成失败" in result.context
    assert "raw_messages" not in result.context
    assert "不得加载" not in result.context


def test_summary_is_bounded_to_eight_kibibytes():
    result = resolve_case_handoff(
        parse_case_handoff("接续处理 case-20260805-123456"),
        actor="owner",
        authorize=lambda *_: True,
        load_summary=lambda _: {"summary": "测" * 5000},
    )

    assert result.status == "ready"
    assert len(result.context.encode("utf-8")) <= 8192


def test_session_helper_only_resolves_explicit_case_commands():
    calls = []

    ordinary = resolve_explicit_case_handoff(
        "继续刚才的问题",
        actor="owner",
        authorize=lambda *_: calls.append("authorize") or True,
        load_summary=lambda _: calls.append("load") or {"summary": "不应加载"},
    )
    explicit = resolve_explicit_case_handoff(
        "继续处理 case-20260805-003",
        actor="owner",
        authorize=lambda *_: True,
        load_summary=lambda _: {"summary": "仅加载摘要"},
    )

    assert ordinary is None
    assert calls == []
    assert explicit is not None
    assert explicit.status == "ready"
    assert "仅加载摘要" in explicit.context


def test_parser_rejects_loose_or_malformed_case_references():
    assert parse_case_handoff("case-20260805-003") is None
    assert parse_case_handoff("继续处理 case-202685-003") is None
    assert parse_case_handoff("继续处理 case-20260805-03") is None
    assert parse_case_handoff("继续处理 case-20260805-003 extra") is None
