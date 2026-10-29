"""Isolated compiled-package Gateway probe, with no provider or channel."""
import asyncio,copy,json,os,sys,tempfile,time
from dataclasses import replace
from pathlib import Path
P=Path(os.environ['IVD_KB_ROOT']).resolve();A=P.parent/'serving-agent'
sys.path.insert(0,str(A))
assert 'scripts' not in sys.modules, 'cold process must not have scripts preloaded'
from gateway.config import Platform
import gateway.ivd_final_validator as final_gate
from tests.gateway.test_ivd_gateway_lifecycle_integration import _runner,_source,_install_runtime,_write_projection
from pytest import MonkeyPatch

async def probe():
    observed=[];original=final_gate.validate_final_response
    def observe(**kwargs):
        result=original(**kwargs);observed.append({'text':kwargs.get('text'),'allowed':result.allowed,'reasons':list(result.reasons)});return result
    final_gate.validate_final_response=observe
    G={'enabled':True,'platforms':['weixin'],'workflow_module':str(P/'scripts/after_sales_workflow_gate.py'),
       'validator_module':str(P/'scripts/after_sales_answer_validator.py'),
       'cards_dir':str(P/'knowledge-base/workflows/facts'),'fast_response_module':str(P/'scripts/hermes_fast_response_pipeline.py')}
    class NoModel:
        def __init__(self,**kwargs):raise AssertionError('model boundary reached')
    with tempfile.TemporaryDirectory(dir=os.environ['HOME']) as tmp:
        config={'after_sales_guard':{**G,'serving_projection_path':str(_write_projection(Path(tmp)))}}
        patch=MonkeyPatch();_install_runtime(patch,config,NoModel)
        try:
            runner=_runner(config);runner._get_proxy_url=lambda:None
            outputs=[]
            for question in ['无创提取需要加入多少血浆','华泛安提取需要多少张石蜡？','无创血浆提取加多少？']:
                result=await runner._run_agent(question,'',[],_source(Platform.WEIXIN),'fixture')
                text=result['final_response'];assert result['api_calls']==0
                assert result['messages'][-1]['content']==text
                outputs.append({'question':question,'final_response':text,'api_calls':result['api_calls']})
            text=outputs[0]['final_response']
            assert all(x in text for x in ['200','手工','SOP-JL-269','A5','仅适用','确认'])
            assert '200' not in outputs[1]['final_response'] and '1–3' not in outputs[1]['final_response']
            assert '200' in outputs[2]['final_response']
            assert any(x['text']==text and x['allowed'] and not x['reasons'] for x in observed)
            from gateway.after_sales_guard import prepare_after_sales_turn,build_preflight_block_result,resolve_after_sales_task
            q=outputs[0]['question'];turn=prepare_after_sales_turn({'after_sales_guard':G},platform='weixin',message=q,history=[],actor_id='a')
            negatives=[]
            for label,t in [('missing_source',replace(turn,source_paths=())),('wrong_product',replace(turn,product_scope='WES')),('missing_helper',replace(turn,expert_validator_path='/nonexistent/expert.py'))]:
                text=build_preflight_block_result(t,q)['final_response'];assert '200' not in text;negatives.append({'case':label,'text':text})
            task=resolve_after_sales_task(G,q,actor_id='a');pending={'task':task,'question':q,'actor_id':'a','status':'awaiting_reply','expires_at':time.time()+300,'missing_fields':['method']};actors={}
            for label,actor,pending_state in [('same','a',pending),('wrong','b',pending),('expired','a',{**pending,'expires_at':0})]:
                t=prepare_after_sales_turn({'after_sales_guard':G},platform='weixin',message='手工',history=[],actor_id=actor,pending_task=pending_state);actors[label]=t.task_contract['continuation']
            assert actors=={'same':'clarification_reply','wrong':'new','expired':'new'}
            from scripts.hermes_fast_response_pipeline import build_fast_response_plan
            from scripts.hermes_conditional_evidence import validate_conditional_cards
            plan=build_fast_response_plan(q);card=plan['numeric_resolution']['candidates'][0]
            assert validate_conditional_cards([card],product='NIFTY',root=P)
            for key,value in [('effective_status','superseded'),('source_locator','sop_jl_269_a5:1-1'),('value','999'),('product_scope','WES')]:
                c=copy.deepcopy(card);c[key]=value;assert validate_conditional_cards([c],product='NIFTY',root=P)==[]
            c=copy.deepcopy(card);c['value']='999';assert validate_conditional_cards([card,c],product='NIFTY',root=P)==[]
            # Synthetic source files validate lifecycle and independently
            # supported same-scope conflicts without touching the package.
            fixture=Path(tmp)/'synthetic'; folder=fixture/'knowledge-base/official';folder.mkdir(parents=True)
            def source_card(doc,value,method='manual',status='current'):
                label='手工' if method=='manual' else '自动化';name=doc+'.md'
                (folder/name).write_text(f'---\nstatus: {status}\n---\n{doc} A1 NIFTY {label}提取\n样本裂解\n加入{value}uL血浆\n')
                c=copy.deepcopy(card);c.update(source_path='knowledge-base/official/'+name,
                    source_document_id=doc,source_version='A1',source_locator=doc.lower().replace('-','_')+'_a1:6-6',value=value)
                c['conditions']={'method':method,'sample_object':'血浆','step_label':'裂解步骤'}
                return c
            one=source_card('SOP-T-01','100');same_scope=source_card('SOP-T-02','150')
            auto_card=source_card('SOP-T-03','200',method='automatic')
            retired=source_card('SOP-T-04','100',status='superseded')
            same_value=source_card('SOP-T-05','100',method='automatic')
            missing_body=source_card('SOP-T-06','100');(folder/'SOP-T-06.md').write_text('')
            assert len(validate_conditional_cards([one,auto_card],product='NIFTY',root=fixture))==2
            assert len(validate_conditional_cards([one,same_value],product='NIFTY',root=fixture))==2
            assert validate_conditional_cards([one,same_scope],product='NIFTY',root=fixture)==[]
            assert validate_conditional_cards([retired],product='NIFTY',root=fixture)==[]
            assert validate_conditional_cards([missing_body],product='NIFTY',root=fixture)==[]
            explicit=build_fast_response_plan('NIFTY手工提取需要加入多少血浆');assert explicit['numeric_resolution']['status']=='resolved_unique'
            auto=build_fast_response_plan('无创自动化提取需要加入多少血浆');assert not auto['numeric_resolution'].get('numeric_answer_allowed')
            for name,module in list(sys.modules.items()):
                if name.startswith('scripts.'):
                    path=getattr(module,'__file__',None)
                    assert path and Path(path).resolve().is_relative_to(P/'scripts'),(name,path)
            return {'status':'ok','output':outputs,'final_validation':observed,'negative':negatives,'actors':actors,
                    'packaged_helper':str(Path(sys.modules['scripts.hermes_conditional_evidence'].__file__).resolve()),
                    'explicit_method_status':explicit['numeric_resolution']['status'],'automatic_status':auto['numeric_resolution']['status'],
                    'synthetic_checks':['different_methods_different_values','different_methods_same_value','same_scope_conflict','retired_source','empty_source']}
        finally:patch.undo()

if __name__=='__main__':
    print(json.dumps(asyncio.run(probe()),ensure_ascii=False))
