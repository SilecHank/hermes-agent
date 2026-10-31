"""Answer-mode smoke: the policy table that a release must not regress.

Each case is a message shape that the product must keep answering in a specific
way.  The incident on 2026-10-31 was not a broken assertion in a unit test --
every unit test passed while every message was answered with a clarification --
so this table asserts the *delivery mode* for the message classes users actually
send, including the ones that carry no business signal at all.

When a policy change flips a row here, that is a deliberate product decision and
must come with a documentation update; it is never an incidental side effect.
"""

from __future__ import annotations

import pytest

from gateway.after_sales_guard import (
    decide_answer_mode,
    is_analysis_question,
    is_business_question,
)


def _plan(
    *,
    intent="product_fact",
    fast_shape="standard",
    plan_shape="direct_fact",
    eligible=False,
    disposition="miss",
    contract_status=None,
    capability="parameter_resolution",
    issues=(),
    direct_response=None,
    answer_card=None,
    contact_fact=None,
    gate_decision="allow",
    matched=None,
):
    plan = {
        "runtime_preflight": {"eligible": eligible, "route_version": "smoke"},
        "preflight_gate": {"decision": gate_decision, "pipeline_action": "continue_final_answer"},
        "task_contract": {"intent": intent,
                          "context": {"provenance_detail": {"matched_terms": matched or {}}}},
        "fast_path": {"hit": bool(direct_response), "disposition": disposition,
                      "answer_shape": fast_shape, "direct_response": direct_response,
                      "answer_card": answer_card},
        "answer_shape": {"answer_shape": plan_shape},
    }
    if contact_fact is not None:
        plan["contact_fact"] = contact_fact
    if contract_status is not None:
        plan["expert_route_contract"] = {
            "status": contract_status,
            "formal_answer_allowed": False,
            "bounded_expert_answer_allowed": False,
            "capability": capability,
            "issues": list(issues),
        }
    return plan


CASES = [
    # (label, message, plan, expected mode)
    ("greeting", "你好？", _plan(contract_status="clarification",
                                 issues=("product_scope_required",)), "agent"),
    ("meta question", "你是谁？", _plan(contract_status="clarification",
                                        issues=("product_scope_required",)), "agent"),
    ("maintenance command", "把运行时切回 43807f5c",
     _plan(contract_status="clarification", issues=("product_scope_required",)), "agent"),
    ("clinical analysis", "稽留流产会导致绒毛都没了吗，取出来只剩母体组织的碎膜了",
     _plan(matched={"sample_type": ["组织"]}, contract_status="clarification",
           issues=("evidence_not_serving", "evidence_required", "product_scope_required")),
     "expert_analysis"),
    ("troubleshooting analysis", "建库浓度低是什么原因",
     _plan(intent="troubleshooting", fast_shape="clarification", plan_shape="diagnostic",
           disposition="control_stop", contract_status="clarification",
           issues=("evidence_not_serving", "evidence_required", "product_scope_required")),
     "expert_analysis"),
    ("scalar parameter", "NIFTY 提取需要多少血浆",
     _plan(intent="parameter", fast_shape="conditional_scalar", plan_shape="clarification",
           disposition="control_stop", eligible=True, contract_status=None), "clarify"),
    ("sop-bound troubleshooting", "NIFTY提取回收低怎么排查",
     _plan(intent="troubleshooting", fast_shape="sop_context_clarification",
           plan_shape="clarification", disposition="control_stop",
           contract_status="clarification", issues=("product_scope_required",)), "clarify"),
    ("case conclusion", "这个样本的报告结论应该怎么判读",
     _plan(intent="report_interpretation", capability="data_interpretation",
           contract_status="clarification", issues=("evidence_not_serving",)), "clarify"),
    ("file request", "把 SOP-JL-269 原件发我",
     _plan(intent="sop_lookup", fast_shape="file_delivery_short_answer",
           plan_shape="direct_fact", contract_status="clarification",
           issues=("evidence_not_serving",)), "clarify"),
    ("contact lookup", "CNVseq的产品经理是谁",
     _plan(fast_shape="contact_lookup_short_answer", disposition="navigation",
           contract_status="clarification", issues=("product_scope_required",)), "agent"),
    ("answered fast path", "NIFTY手工提取血浆体积是多少",
     _plan(intent="parameter", fast_shape="sop_parameter_short_answer",
           plan_shape="direct_fact", eligible=True, disposition="answer",
           direct_response="200 µL", contract_status=None), "fast_answer"),
    ("planner stop", "这个候选结论可以直接回复吗",
     _plan(intent="sop_lookup", gate_decision="block", contract_status=None), "clarify"),
]


@pytest.mark.parametrize("label,message,plan,expected", CASES,
                         ids=[case[0] for case in CASES])
def test_answer_mode_policy_table(label, message, plan, expected):
    decision = decide_answer_mode(plan, message=message)
    assert decision.mode == expected, (label, decision.mode, decision.reasons)


def test_every_case_declares_a_reason():
    for label, message, plan, _expected in CASES:
        decision = decide_answer_mode(plan, message=message)
        assert decision.reasons, label


def test_greeting_and_command_carry_no_business_signal():
    assert is_business_question({}, "你好？") is False
    assert is_business_question({}, "把运行时切回 43807f5c") is False
    assert is_business_question({}, "NIFTY 提取需要多少血浆") is True


def test_case_conclusions_are_never_analysis():
    assert is_analysis_question(
        {"task_contract": {"intent": "report_interpretation"}},
        {"answer_shape": "standard"},
        {"capability": "data_interpretation"},
    ) is False
