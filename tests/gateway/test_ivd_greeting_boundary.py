import pytest
from tests.gateway.test_ivd_gateway_lifecycle_integration import _runner, _source

@pytest.mark.parametrize('message',['你好','您好！'])
def test_plain_greeting_returns_before_model_or_history_resume(message):
    runner=_runner()
    def forbidden():raise AssertionError('greeting crossed run/model boundary')
    runner._get_proxy_url=forbidden
    import asyncio
    result=asyncio.run(runner._run_agent_inner(message,'old task: list contacts',[{'role':'user','content':'CNVseq的产品经理是谁'}],_source(),'test-session'))
    assert result['api_calls']==0 and result['completed']
    assert '你好' in result['final_response']
    assert '产品经理' not in result['final_response'] and '列表' not in result['final_response']

@pytest.mark.parametrize('message',['你好，CNVseq产品经理是谁','继续','刚才的第二个问题','[Gateway sender=测试] 你好'])
def test_business_or_explicit_continuation_is_not_greeting_shortcut(message):
    import asyncio
    runner=_runner()
    def sentinel():raise RuntimeError('normal_route_reached')
    runner._get_proxy_url=sentinel
    with pytest.raises(RuntimeError,match='normal_route_reached'):
        asyncio.run(runner._run_agent_inner(message,'',[],_source(),'test-session'))
