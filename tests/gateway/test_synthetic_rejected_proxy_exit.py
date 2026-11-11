import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch
from tests.gateway.test_proxy_mode import _make_runner, _make_source, _FakeSession, _FakeSSEResponse

class SyntheticTurn:
    dispatch_count=0
    blocks_answer_generation=False
    direct_response=''
    context='合成上下文'
    has_validator=True
    expert_route_contract={'status':'ready'}
    def validate(self, answer, **_kwargs):
        return {'ok':False, 'reasons':['expert_claims_json_invalid'], 'fallback':'来源未知。'}

def test_proxy_does_not_return_or_stream_a_rejected_format_draft():
    draft='SYNTHETIC_REJECTED_999'
    chunk='data: '+json.dumps({'choices':[{'delta':{'content':draft}}]})+'\n\ndata: [DONE]\n\n'
    session=_FakeSession(_FakeSSEResponse(sse_chunks=[chunk]))
    runner=_make_runner()
    with patch('gateway.run._load_gateway_config',return_value={}), \
         patch('gateway.ivd_runtime.ivd_engine_mode',return_value='compatibility'), \
         patch.object(runner,'_prepare_ivd_lifecycle',return_value=(None,SyntheticTurn())), \
         patch.object(runner,'_get_proxy_url',return_value='https://example.invalid'), \
         patch('aiohttp.ClientSession',return_value=session), \
         patch('gateway.stream_consumer.GatewayStreamConsumer') as consumer:
        result=asyncio.run(runner._run_agent_via_proxy(message='合成问题',context_prompt='',history=[],
                           source=_make_source(),session_id='synthetic-session'))
    consumer.assert_not_called()
    assert result['final_response']=='来源未知。'
    assert all(draft not in str(m.get('content','')) for m in result['messages'])

def test_gateway_adapter_sends_only_proxy_validated_fallback(monkeypatch):
    from unittest.mock import AsyncMock
    from tests.gateway.test_active_session_text_merge import _make_adapter, _make_event
    from gateway.platforms.base import SendResult
    from gateway.session import build_session_key
    draft='SYNTHETIC_REJECTED_999'
    chunk='data: '+json.dumps({'choices':[{'delta':{'content':draft}}]})+'\n\ndata: [DONE]\n\n'
    session=_FakeSession(_FakeSSEResponse(sse_chunks=[chunk]))
    runner=_make_runner()
    adapter=_make_adapter()
    adapter.config.typing_indicator=False
    adapter._run_processing_hook=AsyncMock()
    adapter.send=AsyncMock(return_value=SendResult(success=True,message_id='synthetic'))
    event=_make_event('合成问题')
    async def handler(_event):
        result=await runner._run_agent_via_proxy(message='合成问题',context_prompt='',history=[],
                            source=_event.source,session_id='synthetic-session')
        assert draft not in str(result['messages'])
        return result['final_response']
    adapter._message_handler=handler
    with patch('gateway.run._load_gateway_config',return_value={}), \
         patch('gateway.ivd_runtime.ivd_engine_mode',return_value='compatibility'), \
         patch.object(runner,'_prepare_ivd_lifecycle',return_value=(None,SyntheticTurn())), \
         patch.object(runner,'_get_proxy_url',return_value='https://example.invalid'), \
         patch('aiohttp.ClientSession',return_value=session), \
         patch('gateway.stream_consumer.GatewayStreamConsumer') as consumer:
        asyncio.run(adapter._process_message_background(event,build_session_key(event.source)))
    consumer.assert_not_called()
    assert adapter.send.await_count==1
    sent=str(adapter.send.await_args)
    assert '来源未知。' in sent
    assert draft not in sent
