"""Real Gateway lifecycle and KB reads; only external Agent boundary is local."""
import json
import os
from pathlib import Path
import pytest
from tests.gateway.test_ivd_gateway_lifecycle_integration import _runner, _source, _install_runtime, _write_projection

K=Path(os.environ['IVD_KB_ROOT'])
G={'enabled':True,'platforms':['qqbot'],'workflow_module':str(K/'scripts/after_sales_workflow_gate.py'),
   'validator_module':str(K/'scripts/after_sales_answer_validator.py'),
   'cards_dir':str(K/'knowledge-base/workflows/facts'),
   'fast_response_module':str(K/'scripts/hermes_fast_response_pipeline.py')}

def package_config(tmp_path,mode):
 # Evaluation-only SQLite/dispatch fixture; no release compiler or remote input.
 import hashlib
 from tests.gateway.test_ivd_knowledge_engine import package
 from tests.gateway.test_ivd_dispatcher import _policy
 from tests.gateway.ivd_manifest_test_helpers import release_manifest, canonical_digest
 root=package.__wrapped__(tmp_path)
 vocab=root/'indexes/dispatch-vocabulary-v1.json'
 vocab.write_text(json.dumps(_policy(),ensure_ascii=False))
 path=root/'package-manifest.json';body=json.loads(path.read_text())
 body['members']['indexes/dispatch-vocabulary-v1.json']=hashlib.sha256(vocab.read_bytes()).hexdigest()
 body['package_digest']=canonical_digest({'algorithm':'sha256-canonical-members-v1','members':body['members']})
 path.write_text(json.dumps(body))
 projection=tmp_path/'fixture-release.json'
 projection.write_text(json.dumps(release_manifest({'serving_package_path':str(root),'serving_agent_path':str(tmp_path/'serving-agent'),
  'source_vault_path':str(tmp_path/'source-vault'),'dispatch_policy_path':str(vocab),'render_policy_path':str(root/'renders/render-policy.json'),
  'context_budget':8,'retrieval_budget':2,'skill_allowlist':[],'receipt_destination':str(tmp_path/'observability/receipt.jsonl')},package_digest=body['package_digest'])))
 return {'after_sales_guard':{**G,'engine_mode':mode,'serving_projection_path':str(projection)}}

def draft(context):
 supplied=json.loads(context.split('[本轮专家声明合同与实际摘录]\n',1)[1].splitlines()[0])
 record=next(r for r in supplied['bundle']['evidence'] if r['tier']=='controlled_reference' and r.get('excerpts'))
 e=record['excerpts'][0]
 return json.dumps({'task_id':supplied['task_id'],'claims':[{'kind':'hypothesis','scope':'general',
  'text':'表面水化状态可能影响核酸洗脱表现。','verification':'对照复溶前后的回收表现。',
  'boundary':'这是一般原理推断，不能判定个案原因或产品放行。','evidence_ids':[record['evidence_id']],
  'support':[{'evidence_id':record['evidence_id'],'quote':e['text'],'start_line':e['start_line'],
  'relation':'用于提出待验证的一般原理假设，不构成个案结论。'}]}]},ensure_ascii=False)

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['compatibility','package','hybrid'])
@pytest.mark.parametrize('prefix',['','专家模式，'])
@pytest.mark.parametrize('question',['NIFTY为什么磁珠过干影响回收','WES为什么磁珠过干影响回收'])
async def test_real_local_lifecycle_enters_expert_without_extra_instruction(monkeypatch,tmp_path,mode,prefix,question):
 calls=[]
 class Agent:
  def __init__(self,**kwargs):
   self.tools=[];self._session_messages=[];self.context=kwargs.get('ephemeral_system_prompt') or ''
  def run_conversation(self,message,**kwargs):
   calls.append((message,self.context,kwargs))
   answer=draft(self.context)
   return {'final_response':answer,'messages':[{'role':'assistant','content':answer}],'api_calls':1,'completed':True}
 config=package_config(tmp_path,mode)
 _install_runtime(monkeypatch,config,Agent)
 runner=_runner(config);runner._get_proxy_url=lambda:None
 q=prefix+question
 result=await runner._run_agent(q,'',[],_source(),'auto-expert-session')
 assert len(calls)==1
 assert q in calls[0][0]
 assert '本轮专家声明合同与实际摘录' in calls[0][1]
 assert '原理推断' in result['final_response'] and '核查' in result['final_response']
 assert not result['final_response'].lstrip().startswith('{')
 assert result['messages'][-1]['content']==result['final_response']

@pytest.mark.asyncio
@pytest.mark.parametrize('question', ['CNVseq的产品经理是谁','NIFTY手工提取血浆体积是多少','无创三条目标染色体单体的Z值应该是多少以下','NIFTY提取回收低怎么排查'])
async def test_real_local_lifecycle_hit_or_control_does_not_construct_model(monkeypatch,tmp_path,question):
 class Agent:
  def __init__(self,**kwargs):raise AssertionError('hit/control crossed model boundary')
 config={'after_sales_guard':{**G,'serving_projection_path':str(_write_projection(tmp_path))}}
 _install_runtime(monkeypatch,config,Agent)
 runner=_runner(config);runner._get_proxy_url=lambda:None
 result=await runner._run_agent(question,'',[],_source(),'hit-session')
 assert result['api_calls']==0
 assert result['final_response']


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome',['miss','failure','timeout'])
async def test_expert_failure_is_bounded_without_another_expert_attempt(monkeypatch,tmp_path,outcome):
 calls=[]
 class Agent:
  def __init__(self,**kwargs):self.tools=[];self._session_messages=[]
  def run_conversation(self,message,**kwargs):
   calls.append(message)
   if outcome=='failure':raise RuntimeError('local expert sentinel failure')
   if outcome=='timeout':raise TimeoutError('local expert sentinel timeout')
   return {'final_response':'','messages':[],'api_calls':1,'completed':True}
 config={'after_sales_guard':{**G,'serving_projection_path':str(_write_projection(tmp_path))}}
 _install_runtime(monkeypatch,config,Agent)
 runner=_runner(config);runner._get_proxy_url=lambda:None
 if outcome in {'failure','timeout'}:
  with pytest.raises(TimeoutError if outcome=='timeout' else RuntimeError):
   await runner._run_agent('NIFTY为什么磁珠过干影响回收','',[],_source(),'bounded-session')
 else:
  result=await runner._run_agent('NIFTY为什么磁珠过干影响回收','',[],_source(),'bounded-session')
  assert result.get('final_response')
 assert len(calls)==1


@pytest.mark.asyncio
async def test_new_task_does_not_reuse_old_expert_contract(monkeypatch,tmp_path):
 contracts=[]
 class Agent:
  def __init__(self,**kwargs):
   self.tools=[];self._session_messages=[];self.context=kwargs.get('ephemeral_system_prompt') or ''
  def run_conversation(self,message,**kwargs):
   supplied=json.loads(self.context.split('[本轮专家声明合同与实际摘录]\n',1)[1].splitlines()[0])
   contracts.append(supplied)
   answer=draft(self.context)
   return {'final_response':answer,'messages':[{'role':'assistant','content':answer}],'api_calls':1,'completed':True}
 config={'after_sales_guard':{**G,'serving_projection_path':str(_write_projection(tmp_path))}}
 _install_runtime(monkeypatch,config,Agent)
 runner=_runner(config);runner._get_proxy_url=lambda:None
 q='NIFTY为什么磁珠过干影响回收'
 first=await runner._run_agent(q,'',[],_source(),'first-task')
 second=await runner._run_agent('WES为什么磁珠过干影响回收','',[{'role':'user','content':q},{'role':'assistant','content':first['final_response']}],_source(),'second-task')
 assert len(contracts)==2
 assert contracts[0]['task_id']!=contracts[1]['task_id']
 assert contracts[1]['contract']['product_line']=='WES'
 assert '原理推断' in second['final_response']


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome',['failure','timeout'])
async def test_incoming_handler_closes_real_expert_exception_once(monkeypatch,tmp_path,outcome):
 from tests.gateway.test_42039_duplicate_user_message import _bootstrap, _event, _source as incoming_source
 calls=[]
 class Agent:
  def __init__(self,**kwargs):self.tools=[];self._session_messages=[]
  def run_conversation(self,message,**kwargs):
   calls.append(message)
   if outcome=='timeout':raise TimeoutError('bounded local expert timeout')
   raise RuntimeError('bounded local expert failure')
 config={'after_sales_guard':{**G,'platforms':['telegram'],'serving_projection_path':str(_write_projection(tmp_path))}}
 _install_runtime(monkeypatch,config,Agent)
 runner=_bootstrap(monkeypatch,tmp_path)
 runner._get_proxy_url=lambda:None
 event=_event();event.text='NIFTY为什么磁珠过干影响回收'
 result=await runner._handle_message_with_agent(event,incoming_source(),'agent:main:telegram:group:-1001:12345',1)
 assert len(calls)==1
 assert isinstance(result,str) and result
 assert 'error' in result.lower() or '错误' in result
