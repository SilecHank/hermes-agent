import threading

import pytest

from gateway.ivd_receipt_sink import (
    AuthoritativeTurnReceiptSink,
    ReceiptPersistenceError,
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


def test_receipt_failure_blocks_answer_handoff_and_remains_retryable():
    attempts = []

    def submitter(receipt):
        attempts.append(receipt)
        return len(attempts) > 1

    sink = AuthoritativeTurnReceiptSink(submitter=submitter)
    answer = "200 uL。"

    with pytest.raises(RuntimeError, match="receipt"):
        sink.submit_after_handoff(answer, _receipt())

    assert sink.submit_after_handoff(answer, _receipt()) == answer
    assert len(attempts) == 2
    assert sink.authoritative_count == 1
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


def test_concurrent_duplicate_can_retry_one_failed_persistence():
    entered = threading.Event()
    release = threading.Event()
    attempts = []
    outcomes = []

    def submitter(receipt):
        attempts.append(receipt)
        if len(attempts) == 1:
            entered.set()
            release.wait(timeout=2)
            return False
        return True

    sink = AuthoritativeTurnReceiptSink(submitter=submitter)

    def submit():
        try:
            outcomes.append(sink.submit(_receipt()))
        except ReceiptPersistenceError:
            outcomes.append("blocked")

    first = threading.Thread(target=submit)
    second = threading.Thread(target=submit)
    first.start()
    assert entered.wait(timeout=1)
    second.start()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert sorted(outcomes, key=str) == [True, "blocked"]
    assert len(attempts) == 2
    assert sink.authoritative_count == 1
    assert sink.failed_count == 1


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
