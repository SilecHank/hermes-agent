"""Release evidence comes from pytest events, never its display text."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('body,counts,ready', [
    ('def test_ok(): pass\n', {'tests': 1, 'passed': 1}, True),
    ('import pytest\n@pytest.mark.skip\ndef test_skip(): pass\n', {'tests': 1, 'skipped': 1}, False),
    ('# no tests\n', {'tests': 0}, False),
    ('def test_bad(): assert False\n', {'tests': 1, 'failed': 1}, False),
    ('import pytest\n@pytest.mark.xfail\ndef test_xfail(): assert False\n', {'tests': 1, 'xfailed': 1}, False),
    ('import pytest\n@pytest.mark.xfail\ndef test_xpass(): pass\n', {'tests': 1, 'xpassed': 1}, False),
    ('raise RuntimeError("collection")\n', {'errors': 1}, False),
])
def test_machine_report(tmp_path, body, counts, ready):
    repo = Path(__file__).resolve().parents[1]
    probe = tmp_path / 'test_probe.py'
    probe.write_text(body)
    report = tmp_path / 'report.json'
    env = dict(os.environ, HERMES_RELEASE_GATE_REPORT=str(report),
               HERMES_RELEASE_GATE_ATTEMPT='unique-attempt', HERMES_TEST_DURATION_CACHE='0')
    proc = subprocess.run([sys.executable, str(repo / 'scripts/run_tests_parallel.py'),
                           '--files', str(probe), '-j1', '-q'], env=env,
                          capture_output=True, text=True, timeout=40)
    assert report.exists(), proc.stdout + proc.stderr
    data = json.loads(report.read_text())
    assert data['schema_version'] == 1
    assert data['kind'] == 'pytest_suite'
    assert data['attempt_id'] == 'unique-attempt'
    assert (data['status'] == 'ready') is ready
    assert len(data['files']) == 1
    assert data['files'][0]['path'] == str(probe)
    assert data['files'][0]['completed'] is True
    for key, value in counts.items():
        assert data[key] == value
        assert data['files'][0][key] == value


def test_missing_machine_completion_cannot_reuse_stale_ready(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    probe = tmp_path / 'test_abrupt.py'
    probe.write_text('import os\ndef test_abrupt(): os._exit(0)\n')
    report = tmp_path / 'report.json'
    report.write_text('{"status": "ready", "attempt_id": "old"}')
    env = dict(os.environ, HERMES_RELEASE_GATE_REPORT=str(report),
               HERMES_RELEASE_GATE_ATTEMPT='new', HERMES_TEST_DURATION_CACHE='0')
    proc = subprocess.run([sys.executable, str(repo / 'scripts/run_tests_parallel.py'),
                           '--files', str(probe), '-j1'], env=env,
                          capture_output=True, text=True, timeout=40)
    assert proc.returncode == 0, proc.stdout
    data = json.loads(report.read_text())
    assert data['status'] == 'blocked'
    assert data['attempt_id'] == 'new'
    assert data['files'][0]['completed'] is False


def test_parallel_files_have_independent_machine_reports(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    probes = [tmp_path / f'test_pass_{i}.py' for i in range(2)]
    for probe in probes:
        probe.write_text('def test_pass(): pass\n')
    report = tmp_path / 'report.json'
    env = dict(os.environ, HERMES_RELEASE_GATE_REPORT=str(report),
               HERMES_RELEASE_GATE_ATTEMPT='parallel', HERMES_TEST_DURATION_CACHE='0')
    proc = subprocess.run([sys.executable, str(repo / 'scripts/run_tests_parallel.py'),
                           '--files', ':'.join(map(str, probes)), '-j2',
                           '--junitxml=' + str(tmp_path / 'shared.xml')], env=env,
                          capture_output=True, text=True, timeout=40)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(report.read_text())
    assert data['status'] == 'ready'
    assert data['tests'] == data['passed'] == 2
    assert {row['path'] for row in data['files']} == set(map(str, probes))
    assert all(row['tests'] == 1 and row['completed'] for row in data['files'])


def test_unfinished_requested_file_still_has_report_entry(tmp_path):
    from scripts.run_tests_parallel import _write_gate_report

    report = tmp_path / 'report.json'
    missing = tmp_path / 'test_never_completed.py'
    _write_gate_report(str(report), 'attempt', [missing], [], tmp_path)
    data = json.loads(report.read_text())
    assert data['status'] == 'blocked'
    assert data['files'] == [dict(path=missing.name, completed=False,
                                  returncode=None, tests=0, passed=0, failed=0,
                                  errors=0, skipped=0, xfailed=0, xpassed=0)]
