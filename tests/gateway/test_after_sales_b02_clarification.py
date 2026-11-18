"""Routing and presentation contract for B02 sex-chromosome questions; no model.

An unserved report question reaches expert analysis; structured workflow facts
and missing fields remain available without becoming a forced canned reply.
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
    assert turn.route_id == 'expert_mode_auto_fallback'
    assert not turn.expert_route_contract.get('formal_answer_allowed')
    assert '不等于严重程度、转阴或改善' in turn.context


def test_user_confirmed_sop_is_bound_to_declared_chapter():
    turn, _ = run_turn()
    from scripts.hermes_fast_response_pipeline import build_fast_response_plan
    plan = build_fast_response_plan(MECHANISM, task_contract=turn.task_contract,
        workflow_binding=turn.facts.get('workflow_binding'),
        workflow_clarification=turn.facts.get('clarification'))
    declared = [item for item in plan['expert_evidence_bundle']['evidence']
                if item['evidence_id'].startswith('workflow_reference:nifty_report_interpretation')]
    from scripts.after_sales_workflow_gate import load_workflow_card
    from scripts.hermes_expert_evidence_projection import reviewed_source_decision
    card = load_workflow_card(Path(turn.facts['workflow_binding']['path']))
    source = card['sources']['sop_bin_039_b3']
    assert reviewed_source_decision(K, source['path'], product=card['product'])['status'] == 'allowed'
    assert declared
    sop = declared[0]
    assert sop['tier'] == 'controlled_reference' and sop['version'] == 'B3'
    assert sop['source_spans'] and sop['excerpts']
    assert sop['excerpts'][0]['start_line'] > 100
    assert '5.3.4' in sop['excerpts'][0]['text'] and '性染色体' in sop['excerpts'][0]['text']
    assert plan['expert_route_contract']['formal_answer_allowed'] is False


def test_declared_sop_chapter_excerpt_stays_within_the_branch():
    # Test excerpt positioning independently; this does not authorize the SOP.
    import hashlib
    from scripts.after_sales_workflow_gate import load_workflow_card
    from scripts.hermes_expert_evidence_projection import acquire_excerpts
    turn, _ = run_turn()
    card = load_workflow_card(Path(turn.facts['workflow_binding']['path']))
    source = card['sources']['sop_bin_039_b3']
    record = {'source_ref': source['path'], 'tier': 'controlled_reference',
              'source_spans': [[318, 390]],
              'source_digest': hashlib.sha256((K / source['path']).read_bytes()).hexdigest()}
    acquire_excerpts(K, {'test-reference': record}, question=MECHANISM)
    assert record['excerpts']
    chapter = record['excerpts'][0]
    assert chapter['start_line'] > 100
    assert '5.3.4' in chapter['text'] and '性染色体' in chapter['text']


def test_report_interpretation_asks_every_missing_field_in_one_round():
    turn, text = run_turn(INTERPRETATION)
    assert not turn.blocks_answer_generation and not text
    assert set(turn.facts['clarification']['missing']) == {
        'sample_relation', 'repeat_mode', 'result_field',
        'x_concentration', 'y_concentration', 'fetal_sex'}
    assert turn.route_id == 'expert_mode_auto_fallback'


def test_known_core_fields_leave_only_the_remaining_items_in_the_same_message():
    turn, text = run_turn('无创这个样本该怎么判读。' + CORE_FIELDS,
                             history=[{'role': 'user', 'content': INTERPRETATION}])
    assert set(turn.facts['clarification']['missing']) == {
        'first_result', 'repeat_result', 'x_concentration', 'y_concentration', 'fetal_sex'}
    assert not turn.blocks_answer_generation and not text


def test_complete_fields_release_the_clarification():
    turn, _ = run_turn(
        '无创这个样本该怎么判读。' + CORE_FIELDS
        + '第一次是XO（+++），这次是XO（+）。X浓度=0，Y浓度=0，男胎，NIFTY B3分析流程。',
        history=[{'role': 'user', 'content': INTERPRETATION}])
    assert not turn.blocks_answer_generation
    assert turn.facts['clarification']['missing'] == []


def test_conflicting_versions_still_ask_for_the_applicable_version():
    turn, text = run_turn(INTERPRETATION + '，NIFTY B2或B3分析流程')
    assert not turn.blocks_answer_generation and not text
    assert (turn.task_contract.get('context') or {}).get('product_version') not in ('B2', 'B3')


def test_declared_version_narrows_scope_without_authorizing_a_formal_answer():
    turn, _ = run_turn(MECHANISM + '，使用NIFTY B3分析流程')
    from scripts.hermes_fast_response_pipeline import build_fast_response_plan
    contract = build_fast_response_plan(MECHANISM + '，使用NIFTY B3分析流程',
        task_contract=turn.task_contract, workflow_binding=turn.facts.get('workflow_binding'),
        workflow_clarification=turn.facts.get('clarification'))['expert_route_contract']
    assert contract['version_scope'] == 'product'
    assert contract['bounded_expert_answer_allowed'] is True
    assert contract['formal_answer_allowed'] is False


def test_history_from_another_product_does_not_lend_its_version():
    turn, text = run_turn(INTERPRETATION,
                             history=[{'role': 'user', 'content': 'PMseq V5 建库'}])
    assert not turn.blocks_answer_generation and 'V5' not in text
    assert (turn.task_contract.get('context') or {}).get('product_version') != 'V5'


def test_separate_turn_has_no_context_from_a_previous_call():
    run_turn(INTERPRETATION)
    turn, text = run_turn(INTERPRETATION)
    assert set(turn.facts['clarification']['missing']) == {
        'sample_relation', 'repeat_mode', 'result_field',
        'x_concentration', 'y_concentration', 'fetal_sex'}
    assert not turn.blocks_answer_generation and not text


def test_shared_chat_does_not_inherit_another_members_version_or_gaps():
    turn, _ = run_turn('[member-b] ' + MECHANISM,
                       history=[{'role': 'user', 'content':
                                 '[member-a] NIFTY B3分析流程，同次采血重分析，'
                                 '完整字段是性染色体异常提示，对应结果如下'}])
    assert (turn.task_contract.get('context') or {}).get('product_version') != 'B3'
    assert set(turn.facts['clarification']['missing']) == {
        'sample_relation', 'repeat_mode', 'result_field',
        'x_concentration', 'y_concentration', 'fetal_sex'}


def test_cross_product_and_unknown_term_do_not_use_the_sex_branch():
    for question in ['PMseq V5 样本结果变化，可能是什么原因',
                     '无创有个样本，第一次ABC，这次DEF，可能是什么原因']:
        turn, text = run_turn(question)
        assert turn.facts.get('decision_branch') != 'sex_chromosome'
        assert '同次采血' not in text and 'X/Y' not in text
        assert not turn.expert_route_contract.get('formal_answer_allowed')


def test_sex_aliases_share_the_mechanism_route_not_an_xo_conclusion():
    for alias in ['XXX', 'XXY', 'XYY', 'X偏多', 'Y偏少']:
        turn, _ = run_turn(MECHANISM.replace('XO', alias))
        assert turn.facts['decision_branch'] == 'sex_chromosome'
        assert turn.route_id == 'expert_mode_auto_fallback'
        assert not turn.expert_route_contract.get('formal_answer_allowed')


def test_current_other_product_cannot_inherit_the_nifty_sex_card():
    turn, _ = run_turn('PMseq有个样本XO结果变化，可能是什么原因',
                       history=[{'role': 'user', 'content': 'NIFTY B3分析流程'}])
    assert turn.facts.get('decision_branch') != 'sex_chromosome'
    assert 'nifty_report_interpretation' not in json.dumps(turn.facts, ensure_ascii=False)


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
    assert turn.route_id == 'expert_mode_auto_fallback'
    assert not turn.expert_route_contract.get('formal_answer_allowed')
    assert 'SOP-BIN-039' not in json.dumps(turn.facts, ensure_ascii=False)
