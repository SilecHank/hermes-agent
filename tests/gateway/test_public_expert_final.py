"""Real planner -> source reads -> turn -> pre-persistence final validator."""
import json
from gateway.after_sales_guard import CriticalAfterSalesValidator, prepare_after_sales_turn
from agent.final_response_validation import evaluate_final_response
from tests.gateway.test_public_task_boundary import G


def decide_final_response(response, validator, **kwargs):
    return evaluate_final_response(validator, response, **kwargs)


def turn():
    return prepare_after_sales_turn({'after_sales_guard': G}, platform='weixin',
        message='NIFTY为什么磁珠过干影响回收', history=[])


def test_real_expert_turn_can_progress_with_bounded_analysis_and_reject_fact():
    t = turn()
    contract = getattr(t, 'expert_route_contract', None)
    assert contract and contract['status'] == 'ready', 'Real contract must survive turn creation'
    assert t.has_validator and not t.blocks_answer_generation
    bundle = t.expert_evidence_bundle
    record = next(r for r in bundle['evidence'] if r.get('excerpts') and r['tier'] == 'controlled_reference')
    excerpt = record['excerpts'][0]
    # Sentinel replaces the external model boundary, not planner/source/validator.
    calls = []
    def model_sentinel(context):
        calls.append(context)
        supplied = json.loads(context.split('[本轮专家声明合同与实际摘录]\n', 1)[1].splitlines()[0])
        assert record in supplied['bundle']['evidence']
        assert supplied['task_id'] == t.task_contract['task_id']
        return {'task_id': t.task_contract['task_id'], 'claims': [{
            'kind': 'hypothesis', 'scope': 'general',
            'text': '表面水化状态可能影响核酸洗脱表现。',
            'verification': '对比复溶前后的回收表现。',
            'boundary': '这是一般原理推断，不能直接判定当前样本原因或产品放行。',
            'evidence_ids': [record['evidence_id']],
            'support': [{'evidence_id': record['evidence_id'], 'quote': excerpt['text'],
                         'start_line': excerpt['start_line'],
                         'relation': '用于提出需验证的一般原理假设，不构成个案结论。'}]}]}
    draft = model_sentinel(t.context)
    validator = CriticalAfterSalesValidator(t, messages_provider=lambda: [])
    result = decide_final_response(json.dumps(draft, ensure_ascii=False), validator, attempts=1)
    assert result.action == 'accept'
    assert '原理推断' in result.response and '核查' in result.response
    assert not result.response.lstrip().startswith('{')
    assert len(calls) == 1
    draft['claims'][0]['kind'] = 'fact'
    bad = decide_final_response(json.dumps(draft), validator, attempts=1)
    assert bad.action == 'fallback'
    assert 'claim_kind_not_allowed' in bad.reasons


def test_free_prose_and_wrong_task_cannot_bypass_expert_validator():
    t = turn()
    assert getattr(t, 'expert_route_contract', None), 'Planner contract was lost'
    validator = CriticalAfterSalesValidator(t, messages_provider=lambda: [])
    assert decide_final_response('因此这个产品必须按200处理。', validator, attempts=1).action == 'fallback'
    wrong = {'task_id': 'old-task', 'claims': []}
    assert decide_final_response(json.dumps(wrong), validator, attempts=1).action == 'fallback'


def test_recovery_finalizer_cannot_persist_unvalidated_prose(monkeypatch):
    from agent.turn_finalizer import finalize_turn
    from tests.agent.test_turn_finalizer_final_response_persistence import FakeAgent
    monkeypatch.setattr('hermes_cli.plugins.invoke_hook', lambda *_a, **_kw: [])
    agent = FakeAgent()
    raw = '未经来源支持的产品结论：按200放行。'
    messages = [{'role': 'user', 'content': '检测问题'}, {'role': 'assistant', 'content': raw}]
    agent._final_response_validator = CriticalAfterSalesValidator(turn(), messages_provider=lambda: messages)
    result = finalize_turn(agent, final_response=raw, api_call_count=1, interrupted=False,
        failed=False, messages=messages, conversation_history=[], effective_task_id='task',
        turn_id='turn', user_message='检测问题', original_user_message='检测问题',
        _should_review_memory=False, _turn_exit_reason='recovery')
    assert result['final_response'] != raw
    assert all(m.get('content') != raw for m in agent.persisted_messages)
