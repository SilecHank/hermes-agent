"""Synthetic rejection controls from independent B02 review, not medical facts."""
import json
from copy import deepcopy

from tests.gateway.test_after_sales_b02_clarification import (
    CORE_FIELDS, INTERPRETATION, K, MECHANISM, run_blocked, run_turn)

SYNTHETIC = '该样本已经转阴，严重程度降低。'
CARD = K/'knowledge-base/workflows/facts/nifty-report-interpretation.json'
GAP_HEADING = '本轮已核对'


def tampered_card(tmp_path, mutate):
    card = json.loads(CARD.read_text())
    card['decision_action_catalog'] = str(K/'knowledge-base/workflows/decision-actions.json')
    mutate(card, tmp_path)
    (tmp_path/'card.json').write_text(json.dumps(card))
    return tmp_path


def test_candidate_policy_fields_cannot_become_final_text(tmp_path):
    for field in ['acknowledgement', 'questions', 'next_action']:
        def mutate(card, root, field=field):
            for source in card['sources'].values():
                source['type'] = 'candidate_only'
                source['path'] = str(root/'draft.md')
            (root/'draft.md').write_text('synthetic unverified source')
            policy = card['decision_branches']['sex_chromosome']['clarification_policy']
            policy[field] = ([{'id': 'result_field', 'question': SYNTHETIC,
                               'match_groups': [['unknown']]}]
                             if field == 'questions' else SYNTHETIC)
        turn, text = run_blocked(INTERPRETATION, cards_dir=tampered_card(tmp_path, mutate))
        assert SYNTHETIC not in text and SYNTHETIC not in (turn.context or '')
        assert GAP_HEADING in text and '来源' in text


def test_tampered_card_fails_closed_on_the_mechanism_route_too(tmp_path):
    def mutate(card, root):
        policy = card['decision_branches']['sex_chromosome']['clarification_policy']
        policy['acknowledgement'] = SYNTHETIC
    turn, text = run_blocked(MECHANISM, cards_dir=tampered_card(tmp_path, mutate))
    assert SYNTHETIC not in text and SYNTHETIC not in (turn.context or '')
    assert (turn.expert_route_contract or {}).get('formal_answer_allowed') is not True
    assert (turn.expert_route_contract or {}).get('bounded_expert_answer_allowed') is not True


def test_uncertainty_and_negation_do_not_confirm_repeat_or_version():
    turn, text = run_blocked('无创这个样本该怎么判读。' + CORE_FIELDS
                             + '没有重新采血，可能是重分析。')
    missing = set(turn.facts['clarification']['missing'])
    assert 'sample_relation' in missing and 'repeat_mode' in missing
    assert '两次是否同次采血' in text
    turn, text = run_blocked(INTERPRETATION,
                             history=[{'role': 'user',
                                       'content': '是使用NIFTY B3分析流程吗？我不确定。'}])
    assert 'B3' not in text and '版本尚未确认' in text


def test_candidate_source_cannot_become_a_formal_answer(tmp_path):
    def mutate(card, root):
        for source in card['sources'].values():
            source['type'] = 'candidate_only'
            source['path'] = str(root/'candidate-draft.md')
        (root/'candidate-draft.md').write_text('XO变化肯定是污染，严重程度已经降低。')
    turn, text = run_blocked(INTERPRETATION, cards_dir=tampered_card(tmp_path, mutate))
    assert '污染' not in text and '严重程度' not in text and '已检索' not in text
    assert GAP_HEADING in text and '来源' in text
    assert (turn.expert_route_contract or {}).get('formal_answer_allowed') is not True


def test_absent_source_paths_fail_closed(tmp_path):
    def mutate(card, root):
        for source in card['sources'].values():
            source['path'] = str(root/'absent.md')
    turn, text = run_blocked(INTERPRETATION, cards_dir=tampered_card(tmp_path, mutate))
    assert GAP_HEADING in text and '来源' in text
    assert '性染色体分析流程' not in text
    assert (turn.expert_route_contract or {}).get('bounded_expert_answer_allowed') is not True


def test_card_product_or_branch_tampering_falls_back(tmp_path):
    def product(card, root):
        card['product'] = 'PMseq'

    def branch(card, root):
        card['decision_branches']['sex_chromosome']['clarification_policy']['template_id'] = 'other_branch'

    for mutate in (product, branch):
        turn, text = run_blocked(INTERPRETATION, cards_dir=tampered_card(tmp_path, mutate))
        assert GAP_HEADING in text and '性染色体分析流程' not in text
        assert (turn.expert_route_contract or {}).get('formal_answer_allowed') is not True


def test_direct_or_tampered_payloads_never_display_strings():
    from scripts.hermes_workflow_clarification import build_workflow_clarification, render_expert_clarification
    payload = build_workflow_clarification(CARD, 'sex_chromosome', INTERPRETATION)
    assert payload
    variants = [{'acknowledgement': SYNTHETIC, 'gaps': [{'question': SYNTHETIC}],
                 'next_action': SYNTHETIC}]
    for key, value in [('scope', {'product': 'PMseq', 'workflow': 'other',
                                  'branch': 'target_autosome'}),
                       ('missing', [SYNTHETIC]), ('binding', {}), ('integrity', 'f'*64)]:
        bad = deepcopy(payload)
        bad[key] = value
        variants.append({'workflow': bad})
    bad = deepcopy(payload)
    bad.pop('integrity')
    variants.append({'workflow': bad})
    for candidate in variants:
        text = render_expert_clarification(candidate, product='NIFTY')
        assert SYNTHETIC not in text and '完整结果字段' in text


def test_source_and_scope_binding_survives_only_json_formatting(tmp_path, monkeypatch):
    # Isolated file-binding unit control; full callable integration is covered above.
    from scripts import hermes_workflow_clarification as policy
    canonical = tmp_path/policy.CARD_PATH
    canonical.parent.mkdir(parents=True)
    source = tmp_path/policy.SOURCE_PATH
    source.parent.mkdir(parents=True)
    source.write_bytes((K/policy.SOURCE_PATH).read_bytes())
    original = json.loads((K/policy.CARD_PATH).read_text())
    monkeypatch.setattr(policy, '__file__',
                        str(tmp_path/'scripts/hermes_workflow_clarification.py'))
    canonical.write_text(json.dumps(original, ensure_ascii=False,
                                    sort_keys=True, separators=(',', ':')))
    assert policy.build_workflow_clarification(canonical, 'sex_chromosome', INTERPRETATION)
    for kind in ['product', 'workflow', 'branch', 'source_type', 'source_path',
                 'source_version', 'policy_text']:
        card = deepcopy(original)
        if kind == 'product':
            card['product'] = 'PMseq'
        elif kind == 'workflow':
            card['workflow_id'] = 'other'
        elif kind == 'branch':
            card['decision_branches']['target_autosome']['clarification_policy'] = (
                card['decision_branches']['sex_chromosome'].pop('clarification_policy'))
        elif kind == 'source_type':
            card['sources']['sop_bin_039_b3']['type'] = 'candidate_only'
        elif kind == 'source_path':
            card['sources']['sop_bin_039_b3']['path'] = 'unverified.md'
        elif kind == 'source_version':
            card['sources']['sop_bin_039_b3']['version'] = 'unverified'
        else:
            card['decision_branches']['sex_chromosome']['clarification_policy']['next_action'] = SYNTHETIC
        canonical.write_text(json.dumps(card, ensure_ascii=False))
        assert not policy.build_workflow_clarification(canonical, 'sex_chromosome', INTERPRETATION)
    canonical.write_text(json.dumps(original, ensure_ascii=False))
    source.write_text('synthetic unverified replacement')
    assert not policy.build_workflow_clarification(canonical, 'sex_chromosome', INTERPRETATION)
