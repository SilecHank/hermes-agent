"""A formal answer card must not need model tool evidence to be served.

Regression (2026-11-02): the 五癌 sample question produced the right card answer
on every probe and was still replaced in production by
「当前回答的数值或来源未通过核对，暂不提供该结论。」 because the legacy
validator demanded trusted tool numeric evidence -- and a deterministic card
answer runs no tools (api_calls=0).
"""

from __future__ import annotations

from gateway.after_sales_guard import AfterSalesTurn
from gateway.resolved_answer import ResolvedAnswer

CARD_TEXT = (
    "石蜡切片：手术大组织≥10张、活检穿刺组织≥20张，厚度约5~10µm；"
    "肿瘤细胞含量≥10%，坏死细胞含量<10%。"
)


class _DummyValidator:
    def extract_numeric_claims(self, text):  # pragma: no cover - not reached
        return ["10", "20"]


def _turn(*, source_kind: str, text: str = CARD_TEXT, answer: str = CARD_TEXT) -> AfterSalesTurn:
    return AfterSalesTurn(
        context="",
        facts={},
        validator=_DummyValidator(),
        allowed_numeric_claims=(),
        requires_source_validation=True,
        parameter_contract={"absolute_source_path": "/nonexistent/sop.md"},
        resolved_answer=ResolvedAnswer(text=text, source_kind=source_kind),
    )


def test_formal_answer_card_is_served_without_tool_evidence():
    result = _turn(source_kind="formal_answer_card").validate(CARD_TEXT)
    assert result["ok"] is True
    assert result["normalized_response"] == CARD_TEXT


def test_a_deterministic_short_answer_route_is_served_verbatim():
    # The KB renders these; the model never runs, so the fabrication gates must
    # not reject them (2026-11-02: route=sop_parameter_short_answer, api_calls=0).
    turn = _turn(source_kind="resolved_card")
    object.__setattr__(turn, "route_id", "sop_parameter_short_answer")
    result = turn.validate(CARD_TEXT)
    assert result["ok"] is True
    assert result["normalized_response"] == CARD_TEXT


def test_a_model_drafted_answer_still_needs_source_evidence():
    # Same turn shape, but the submitted text is not the card render: the
    # trusted-tool-evidence rule must still apply.
    turn = _turn(source_kind="formal_answer_card")
    result = turn.validate("手册里说切片要 30 张。")
    assert result["ok"] is False
    assert "formal_source_not_read" in result["reasons"]
