"""Behavioral cross-path presentation and delivery invariants."""
import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from gateway.after_sales_guard import (
    AfterSalesTurn, _render_fast_response_context,
    build_direct_fact_result, build_preflight_block_result,
)
from gateway.config import PlatformConfig
from gateway.platforms.weixin import _wrap_copy_friendly_lines_for_weixin
from plugins.platforms.wecom.adapter import WeComAdapter


def _policy(tmp_path):
    p = tmp_path / 'scripts'
    p.mkdir()
    (p / 'hermes_channel_presentation.py').write_text(
        "calls=[]\n"
        "def presentation_contract(platform,detail_requested=False,task_type='general'):\n"
        " return {'version':'test-policy','platform':platform,'task_type':task_type}\n"
        "def render_presentation_instructions(c): return 'POLICY:'+c['platform']\n"
        "def prepare_user_text(text,c): calls.append(c); return text\n"
    )
    fast = p / 'hermes_fast_response_pipeline.py'
    fast.write_text(
        "def build_fast_response_plan(message,question_type='general',platform='generic'):\n"
        " return {'runtime_preflight':{'eligible':True},'answer_contract':{'task_kind':'compare'},'fast_path':{},'answer_shape':{}}\n"
    )
    return str(fast)


def test_model_prompt_consumes_actual_platform_release_policy(tmp_path):
    module = _policy(tmp_path)
    result = _render_fast_response_context({'fast_response_module':module},
                                          message='比较流程',match=None,platform='wecom')
    assert 'POLICY:wecom' in result['context']
    assert result['presentation_contract']['version'] == 'test-policy'


def test_direct_and_clarification_share_lossless_policy(tmp_path):
    module = _policy(tmp_path)
    turn = AfterSalesTurn(context='',facts={},validator=None,allowed_numeric_claims=(),
                         direct_response='条件：≥ 10 μL；按 SOP-JL-181 A5。',
                         presentation_contract={'version':'test-policy','platform':'wecom'},
                         presentation_module_path=str(Path(module).with_name('hermes_channel_presentation.py')))
    assert build_direct_fact_result(turn,'参数')['final_response'] == turn.direct_response
    assert build_preflight_block_result(turn,'参数')['presentation_contract']['version'] == 'test-policy'
    from gateway.after_sales_guard import _load_module
    policy_path=Path(turn.presentation_module_path)
    loaded=_load_module(str(policy_path),policy_path.stat().st_mtime_ns)
    assert len(loaded.calls)==2


def test_weixin_does_not_insert_newline_inside_value_unit_or_sop_version():
    for tail in ('10 μL','SOP-JL-181 A5'):
        original = 'A' * 116 + ' ' + tail
        formatted = _wrap_copy_friendly_lines_for_weixin(original)
        assert tail in formatted
        assert ' '.join(formatted.split()) == ' '.join(original.split())


def test_wecom_long_reply_and_proactive_send_preserve_tail():
    async def run(reply):
        a = WeComAdapter(PlatformConfig(enabled=True,extra={'bot_id':'test','secret':'test'}))
        a._send_reply_request = AsyncMock(return_value={'headers':{'req_id':'reply'},'body':{'errcode':0}})
        a._send_request = AsyncMock(return_value={'headers':{'req_id':'test'},'body':{'errcode':0}})
        if reply:
            a._last_chat_req_ids['test-chat']='reply-id'
        text = '已核实正文。' * 900 + '适用条件、单位及来源在末尾。'
        result = await a.send('test-chat',text)
        payloads = [call.args[1]['markdown']['content'] for call in [*a._send_reply_request.call_args_list, *a._send_request.call_args_list]]
        assert result.success
        assert len(payloads) > 1
        assert all(len(chunk) <= a.MAX_MESSAGE_LENGTH for chunk in payloads)
        assert ''.join(payloads) == text
    asyncio.run(run(False))
    asyncio.run(run(True))


def test_package_scalar_and_clarification_consume_release_policy(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from tests.gateway.test_ivd_exclusive_runtime import _prepared_package_turn
    from gateway.ivd_runtime import execute_exclusive_ivd_turn
    from gateway.after_sales_guard import _load_module
    prepared = _prepared_package_turn(tmp_path)
    fast = _policy(tmp_path)
    state = {'clarify':False}
    class Engine:
        def __init__(self,*args,**kwargs): pass
        def close(self): pass
    class Dispatcher:
        def __init__(self,*args): pass
        def execute(self,*args,**kwargs):
            result = None if state['clarify'] else SimpleNamespace(
                text='200 uL.',answer_shape='scalar',outcome='answer',model_calls=0,
                index_transactions=0,filesystem_scans=0,effect_count=0,sources=())
            if result is not None and kwargs.get('presentation_formatter'):
                result.text = kwargs['presentation_formatter'](result.text,result.sources)
            return SimpleNamespace(envelope=SimpleNamespace(clarifying_questions=('请确认产品。',),
                model_call_budget=0,indexed_retrieval_budget=0),result=result)
    monkeypatch.setattr('gateway.ivd_runtime.IVDKnowledgeEngine',Engine)
    monkeypatch.setattr('gateway.ivd_runtime.IVDDispatcher',Dispatcher)
    for clarify in (False,True):
        state['clarify']=clarify
        answer=execute_exclusive_ivd_turn(prepared,question='参数',platform='weixin',fast_response_module=fast)
        assert answer.presentation_contract['version']=='test-policy'
        assert answer.model_calls==0
        assert answer.text == ('请确认产品。' if clarify else '200 uL.')
    path=Path(fast).with_name('hermes_channel_presentation.py')
    module=_load_module(str(path),path.stat().st_mtime_ns)
    assert len(module.calls)==2
    assert all(c['platform']=='weixin' for c in module.calls)


import pytest
@pytest.mark.asyncio
async def test_actual_gateway_model_constructor_receives_turn_policy(tmp_path,monkeypatch):
    from tests.gateway.test_ivd_gateway_lifecycle_integration import (
        _runner,_source,_write_projection,_install_runtime,
    )
    observed=[]
    class Agent:
        def __init__(self,**kwargs):
            observed.append(kwargs.get('ephemeral_system_prompt',''))
            self.tools=[]
            self._session_messages=[]
        def run_conversation(self,message,**kwargs):
            return {'final_response':'已说明当前条件。','messages':[],'api_calls':1}
    config={'after_sales_guard':{'enabled':True,'platforms':['qqbot'],
        'engine_mode':'compatibility','serving_projection_path':str(_write_projection(tmp_path)),
        'fast_response_module':_policy(tmp_path)}}
    _install_runtime(monkeypatch,config,Agent)
    result=await _runner(config)._run_agent('比较流程','',[],_source(),'session',session_key='qqbot:123',run_generation=42)
    assert len(observed)==1
    assert 'POLICY:qqbot' in observed[0]
    assert result['final_response']=='已说明当前条件。'
    assert result['api_calls']==1


@pytest.mark.parametrize("invalid", ["fast_path", "changed body", "200 uL. 900 mL"])
def test_direct_formatter_invalid_output_is_blocked_without_model(tmp_path,invalid):
    from types import SimpleNamespace
    import re
    module = _policy(tmp_path)
    path=Path(module).with_name("hermes_channel_presentation.py")
    path.write_text("def prepare_user_text(text,contract): return " + repr(invalid) + "\n")
    validator=SimpleNamespace(extract_numeric_claims=lambda text: re.findall(r"\d+\s*(?:uL|mL)",text))
    turn=AfterSalesTurn(context='',facts={},validator=validator,allowed_numeric_claims=(),
        direct_response='200 uL.',presentation_contract={'version':'test-policy','verified_sources':[{'document':'SOP-1'}]},
        presentation_module_path=str(path))
    result=build_direct_fact_result(turn,'参数')
    assert result['preflight_blocked']
    assert not result['verified_fact_reused']
    assert result['api_calls']==0
    assert result['final_response'] != invalid
    assert not result['presentation_validation']['ok']


@pytest.mark.parametrize("old_or_invalid", ["old", "invalid"])
def test_older_or_invalid_display_policy_keeps_domain_preflight(tmp_path,old_or_invalid):
    from gateway.after_sales_guard import prepare_after_sales_turn
    fast=_policy(tmp_path)
    path=Path(fast).with_name("hermes_channel_presentation.py")
    if old_or_invalid=="old":
        path.write_text("def presentation_contract(platform,detail_requested=False): return {}\n")
    else:
        path.write_text("def presentation_contract(*a,**k): raise ValueError('invalid')\n"
                        "def render_presentation_instructions(c): return ''\n"
                        "def prepare_user_text(text,c): return text\n")
    Path(fast).write_text("def build_fast_response_plan(message,question_type='general'):\n"
        " return {'runtime_preflight':{'eligible':True},'answer_shape':{'answer_shape':'scalar_lookup'},"
        "'expert_route_contract':{'status':'pending_evidence','formal_answer_allowed':False,'bounded_expert_answer_allowed':False}}\n")
    turn=prepare_after_sales_turn({'after_sales_guard':{'enabled':True,'platforms':['qqbot'],
        'fast_response_module':fast}},platform='qqbot',message='参数是多少',history=[])
    assert turn is not None
    assert turn.blocks_answer_generation
    assert '未经核实' in turn.direct_response
    assert not turn.presentation_contract

@pytest.mark.parametrize('old_or_invalid',['old','invalid'])
def test_compatible_card_without_new_display_api_keeps_valid_direct_answer(tmp_path,old_or_invalid):
    from gateway.after_sales_guard import prepare_after_sales_turn
    fast=_policy(tmp_path)
    path=Path(fast).with_name('hermes_channel_presentation.py')
    if old_or_invalid=='old':
        path.write_text("def presentation_contract(platform,detail_requested=False): return {}\n")
    else:
        path.write_text("def presentation_contract(*a,**k): raise ValueError('invalid')\n"
                        "def render_presentation_instructions(c): return ''\n"
                        "def prepare_user_text(text,c): raise ValueError('invalid')\n")
    plan={'runtime_preflight':{'eligible':True},'answer_shape':{'answer_shape':'scalar_lookup'},
          'fast_path':{'answer_shape':'sop_parameter_short_answer','answer_card':{
              'answer_text':'200 uL.','source_document_id':'SOP-JL-001','source_version':'A5',
              'source_locator':'line 1','stop_after_fast_path':True,'knowledge_kind':'parameter',
              'product_scope':'NIFTY'}}}
    Path(fast).write_text("def build_fast_response_plan(message,question_type='general'):\n return "+repr(plan)+"\n")
    turn=prepare_after_sales_turn({'after_sales_guard':{'enabled':True,'platforms':['qqbot'],
        'fast_response_module':fast}},platform='qqbot',message='无创提取参数',history=[])
    result=build_direct_fact_result(turn,'无创提取参数')
    assert not turn.presentation_contract
    assert result['final_response']=='200 uL.'
    assert not result['preflight_blocked']
    assert result['api_calls']==0


def test_blocked_expert_contract_preserves_specific_clarification():
    turn = AfterSalesTurn(context='', facts={}, validator=None, allowed_numeric_claims=(),
        preflight_decision='block', preflight_issues=('expert_route_contract_not_ready',),
        direct_response='已确认产品与版本；需要核对对应受控来源。')
    result = build_preflight_block_result(turn, '已指定产品版本的问题')
    assert result['final_response'] == turn.direct_response
    assert result['api_calls'] == 0
