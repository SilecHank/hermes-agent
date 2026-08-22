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


def test_resolved_contact_fact_survives_existing_workflow_match(tmp_path):
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
                'answer': '无创基础版&全因的产品经理是王阳。',
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
    workflow_module = tmp_path / "workflow.py"
    workflow_module.write_text(
        """
def match_case_facts(cards_dir, message, history):
    return {'facts': {'product': 'NIFTY', 'workflow_id': 'existing-case'}}

def render_fact_context(match):
    return 'existing workflow context'
""",
        encoding="utf-8",
    )
    validator_module = tmp_path / "validator.py"
    validator_module.write_text(
        """
def extract_numeric_claims(text):
    return []
""",
        encoding="utf-8",
    )
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()

    turn = prepare_after_sales_turn(
        {
            "after_sales_guard": {
                "enabled": True,
                "platforms": ["qqbot"],
                "fast_response_module": str(fast_module),
                "workflow_module": str(workflow_module),
                "validator_module": str(validator_module),
                "cards_dir": str(cards_dir),
            }
        },
        platform="qqbot",
        message="无创产品经理是谁",
        history=[],
    )

    assert turn is not None
    assert turn.direct_response == "无创基础版&全因的产品经理是王阳。"
