"""Run the packaged Gateway in a fresh child process, without weakening bindings."""
import json,os,subprocess,sys
from pathlib import Path


def test_compiled_package_gateway_conditional_and_other_product():
    agent=Path(__file__).resolve().parents[2]
    candidate=agent.parent if agent.name=='serving-agent' else agent.parent/'private-candidate'
    package=candidate/'serving-package';serving_agent=candidate/'serving-agent'
    env=os.environ.copy();env['IVD_KB_ROOT']=str(package)
    probe=serving_agent/'tests/gateway/conditional_cold_probe.py'
    run=subprocess.run([sys.executable,'-I','-B',str(probe)],cwd=serving_agent,env=env,capture_output=True,text=True,timeout=35)
    assert run.returncode==0,run.stderr+'\n'+run.stdout
    result=json.loads(run.stdout.strip())
    assert result['status']=='ok'
    text=result['output'][0]['final_response']
    assert '200' in text and '手工' in text and 'SOP-JL-269' in text
    assert '仅适用' in text and '确认' in text
    assert result['output'][0]['api_calls']==0
    assert Path(result['packaged_helper']).is_relative_to(package/'scripts')
    (candidate.parent/'package-cold-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
