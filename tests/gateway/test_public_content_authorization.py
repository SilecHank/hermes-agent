"""Content permissions through the actual source/turn/final validation path."""
import copy
import json
import pytest
from gateway.after_sales_guard import CriticalAfterSalesValidator, prepare_after_sales_turn
from agent.final_response_validation import evaluate_final_response
from tests.gateway.test_public_task_boundary import G


def prepared():
    turn = prepare_after_sales_turn({'after_sales_guard': G}, platform='weixin',
        message='为什么乙醇残留会让PCR扩增变差？', history=[])
    record = next(r for r in turn.expert_evidence_bundle['evidence']
                  if r.get('excerpts') and r['tier'] == 'controlled_reference')
    excerpt = record['excerpts'][0]
    claim = {'kind':'hypothesis', 'scope':'general',
        'text':'乙醇等残留可能抑制聚合酶，需结合内参与对照核查。',
        'verification':'核对纯化残留、内参与重复曲线。',
        'boundary':'一般原理，不构成个案归因或报告结论。',
        'evidence_ids':[record['evidence_id']],
        'support':[{'evidence_id':record['evidence_id'], 'quote':excerpt['text'],
                    'start_line':excerpt['start_line'], 'relation':'一般原理解释的支持'}]}
    return turn, {'task_id':turn.task_contract['task_id'], 'claims':[claim]}


def check(turn, draft):
    return evaluate_final_response(CriticalAfterSalesValidator(turn, messages_provider=lambda:[]),
        json.dumps(draft, ensure_ascii=False), attempts=1)


@pytest.mark.parametrize('field', ['text','verification','boundary'])
@pytest.mark.parametrize('content', [
    '该个案结果已经转为正常，可以直接放行报告。',
    'NIFTY建库投入量必须设置为999 ng，并按该值正式放行。',
    '本样本已经可以正式判定阳性，无需复核。',
    '建议重新建库。',
    '不可以放行，但建议重新采血。',
    '不能否认可以出报告。',
    '“禁止放行”是提示；因此可以放行。',
    '不能不放行报告。',
    '不禁止放行报告。',
    '核对后直接放行报告。',
    '检查完成后直接出报告。',
])
def test_general_tags_do_not_authorize_visible_content(field, content):
    turn, draft = prepared()
    draft['claims'][0][field] = content
    result = check(turn, draft)
    assert result.action == 'fallback', (field, content, result)
    assert any(r.startswith('content_authority:') for r in result.reasons)


@pytest.mark.parametrize('boundary', [
    '一般原理，不构成个案归因或报告结论。',
    '不能直接判定当前样本原因或产品放行。',
    '不能据此放行，也不能据此建议重建库。',
    '不要把“可以直接放行报告”作为本次结论。',
    '尚不能确定个案原因。',
])
def test_ordinary_negative_boundaries_preserve_free_explanation(boundary):
    turn, draft = prepared()
    draft['claims'][0]['boundary'] = boundary
    result = check(turn, draft)
    assert result.action == 'accept', result.reasons
    assert draft['claims'][0]['text'] in result.response


def test_one_negative_claim_does_not_authorize_next_claim():
    turn, draft = prepared()
    other = copy.deepcopy(draft['claims'][0])
    other['text'] = '可以直接放行报告。'
    draft['claims'].append(other)
    assert check(turn, draft).action == 'fallback'


@pytest.mark.parametrize('text', [
    '乙醇残留可能影响酶反应；重建库与重分析涉及不同环节，应结合内参与对照核查。',
    '“乙醇等残留可抑制聚合酶”是来源中的机制描述，不能据此断定个案原因。',
    '重新建库和重新采血涉及不同阶段，需核对纯化残留与对照。',
])
def test_free_comparison_and_attributed_quotation_are_not_action_execution(text):
    turn, draft = prepared()
    draft['claims'][0]['text'] = text
    result = check(turn, draft)
    assert result.action == 'accept', result.reasons


@pytest.mark.parametrize('verification', ['核对报告放行条件。','需要核查样本浓度与对应来源。',
                                          '本样本是否受污染仍需核查。','核对完整的报告放行条件。'])
def test_inspecting_a_rule_or_parameter_does_not_authorize_executing_it(verification):
    turn, draft = prepared()
    draft['claims'][0]['verification'] = verification
    assert check(turn, draft).action == 'accept'
    draft['claims'][0]['verification'] = verification.rstrip('。') + '后可以放行报告。'
    assert check(turn, draft).action == 'fallback'


@pytest.mark.parametrize('field', ['text','verification','boundary'])
def test_recovery_returns_and_persists_the_same_rejection(field, monkeypatch):
    from agent.turn_finalizer import finalize_turn
    from tests.agent.test_turn_finalizer_final_response_persistence import FakeAgent
    monkeypatch.setattr('hermes_cli.plugins.invoke_hook', lambda *_a, **_kw: [])
    turn, draft = prepared()
    forbidden = '投入量必须设置为999 ng，可以直接放行报告。'
    draft['claims'][0][field] = forbidden
    raw = json.dumps(draft, ensure_ascii=False)
    messages = [{'role':'user','content':'PCR原理问题'}, {'role':'assistant','content':raw}]
    agent = FakeAgent()
    trajectories = []
    agent._save_trajectory = lambda *a, **kw: trajectories.append(copy.deepcopy((a, kw)))
    agent._final_response_validator = CriticalAfterSalesValidator(turn, messages_provider=lambda:messages)
    result = finalize_turn(agent, final_response=raw, api_call_count=0, interrupted=False,
        failed=False, messages=messages, conversation_history=[], effective_task_id='task',
        turn_id='turn', user_message='PCR原理问题', original_user_message='PCR原理问题',
        _should_review_memory=False, _turn_exit_reason='recovery')
    assert forbidden not in str(result) + str(agent.persisted_messages) + str(trajectories)
    assert '停止发送' in result['final_response']
    assert agent.persisted_messages[-1]['content'] == result['final_response']
