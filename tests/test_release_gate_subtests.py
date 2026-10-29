"""Gate reports count parent cases once while retaining every subtest failure."""
import json,os,subprocess,sys
from pathlib import Path
import pytest

@pytest.mark.parametrize('outcome', ['pass','fail','skip'])
def test_subtest_outcomes_are_not_lost(tmp_path,outcome):
    repo=Path(__file__).resolve().parents[1]
    action={'pass':'pass','fail':'self.assertEqual(i, 0)','skip':'self.skipTest("not run")'}[outcome]
    probe=tmp_path/'test_case.py'
    probe.write_text('import unittest\nclass Case(unittest.TestCase):\n def test_cases(self):\n  for i in range(2):\n   with self.subTest(i=i):\n    '+action+'\n')
    report=tmp_path/'report.json';env=dict(os.environ,HERMES_RELEASE_GATE_REPORT=str(report),HERMES_RELEASE_GATE_ATTEMPT='subtest',HERMES_TEST_DURATION_CACHE='0')
    subprocess.run([sys.executable,str(repo/'scripts/run_tests_parallel.py'),'--files',str(probe),'-j1','-q'],env=env,capture_output=True,text=True,timeout=40)
    data=json.loads(report.read_text())
    if outcome=='pass':
        assert data['status']=='ready'
        assert data['tests']==1 and data['passed']==1
    else:
        assert data['status']=='blocked'
        assert data['failed']>0 if outcome=='fail' else data['skipped']>0
