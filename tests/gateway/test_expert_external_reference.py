"""Public Gateway expert fallback with an executor substitute."""
import pytest
from tests.gateway.test_auto_expert_minimal_release import package_config
from tests.gateway.test_ivd_gateway_lifecycle_integration import _runner,_source,_install_runtime

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['compatibility','hybrid','package'])
@pytest.mark.parametrize('kind',['hypothesis','fact','numeric_directive'])
async def test_expert_fallback_delivers_the_model_output(monkeypatch,tmp_path,mode,kind):
    calls=[]
    question='NIFTY为什么无结果需要复核'
    class Agent:
        def __init__(self,**kwargs):
            self.tools=[];self._session_messages=[]
            self.context=kwargs.get('ephemeral_system_prompt') or ''
        def run_conversation(self,message,**kwargs):
            calls.append(message)
            assert question in message
            assert '[本轮专家声明合同与实际摘录]' not in self.context
            answer={'hypothesis':'可以结合外部参考分析筛查范围。',
                    'fact':'先结合检测项目和报告内容分析。',
                    'numeric_directive':'示例数值为 200 uL。'}[kind]
            return {'final_response':answer,'messages':[{'role':'assistant','content':answer}],'api_calls':1,'completed':True}
    config=package_config(tmp_path,mode)
    _install_runtime(monkeypatch,config,Agent)
    runner=_runner(config);runner._get_proxy_url=lambda:None
    result=await runner._run_agent(question,'',[],_source(),'external-reference-session')
    assert len(calls)==1
    expected={'hypothesis':'可以结合外部参考分析筛查范围。',
              'fact':'先结合检测项目和报告内容分析。',
              'numeric_directive':'示例数值为 200 uL。'}[kind]
    assert result['final_response']==expected
    assert result['messages'][-1]['content']==result['final_response']
