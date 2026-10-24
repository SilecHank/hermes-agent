"""Exercise the shipped startup graph, with no editable-source import fallback."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

# Build-time utility belongs to this authenticated Agent checkout, not the KB
# runtime scripts namespace. Load only the exact sibling file under a distinct
# test-tool name; do not append Agent scripts into the production namespace.
import importlib.util
_builder_path = Path(__file__).resolve().parents[1] / "scripts/build_ivd_serving_agent.py"
_builder_spec = importlib.util.spec_from_file_location("agent_serving_builder_test_tool", _builder_path)
_builder_module = importlib.util.module_from_spec(_builder_spec)
sys.modules[_builder_spec.name] = _builder_module
_builder_spec.loader.exec_module(_builder_module)
build_serving_agent = _builder_module.build_serving_agent


def _probe(source, artifact, private, entrypoint):
    for name in ('home', 'cache', 'tmp', 'cwd'):
        (private / name).mkdir(parents=True)
    # Block editable finders as well as sys.path leakage for every project root.
    project_names = sorted(
        {path.stem for path in source.glob('*.py')}
        | {path.name for path in source.iterdir()
           if path.is_dir() and (path / '__init__.py').is_file()}
        | {'gateway', 'agent', 'tools', 'plugins', 'hermes_cli', 'cron', 'acp', 'scripts'}
    )
    return subprocess.run(
        [sys.executable, '-I', '-B', '-s', str(source / 'tests/serving_agent_import_probe.py'),
         str(artifact), str(private), json.dumps(project_names), entrypoint],
        cwd=private / 'cwd',
        env={
            'HOME': str(private / 'home'), 'HERMES_HOME': str(private / 'home/hermes'),
            'XDG_CACHE_HOME': str(private / 'cache'), 'TMPDIR': str(private / 'tmp'),
            'PATH': os.defpath, 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
        },
        capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize('entrypoint', ['gateway.ivd_runtime', 'gateway.run'])
def test_serving_artifact_cold_start_import_is_closed(tmp_path, entrypoint):
    source = Path(__file__).resolve().parents[1]
    built = build_serving_agent(source, tmp_path / 'serving-agent')
    result = _probe(source, built.root, tmp_path / 'private', entrypoint)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload['status'] == 'ready'
    for name in ('gateway.ivd_runtime', 'gateway.ivd_dispatcher', 'gateway.ivd_parameter_scope'):
        assert name in payload['origins']
    assert all(not Path(path).is_absolute() for path in payload['origins'].values())


@pytest.mark.parametrize('omitted', [
    'gateway/ivd_parameter_scope.py', 'tools/threat_patterns.py', 'tools/skill_provenance.py',
])
def test_missing_startup_dependency_cannot_fall_back_to_editable_source(tmp_path, omitted):
    source = Path(__file__).resolve().parents[1]
    allowlist = json.loads((source / 'knowledge-base/ivd-serving-agent-allowlist.json').read_text())
    allowlist['paths'].remove(omitted)
    override = tmp_path / 'allowlist.json'
    override.write_text(json.dumps(allowlist))
    built = build_serving_agent(source, tmp_path / 'serving-agent', override)
    result = _probe(source, built.root, tmp_path / 'private', 'gateway.run')
    assert result.returncode != 0
    assert 'serving artifact missing ' + omitted[:-3].replace('/', '.') in result.stderr
