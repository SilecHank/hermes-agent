"""Application-owned topic state carries one bare follow-up onto the served card."""
import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionStore, SessionSource
from gateway.run import GatewayRunner
from tests.gateway.test_public_task_boundary import G

OPENER = 'NIFTY样本质控MCA偏高怎么处理'


@pytest.fixture
def dialogue(tmp_path):
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    source = SessionSource(platform=Platform.WEIXIN, chat_id='topic-carry', user_id='actor-a')
    entry = store.get_or_create_session(source)
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._ivd_prepared_contracts = {}

    def send(message, actor='actor-a'):
        _, turn = runner._prepare_ivd_lifecycle(
            {'after_sales_guard': G}, platform='weixin', message=message,
            history=[], actor_id=actor, session_key=entry.session_key)
        pending = store.get_session_metadata(entry.session_key, 'ivd_pending_task')
        return turn, pending

    send.store = store
    send.key = entry.session_key
    yield send
    if store._db is not None:
        store._db.close()


def binding_of(turn):
    return (getattr(turn, 'facts', {}) or {}).get('workflow_binding') or {}


def test_answered_card_turn_leaves_topic_state(dialogue):
    turn, pending = dialogue(OPENER)
    assert turn is not None and not turn.blocks_answer_generation
    assert binding_of(turn)['workflow_id'] == 'nifty_sample_qc'
    assert pending and pending['status'] == 'awaiting_reply'
    assert pending['topic_carry'] is True
    assert pending['missing_fields'] == []
    assert pending['workflow_binding']['branch'] == 'mca'
    assert pending['question'] == turn.task_contract['question']


@pytest.mark.parametrize('followup', ['那重抽几次？', '那次要重抽吗', '重抽后还要重测吗'])
def test_bare_followup_keeps_the_served_branch(dialogue, followup):
    opener_turn, _ = dialogue(OPENER)
    turn, pending = dialogue(followup)
    assert turn is not None
    assert turn.task_contract['continuation'] == 'topic_carry'
    assert turn.task_contract['task_id'] == opener_turn.task_contract['task_id']
    assert binding_of(turn)['workflow_id'] == 'nifty_sample_qc'
    assert binding_of(turn)['branch'] == 'mca'
    assert turn.facts.get('product') == 'NIFTY'
    assert pending and pending['topic_carry'] is True


def test_followup_naming_another_product_is_a_new_task(dialogue):
    dialogue(OPENER)
    turn, pending = dialogue('PMseq提取需要多少体积？')
    assert turn is None or turn.task_contract['continuation'] != 'topic_carry'
    assert not binding_of(turn)


def test_other_actor_cannot_continue_the_topic(dialogue):
    dialogue(OPENER)
    turn, _ = dialogue('那重抽几次？', actor='actor-b')
    assert turn is None or turn.task_contract['continuation'] != 'topic_carry'
    assert not binding_of(turn)


def test_explicit_switch_phrase_is_a_new_task(dialogue):
    dialogue(OPENER)
    turn, _ = dialogue('换个话题，PMseq提取需要多少体积？')
    assert turn is None or turn.task_contract['continuation'] != 'topic_carry'


def test_carried_topic_stays_bounded_across_followups(dialogue):
    dialogue(OPENER)
    dialogue('那重抽几次？')
    turn, pending = dialogue('重抽后还要重测吗')
    assert turn.task_contract['continuation'] == 'topic_carry'
    assert pending['question'] == OPENER
    assert turn.task_contract['question'].count(OPENER) == 1
    assert binding_of(turn)['branch'] == 'mca'
