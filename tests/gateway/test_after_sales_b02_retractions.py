"""Retraction and completion controls inside one intake round; no model."""
from tests.gateway.test_after_sales_b02_clarification import (
    CORE_FIELDS, INTERPRETATION, run_turn)

OPENER = {'role': 'user', 'content': INTERPRETATION}
HISTORY = [OPENER]
INTRO = '无创这个样本该怎么判读。' + CORE_FIELDS
RESULT_ONLY_GAP = {'first_result', 'repeat_result'}


def test_x_retraction_in_the_same_reply_keeps_the_axis_open():
    turn, text = run_turn(INTRO + 'X浓度=0，但刚才说错了，没有X浓度数据。', history=HISTORY)
    missing = set(turn.facts['clarification']['missing'])
    assert 'x_concentration' in missing and 'y_concentration' in missing
    assert 'sample_relation' not in missing and 'result_field' not in missing
    assert not turn.blocks_answer_generation and not text


def test_version_retraction_is_not_resurrected_by_the_same_reply():
    turn, text = run_turn(INTRO + 'NIFTY B3分析流程。刚才说错了，分析版本不知道。',
                             history=HISTORY)
    assert not turn.blocks_answer_generation and not text
    assert (turn.task_contract.get('context') or {}).get('product_version') != 'B3'


def test_withdrawn_axis_is_only_asked_alone():
    turn, text = run_turn(INTRO + '没有X浓度数据，但Y浓度=0。', history=HISTORY)
    missing = set(turn.facts['clarification']['missing'])
    assert 'x_concentration' in missing and 'y_concentration' not in missing
    assert not turn.blocks_answer_generation and not text


def test_supplied_sex_and_axis_leave_only_the_missing_axis():
    turn, text = run_turn(INTRO + '男胎。Y浓度=0。', history=HISTORY)
    assert set(turn.facts['clarification']['missing']) == RESULT_ONLY_GAP | {'x_concentration'}
    assert not turn.blocks_answer_generation and not text


def test_announcing_results_is_not_supplying_values():
    turn, text = run_turn(INTRO + '对应结果如下。', history=HISTORY)
    assert set(turn.facts['clarification']['missing']) == (
        RESULT_ONLY_GAP | {'x_concentration', 'y_concentration', 'fetal_sex'})
    assert not turn.blocks_answer_generation and not text


def test_complete_reply_releases_the_round_instead_of_looping():
    turn, text = run_turn(
        INTRO + '第一次是XO（+++），这次是XO（+）。X浓度=0，Y浓度=0，男胎，NIFTY B3分析流程。',
        history=HISTORY)
    assert not turn.blocks_answer_generation and text == ''
    assert turn.facts['clarification']['missing'] == []
