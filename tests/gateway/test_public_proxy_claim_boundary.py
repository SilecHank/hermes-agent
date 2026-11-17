"""Proxy IO is a sentinel; all task/source/claim decisions run for real."""
import json as jsonlib
import unittest
from unittest.mock import patch
from tests.gateway.test_proxy_mode import _make_runner, _make_source, _FakeSession, _FakeSSEResponse
from tests.gateway.test_public_task_boundary import G

class GovernedProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_receives_only_current_task_and_releases_validated_text(self):
        config = {'after_sales_guard': {**G, 'platforms': ['matrix']}}
        question = 'NIFTY为什么磁珠过干影响回收'
        for kind in ['hypothesis', 'fact', 'numeric']:
            with self.subTest(kind=kind):
                runner = _make_runner()
                session = _FakeSession(_FakeSSEResponse())
                post_calls = []
                original_post = session.post
                def post(url, json=None, headers=None, **kwargs):
                    post_calls.append(json)
                    messages = json['messages']
                    self.assertNotIn('[本轮专家声明合同与实际摘录]', messages[0]['content'])
                    draft = {'hypothesis': '表面水化状态可能影响洗脱表现。',
                             'fact': '可以从洗脱步骤分析。',
                             'numeric': '这里使用示例数值 999 ng。'}[kind]
                    chunk = 'data: ' + jsonlib.dumps({'choices':[{'delta':{
                        'content': draft}}]}) + '\n\ndata: [DONE]\n\n'
                    session._response = _FakeSSEResponse(sse_chunks=[chunk])
                    return original_post(url, json=json, headers=headers, **kwargs)
                session.post = post
                with patch('gateway.run._load_gateway_config', return_value=config), \
                     patch.object(runner, '_get_proxy_url', return_value='https://example.invalid'), \
                     patch('aiohttp.ClientSession', return_value=session), \
                     patch('gateway.stream_consumer.GatewayStreamConsumer') as consumer:
                    result = await runner._run_agent_via_proxy(message='untrusted wrapper',
                        business_message=question, context_prompt='',
                        history=[{'role':'user','content':'旧病例不应进入当前任务'}],
                        source=_make_source(), session_id='fixture-session')
                self.assertEqual(len(post_calls), 1)
                self.assertEqual(post_calls[0]['messages'][-1]['content'], question)
                self.assertNotIn('X-Hermes-Session-Id', session.captured_headers)
                self.assertNotIn('旧病例', str(post_calls))
                consumer.assert_not_called()
                self.assertEqual(result['final_response'], {
                    'hypothesis': '表面水化状态可能影响洗脱表现。',
                    'fact': '可以从洗脱步骤分析。',
                    'numeric': '这里使用示例数值 999 ng。'}[kind])
                self.assertEqual(result['messages'][-1]['content'], result['final_response'])
