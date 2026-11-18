"""Run the packaged Gateway in a fresh child process, without weakening bindings."""
import json,os,subprocess,sys
from pathlib import Path


def _build_candidate(tmp_path, agent, kb):
    """Compile and stage genuine artifacts from isolated copies of both HEADs."""
    copies = tmp_path/'repos'
    copies.mkdir()
    for source, name in ((agent, 'agent'), (kb, 'knowledgehub')):
        subprocess.run(['git', 'clone', '--quiet', '--depth=1', source.as_uri(),
                        str(copies/name)], check=True, capture_output=True, text=True, timeout=120)
    runner = tmp_path/'stage.py'
    runner.write_text('''
import json, os, subprocess, sys
from pathlib import Path
root = Path(sys.argv[1]); agent = root/'repos/agent'; kb = root/'repos/knowledgehub'
sys.path.insert(0, str(kb))
from scripts.hermes_runtime_paths import RuntimePaths
from scripts.hermes_transactional_deploy import stage_release
from scripts.hermes_unified_release_builder import prepare_unified_release
from scripts.test_hermes_transactional_deploy import _manifest
def head(repo):
    return subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
compiled = subprocess.run([sys.executable, str(kb/'scripts/hermes_compile_candidate.py'),
                           '--repo-root', str(kb), '--output-root', str(root/'preview')],
                          cwd=kb, capture_output=True, text=True, check=True, timeout=180)
digests = json.loads(compiled.stdout)
manifest = _manifest(head(kb), head(agent), package_digest=digests['package_digest'],
                     source_vault_digest=digests['source_vault_digest'])
paths = RuntimePaths(home=root/'home', hermes_home=root/'hermes', kb_root=kb,
                     agent_root=agent, state_root=root/'state')
staged = stage_release(manifest, paths)
if staged.get('status') != 'ready':
    raise RuntimeError('stage_release: ' + json.dumps(staged))
built = prepare_unified_release(staged['path'], manifest)
if built.get('status') != 'ready':
    raise RuntimeError('prepare_unified_release: ' + json.dumps(built))
print(json.dumps({'path': staged['path']}))
''', encoding='utf-8')
    env = os.environ.copy()
    env.update(HOME=str(tmp_path), HERMES_HOME=str(tmp_path/'hermes'),
               PYTHONDONTWRITEBYTECODE='1')
    run = subprocess.run([sys.executable, '-I', '-B', str(runner), str(tmp_path)],
                         cwd=tmp_path, env=env, capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, run.stderr + '\n' + run.stdout
    return Path(json.loads(run.stdout.strip())['path'])


def test_compiled_package_gateway_conditional_and_other_product(tmp_path):
    agent=Path(__file__).resolve().parents[2]
    if agent.name == 'serving-agent':
        candidate = agent.parent
    else:
        candidate = _build_candidate(tmp_path, agent, Path(os.environ['IVD_KB_ROOT']).resolve())
    package=candidate/'serving-package';serving_agent=candidate/'serving-agent'
    env=os.environ.copy();env['IVD_KB_ROOT']=str(package)
    # The serving agent deliberately excludes tests; run the fixed checkout's
    # probe with the real serving-agent first on the module search path.
    probe=agent/'tests/gateway/conditional_cold_probe.py'
    bootstrap = ('import runpy,sys;sys.path.insert(0,sys.argv[1]);'
                 'runpy.run_path(sys.argv[2],run_name="__main__")')
    run=subprocess.run([sys.executable,'-I','-B','-c',bootstrap,str(agent),str(probe)],cwd=serving_agent,env=env,capture_output=True,text=True,timeout=35)
    assert run.returncode==0,run.stderr+'\n'+run.stdout
    result=json.loads(run.stdout.strip())
    assert result['status']=='ok'
    text=result['output'][0]['final_response']
    assert '200' in text and '手工' in text and 'SOP-JL-269' in text
    assert '仅适用' in text and '确认' in text
    assert result['output'][0]['api_calls']==0
    assert Path(result['packaged_helper']).is_relative_to(package/'scripts')
