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


def test_explicit_current_request_is_not_a_pending_field_reply():
    task = resolve_after_sales_task(G, 'NIFTY提取体积是多少', actor_id='a')
    pending = {'task':task,'actor_id':'a','status':'awaiting_reply',
               'expires_at':time.time()+300,'missing_fields':['product_version']}
    for question in ['B3，发送SOP','B3，给我文件','B3，帮我解释磁珠过干原理']:
        turn = prepare_after_sales_turn({'after_sales_guard':G},platform='weixin',
            message=question,history=[],actor_id='a',pending_task=pending)
        assert turn.task_contract['task_id'] != task['task_id']
        assert turn.task_contract['question'] == question
        assert turn.task_contract['continuation'] == 'new'


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


def test_real_lifecycle_records_and_consumes_same_actor_pending_version():
    from gateway.run import GatewayRunner
    class MetadataStore:
        value = None
        def get_session_metadata(self, session_key, key):
            assert (session_key, key) == ('fixture-chat', 'ivd_pending_task')
            return self.value
        def set_session_metadata(self, session_key, key, value):
            self.value = value
            return True
    runner = object.__new__(GatewayRunner)
    runner.session_store = MetadataStore()
    runner._ivd_prepared_contracts = {}
    # A report-interpretation question still gates on the case fields, so it is
    # the right lifecycle fixture for the version hand-off.  The "what could
    # explain this" wording answers under a bounded contract instead.
    blocking = '无创样本第一次XO（+++），这次XO（+），报告该怎么判读，浓度差多少算异常'
    _, first = runner._prepare_ivd_lifecycle({'after_sales_guard': G}, platform='weixin',
        message=blocking, history=[], actor_id='actor-a', session_key='fixture-chat')
    assert first.blocks_answer_generation
    assert 'product_version' in runner.session_store.value['missing_fields']
    _, second = runner._prepare_ivd_lifecycle({'after_sales_guard': G}, platform='weixin',
        message='B3', history=[], actor_id='actor-a', session_key='fixture-chat')
    assert second.task_contract['task_id'] == first.task_contract['task_id']
    assert second.task_contract['continuation'] == 'clarification_reply'
    assert second.task_contract['context']['product_version'] == 'B3'
    assert second.blocks_answer_generation  # Case comparability fields still missing.
    _, isolated = runner._prepare_ivd_lifecycle({'after_sales_guard': G}, platform='weixin',
        message='B3', history=[], actor_id='actor-a', session_key='fixture-chat',
        business_history_isolated=True)
    assert isolated.task_contract['continuation'] == 'new'
    _, third = runner._prepare_ivd_lifecycle({'after_sales_guard': G}, platform='weixin',
        message='CNV-seq的产品经理是谁', history=[], actor_id='actor-a', session_key='fixture-chat')
    assert not third.blocks_answer_generation and third.direct_response
    assert runner.session_store.value is None


def test_bounded_answer_carries_the_open_collection_to_the_follow_up():
    """A bounded answer must not drop the open case collection.

    The mechanism answer does not gate the reply, but the collection it belongs
    to is still open.  Without carry-over the next supplement is re-resolved
    from a bare sentence: the product reads as unknown and the markers that
    were already supplied look missing again.
    """
    from gateway.run import GatewayRunner
    class MetadataStore:
        value = None
        def get_session_metadata(self, session_key, key):
            return self.value
        def set_session_metadata(self, session_key, key, value):
            self.value = value
            return True
    runner = object.__new__(GatewayRunner)
    runner.session_store = MetadataStore()
    runner._ivd_prepared_contracts = {}

    _, first = runner._prepare_ivd_lifecycle({'after_sales_guard': G}, platform='weixin',
        message=OLD, history=[], actor_id='actor-a', session_key='fixture-chat')
    assert not first.blocks_answer_generation
    pending = runner.session_store.value
    assert pending and pending['missing_fields']

    _, second = runner._prepare_ivd_lifecycle({'after_sales_guard': G}, platform='weixin',
        message='两次同次采血，这次重建库', history=[], actor_id='actor-a',
        session_key='fixture-chat')
    assert second.task_contract['continuation'] in {'clarification_reply', 'field_patch'}
    assert second.task_contract['subject'] == 'NIFTY'
    missing = set(second.facts['clarification']['missing'])
    assert not missing.intersection({'sample_relation', 'repeat_mode', 'first_result', 'repeat_result'})

    _, third = runner._prepare_ivd_lifecycle({'after_sales_guard': G}, platform='weixin',
        message='X浓度 7.4%，Y浓度 0.3%，女胎', history=[], actor_id='actor-a',
        session_key='fixture-chat')
    assert set(third.facts['clarification']['missing']) <= {'result_field'}
