"""Real local gateway, no provider/model; KB archive has a test-only .git marker."""
import pytest
from gateway.config import Platform
from tests.gateway.test_public_task_boundary import G
from tests.gateway.test_ivd_gateway_lifecycle_integration import _runner,_source,_install_runtime,_write_projection

@pytest.mark.asyncio
async def test_real_gateway_exposes_scoped_grounded_value_without_model(monkeypatch,tmp_path):
    class NoModel:
        def __init__(self,**kwargs):raise AssertionError('unexpected model')
    config={'after_sales_guard':{**G,'serving_projection_path':str(_write_projection(tmp_path))}}
    _install_runtime(monkeypatch,config,NoModel)
    runner=_runner(config);runner._get_proxy_url=lambda:None
    result=await runner._run_agent('无创提取需要加入多少血浆','',[],_source(Platform.WEIXIN),'conditional-fixture')
    text=result['final_response']
    assert '200' in text and '手工' in text and 'SOP-JL-269' in text
    assert '仅适用' in text and '确认' in text
    assert result['api_calls']==0
    print('VISIBLE_FINAL_FIXTURE:',text)


def test_source_binding_or_product_mismatch_retains_non_numeric_stop():
    from dataclasses import replace
    from gateway.after_sales_guard import prepare_after_sales_turn,build_preflight_block_result
    q='无创提取需要加入多少血浆'
    turn=prepare_after_sales_turn({'after_sales_guard':G},platform='weixin',message=q,history=[],actor_id='fixture')
    assert turn.blocks_answer_generation
    for changed in [replace(turn,source_paths=()),replace(turn,product_scope='WES'),
                    replace(turn,expert_validator_path='/nonexistent/expert.py')]:
        text=build_preflight_block_result(changed,q)['final_response']
        assert '200' not in text
        assert '方法' in text


@pytest.mark.asyncio
@pytest.mark.parametrize('question',['华泛安提取需要多少张石蜡？','五癌共检石蜡样本需要多少张？','多少血浆'])
async def test_unknown_or_other_product_does_not_borrow_manual_number(monkeypatch,tmp_path,question):
    class NoModel:
        def __init__(self,**kwargs):raise AssertionError('unexpected model')
    config={'after_sales_guard':{**G,'serving_projection_path':str(_write_projection(tmp_path))}}
    _install_runtime(monkeypatch,config,NoModel)
    runner=_runner(config);runner._get_proxy_url=lambda:None
    result=await runner._run_agent(question,'',[],_source(Platform.WEIXIN),'negative-fixture')
    assert '200' not in result['final_response']
    assert result['api_calls']==0


def test_trusted_method_followup_and_untrusted_reply_are_not_equivalent():
    import time
    from gateway.after_sales_guard import prepare_after_sales_turn,resolve_after_sales_task
    q='无创提取需要加入多少血浆'; task=resolve_after_sales_task(G,q,actor_id='a')
    pending={'task':task,'question':q,'actor_id':'a','status':'awaiting_reply',
             'expires_at':time.time()+300,'missing_fields':['method']}
    live=prepare_after_sales_turn({'after_sales_guard':G},platform='weixin',message='手工',history=[],actor_id='a',pending_task=pending)
    other=prepare_after_sales_turn({'after_sales_guard':G},platform='weixin',message='手工',history=[],actor_id='b',pending_task=pending)
    expired=prepare_after_sales_turn({'after_sales_guard':G},platform='weixin',message='手工',history=[],actor_id='a',pending_task={**pending,'expires_at':0})
    assert live.task_contract['continuation']=='clarification_reply'
    assert live.task_contract['task_id']==task['task_id']
    assert other.task_contract['continuation']=='new'
    assert expired.task_contract['continuation']=='new'
