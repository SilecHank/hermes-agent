"""Real Gateway lifecycle/run entry boundaries; no real model or transport."""
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from gateway.run import GatewayRunner
from tests.gateway.test_ivd_gateway_lifecycle_integration import _runner, _source


def test_trusted_business_body_is_shared_by_package_and_guard():
    runner = _runner();seen=[]
    def package(*args, **kwargs):
        seen.append(('package',kwargs['question'],kwargs.get('business_question')))
        return SimpleNamespace(answer_shape='clarification')
    def guard(*args, **kwargs):
        seen.append(('guard',kwargs['message'],None));return None
    raw='[Gateway sender=时间测试] CNVseq的产品经理是谁'
    with patch('gateway.ivd_runtime.ivd_engine_mode',return_value='package'), patch('gateway.ivd_runtime.execute_exclusive_ivd_turn',side_effect=package), patch('gateway.after_sales_guard.prepare_after_sales_turn',side_effect=guard):
        runner._prepare_ivd_lifecycle({},platform='qqbot',message=raw,history=[],business_message='CNVseq的产品经理是谁')
    assert seen==[('package',raw,'CNVseq的产品经理是谁'),('guard','CNVseq的产品经理是谁',None)]



@pytest.mark.parametrize('body',[
    'CNVseq的产品经理是谁',
    '提取时间参数是多少',
    '原文引用：[Gateway sender=时间测试] 不得删除\n下一行参数',
    '[Gateway sender=时间测试] CNVseq的产品经理是谁',
])
def test_body_characters_are_never_regex_stripped(body):
    runner=_runner();seen=[]
    with patch('gateway.ivd_runtime.ivd_engine_mode',return_value='compatibility'),patch('gateway.after_sales_guard.prepare_after_sales_turn',side_effect=lambda *a,**kw:seen.append(kw) or None):
        runner._prepare_ivd_lifecycle({},platform='qqbot',message=body,history=[])
    assert seen[0]['message']==body


def test_shared_transport_cannot_lend_other_actor_history():
    runner=_runner();seen=[];history=[{'role':'user','content':'NIFTY B3'}]
    with patch('gateway.ivd_runtime.ivd_engine_mode',return_value='compatibility'),patch('gateway.after_sales_guard.prepare_after_sales_turn',side_effect=lambda *a,**kw:seen.append(kw) or None):
        runner._prepare_ivd_lifecycle({},platform='qqbot',message='[shared] body',business_message='body',business_history_isolated=True,history=history)
    assert seen[0]['history']==[]
    assert history==[{'role':'user','content':'NIFTY B3'}]


def test_real_guard_wrapped_contact_matches_bare_without_numeric_metadata():
    import os
    from pathlib import Path
    k=Path(os.environ['IVD_KB_ROOT'])
    guard={'enabled':True,'platforms':['qqbot'],'workflow_module':str(k/'scripts/after_sales_workflow_gate.py'),'validator_module':str(k/'scripts/after_sales_answer_validator.py'),'cards_dir':str(k/'knowledge-base/workflows/facts'),'fast_response_module':str(k/'scripts/hermes_fast_response_pipeline.py')}
    runner=_runner();config={'after_sales_guard':guard};q='CNVseq的产品经理是谁'
    with patch('gateway.ivd_runtime.ivd_engine_mode',return_value='compatibility'):
        _,bare=runner._prepare_ivd_lifecycle(config,platform='qqbot',message=q,history=[])
        _,wrapped=runner._prepare_ivd_lifecycle(config,platform='qqbot',message='[Gateway sender=时间测试] '+q,business_message=q,history=[])
    assert bare.facts==wrapped.facts
    assert bare.direct_response==wrapped.direct_response
    assert bare.blocks_answer_generation==wrapped.blocks_answer_generation
    assert '数值' not in wrapped.direct_response


def test_inbound_producer_keeps_identity_note_separate_from_user_body():
    import asyncio
    runner=_runner();runner.config.identity_aliases={'configured':True}
    runner.config.get_identity_alias=lambda *args:{'display_name':'时间测试'}
    event=SimpleNamespace(text='CNVseq的产品经理是谁',media_urls=[],media_types=[],channel_context=None,reply_to_text=None,reply_to_message_id=None)
    wrapped=asyncio.run(runner._prepare_inbound_message_text(event=event,source=_source(),history=[]))
    assert wrapped.startswith('[Gateway verified sender identity:')
    assert event._gateway_business_text=='CNVseq的产品经理是谁'
    assert not event._gateway_business_history_isolated
