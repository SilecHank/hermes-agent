"""Routing and presentation contract for B02 sex-chromosome questions; no model.

Policy (2026-10-28): a "what could explain this change" question is answered
under a bounded mechanism contract, so it no longer stops for intake.  Report
interpretation still stops, and every missing field is asked in one round
instead of one field per turn.
"""
import json
import os
import sys
from pathlib import Path

K = Path(os.environ['IVD_KB_ROOT']).resolve()
sys.path.insert(0, str(K))
from gateway.after_sales_guard import prepare_after_sales_turn, build_preflight_block_result

MECHANISM = '无创有个样本，第一次检测是XO（+++），这次做出来是XO（+），可能是什么原因'
INTERPRETATION = '无创这个样本第一次检测是XO（+++），这次是XO（+），报告该怎么判读，浓度差多少算异常'
QUESTION = MECHANISM  # shared with the sibling B02 suites

CORE_FIELDS = '两次同次采血，这次重分析；完整字段是性染色体异常提示。'
EVIDENCE_MARKER = '[本轮专家声明合同与实际摘录]'


def run_turn(question=QUESTION, history=None, cards_dir=None):
    """Run one real planner/workflow/guard turn and return (turn, blocked_text)."""
    guard = {'enabled': True, 'platforms': ['qqbot'],
             'fast_response_module': str(K/'scripts/hermes_fast_response_pipeline.py'),
             'workflow_module': str(K/'scripts/after_sales_workflow_gate.py'),
             'validator_module': str(K/'scripts/after_sales_answer_validator.py'),
             'cards_dir': str(cards_dir or K/'knowledge-base/workflows/facts')}
    turn = prepare_after_sales_turn({'after_sales_guard': guard}, platform='qqbot',
                                    message=question, history=history or [])
    if not turn.blocks_answer_generation:
        return turn, ''
    result = build_preflight_block_result(turn, question)
    assert result['api_calls'] == 0 and result['completed']
    return turn, result['final_response']


def run_blocked(question, history=None, cards_dir=None):
    turn, text = run_turn(question, history, cards_dir)
    assert turn.blocks_answer_generation and text
    return turn, text


def evidence_bundle(turn):
    """Parse the declared contract + real excerpts appended to the turn context."""
    context = turn.context or ''
    assert EVIDENCE_MARKER in context
    tail = context.split(EVIDENCE_MARKER, 1)[1].lstrip('\n')
    start = tail.index('{')
    depth = 0
    for index, char in enumerate(tail[start:], start):
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return json.loads(tail[start:index + 1])
    raise AssertionError('expert contract payload is not closed')


def test_mechanism_question_is_answered_under_a_bounded_contract():
    turn, text = run_turn()
    assert not turn.blocks_answer_generation and text == ''
    assert turn.facts['decision_branch'] == 'sex_chromosome'
    contract = turn.expert_route_contract
    assert contract['status'] == 'ready'
    assert contract['capability'] == 'mechanism_explanation'
    assert contract['bounded_expert_answer_allowed'] is True
    assert contract['formal_answer_allowed'] is False
    assert contract['version_scope'] == 'general_mechanism'
    assert set(contract['allowed_claim_kinds']) == {
        'hypothesis', 'verification_action', 'boundary', 'unknown'}


def test_mechanism_answer_reads_the_declared_sop_chapter_not_the_whole_document():
    turn, _ = run_turn()
    payload = evidence_bundle(turn)
    declared = [item for item in payload['bundle']['evidence']
                if item['evidence_id'].startswith('workflow_reference:nifty_report_interpretation')]
    assert declared, payload['bundle']['evidence']
    sop = declared[0]
    assert sop['tier'] == 'controlled_reference' and sop['version'] == 'B3'
    assert sop['source_spans'], 'excerpt must be anchored to declared chapter spans'
    assert sop['excerpts'], 'declared chapter must yield real text'
    chapter = sop['excerpts'][0]
    # Line 1 of the SOP is never the answer: the excerpt starts inside 5.3.4.
    assert chapter['start_line'] > 100
    assert '5.3.4' in chapter['text'] and '性染色体' in chapter['text']
    assert payload['contract']['formal_answer_allowed'] is False


def test_report_interpretation_asks_every_missing_field_in_one_round():
    turn, text = run_blocked(INTERPRETATION)
    contract = turn.expert_route_contract
    assert contract['capability'] == 'data_interpretation'
    assert contract['bounded_expert_answer_allowed'] is False
    assert set(turn.facts['clarification']['missing']) == {
        'sample_relation', 'repeat_mode', 'result_field',
        'x_concentration', 'y_concentration', 'fetal_sex'}
    assert text.count('如果方便，请一次补齐这几项') == 1
    for prefix, asked in [('①', '同次采血'), ('②', '分析流程版本'),
                          ('③', 'X/Y估算浓度和胎儿性别')]:
        assert prefix in text and asked in text
    # One numbered message, not a question per turn.
    assert '？' not in text
    assert text.count('①') == 1 and text.count('②') == 1 and text.count('③') == 1


def test_known_core_fields_leave_only_the_remaining_items_in_the_same_message():
    turn, text = run_blocked('无创这个样本该怎么判读。' + CORE_FIELDS,
                             history=[{'role': 'user', 'content': INTERPRETATION}])
    assert set(turn.facts['clarification']['missing']) == {
        'first_result', 'repeat_result', 'x_concentration', 'y_concentration', 'fetal_sex'}
    assert '两次是否同次采血' not in text and '完整字段及对应结果' not in text
    assert '①第一次和这次对应结果原文' in text
    assert '③对应的X/Y估算浓度和胎儿性别' in text


def test_complete_fields_release_the_clarification():
    turn, _ = run_turn(
        '无创这个样本该怎么判读。' + CORE_FIELDS
        + '第一次是XO（+++），这次是XO（+）。X浓度=0，Y浓度=0，男胎，NIFTY B3分析流程。',
        history=[{'role': 'user', 'content': INTERPRETATION}])
    assert not turn.blocks_answer_generation
    assert turn.facts['clarification']['missing'] == []


def test_conflicting_versions_still_ask_for_the_applicable_version():
    _, text = run_blocked(INTERPRETATION + '，NIFTY B2或B3分析流程')
    assert '当前适用的产品和分析版本分别是什么' in text


def test_declared_version_narrows_scope_without_authorizing_a_formal_answer():
    turn, _ = run_turn(MECHANISM + '，使用NIFTY B3分析流程')
    contract = turn.expert_route_contract
    assert contract['version_scope'] == 'product'
    assert contract['bounded_expert_answer_allowed'] is True
    assert contract['formal_answer_allowed'] is False


def test_history_from_another_product_does_not_lend_its_version():
    turn, text = run_blocked(INTERPRETATION,
                             history=[{'role': 'user', 'content': 'PMseq V5 建库'}])
    assert 'V5' not in text and '版本尚未确认' in text
    assert turn.expert_route_contract['version_scope'] is None


def test_separate_turn_has_no_context_from_a_previous_call():
    run_blocked(INTERPRETATION)
    turn, text = run_blocked(INTERPRETATION)
    assert set(turn.facts['clarification']['missing']) == {
        'sample_relation', 'repeat_mode', 'result_field',
        'x_concentration', 'y_concentration', 'fetal_sex'}
    assert '版本尚未确认' in text


def test_shared_chat_does_not_inherit_another_members_version_or_gaps():
    turn, _ = run_turn('[member-b] ' + MECHANISM,
                       history=[{'role': 'user', 'content':
                                 '[member-a] NIFTY B3分析流程，同次采血重分析，'
                                 '完整字段是性染色体异常提示，对应结果如下'}])
    assert turn.expert_route_contract['version_scope'] == 'general_mechanism'
    assert set(turn.facts['clarification']['missing']) == {
        'sample_relation', 'repeat_mode', 'result_field',
        'x_concentration', 'y_concentration', 'fetal_sex'}


def test_cross_product_and_unknown_term_do_not_use_the_sex_branch():
    for question in ['PMseq V5 样本结果变化，可能是什么原因',
                     '无创有个样本，第一次ABC，这次DEF，可能是什么原因']:
        turn, text = run_turn(question)
        assert turn.facts.get('decision_branch') != 'sex_chromosome'
        assert '同次采血' not in text and 'X/Y' not in text
        assert turn.expert_route_contract['formal_answer_allowed'] is False


def test_sex_aliases_share_the_mechanism_route_not_an_xo_conclusion():
    for alias in ['XXX', 'XXY', 'XYY', 'X偏多', 'Y偏少']:
        turn, _ = run_turn(MECHANISM.replace('XO', alias))
        assert turn.facts['decision_branch'] == 'sex_chromosome'
        assert turn.expert_route_contract['capability'] == 'mechanism_explanation'
        assert turn.expert_route_contract['formal_answer_allowed'] is False


def test_current_other_product_cannot_inherit_the_nifty_sex_card():
    turn, _ = run_turn('PMseq有个样本XO结果变化，可能是什么原因',
                       history=[{'role': 'user', 'content': 'NIFTY B3分析流程'}])
    assert turn.facts.get('decision_branch') != 'sex_chromosome'
    payload = evidence_bundle(turn)
    assert not [item for item in payload['bundle']['evidence']
                if item['evidence_id'].startswith('workflow_reference:nifty_report_interpretation')]


def test_candidate_and_absent_evidence_do_not_authorize_answers():
    from scripts.hermes_expert_route_contract import build_expert_route_contract
    from scripts.hermes_fast_response_pipeline import _load_json_config, EXPERT_CAPABILITY_REGISTRY, DIAGNOSTIC_GRAPH_CONFIG
    for tier in ['candidate_only', 'pending_verify', 'no_evidence', 'formal_authority', 'controlled_reference']:
        contract = build_expert_route_contract(
            {'action': 'expert_fallback', 'question_type': 'report_interpretation',
             'product_line': 'NIFTY', 'version': 'B3', 'evidence_tier': tier},
            registry=_load_json_config(EXPERT_CAPABILITY_REGISTRY),
            graph=_load_json_config(DIAGNOSTIC_GRAPH_CONFIG))
        assert not contract['formal_answer_allowed'] and not contract['bounded_expert_answer_allowed']


def test_missing_card_mechanism_route_cannot_claim_product_rules(tmp_path):
    turn, _ = run_turn(cards_dir=tmp_path)
    contract = turn.expert_route_contract
    assert contract['capability'] == 'mechanism_explanation'
    assert contract['version_scope'] == 'general_mechanism'
    assert contract['formal_answer_allowed'] is False
    payload = evidence_bundle(turn)
    assert 'SOP-BIN-039' not in json.dumps(payload['bundle'], ensure_ascii=False)
