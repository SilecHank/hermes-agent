"""Real planner/workflow/guard/presentation regression for B02; no model."""
import os
import sys
import json
from pathlib import Path

K = Path(os.environ['IVD_KB_ROOT']).resolve()
sys.path.insert(0, str(K))
from gateway.after_sales_guard import prepare_after_sales_turn, build_preflight_block_result
from scripts.hermes_fast_response_pipeline import build_fast_response_plan
from scripts.after_sales_workflow_gate import match_case_facts

QUESTION = '无创有个样本，第一次检测是XO（+++），这次做出来是XO（+），可能是什么原因'


def run_turn(question=QUESTION, history=None, cards_dir=None):
    guard = {'enabled': True, 'platforms': ['qqbot'],
             'fast_response_module': str(K/'scripts/hermes_fast_response_pipeline.py'),
             'workflow_module': str(K/'scripts/after_sales_workflow_gate.py'),
             'validator_module': str(K/'scripts/after_sales_answer_validator.py'),
             'cards_dir': str(cards_dir or K/'knowledge-base/workflows/facts')}
    turn = prepare_after_sales_turn({'after_sales_guard': guard}, platform='qqbot',
                                    message=question, history=history or [])
    assert turn.blocks_answer_generation
    result = build_preflight_block_result(turn, question)
    assert result['api_calls'] == 0 and result['completed']
    return turn, result['final_response']


def test_original_question_yields_two_answerable_groups_and_next_action():
    turn, text = run_turn()
    assert turn.facts['decision_branch'] == 'sex_chromosome'
    assert '两次' in text and '标记' in text
    assert '同次采血' in text and '重分析' in text and '重建库' in text and '重新采血' in text
    assert '完整字段' in text and '对应结果' in text
    assert text.count('？') == 2
    assert '分析流程' in text
    assert 'X/Y' not in text and '质控' not in text
    assert '转阴' not in text and '母源' not in text and '严重' not in text
    assert '已检索' not in text and '正式来源' not in text


def test_extra_sex_chromosome_alias_alone_does_not_end_in_static_refusal():
    _, text = run_turn(QUESTION + '，性染色体结果')
    assert '完整字段' in text and '同次采血' in text


def test_current_version_is_preserved_but_unbound_history_is_not_authority():
    _, unbound = run_turn(history=[{'role': 'user', 'content': '使用NIFTY B3分析流程'}])
    assert 'B3' not in unbound
    _, text = run_turn(QUESTION + '，使用NIFTY B3分析流程')
    assert 'B3' in text
    assert '版本是否' not in text
    _, missing = run_turn()
    assert '版本尚未确认' in missing


def test_known_first_group_moves_to_existing_card_gap():
    _, text = run_turn(QUESTION + '，两次同次采血，这次重分析；标记完整字段是性染色体异常提示，对应结果如下')
    assert '同次采血吗' not in text
    assert '完整字段及对应结果吗' not in text
    assert 'X/Y' in text


def test_history_from_another_product_does_not_lend_its_version():
    _, text = run_turn(history=[{'role': 'user', 'content': 'PMseq V5 建库'}])
    assert 'V5' not in text and '版本尚未确认' in text


def test_separate_user_turn_has_no_context_from_previous_call():
    run_turn(history=[{'role': 'user', 'content': 'NIFTY B3分析流程'}])
    _, text = run_turn()
    assert 'B3' not in text and '版本尚未确认' in text


def test_candidate_and_absent_evidence_do_not_authorize_answers():
    from scripts.hermes_expert_route_contract import build_expert_route_contract
    from scripts.hermes_fast_response_pipeline import _load_json_config, EXPERT_CAPABILITY_REGISTRY, DIAGNOSTIC_GRAPH_CONFIG
    for tier in ['candidate_only', 'pending_verify', 'no_evidence', 'formal_authority', 'controlled_reference']:
        c = build_expert_route_contract({'action':'expert_fallback', 'question_type':'report_interpretation',
            'product_line':'NIFTY', 'version':'B3', 'evidence_tier':tier},
            registry=_load_json_config(EXPERT_CAPABILITY_REGISTRY), graph=_load_json_config(DIAGNOSTIC_GRAPH_CONFIG))
        assert not c['formal_answer_allowed'] and not c['bounded_expert_answer_allowed']


def test_cross_product_and_unknown_term_do_not_use_sex_branch():
    for q in ['PMseq V5 样本结果变化，可能是什么原因', '无创有个样本，第一次ABC，这次DEF，可能是什么原因']:
        turn, text = run_turn(q)
        assert turn.facts.get('decision_branch') != 'sex_chromosome'
        assert '同次采血' not in text and 'X/Y' not in text
        assert not turn.expert_route_contract['formal_answer_allowed']
        assert '下一步' in text or '请确认' in text


def test_shared_chat_does_not_inherit_other_members_version_or_gaps():
    _, text = run_turn('[member-b] ' + QUESTION, history=[{'role': 'user',
        'content':'[member-a] NIFTY B3分析流程，同次采血重分析，完整字段是性染色体异常提示，对应结果如下'}])
    assert 'B3' not in text and '同次采血' in text and '完整字段' in text


def test_no_workflow_card_still_offers_specific_safe_gap(tmp_path):
    _, text = run_turn(cards_dir=tmp_path)
    assert '本轮' in text and '来源' in text
    assert '已进入专家检索' not in text
    assert '性染色体' not in text and '已检索' not in text


def test_asked_or_unknown_discriminators_are_not_treated_as_confirmed():
    _, text = run_turn(QUESTION + '。不确定是否同次采血，是重分析吗？完整字段和对应结果不知道。')
    assert '两次是否同次采血' in text and '完整字段及对应结果吗' in text


def test_missing_formal_source_card_fails_closed_to_safe_clarification(tmp_path):
    card = json.loads((K/'knowledge-base/workflows/facts/nifty-report-interpretation.json').read_text())
    card['decision_action_catalog'] = str(K/'knowledge-base/workflows/decision-actions.json')
    for source in card['sources'].values():
        source['path'] = str(tmp_path/'absent.md')
    (tmp_path/'report.json').write_text(json.dumps(card))
    turn, text = run_turn(cards_dir=tmp_path)
    assert not turn.facts and not turn.expert_route_contract['formal_answer_allowed']
    assert '本轮' in text and '来源' in text
    assert '性染色体分析流程' not in text and '已检索' not in text


def test_conflicting_versions_still_stop_for_clarification():
    _, text = run_turn(QUESTION + '，NIFTY B2或B3分析流程')
    assert '当前适用的产品和分析版本分别是什么' in text


def test_candidate_source_cannot_become_a_formal_answer(tmp_path):
    card = json.loads((K/'knowledge-base/workflows/facts/nifty-report-interpretation.json').read_text())
    card['decision_action_catalog'] = str(K/'knowledge-base/workflows/decision-actions.json')
    for source in card['sources'].values():
        source['type'] = 'candidate_only'
        source['path'] = str(tmp_path/'candidate-draft.md')
    (tmp_path/'candidate-draft.md').write_text('XO变化肯定是污染，严重程度已经降低。')
    (tmp_path/'report.json').write_text(json.dumps(card))
    _, text = run_turn(cards_dir=tmp_path)
    assert '污染' not in text and '严重程度' not in text and '已检索' not in text
    assert '下一步' in text and '已进入专家检索' not in text


def test_existing_sex_branch_aliases_share_clarification_not_an_xo_answer():
    for alias in ['XXX', 'XXY', 'XYY', 'X偏多', 'Y偏少']:
        turn, text = run_turn(QUESTION.replace('XO', alias))
        assert turn.facts['decision_branch'] == 'sex_chromosome'
        assert '同次采血' in text and '完整字段' in text
        assert 'XO' not in text


def test_current_other_product_cannot_inherit_nifty_sex_card():
    turn, text = run_turn('PMseq有个样本XO结果变化，可能是什么原因',
                         history=[{'role':'user','content':'NIFTY B3分析流程'}])
    assert turn.facts.get('decision_branch') != 'sex_chromosome'
    assert '同次采血' not in text and '性染色体分析流程' not in text
