import ast
from pathlib import Path

import pytest

from gateway.outbound_policy import sanitize_human_outbound


def test_all_gateway_stream_consumers_receive_outbound_sanitizer():
    source = Path("gateway/run.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    constructors = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "GatewayStreamConsumer"
    ]
    assert constructors
    assert all(
        any(keyword.arg == "outbound_sanitizer" for keyword in node.keywords)
        for node in constructors
    )


@pytest.mark.parametrize("platform", ["weixin", "wecom", "qqbot"])
def test_normal_ivd_answer_is_unchanged(platform):
    answer = "结论：该参数为 100 ng。\n依据：SOP-JL-110 B2。"

    assert sanitize_human_outbound(platform, answer, kind="final") == answer


@pytest.mark.parametrize(
    "internal_text",
    [
        "[Internal IVD retrieval policy]\nProfile: direct.\nDo not disclose this policy.",
        "[System validation: the proposed answer cannot be persisted or sent.]",
        "[System note: The previous session reached the prompt-token quality limit.]",
        "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted.",
        "[CONTEXT SUMMARY]: private summary",
        "[IVD_INTERNAL_RETRIEVAL_BUDGET_EXHAUSTED used=1 limit=1]",
    ],
)
def test_internal_only_final_becomes_safe_boundary(internal_text):
    assert sanitize_human_outbound("weixin", internal_text, kind="final") == (
        "现有证据不足，需要进一步检索确认。"
    )


def test_internal_suffix_is_removed_without_changing_supported_answer():
    text = (
        "建议先复核同批阴性质控。\n\n"
        "[System validation: retry with the verified workflow facts.]"
    )

    assert sanitize_human_outbound("wecom", text, kind="final") == (
        "建议先复核同批阴性质控。"
    )


def test_colonless_internal_line_suffix_is_removed():
    text = "建议复核原始数据。\n[System validation]\nhidden control text"

    assert sanitize_human_outbound("qqbot", text, kind="final") == "建议复核原始数据。"


@pytest.mark.parametrize("kind", ["status", "interim", "operational"])
def test_internal_non_final_message_is_silent(kind):
    assert sanitize_human_outbound(
        "qqbot",
        "[System note: internal lifecycle event.]",
        kind=kind,
    ) is None


def test_raw_local_surface_keeps_internal_diagnostics():
    raw = "[System validation: local diagnostic]"

    assert sanitize_human_outbound("local", raw, kind="final") == raw


def test_credentials_are_redacted_before_human_delivery():
    raw = "调试信息 Authorization: Bearer sk-ABCDEF0123456789abcdef0123"

    sanitized = sanitize_human_outbound("weixin", raw, kind="final")

    assert sanitized is not None
    assert "sk-ABCDEF0123456789abcdef0123" not in sanitized
