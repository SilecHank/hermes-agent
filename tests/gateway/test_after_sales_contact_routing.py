"""Regression tests for deterministic internal contact lookup."""

from __future__ import annotations

from gateway.after_sales_guard import prepare_after_sales_turn


def test_resolved_contact_fact_can_bypass_model_fallback(tmp_path):
    fast_module = tmp_path / "fast_response.py"
    fast_module.write_text(
        """
def build_fast_response_plan(question, question_type='general'):
    return {
        'workflow': 'product-fit.md',
        'fast_path': {
            'hit': True,
            'answer_shape': 'contact_lookup_short_answer',
            'contact_fact': {
                'status': 'resolved',
                'answer': '新生儿基因筛查、新生儿质谱筛查（新筛）的产品经理是蓝丽萍。',
            },
        },
        'initial_files': [],
        'runtime_preflight': {'eligible': True, 'route_version': 'test'},
        'answer_template': {},
        'answer_contract': {},
        'answer_shape': {'answer_shape': 'contact_lookup_short_answer'},
    }
""",
        encoding="utf-8",
    )
    turn = prepare_after_sales_turn(
        {
            "after_sales_guard": {
                "enabled": True,
                "platforms": ["qqbot"],
                "fast_response_module": str(fast_module),
            }
        },
        platform="qqbot",
        message="新生儿产品经理是谁",
        history=[],
    )

    assert turn is not None
    assert turn.direct_response == "新生儿基因筛查、新生儿质谱筛查（新筛）的产品经理是蓝丽萍。"
