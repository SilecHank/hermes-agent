import threading

from gateway.ivd_receipt_sink import (
    AuthoritativeTurnReceiptSink,
    TurnReceipt,
)
from gateway.run import _enqueue_gateway_ivd_receipt


def _receipt(turn_id: str = "turn-1") -> TurnReceipt:
    return TurnReceipt(
        turn_id=turn_id,
        contract_id="contract-1",
        event_id="event-1",
        package_digest="a" * 64,
        serving_projection_digest="b" * 64,
        validation_status="pass",
        child_spans={"dispatch_ms": 1.0, "lookup_ms": 2.0},
        counters={"model_calls": 0, "index_calls": 1, "filesystem_calls": 0, "skill_calls": 0},
    )


def test_turn_emits_one_authoritative_receipt():
    submitted = []
    sink = AuthoritativeTurnReceiptSink(submitter=lambda receipt: submitted.append(receipt) or True)

    assert sink.submit(_receipt()) is True
    assert sink.submit(_receipt()) is True
    assert len(submitted) == 1
    assert sink.authoritative_count == 1


def test_receipt_failure_does_not_change_answer():
    sink = AuthoritativeTurnReceiptSink(submitter=lambda _receipt: False)
    answer = "200 uL。"

    returned = sink.submit_after_handoff(answer, _receipt())

    assert returned == answer
    assert sink.failed_count == 1


def test_concurrent_duplicate_submissions_still_write_once():
    submitted = []
    sink = AuthoritativeTurnReceiptSink(submitter=lambda receipt: submitted.append(receipt) or True)
    threads = [threading.Thread(target=sink.submit, args=(_receipt(),)) for _ in range(8)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(submitted) == 1
    assert sink.authoritative_count == 1


def test_receipt_rejects_competing_outcome_payloads():
    receipt = _receipt().to_event()

    assert receipt["event_type"] == "ivd_turn_receipt"
    assert receipt["authoritative"] is True
    assert "answer_text" not in receipt
    assert receipt["child_spans"] == {"dispatch_ms": 1.0, "lookup_ms": 2.0}


def test_gateway_duplicate_terminal_paths_submit_one_receipt(monkeypatch):
    submitted = []
    contract = type(
        "Contract",
        (),
        {
            "contract_id": "contract-1",
            "package_digest": "a" * 64,
            "serving_projection_digest": "b" * 64,
            "receipt_destination": object(),
        },
    )()
    prepared = type("Prepared", (), {"execution_contract": contract})()
    monkeypatch.setattr(
        "gateway.ivd_runtime.enqueue_ivd_receipt",
        lambda _destination, receipt: submitted.append(receipt) or True,
    )

    for _ in range(2):
        assert _enqueue_gateway_ivd_receipt(
            "200 uL。",
            prepared,
            platform="weixin",
            session_key="session-1",
            event_id="message-1",
            validation_status="pass",
            child_spans={"total_ms": 12.5},
            counters={"model_calls": 0, "index_calls": 1},
        ) == "200 uL。"

    assert len(submitted) == 1
    assert submitted[0]["event_type"] == "ivd_turn_receipt"
    assert submitted[0]["authoritative"] is True
    assert submitted[0]["child_spans"] == {"total_ms": 12.5}
    assert submitted[0]["counters"] == {"model_calls": 0, "index_calls": 1}
