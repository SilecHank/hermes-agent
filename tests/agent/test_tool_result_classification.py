"""Tests for shared tool result classification helpers."""

import json

from agent.tool_result_classification import (
    build_effect_receipts_from_messages,
    build_effect_receipt,
    continuation_action,
    file_mutation_result_landed,
)


def test_write_file_with_nested_lint_error_counts_as_landed():
    result = json.dumps({
        "bytes_written": 12,
        "lint": {"status": "error", "output": "SyntaxError: invalid syntax"},
    })

    assert file_mutation_result_landed("write_file", result) is True


def test_patch_with_nested_lsp_diagnostics_counts_as_landed():
    result = json.dumps({
        "success": True,
        "diff": "--- a/tmp.py\n+++ b/tmp.py\n",
        "lsp_diagnostics": "<diagnostics>ERROR [1:1] type mismatch</diagnostics>",
    })

    assert file_mutation_result_landed("patch", result) is True


def test_top_level_file_mutation_error_does_not_count_as_landed():
    result = json.dumps({"success": True, "error": "post-write verification failed"})

    assert file_mutation_result_landed("patch", result) is False


def test_side_effect_classification_keeps_session_mutations():
    from agent.tool_result_classification import tool_may_have_side_effect

    assert tool_may_have_side_effect("todo") is True
    assert tool_may_have_side_effect("memory") is True
    assert tool_may_have_side_effect("write_file") is True
    assert tool_may_have_side_effect("mcp_unknown") is True
    assert tool_may_have_side_effect("read_file") is False
    assert tool_may_have_side_effect("web_search") is False


def test_unknown_effect_is_preserved_and_never_retried():
    receipt = build_effect_receipt(
        "call-deploy", "deploy", "unknown_effect", "message:81", recorded_at=10
    )
    assert continuation_action(receipt) == "verify_status"
    assert continuation_action(receipt) != "retry"


def test_completed_effect_uses_idempotency_receipt_after_rotation():
    receipt = build_effect_receipt(
        "call-approve", "approve", "effect_committed", "message:82", recorded_at=10
    )
    assert receipt["idempotency_id"] == "call-approve"
    assert continuation_action(receipt) == "reuse_result"


def test_read_only_tool_replay_depends_on_source_revision():
    receipt = build_effect_receipt(
        "call-read", "read_file", "read_only", "knowledge-base/products/WES.md",
        recorded_at=10,
    )
    assert continuation_action(receipt, source_revision_matches=True) == "read_again"
    assert continuation_action(receipt, source_revision_matches=False) == "revalidate_source"


def test_effect_receipts_are_derived_without_persisting_arguments():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "read-1", "function": {"name": "read_file", "arguments": '{"path":"secret"}'}},
                {"id": "deploy-1", "function": {"name": "deploy", "arguments": '{"token":"secret"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "read-1", "content": "formal source"},
        {"role": "tool", "tool_call_id": "deploy-1", "content": '{"success":true,"status":"completed"}'},
    ]
    receipts = build_effect_receipts_from_messages(messages, recorded_at=10)
    by_id = {item["idempotency_id"]: item for item in receipts}
    assert by_id["read-1"]["effect_disposition"] == "read_only"
    assert by_id["deploy-1"]["effect_disposition"] == "effect_committed"
    assert "secret" not in str(receipts)
