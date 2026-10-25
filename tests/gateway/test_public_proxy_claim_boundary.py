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
        for kind in ['hypothesis', 'fact', 'bad_verification', 'bad_boundary']:
            with self.subTest(kind=kind):
                runner = _make_runner()
                session = _FakeSession(_FakeSSEResponse())
                post_calls = []
                original_post = session.post
                def post(url, json=None, headers=None, **kwargs):
                    post_calls.append(json)
                    messages = json['messages']
                    supplied = jsonlib.loads(messages[0]['content'].split(
                        '[本轮专家声明合同与实际摘录]\n', 1)[1].splitlines()[0])
                    r = next(r for r in supplied['bundle']['evidence'] if r['tier'] == 'controlled_reference' and r.get('excerpts'))
                    e = r['excerpts'][0]
                    draft = {'task_id': supplied['task_id'], 'claims': [{
                        'kind': 'hypothesis' if kind.startswith('bad_') else kind,
                        'scope': 'general', 'text': '表面水化状态可能影响洗脱表现。',
                        'verification': '对照复溶前后的回收表现。', 'boundary': '一般原理推断，不能判定个案或产品参数。',
                        'evidence_ids': [r['evidence_id']], 'support': [{
                            'evidence_id': r['evidence_id'], 'start_line': e['start_line'],
                            'quote': e['text'], 'relation': '用于提出待验证的一般原理假设。'}]}]}
                    if kind.startswith('bad_'):
                        draft['claims'][0][kind[4:]] = '投入量必须设置为999 ng，可以直接放行报告。'
                    chunk = 'data: ' + jsonlib.dumps({'choices':[{'delta':{
                        'content': jsonlib.dumps(draft, ensure_ascii=False)}}]}) + '\n\ndata: [DONE]\n\n'
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
                if kind == 'hypothesis':
                    self.assertIn('原理推断', result['final_response'])
                    self.assertIn('核查', result['final_response'])
                else:
                    self.assertIn('停止发送', result['final_response'])
                self.assertEqual(result['messages'][-1]['content'], result['final_response'])
