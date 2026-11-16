"""Keep unserved questions flowing to expert analysis, not boilerplate."""
import os
from pathlib import Path

import pytest
from gateway.after_sales_guard import prepare_after_sales_turn


def prepare(question, platform="weixin"):
    root_value = os.environ.get('IVD_KB_ROOT')
    if not root_value:
        pytest.skip('requires an explicitly bound KnowledgeHub checkout')
    root = Path(root_value)
    guard = {'enabled': True, 'platforms': ['weixin', 'wecom', 'qqbot', 'telegram'],
             'workflow_module': str(root / 'scripts/after_sales_workflow_gate.py'),
             'validator_module': str(root / 'scripts/after_sales_answer_validator.py'),
             'cards_dir': str(root / 'knowledge-base/workflows/facts'),
             'fast_response_module': str(root / 'scripts/hermes_fast_response_pipeline.py')}
    return prepare_after_sales_turn({'after_sales_guard': guard}, platform=platform,
                                    message=question, history=[])


@pytest.mark.parametrize('question', [
    '建库浓度低是什么原因',
    'PTseq-TB 报告的耐药结果如何解读',
    'NIFTY结果阈值是多少',
    'tNGS 建库投入量是多少',
    'NIFTY为什么磁珠过干影响回收',
    'NIFTY提取需要多少血浆',
])
def test_unserved_business_question_reaches_expert_without_canned_response(question):
    turn = prepare(question)
    assert turn is not None
    assert turn.route_id == 'expert_mode_auto_fallback'
    assert not turn.blocks_answer_generation
    assert not turn.direct_response
    assert not turn.has_validator


def test_social_messages_do_not_enter_business_fallback():
    for question in ('你好', '谢谢你', '你好，今天怎么样？'):
        turn = prepare(question)
        assert turn is not None and not turn.blocks_answer_generation
        assert not turn.has_validator and not turn.direct_response
        assert turn.task_contract.get('intent') == 'social'


def test_existing_formal_scalar_answer_keeps_its_source_validator():
    turn = prepare('NIFTY手工提取血浆体积是多少')
    assert turn is not None and not turn.blocks_answer_generation
    assert turn.requires_source_validation and turn.has_validator and turn.direct_response


@pytest.mark.parametrize(('question', 'draft'), [
    ('建库浓度低是什么原因', '可以从实验步骤、样本状态和同批结果三个方向分析。'),
    ('PTseq-TB 报告的耐药结果如何解读', '可以先梳理报告中的检测对象和结果，再结合相应信息解释。'),
    ('NIFTY结果阈值是多少', '需要结合具体指标理解阈值；可以先从指标含义和适用条件分析。'),
    ('NIFTY为什么磁珠过干影响回收', '可以从磁珠状态、洗脱和同批对照几个方面分析。'),
    ('tNGS 建库投入量是多少', '不同建库方案的投入量可能不同，可以按常见方案分别理解。'),
])
def test_model_loop_delivers_expert_answer_not_canned_clarification(question, draft):
    from tests.gateway.test_synthetic_adapter_final_exit import _agent
    turn = prepare(question)
    assert turn.route_id == 'expert_mode_auto_fallback' and not turn.has_validator
    outputs, requests = [draft], []
    agent = _agent(outputs, requests)
    agent._final_response_validator = None
    result = agent.run_conversation(question, system_message='SYNTHETIC_TEST_ONLY\n' + turn.context,
                                    task_id=turn.task_contract['task_id'])
    assert result['final_response'] == draft
    assert '证据不足' not in result['final_response']
    assert '尚未核实' not in result['final_response']
    assert len(requests) == 1 and not outputs


def test_expert_prompt_does_not_require_a_clarification_first():
    turn = prepare('tNGS 建库投入量是多少')
    assert turn.route_id == 'expert_mode_auto_fallback'
    assert '先追问最少关键条件' not in turn.context
    assert '缺正式参数依据继续澄清' not in turn.context


def test_planner_error_does_not_force_an_evidence_insufficient_reply(tmp_path, monkeypatch):
    from gateway import after_sales_guard
    path = tmp_path / 'hermes_fast_response_pipeline.py'
    path.write_text('# synthetic planner', encoding='utf-8')
    monkeypatch.setattr(after_sales_guard, '_load_module',
                        lambda *_args: (_ for _ in ()).throw(OSError('synthetic read failure')))
    result = after_sales_guard._render_fast_response_context(
        {'fast_response_module': str(path)}, message='建库浓度低是什么原因', match=None)
    assert result.get('route_id') == 'expert_mode_auto_fallback'
    assert not result.get('direct_response')
    assert result.get('preflight_decision') != 'block'
    assert '[专家模式]' in result.get('context', '')


@pytest.mark.parametrize('platform', ['weixin', 'wecom', 'qqbot', 'telegram'])
@pytest.mark.parametrize('question', ['建库浓度低是什么原因', 'NIFTY提取需要多少血浆'])
def test_social_and_expert_fallback_share_behavior_across_channels(platform, question):
    greeting = prepare('你好', platform=platform)
    assert greeting is not None and not greeting.blocks_answer_generation
    assert not greeting.has_validator and not greeting.direct_response
    assert greeting.task_contract.get('intent') == 'social'
    turn = prepare(question, platform=platform)
    assert turn is not None and turn.route_id == 'expert_mode_auto_fallback'
    assert not turn.blocks_answer_generation and not turn.has_validator
    assert not turn.direct_response
