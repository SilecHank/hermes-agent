"""Clarification outlets must never ask the reader for a version or SOP number.

The person on the other end knows which project and which experimental step is
in question; the applicable version and document number are the system's own
lookup.  Every fallback that asked for them turned a parameter question into a
dead end, so the wording is pinned here.
"""

from types import SimpleNamespace

from gateway.after_sales_guard import (
    EXPERT_CONTRACT_EVIDENCE_ASKS,
    build_source_validation_fallback,
)


def _turn(**source_location):
    return SimpleNamespace(
        source_location=dict(source_location),
        product_scope="NIFTY",
        product_variant="",
    )


def test_multiple_formal_candidates_does_not_request_a_version():
    text = build_source_validation_fallback(
        _turn(status="multiple_formal_candidates", missing_discriminator="版本"),
        "source_not_unique",
    )

    assert "请确认" not in text
    assert "请补充" not in text
    assert "说明是哪一种" in text


def test_source_validation_fallbacks_never_ask_for_document_numbers():
    for status in (
        "multiple_formal_candidates",
        "internal_lookup_blocked",
        "not_read",
    ):
        text = build_source_validation_fallback(_turn(status=status), "missing_source")
        assert "SOP编号" not in text or "无需重复提供" in text


def test_evidence_asks_stay_within_what_the_reader_can_supply():
    for ask in EXPERT_CONTRACT_EVIDENCE_ASKS.values():
        assert "请补充产品版本" not in ask
        assert "请补充SOP" not in ask


def test_report_questions_ask_for_the_report_stage_not_the_rule_version():
    ask = EXPERT_CONTRACT_EVIDENCE_ASKS["data_interpretation"]

    assert "报告环节" in ask
    assert "我按项目自己核对" in ask
