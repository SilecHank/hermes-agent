import pytest

from gateway.run import (
    _build_ivd_receipt,
    _enqueue_gateway_ivd_receipt,
    _final_validation_status,
    _prepare_gateway_ivd_boundary,
)
from gateway.ivd_execution_contract import IVDRuntimeConfigurationError
from gateway.ivd_runtime import enqueue_ivd_receipt


def test_final_validation_status_reuses_validator_state_without_second_validation():
    class Turn:
        has_validator = True

        def validate(self, *_args, **_kwargs):
            raise AssertionError("second validator call")

    validator = type("Validator", (), {"validation_status": "pass"})()
    agent = type("Agent", (), {"_final_response_validator": validator})()

    assert _final_validation_status(agent, Turn()) == "pass"


def test_gateway_boundary_blocks_before_after_sales_prepare(monkeypatch):
    called = []
    monkeypatch.setattr(
        "gateway.after_sales_guard.prepare_after_sales_turn",
        lambda *args, **kwargs: called.append(1),
    )

    with pytest.raises(IVDRuntimeConfigurationError):
        _prepare_gateway_ivd_boundary(
            {"after_sales_guard": {"enabled": True, "platforms": ["qqbot"]}},
            platform="qqbot",
            message="问题",
            history=[],
        )

    assert called == []


def test_receipt_has_event_identity_and_no_answer_text(tmp_path):
    digest = "c" * 64
    projection = tmp_path / "projection.json"
    projection.write_text(
        '{"shared_identity":{"package_digest":"' + digest
        + '"},"projections":{"serving":{"package_digest":"' + digest
        + '","receipt_destination":"' + str(tmp_path / "receipt.jsonl")
        + '","records":[]}}}',
        encoding="utf-8",
    )
    prepared, turn = _prepare_gateway_ivd_boundary(
        {"after_sales_guard": {"enabled": True, "platforms": ["qqbot"],
        "serving_projection_path": str(projection)}},
        platform="qqbot", message="问题", history=[]
    )
    receipt = _build_ivd_receipt(
        prepared, platform="qqbot", session_key="session", event_id="message-7",
        validation_status="pass"
    )

    assert receipt["contract_id"].startswith("ivd-contract-")
    assert receipt["turn_id"].startswith("ivd-turn-")
    assert receipt["event_id"] == "message-7"
    assert receipt["package_digest"] == digest
    assert receipt["validation_status"] == "pass"
    assert "answer" not in receipt
    assert "answer_delivered" not in receipt
    assert turn is None


def test_receipt_enqueue_failure_keeps_answer(monkeypatch):
    monkeypatch.setattr("gateway.ivd_runtime._RECEIPT_QUEUE", None)
    assert enqueue_ivd_receipt("/tmp/receipt", {"turn_id": "x"}) is False


def test_gateway_enqueues_receipt_once_and_preserves_final_response(monkeypatch):
    calls = []
    contract = type(
        "Contract",
        (),
        {
            "contract_id": "ivd-contract-1",
            "package_digest": "e" * 64,
            "receipt_destination": "/tmp/receipt",
        },
    )()
    prepared = type("Prepared", (), {"execution_contract": contract})()
    monkeypatch.setattr(
        "gateway.ivd_runtime.enqueue_ivd_receipt",
        lambda destination, receipt: calls.append((destination, receipt)) and False,
    )

    answer = _enqueue_gateway_ivd_receipt(
        "最终答案",
        prepared,
        platform="qqbot",
        session_key="session",
        event_id="event",
        validation_status="pass",
    )

    assert answer == "最终答案"
    assert len(calls) == 1
