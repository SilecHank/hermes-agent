"""Real lifecycle and temporary SessionStore; old expectations stay unchanged."""
import time
import pytest
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionStore, SessionSource
from gateway.run import GatewayRunner
from gateway.after_sales_guard import build_preflight_block_result
from tests.gateway.test_public_task_boundary import G
from tests.gateway import test_after_sales_b02_retractions as original


@pytest.fixture
def dialogue(tmp_path):
    store = SessionStore(tmp_path/'sessions', GatewayConfig())
    source = SessionSource(platform=Platform.WEIXIN,chat_id='field-fixture',user_id='actor-a')
    entry = store.get_or_create_session(source)
    runner = object.__new__(GatewayRunner)
    runner.session_store=store; runner._ivd_prepared_contracts={}
    def send(message,actor='actor-a'):
        before = store.get_session_metadata(entry.session_key,'ivd_pending_task')
        _, turn = runner._prepare_ivd_lifecycle({'after_sales_guard':G},platform='weixin',
            message=message,history=[],actor_id=actor,session_key=entry.session_key)
        after = store.get_session_metadata(entry.session_key,'ivd_pending_task')
        if before and after and turn.task_contract.get('continuation') in {'clarification_reply','field_patch'}:
            assert after['expires_at'] == before['expires_at']
        text = (build_preflight_block_result(turn,message)['final_response']
                if turn.blocks_answer_generation else turn.direct_response)
        return turn,text,before,after
    send.store=store;send.key=entry.session_key
    yield send
    if store._db is not None: store._db.close()


def seed(send):
    send(original.HISTORY[0]['content'])
    turn,_,_,pending=send(original.HISTORY[1]['content'])
    assert pending and turn.task_contract['continuation']=='clarification_reply'
    assert set(turn.facts['clarification']['missing'])=={'y_concentration'}
    return pending


OLD_CASES=[name for name in vars(original) if name.startswith('test_')]
@pytest.mark.parametrize('name',OLD_CASES)
def test_old_expectations_through_authorized_sessionstore(name, dialogue, monkeypatch):
    def actual_turn(question=original.HISTORY[0]['content'],history=None,cards_dir=None):
        for row in history or []:
            if row.get('role')=='user': dialogue(row['content'])
        turn,text,_,_=dialogue(question)
        assert turn.blocks_answer_generation
        result=build_preflight_block_result(turn,question)
        assert result['api_calls']==0 and result['completed']
        assert text==result['final_response']
        return turn,text
    monkeypatch.setattr(original,'run_turn',actual_turn)
    getattr(original,name)()


@pytest.mark.parametrize('message,removed',[
    ('刚才X浓度写错了，实际数值暂时不确定。','x_concentration'),
    ('更正，分析版本先不要按B3记录。','product_version'),
    ('分析版本不知道，Y浓度=0。','product_version'),
    ('X浓度之前只是猜测。','x_concentration'),
])
def test_field_patch_preserves_other_values_and_authority(dialogue,message,removed):
    pending=seed(dialogue)
    turn,text,_,after=dialogue(message)
    assert turn.task_contract['task_id']==pending['task']['task_id']
    assert turn.task_contract['continuation']=='field_patch'
    assert removed in turn.task_contract['field_patch']['remove']
    assert after and after['expires_at']==pending['expires_at']
    missing=set(turn.facts['clarification']['missing'])
    assert not missing & {'sample_relation','repeat_mode','result_field','fetal_sex'}
    if removed=='product_version':
        assert not turn.task_contract['context']['product_version']
        assert 'x_concentration' not in missing and 'B3' not in text
    else:
        assert missing=={'x_concentration','y_concentration'}
        assert turn.task_contract['context']['product_version']=='B3'


@pytest.mark.parametrize('field,message',[
    ('sample_relation','两次采血关系不确定。'),
    ('repeat_mode','这次重分析还是重建库不确定。'),
    ('result_field','完整字段不确定。'),
    ('first_result','第一次结果不确定。'),
    ('repeat_result','这次结果不确定。'),
    ('x_concentration','X浓度不确定。'),
    ('fetal_sex','胎儿性别不确定。'),
])
def test_allowed_field_withdrawal_preserves_other_known_fields(dialogue,field,message):
    pending=seed(dialogue)
    turn,_,_,after=dialogue(message)
    assert turn.task_contract['continuation']=='field_patch'
    assert turn.task_contract['task_id']==pending['task']['task_id']
    assert field in turn.task_contract['field_patch']['remove']
    assert set(turn.facts['clarification']['missing'])=={field,'y_concentration'}
    assert after and after['expires_at']==pending['expires_at']


@pytest.mark.parametrize('message,field,value',[
    ('X浓度改为1。','x_concentration','X浓度为1'),
    ('分析版本改为B2。','product_version','B2'),
])
def test_explicit_assignment_replaces_old_value_without_extending_authority(dialogue,message,field,value):
    pending=seed(dialogue)
    turn,_,_,after=dialogue(message)
    assert turn.task_contract['task_id']==pending['task']['task_id']
    assert turn.task_contract['continuation']=='field_patch'
    assert turn.task_contract['field_patch']['set'][field]==value
    assert after and after['expires_at']==pending['expires_at']
    if field=='product_version':
        assert turn.task_contract['context'][field]==value
        assert 'B3' not in turn.task_contract['question']
    else:
        assert 'X浓度=0' not in turn.task_contract['question']


def test_unknown_withdrawal_does_not_erase_a_separate_valid_assignment(dialogue):
    pending=seed(dialogue)
    turn,_,_,after=dialogue('更正，X浓度=1，颜色不确定。')
    assert turn.task_contract['task_id']==pending['task']['task_id']
    assert turn.task_contract['field_patch']['set']['x_concentration']=='X浓度=1'
    assert 'x_concentration' not in turn.task_contract['field_patch']['remove']
    assert set(turn.facts['clarification']['missing'])=={'y_concentration'}
    assert after['expires_at']==pending['expires_at']


def test_pending_patch_commits_once_after_admission(dialogue,monkeypatch):
    pending=seed(dialogue)
    writes=[]
    original_setter=dialogue.store.set_session_metadata
    def record(session,key,value):
        writes.append(value)
        return original_setter(session,key,value)
    monkeypatch.setattr(dialogue.store,'set_session_metadata',record)
    _,_,_,after=dialogue('X浓度不确定。')
    assert len(writes)==1 and writes[0] is not None
    assert after['task']['task_id']==pending['task']['task_id']


def test_context_method_withdrawal_and_return_reuse_existing_method_semantics(dialogue):
    dialogue(original.HISTORY[0]['content'])
    turn,_,_,pending=dialogue(original.HISTORY[1]['content']+'手工实验。')
    assert pending and turn.task_contract['context']['method']=='manual'
    task_id=turn.task_contract['task_id']
    turn,_,_,after=dialogue('实验方法不确定。')
    assert turn.task_contract['task_id']==task_id
    assert turn.task_contract['continuation']=='field_patch'
    assert 'method' in turn.task_contract['field_patch']['remove']
    assert turn.task_contract['context']['method'] is None
    assert '手工' not in turn.task_contract['question']
    assert after and 'method' in after['missing_fields']
    assert after['expires_at']==pending['expires_at']
    turn,_,_,after=dialogue('手工')
    assert turn.task_contract['task_id']==task_id
    assert turn.task_contract['context']['method']=='manual'
    assert after and after['expires_at']==pending['expires_at']


@pytest.mark.parametrize('message',[
    '撤回颜色字段，颜色不确定。',
    '更正，X浓度不知道，帮我解释PCR抑制原理。',
    '分析版本不确定，发送SOP。',
    '换个话题，PMseq提取需要多少体积？',
    '更正，X浓度不确定，NIFTY提取需要多少血浆？',
    '更正，分析版本是不是B3？',
    '分析版本不知道吗？',
    '请更正一下。',
    '更正，我要问的是上机通量，不是样本加入量。',
])
def test_unknown_or_new_request_cannot_authorize_old_field_patch(dialogue,message):
    pending=seed(dialogue)
    turn,_,_,_=dialogue(message)
    assert turn.task_contract['task_id']!=pending['task']['task_id']
    assert not turn.task_contract.get('field_patch')


@pytest.mark.parametrize('mode',['other_actor','expired'])
def test_field_patch_requires_live_actor_authority(dialogue,mode):
    pending=seed(dialogue)
    if mode=='expired':
        pending={**pending,'expires_at':time.time()-1}
        dialogue.store.set_session_metadata(dialogue.key,'ivd_pending_task',pending)
    turn,_,_,_=dialogue('X浓度不确定。',actor='actor-b' if mode=='other_actor' else 'actor-a')
    assert turn.task_contract['task_id']!=pending['task']['task_id']
    assert not turn.task_contract.get('field_patch')
