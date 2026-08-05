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


@pytest.mark.parametrize("platform", ["weixin", "wecom", "qqbot", "telegram"])
def test_normal_ivd_answer_is_unchanged(platform):
    answer = "结论：该参数为 100 ng。\n依据：SOP-JL-110 B2。"

    assert sanitize_human_outbound(platform, answer, kind="final") == answer


@pytest.mark.parametrize("platform", ["weixin", "wecom", "qqbot", "telegram"])
def test_ivd_answer_is_normalized_to_copy_friendly_plain_text(platform):
    answer = (
        "# 主要差异\n\n"
        "**V5 流程**\n"
        "- 使用 `PMseq RNA V5` 试剂。\n"
        "- 参见 [正式 SOP](https://example.test/sop-v5)。"
    )

    assert sanitize_human_outbound(platform, answer, kind="final") == (
        "主要差异\n\n"
        "V5 流程\n"
        "• 使用 PMseq RNA V5 试剂。\n"
        "• 参见 正式 SOP：https://example.test/sop-v5。"
    )


def test_ivd_plain_text_preserves_code_and_professional_identifiers():
    answer = (
        "**复核命令**\n\n"
        "```sh\npython3 -B tool.py --sample sample_id --glob '*.fq.gz'\n```\n\n"
        "HBA1/HBA2、anti-3.7、sample_id、/path_with_under/file.md、5*10 均保持原样。"
    )

    assert sanitize_human_outbound("telegram", answer, kind="final") == (
        "复核命令\n\n"
        "python3 -B tool.py --sample sample_id --glob '*.fq.gz'\n\n"
        "HBA1/HBA2、anti-3.7、sample_id、/path_with_under/file.md、5*10 均保持原样。"
    )


def test_ivd_plain_text_removes_safe_italic_and_partial_fence_markers():
    answer = "_关键差异_，但 sample_id 保持原样。\n```text\n正在生成"

    assert sanitize_human_outbound("weixin", answer, kind="interim") == (
        "关键差异，但 sample_id 保持原样。\n正在生成"
    )


def test_ivd_markdown_table_becomes_readable_plain_lines():
    answer = (
        "| 版本 | 关键差异 |\n"
        "| --- | --- |\n"
        "| V4 | 旧流程 |\n"
        "| V5 | 新流程 |"
    )

    sanitized = sanitize_human_outbound("wecom", answer, kind="final")

    assert sanitized == "版本：V4；关键差异：旧流程\n版本：V5；关键差异：新流程"
    assert "|" not in sanitized


@pytest.mark.parametrize("kind", ["final", "status", "interim", "operational"])
def test_ivd_streaming_partial_does_not_expose_unmatched_markdown(kind):
    assert sanitize_human_outbound("qqbot", "**正在核实", kind=kind) == "正在核实"


@pytest.mark.parametrize("platform", ["local", "api_server", "webhook", "msgraph_webhook"])
def test_raw_surfaces_keep_markdown_byte_identical(platform):
    raw = "# 标题\n**原始诊断** [链接](https://example.test/raw)"

    assert sanitize_human_outbound(platform, raw, kind="final") == raw


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
