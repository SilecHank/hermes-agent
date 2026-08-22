from __future__ import annotations

from pathlib import Path

from gateway.after_sales_guard import prepare_after_sales_turn


def test_formal_parameter_card_becomes_generic_resolved_answer(tmp_path: Path):
    fast_module = tmp_path / "fast_response.py"
    fast_module.write_text(
        """
def build_fast_response_plan(question, question_type='general'):
    return {
        'workflow': 'qc-troubleshooting.md',
        'fast_path': {
            'hit': True,
            'answer_shape': 'sop_parameter_short_answer',
            'route_source': 'formal_answer_card',
                'answer_card': {
                'fact_key': 'plasma_input',
                'knowledge_kind': 'parameter',
                'answer_text': '200 uL',
                'source_path': 'knowledge-base/formal.md',
                'source_document_id': 'SOP-JL-269',
                'source_version': 'A5',
                'source_locator': 'line 1',
                'product_scope': 'NIFTY',
                    'product_variant': '',
                    'stop_after_fast_path': True,
            },
        },
        'initial_files': [],
        'runtime_preflight': {'eligible': True, 'route_version': 'test'},
        'answer_template': {},
        'answer_contract': {},
        'answer_shape': {'answer_shape': 'scalar_lookup'},
    }
""",
        encoding="utf-8",
    )
    turn = prepare_after_sales_turn(
        {
            'after_sales_guard': {
                'enabled': True,
                'platforms': ['qqbot'],
                'fast_response_module': str(fast_module),
            }
        },
        platform='qqbot',
        message='无创提取需要多少血浆',
        history=[],
    )

    assert turn is not None
    assert turn.resolved_answer is not None
    assert turn.resolved_answer.text == '200 uL'
    assert turn.resolved_answer.source_kind == 'formal_answer_card'
