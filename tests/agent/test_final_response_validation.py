from agent.final_response_validation import (
    evaluate_final_response,
    strip_validation_scaffolding,
)
import run_agent as ra


def test_accepts_valid_response():
    decision = evaluate_final_response(
        lambda _text: {"ok": True, "reasons": (), "fallback": ""},
        "valid",
        attempts=0,
    )

    assert decision.action == "accept"
    assert decision.response == "valid"


def test_first_invalid_response_requests_internal_retry():
    decision = evaluate_final_response(
        lambda _text: {
            "ok": False,
            "reasons": ("future_stage:dnb_preparation",),
            "fallback": "focused question",
        },
        "invalid",
        attempts=0,
    )

    assert decision.action == "retry"
    assert "future_stage:dnb_preparation" in decision.retry_prompt
    assert "Do not reveal" in decision.retry_prompt


def test_numeric_failure_retry_removes_only_the_unsupported_value():
    decision = evaluate_final_response(
        lambda _text: {
            "ok": False,
            "reasons": ("unsupported_numeric_claim:<3%",),
            "fallback": "focused question",
        },
        "use <3% as the cutoff",
        attempts=0,
    )

    assert decision.action == "retry"
    assert "Remove the unsupported numeric value" in decision.retry_prompt
    assert "do not replace it with another number" in decision.retry_prompt
    assert "Preserve all supported conclusions" in decision.retry_prompt
    assert "Answer the user's original question first" in decision.retry_prompt


def test_formal_action_retry_removes_only_the_source_overclaim():
    decision = evaluate_final_response(
        lambda _text: {
            "ok": False,
            "reasons": (
                "decision_authority:unsupported_formal_action:rebuild_library",
            ),
            "fallback": "focused question",
        },
        "the SOP requires rebuild",
        attempts=0,
    )

    assert decision.action == "retry"
    assert "Remove only the unsupported SOP/formal attribution" in decision.retry_prompt
    assert "keep a supported conditional analysis" in decision.retry_prompt


def test_retry_does_not_introduce_new_actions_or_diagnoses():
    decision = evaluate_final_response(
        lambda _text: {
            "ok": False,
            "reasons": (
                "decision_authority:unsupported_action:rebuild_library",
                "decision_authority:modality_overclaim:redraw_sample",
            ),
            "fallback": "focused question",
        },
        "rebuild or redraw",
        attempts=0,
    )

    assert decision.action == "retry"
    assert "Do not introduce any new action, diagnosis, stage" in decision.retry_prompt
    assert "not present in the user's original question or rejected draft" in decision.retry_prompt


def test_second_invalid_response_uses_safe_fallback():
    decision = evaluate_final_response(
        lambda _text: {
            "ok": False,
            "reasons": ("control_coverage_overclaim",),
            "fallback": "focused question",
        },
        "invalid again",
        attempts=1,
    )

    assert decision.action == "fallback"
    assert decision.response == "focused question"


def test_validator_exception_fails_open():
    def broken(_text):
        raise RuntimeError("boom")

    decision = evaluate_final_response(broken, "keep response", attempts=0)

    assert decision.action == "accept"
    assert decision.response == "keep response"
    assert decision.error == "boom"


def test_validation_retry_scaffolding_is_never_persisted():
    message = {
        "role": "user",
        "content": "internal retry",
        "_final_validation_synthetic": True,
    }

    assert "_final_validation_synthetic" in ra._EPHEMERAL_SCAFFOLDING_FLAGS
    assert ra._is_ephemeral_scaffolding(message)


def test_validation_scaffolding_is_removed_even_when_buried_by_tool_messages():
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "rejected",
            "_final_validation_synthetic": True,
        },
        {
            "role": "user",
            "content": "retry",
            "_final_validation_synthetic": True,
        },
        {"role": "assistant", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "result"},
    ]

    strip_validation_scaffolding(messages)

    assert [message["role"] for message in messages] == ["user", "assistant", "tool"]
    assert all(not message.get("_final_validation_synthetic") for message in messages)
