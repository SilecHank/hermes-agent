"""Synthetic rejection controls from independent B02 review, not medical facts."""
import json
from tests.gateway.test_after_sales_b02_clarification import K, QUESTION, run_turn

SYNTHETIC = '该样本已经转阴，严重程度降低。'


def test_candidate_policy_fields_cannot_become_final_text(tmp_path):
    for field in ['acknowledgement', 'questions', 'next_action']:
        card = json.loads((K/'knowledge-base/workflows/facts/nifty-report-interpretation.json').read_text())
        card['decision_action_catalog'] = str(K/'knowledge-base/workflows/decision-actions.json')
        for source in card['sources'].values():
            source['type'] = 'candidate_only'; source['path'] = str(tmp_path/'draft.md')
        (tmp_path/'draft.md').write_text('synthetic unverified source')
        policy = card['decision_branches']['sex_chromosome']['clarification_policy']
        policy[field] = ([{'id':'result_field','question':SYNTHETIC,'match_groups':[['unknown']]}]
                         if field == 'questions' else SYNTHETIC)
        (tmp_path/'card.json').write_text(json.dumps(card))
        _, text = run_turn(cards_dir=tmp_path)
        assert SYNTHETIC not in text
        assert '完整结果字段' in text and '分析流程' in text


def test_uncertainty_and_negation_do_not_confirm_repeat_or_version():
    _, text = run_turn(QUESTION + '。没有重新采血，可能是重分析。')
    assert '两次是否同次采血' in text
    _, text = run_turn(history=[{'role':'user','content':'是使用NIFTY B3分析流程吗？我不确定。'}])
    assert 'B3' not in text and '版本尚未确认' in text


def test_sex_alone_keeps_both_concentration_fields_and_no_repeat_questions():
    _, text = run_turn(QUESTION + '。两次同次采血，这次重分析；完整字段是性染色体异常提示，对应结果如下。男胎。')
    assert 'X/Y' in text and '估算浓度' in text
    assert '完整字段及对应结果吗' not in text and '两次是否同次采血' not in text
    assert '胎儿性别信息是什么' not in text


def test_announcing_results_is_not_supplying_values():
    _, text = run_turn('无创两次性染色体标记发生变化，可能是什么原因。两次同次采血，这次重分析；完整字段是性染色体异常提示，对应结果如下。男胎。')
    assert '两次' in text and '对应结果' in text
    assert 'X/Y' not in text


def test_actual_followup_sex_keeps_xy_and_does_not_reask_known_core():
    history = [{'role':'user','content':QUESTION},
               {'role':'user','content':'两次同次采血，这次重分析；完整字段是性染色体异常提示，对应结果如下。'}]
    _, text = run_turn('男胎', history=history)
    assert 'X/Y' in text and '估算浓度' in text
    assert '完整字段' not in text and '同次采血' not in text


def test_direct_or_tampered_payloads_never_display_strings():
    from scripts.hermes_workflow_clarification import build_workflow_clarification, render_expert_clarification
    from copy import deepcopy
    payload = build_workflow_clarification(K/'knowledge-base/workflows/facts/nifty-report-interpretation.json',
                                            'sex_chromosome', QUESTION)
    assert payload
    variants = [ {'acknowledgement':SYNTHETIC, 'gaps':[{'question':SYNTHETIC}], 'next_action':SYNTHETIC} ]
    for key, value in [('scope', {'product':'PMseq','workflow':'other','branch':'target_autosome'}),
                       ('missing',[SYNTHETIC]), ('binding',{}), ('integrity','f'*64)]:
        p = deepcopy(payload);p[key] = value;variants.append({'workflow':p})
    p = deepcopy(payload);p.pop('integrity');variants.append({'workflow':p})
    for bad in variants:
        text = render_expert_clarification(bad, product='NIFTY')
        assert SYNTHETIC not in text and '完整结果字段' in text


def test_card_product_branch_and_source_tampering_fall_back(tmp_path):
    from copy import deepcopy
    original = json.loads((K/'knowledge-base/workflows/facts/nifty-report-interpretation.json').read_text())
    for kind in ['product', 'branch', 'source']:
        card = deepcopy(original);card['decision_action_catalog'] = str(K/'knowledge-base/workflows/decision-actions.json')
        if kind == 'product':card['product'] = 'PMseq'
        if kind == 'branch':card['decision_branches']['sex_chromosome']['clarification_policy']['template_id'] = 'other_branch'
        if kind == 'source':card['sources']['sop_bin_039_b3']['type'] = 'candidate_only'
        (tmp_path/'card.json').write_text(json.dumps(card))
        _, text = run_turn(cards_dir=tmp_path)
        assert '完整结果字段' in text and '性染色体分析流程' not in text


def test_single_axis_and_complete_followups_do_not_loop():
    # Synthetic user-input presence values, not thresholds or medical conclusions.
    history = [{'role':'user','content':QUESTION},
               {'role':'user','content':'NIFTY B3分析流程。两次同次采血，这次重分析；完整字段是性染色体异常提示。男胎。'}]
    _, partial = run_turn('X浓度=0', history=history)
    assert 'Y估算浓度' in partial and 'X/Y' not in partial
    assert '胎儿性别' not in partial and '完整字段' not in partial
    _, complete = run_turn('X浓度=0，Y浓度=0', history=history)
    assert '所需核对信息已记录' in complete
    assert '？' not in complete and '完整字段' not in complete
    assert '个案原因或报告结论' in complete


def test_invalid_policy_followup_also_stops_instead_of_falling_through(tmp_path):
    card = json.loads((K/'knowledge-base/workflows/facts/nifty-report-interpretation.json').read_text())
    card['decision_action_catalog'] = str(K/'knowledge-base/workflows/decision-actions.json')
    card['decision_branches']['sex_chromosome']['clarification_policy']['acknowledgement'] = SYNTHETIC
    (tmp_path/'card.json').write_text(json.dumps(card))
    _, text = run_turn('男胎', history=[{'role':'user','content':QUESTION}], cards_dir=tmp_path)
    assert '完整结果字段' in text and SYNTHETIC not in text


def test_source_and_scope_binding_survives_only_json_formatting(tmp_path, monkeypatch):
    # Isolated file-binding unit control; full callable integration is covered above.
    from copy import deepcopy
    from scripts import hermes_workflow_clarification as policy
    canonical = tmp_path/policy.CARD_PATH; canonical.parent.mkdir(parents=True)
    source = tmp_path/policy.SOURCE_PATH; source.parent.mkdir(parents=True)
    source.write_bytes((K/policy.SOURCE_PATH).read_bytes())
    original = json.loads((K/policy.CARD_PATH).read_text())
    monkeypatch.setattr(policy, '__file__', str(tmp_path/'scripts/hermes_workflow_clarification.py'))
    canonical.write_text(json.dumps(original, ensure_ascii=False, sort_keys=True, separators=(',',':')))
    assert policy.build_workflow_clarification(canonical, 'sex_chromosome', QUESTION)
    for kind in ['product','workflow','branch','source_type','source_path','source_version','policy_text']:
        card = deepcopy(original)
        if kind == 'product':card['product']='PMseq'
        elif kind == 'workflow':card['workflow_id']='other'
        elif kind == 'branch':card['decision_branches']['target_autosome']['clarification_policy'] = card['decision_branches']['sex_chromosome'].pop('clarification_policy')
        elif kind == 'source_type':card['sources']['sop_bin_039_b3']['type']='candidate_only'
        elif kind == 'source_path':card['sources']['sop_bin_039_b3']['path']='unverified.md'
        elif kind == 'source_version':card['sources']['sop_bin_039_b3']['version']='unverified'
        else:card['decision_branches']['sex_chromosome']['clarification_policy']['next_action']=SYNTHETIC
        canonical.write_text(json.dumps(card,ensure_ascii=False))
        assert not policy.build_workflow_clarification(canonical, 'sex_chromosome', QUESTION)
    canonical.write_text(json.dumps(original,ensure_ascii=False))
    source.write_text('synthetic unverified replacement')
    assert not policy.build_workflow_clarification(canonical, 'sex_chromosome', QUESTION)
