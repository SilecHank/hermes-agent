"""Cold real-entry regression: no KB path/finder/helper pre-import."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

A = Path(__file__).resolve().parents[2]
K = Path(os.environ['IVD_KB_ROOT']).resolve()


def cold(question, pollution=''):
    code = f'''import sys,json,types
from pathlib import Path
A=Path({str(A)!r});K=Path({str(K)!r})
sys.path.insert(0,str(A))
from gateway.after_sales_guard import prepare_after_sales_turn
assert 'scripts.hermes_workflow_clarification' not in sys.modules
{pollution}
g={{'enabled':True,'platforms':['qqbot'],'workflow_module':str(K/'scripts/after_sales_workflow_gate.py'),'validator_module':str(K/'scripts/after_sales_answer_validator.py'),'cards_dir':str(K/'knowledge-base/workflows/facts'),'fast_response_module':str(K/'scripts/hermes_fast_response_pipeline.py')}}
t=prepare_after_sales_turn({{'after_sales_guard':g}},platform='qqbot',message={question!r},history=[])
m=sys.modules['scripts.hermes_workflow_clarification']
assert Path(m.__file__).resolve()==K/'scripts/hermes_workflow_clarification.py'
print('cold_entry_completed')
'''
    return subprocess.run([sys.executable, '-I', '-B', '-c', code],
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize('question', ['你好', 'CNVseq的产品经理是谁'])
def test_cold_first_workflow_import_completes(question):
    done = cold(question)
    assert done.returncode == 0, done.stderr
    assert 'cold_entry_completed' in done.stdout


@pytest.mark.parametrize('pollution', [
    "m=types.ModuleType('scripts');m.__path__=['/tmp/foreign-scripts'];sys.modules['scripts']=m",
    "m=types.ModuleType('scripts.hermes_workflow_clarification');m.__file__='/tmp/other-release/helper.py';sys.modules[m.__name__]=m",
])
def test_foreign_namespace_or_cached_helper_is_rejected(pollution):
    done = cold('你好', pollution)
    assert done.returncode != 0
    assert 'workflow scripts' in done.stderr and 'conflict' in done.stderr
    assert 'cold_entry_completed' not in done.stdout



def test_greeting_after_contact_question_is_not_a_guard_fact_continuation():
    # Deterministic guard boundary only: downstream Agent/model history is not
    # executed here, so this cannot establish the reason for screenshot prose.
    code = "import sys\nfrom pathlib import Path\nA=Path({agent});K=Path({kb});sys.path.insert(0,str(A))\nfrom gateway.after_sales_guard import prepare_after_sales_turn\ng={{'enabled':True,'platforms':['qqbot'],'workflow_module':str(K/'scripts/after_sales_workflow_gate.py'),'validator_module':str(K/'scripts/after_sales_answer_validator.py'),'cards_dir':str(K/'knowledge-base/workflows/facts'),'fast_response_module':str(K/'scripts/hermes_fast_response_pipeline.py')}}\nfirst=prepare_after_sales_turn({{'after_sales_guard':g}},platform='qqbot',message='CNVseq的产品经理是谁',history=[])\nt=prepare_after_sales_turn({{'after_sales_guard':g}},platform='qqbot',message='你好',history=[{{'role':'user','content':'CNVseq的产品经理是谁'}}])\nassert t is not None and not t.blocks_answer_generation\nassert not t.facts\nprint('greeting_guard_boundary_only')\n".format(agent=repr(str(A)), kb=repr(str(K)))
    done = subprocess.run([sys.executable, '-I', '-B', '-c', code],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert 'greeting_guard_boundary_only' in done.stdout
