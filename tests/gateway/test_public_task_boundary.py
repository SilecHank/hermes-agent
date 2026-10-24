"""Real bound planner/workflow entry; no preloaded KB finder or model."""
import os
import time
from pathlib import Path
from gateway.after_sales_guard import prepare_after_sales_turn, resolve_after_sales_task

K = Path(os.environ['IVD_KB_ROOT'])
G = {'enabled': True, 'platforms': ['weixin'],
     'workflow_module': str(K/'scripts/after_sales_workflow_gate.py'),
     'validator_module': str(K/'scripts/after_sales_answer_validator.py'),
     'cards_dir': str(K/'knowledge-base/workflows/facts'),
     'fast_response_module': str(K/'scripts/hermes_fast_response_pipeline.py')}
OLD = '无创样本第一次XO（+++），这次XO（+），可能什么原因'


def test_real_guard_does_not_inherit_business_for_new_intent():
    for question, intent in [('你好', 'social'), ('NIFTY性染色体分析SOP在哪里', 'sop_lookup'),
                             ('换个话题。NIFTY排查流程为什么采用非富集子文库验证', 'mechanism')]:
        turn = prepare_after_sales_turn({'after_sales_guard': G}, platform='weixin',
            message=question, history=[{'role': 'user', 'content': OLD}])
        assert not turn.facts
        assert turn.task_contract['intent'] == intent
        assert turn.task_contract['admitted_history_fields'] == []
        if intent == 'social':
            assert not turn.blocks_answer_generation


def test_live_actor_pending_is_the_only_short_reply_context_authority():
    task = resolve_after_sales_task(G, 'NIFTY血浆提取体积是多少', actor_id='a')
    pending = {'task': task, 'question': task['question'], 'actor_id': 'a',
               'status': 'awaiting_reply', 'expires_at': time.time()+300,
               'missing_fields': ['product_version']}
    turn = prepare_after_sales_turn({'after_sales_guard': G}, platform='weixin',
        message='V5', history=[], actor_id='a', pending_task=pending)
    assert turn.task_contract['task_id'] == task['task_id']
    assert turn.task_contract['continuation'] == 'clarification_reply'
    other = prepare_after_sales_turn({'after_sales_guard': G}, platform='weixin',
        message='V5', history=[], actor_id='b', pending_task=pending)
    assert other.task_contract['continuation'] == 'new'
