from agent.turn_finalizer import finalize_turn
from tests.agent.test_turn_finalizer_final_response_persistence import FakeAgent

class Reject:
    requires_structured_claims = True
    fail_closed = True
    def __call__(self, answer):
        return {'ok': False, 'reasons': ['expert_claims_json_invalid'], 'fallback': '来源未知。'}

def test_iteration_limit_removes_rejected_scaffolding_from_all_session_exits(monkeypatch):
    monkeypatch.setattr('hermes_cli.plugins.invoke_hook', lambda *_a, **_kw: [])
    a=FakeAgent(); a._final_response_validator=Reject()
    rejected='SYNTHETIC_REJECTED_999'
    messages=[{'role':'user','content':'合成问题'},
              {'role':'assistant','content':rejected,'_final_validation_synthetic':True},
              {'role':'user','content':'合成重试','_final_validation_synthetic':True}]
    result=finalize_turn(a,final_response=rejected,api_call_count=1,interrupted=False,failed=False,
        messages=messages,conversation_history=[],effective_task_id='synthetic',turn_id='synthetic',
        user_message='合成问题',original_user_message='合成问题',_should_review_memory=False,
        _turn_exit_reason='recovery')
    assert result['final_response']=='来源未知。'
    assert all(m.get('content')!=rejected for m in result['messages'])
    assert all(m.get('content')!=rejected for m in a.persisted_messages)
    assert not any(m.get('_final_validation_synthetic') for m in result['messages'])
    assert result['messages'][0]['content']=='合成问题'

def test_real_agent_continuation_does_not_replay_rejected_draft(monkeypatch):
    import socket
    from tests.gateway.test_synthetic_adapter_final_exit import _agent
    def deny(*_args, **_kwargs):
        raise AssertionError('network is forbidden')
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    connect = socket.socket.connect
    def local_only(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            deny()
        return connect(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', local_only)
    rejected='SYNTHETIC_REJECTED_999'
    calls=[]
    agent=_agent([rejected, '合成排查建议：检查记录。'], calls)
    class Validator:
        requires_structured_claims=True
        fail_closed=True
        output_contract=''
        def __call__(self, answer):
            if rejected in answer:
                return {'ok':False,'reasons':['expert_claims_json_invalid'],'fallback':'来源未知。'}
            return {'ok':True,'reasons':[],'fallback':''}
    agent._final_response_validator=Validator()
    first=agent.run_conversation('合成首问',system_message='纯合成系统提示',task_id='synthetic')
    assert first['final_response']=='来源未知。'
    assert rejected not in str(first['messages'])
    assert rejected not in str(agent._session_messages)
    second=agent.run_conversation('合成追问',system_message='纯合成系统提示',
                                  conversation_history=first['messages'],task_id='synthetic')
    assert second['final_response']=='合成排查建议：检查记录。'
    assert len(calls)==2
    assert rejected not in str(calls[1]['messages'])
    assert rejected not in str(second['messages'])
    assert rejected not in str(agent._session_messages)
