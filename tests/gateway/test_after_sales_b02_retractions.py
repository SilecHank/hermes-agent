"""Exact quality-review corrections and bounded positive followups; no model."""
from tests.gateway.test_after_sales_b02_clarification import QUESTION, run_turn

HISTORY = [{'role':'user','content':QUESTION}, {'role':'user','content':
    'NIFTY B3分析流程。两次同次采血，这次重分析；完整字段是性染色体异常提示。男胎。X浓度=0。'}]
RETRACT_X = '刚才说错了，没有X浓度数据，Y浓度也不知道。'
RETRACT_VERSION = '分析版本不知道，之前B3只是猜的。'


def test_exact_quality_x_retraction_preserves_other_confirmed_fields():
    turn, text = run_turn(RETRACT_X, history=HISTORY)
    assert set(turn.facts['clarification']['missing']) == {'x_concentration', 'y_concentration'}
    assert 'X/Y估算浓度' in text and '完整字段' not in text and '同次采血' not in text


def test_exact_quality_version_retraction_overrides_inherited_version_only():
    turn, text = run_turn(RETRACT_VERSION, history=HISTORY)
    assert 'B3' not in text and '版本尚未确认' in text
    assert set(turn.facts['clarification']['missing']) == {'y_concentration'}


def test_retracted_axis_can_be_provided_again_without_reasking_core():
    _, text = run_turn('X浓度=0，Y浓度=0', history=HISTORY + [{'role':'user','content':RETRACT_X}])
    assert '所需核对信息已记录' in text and '？' not in text
    assert '完整字段' not in text and '同次采血' not in text and 'B3' in text


def test_retracted_version_can_be_confirmed_again():
    turn, text = run_turn('NIFTY B3分析流程已确认', history=HISTORY + [{'role':'user','content':RETRACT_VERSION}])
    assert '已记录你提供的分析版本B3' in text
    assert set(turn.facts['clarification']['missing']) == {'y_concentration'}
    assert '完整字段' not in text and '同次采血' not in text


def test_one_clause_retraction_does_not_erase_other_axis_confirmation():
    turn, text = run_turn('没有X浓度数据，但Y浓度=0。', history=HISTORY)
    assert set(turn.facts['clarification']['missing']) == {'x_concentration'}
    assert '对应的X估算浓度' in text and 'X/Y' not in text


def test_unknown_version_is_not_resurrected_when_other_data_arrives():
    _, text = run_turn('Y浓度=0', history=HISTORY + [{'role':'user','content':RETRACT_VERSION}])
    assert 'B3' not in text and '版本尚未确认' in text
    assert '分析流程版本分别是什么' in text
    assert 'X/Y' not in text and '完整字段' not in text
