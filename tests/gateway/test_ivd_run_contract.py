import pytest

from gateway.run import (
    _final_validation_status,
    _submit_ivd_receipt_preserving_answer,
)


def test_final_validation_status_reuses_validator_state_without_second_validation():
    class Turn:
        has_validator = True

        def validate(self, *_args, **_kwargs):
            raise AssertionError("second validator call")

    validator = type("Validator", (), {"validation_status": "pass"})()
    agent = type("Agent", (), {"_final_response_validator": validator})()

    assert _final_validation_status(agent, Turn()) == "pass"


@pytest.mark.asyncio
async def test_receipt_failure_does_not_alter_answer_and_is_not_retried():
    attempts = []

    def fail(_receipt):
        attempts.append(1)
        raise OSError("unavailable")

    answer = await _submit_ivd_receipt_preserving_answer(
        "原始答案",
        {"contract_count": 1},
        submitter=fail,
        timeout_seconds=0.2,
    )

    assert answer == "原始答案"
    assert attempts == [1]
