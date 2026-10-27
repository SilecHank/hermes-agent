"""Actual local literature -> public Gateway -> final permission checks; executor substitute."""
import json
import pytest
from tests.gateway.test_auto_expert_minimal_release import package_config
from tests.gateway.test_ivd_gateway_lifecycle_integration import _runner,_source,_install_runtime

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['compatibility','hybrid','package'])
@pytest.mark.parametrize('kind',['hypothesis','fact','numeric_directive'])
async def test_external_source_supports_bounded_output_without_formal_permission(monkeypatch,tmp_path,mode,kind):
    calls=[]
    question='NIFTY为什么无结果需要复核'
    class Agent:
        def __init__(self,**kwargs):
            self.tools=[];self._session_messages=[]
            self.context=kwargs.get('ephemeral_system_prompt') or ''
        def run_conversation(self,message,**kwargs):
            calls.append(message)
            assert question in message
            supplied=json.loads(self.context.split('[本轮专家声明合同与实际摘录]\n',1)[1].splitlines()[0])
            contract=supplied['contract']
            assert contract['evidence_tier']=='external_reference'
            assert not contract['formal_answer_allowed']
            assert not contract['answer_registry_read_allowed']
            assert not contract['diagnostic_graph_read_allowed']
            record=next(r for r in supplied['bundle']['evidence'] if r['tier']=='external_reference' and r.get('excerpts'))
            excerpt=record['excerpts'][0]
            claim={'kind':'fact' if kind=='fact' else 'hypothesis','scope':'general',
                'text':'外部文献可用于理解筛查局限，当前情况仍需核实适用范围。',
                'verification':'核实当前检测的适用范围和分析流程。',
                'boundary':'这是外部参考推断，不能判定当前个案结论或替代正式流程。',
                'evidence_ids':[record['evidence_id']],
                'support':[{'evidence_id':record['evidence_id'],'quote':excerpt['text'],
                            'start_line':excerpt['start_line'],'relation':'用于提出需要核实的筛查范围问题。'}]}
            if kind=='numeric_directive':claim['text']='投入量必须设置为200 uL。'
            answer=json.dumps({'task_id':supplied['task_id'],'claims':[claim]},ensure_ascii=False)
            return {'final_response':answer,'messages':[{'role':'assistant','content':answer}],'api_calls':1,'completed':True}
    config=package_config(tmp_path,mode)
    _install_runtime(monkeypatch,config,Agent)
    runner=_runner(config);runner._get_proxy_url=lambda:None
    result=await runner._run_agent(question,'',[],_source(),'external-reference-session')
    assert len(calls)==1
    if kind=='hypothesis':
        assert '外部参考推断' in result['final_response']
        assert '核查' in result['final_response']
        assert not result['final_response'].lstrip().startswith('{')
    else:
        assert '停止发送' in result['final_response']
        assert '200 uL' not in result['final_response']
    assert result['messages'][-1]['content']==result['final_response']
