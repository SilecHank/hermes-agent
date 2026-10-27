"""Private source bytes -> real projection/route -> actual final callback.

These documents simulate authority types; they are not business-approved sources.
"""
import importlib
import json
import os
from pathlib import Path
import pytest
from gateway.after_sales_guard import AfterSalesTurn, CriticalAfterSalesValidator, _load_module

K = Path(os.environ['IVD_KB_ROOT'])


def modules():
    path = K/'scripts/hermes_fast_response_pipeline.py'
    _load_module(str(path), path.stat().st_mtime_ns)
    return (importlib.import_module('scripts.hermes_expert_evidence_projection'),
            importlib.import_module('scripts.hermes_expert_route_contract'))


def contract_and_validator(projection):
    _, route = modules()
    bundle = projection.get('bundle') or projection.get('reference_bundle') or {}
    contract = route.build_expert_route_contract(
        {'action':'expert_fallback','evidence_tier':projection['tier'],
         'product_line':'NIFTY','question_type':'why_or_mechanism'},
        evidence_bundle=bundle or None,
        registry=json.loads((K/'knowledge-base/config/ivd-expert-capability-registry.json').read_text()),
        graph=json.loads((K/'knowledge-base/config/ivd-diagnostic-graph.json').read_text()))
    task = {'task_id':'private-source-boundary','subject':'NIFTY','context':{},
            'question':'水化状态如何影响核酸洗脱','intent':'mechanism'}
    turn = AfterSalesTurn(context='', facts={}, validator=None, allowed_numeric_claims=(),
        task_contract=task, expert_route_contract=contract, expert_evidence_bundle=bundle,
        expert_validator_path=str(K/'scripts/hermes_expert_answer_contract.py'))
    return bundle, contract, CriticalAfterSalesValidator(turn,messages_provider=lambda:[])


def answer(record, text, *, kind='hypothesis', field='text'):
    excerpt = record['excerpts'][0]
    claim = {'kind':kind,'scope':'general','text':'水化状态可能影响核酸洗脱表现。',
        'verification':'核对原始观察记录。','boundary':'这是一般推断，不能判定个案原因。',
        'evidence_ids':[record['evidence_id']], 'support':[{
            'evidence_id':record['evidence_id'],'quote':excerpt['text'],
            'start_line':excerpt['start_line'],'relation':'用于核对来源中的观察描述。'}]}
    claim[field] = text
    return json.dumps({'task_id':'private-source-boundary','claims':[claim]},ensure_ascii=False)


HEADERS = [
    ('active','---\nstatus: active\n---\n',True),
    ('bom-active','\ufeff---\nstatus: active\n---\n',True),
    ('bom-crlf-active','\ufeff---\r\nstatus: active\r\n---\r\n',True),
    ('quoted-active','---\n"status": \'active\' # current\n---\n',True),
    ('retired','---\nstatus: superseded\n---\n',False),
    ('bom-retired','\ufeff---\nstatus: superseded\n---\n',False),
    ('conflict','---\nstatus: active\nstatus: superseded\n---\n',False),
    ('reverse-conflict','---\nstatus: superseded\nstatus: active\n---\n',False),
    ('same-duplicate','---\nstatus: active\nstatus: active\n---\n',False),
    ('case-conflict','---\nstatus: active\nSTATUS: superseded\n---\n',False),
    ('quoted-conflict','---\nstatus: active\n"status": superseded\n---\n',False),
    ('truncated','---\nstatus: active\n',False),
    ('malformed','---\nstatus: "active\n---\n',False),
    ('list-status','---\nstatus: [active]\n---\n',False),
    ('empty-status','---\nstatus:\n---\n',False),
    ('opaque-metadata','---\nstatus: active\nnot valid metadata\n---\n',False),
]

@pytest.mark.parametrize('name,header,allowed',HEADERS,ids=[row[0] for row in HEADERS])
def test_lifecycle_differential_through_real_final_validator(tmp_path,name,header,allowed):
    projection, _ = modules()
    relative = 'knowledge-base/reference/private.md'
    path = tmp_path/relative;path.parent.mkdir(parents=True)
    quote = '水化状态可能影响核酸洗脱表现。'
    path.write_text(header+'## 水化状态\n'+quote+'\n',encoding='utf-8',newline='')
    card = {'fact_key':'private-type-fixture','source_path':relative,
        'effective_status':'active','authority_level':'formal_authority',
        'source_version':'general','product_scope':'','answer_text':quote}
    projected = projection.project_expert_evidence(root=tmp_path,formal_cards=[card],question='水化状态')
    bundle, contract, validator = contract_and_validator(projected)
    if allowed:
        assert projected['tier']=='formal_authority'
        assert contract['formal_answer_allowed']
        result = validator(answer(bundle['evidence'][0],quote,kind='fact'))
        assert result['ok'] and '来源摘录' in result['normalized_response']
    else:
        assert projected['tier']=='no_evidence'
        assert not contract['formal_answer_allowed']
        result = validator(json.dumps({'task_id':'private-source-boundary','claims':[{
            'kind':'fact','scope':'general','text':quote,'evidence_ids':['unadmitted'],'support':[]}]}))
        assert not result['ok']


def mixed_projection(tmp_path,other=None,*,other_has_number=False):
    projection,_ = modules()
    root = tmp_path
    folder = root/'knowledge-base/reference';folder.mkdir(parents=True)
    external = folder/'external.md'
    external.write_text('---\nstatus: active\n---\n## 水化状态\n水化状态试验观察量200 uL。\n')
    kwargs={'literature_items':[{'path':str(external.relative_to(root))}]}
    if other:
        path=folder/'admitted.md'
        quote = '水化状态试验观察量200 uL。' if other_has_number else '水化状态可能影响核酸洗脱表现。'
        path.write_text('---\nstatus: active\n---\n## 水化状态\n'+quote+'\n')
        if other=='controlled_reference':
            kwargs['reviewed_cases']=[{'case_id':'private','status':'current',
                'source_ref':str(path.relative_to(root))}]
        else:
            kwargs['formal_cards']=[{'fact_key':'private','effective_status':'active',
                'authority_level':'formal_authority','source_path':str(path.relative_to(root)),
                'source_version':'general','product_scope':'','answer_text':quote}]
    return projection.project_expert_evidence(root=root,question='水化状态',**kwargs)


@pytest.mark.parametrize('other',[None,'controlled_reference','formal_authority'])
@pytest.mark.parametrize('field',['text','verification','boundary'])
def test_external_number_cannot_borrow_mixed_authority(tmp_path,other,field):
    p=mixed_projection(tmp_path,other)
    bundle,contract,validator=contract_and_validator(p)
    assert contract['status']=='ready'
    record=next(r for r in bundle['evidence'] if r['tier']=='external_reference')
    result=validator(answer(record,'水化状态试验观察量200 uL。',field=field))
    assert not result['ok']
    assert 'content_authority:measurement:'+field in result['reasons']


@pytest.mark.parametrize('tier',['controlled_reference','formal_authority'])
def test_only_actually_cited_admitted_source_can_support_measurement(tmp_path,tier):
    p=mixed_projection(tmp_path,tier,other_has_number=True)
    bundle,contract,validator=contract_and_validator(p)
    assert contract['status']=='ready'
    admitted=next(r for r in bundle['evidence'] if r['tier']==tier)
    external=next(r for r in bundle['evidence'] if r['tier']=='external_reference')
    assert not validator(answer(external,'水化状态试验观察量200 uL。'))['ok']
    good=validator(answer(admitted,'水化状态试验观察量200 uL。'))
    assert good['ok'] and '200 uL' in good['normalized_response']
    assert not validator(answer(admitted,'投入量必须设置为200 uL。'))['ok']


@pytest.mark.parametrize('other',[None,'controlled_reference','formal_authority'])
def test_external_non_numeric_hypothesis_stays_available(tmp_path,other):
    p=mixed_projection(tmp_path,other)
    bundle,contract,validator=contract_and_validator(p)
    record=next(r for r in bundle['evidence'] if r['tier']=='external_reference')
    result=validator(answer(record,'水化状态可能影响核酸洗脱表现。'))
    assert result['ok'] and '核查' in result['normalized_response']
