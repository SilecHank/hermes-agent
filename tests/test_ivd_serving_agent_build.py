from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.build_ivd_serving_agent import (
    ServingAgentBuildError,
    build_serving_agent,
    verify_serving_agent,
)


def _write(path: Path, content: bytes = b"pass\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _allowlist(root: Path, paths: list[str]) -> Path:
    value = {
        "schema_version": 1,
        "entrypoint": "gateway/run.py",
        "paths": paths,
        "forbidden_paths": [
            ".git",
            "tests",
            "skills",
            "tools/search_files.py",
        ],
    }
    target = root / "knowledge-base/ivd-serving-agent-allowlist.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value), encoding="utf-8")
    return target


def test_build_copies_only_explicit_allowlist_and_emits_bound_manifest(tmp_path):
    root = tmp_path / "agent"
    _write(root / "gateway/__init__.py", b"")
    _write(root / "gateway/run.py", b"VALUE = 1\n")
    _write(root / "gateway/ivd_runtime.py", b"VALUE = 2\n")
    _write(root / "tests/test_hidden.py")
    _write(root / "skills/hidden/SKILL.md")
    _write(root / "tools/search_files.py")
    _write(root / ".git/config")
    allowlist = _allowlist(
        root,
        ["gateway/__init__.py", "gateway/run.py", "gateway/ivd_runtime.py"],
    )

    result = build_serving_agent(root, tmp_path / "serving-agent", allowlist)

    assert result.file_count == 3
    assert (result.root / "gateway/run.py").read_bytes() == b"VALUE = 1\n"
    for forbidden in ("tests", "skills", "tools/search_files.py", ".git"):
        assert not (result.root / forbidden).exists()
    manifest = json.loads((result.root / "serving-agent-manifest.json").read_text())
    assert manifest["entrypoint"] == "gateway/run.py"
    assert [item["path"] for item in manifest["files"]] == sorted(
        ["gateway/__init__.py", "gateway/run.py", "gateway/ivd_runtime.py"]
    )
    run_entry = next(item for item in manifest["files"] if item["path"] == "gateway/run.py")
    assert run_entry["sha256"] == hashlib.sha256(
        (result.root / "gateway/run.py").read_bytes()
    ).hexdigest()
    assert verify_serving_agent(result.root)["status"] == "ready"


def test_repository_allowlist_builds_required_ivd_runtime_without_forbidden_surfaces(tmp_path):
    root = Path(__file__).resolve().parents[1]
    allowlist = root / "knowledge-base/ivd-serving-agent-allowlist.json"

    result = build_serving_agent(root, tmp_path / "serving-agent", allowlist)

    required = {
        "gateway/run.py",
        "gateway/ivd_dispatcher.py",
        "gateway/ivd_knowledge_engine.py",
        "gateway/ivd_renderer.py",
        "gateway/ivd_final_validator.py",
        "gateway/ivd_receipt_sink.py",
        "gateway/platforms/weixin.py",
        "gateway/platforms/qqbot/adapter.py",
        "plugins/platforms/wecom/adapter.py",
    }
    built = {
        item["path"]
        for item in json.loads(
            (result.root / "serving-agent-manifest.json").read_text()
        )["files"]
    }
    assert required <= built
    assert not any(
        path == ".git"
        or path.startswith((".git/", "tests/", "skills/", "tools/"))
        for path in built
    )

    script = (
        "import pathlib, sys; "
        f"root=pathlib.Path({str(result.root)!r}).resolve(); "
        "sys.path=[str(root)]+[p for p in sys.path if p and "
        "'unified-engine-phase-b' not in p and pathlib.Path(p).resolve()!=root]; "
        "import gateway.run; "
        "outside=[]; "
        "names=('gateway','agent','plugins','hermes_cli','cron'); "
        "[(outside.append((n,str(pathlib.Path(m.__file__).resolve())))) "
        "for n,m in list(sys.modules.items()) if n.split('.')[0] in names "
        "and getattr(m,'__file__',None) and not pathlib.Path(m.__file__).resolve().is_relative_to(root)]; "
        "assert not outside, outside"
    )
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=result.root,
        env={**os.environ, "PYTHONPATH": str(result.root)},
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "path",
    ["../outside.py", "/tmp/outside.py", "tests/test_x.py", "skills/x.py", "tools/search_files.py"],
)
def test_build_rejects_unsafe_or_forbidden_allowlist_members(tmp_path, path):
    root = tmp_path / "agent"
    _write(root / "gateway/run.py")
    allowlist = _allowlist(root, ["gateway/run.py", path])

    with pytest.raises(ServingAgentBuildError):
        build_serving_agent(root, tmp_path / "serving-agent", allowlist)


def test_allowlist_cannot_relax_mandatory_development_boundaries(tmp_path):
    root = tmp_path / "agent"
    _write(root / "gateway/run.py")
    _write(root / "tests/hidden.py")
    allowlist = _allowlist(root, ["gateway/run.py", "tests/hidden.py"])
    payload = json.loads(allowlist.read_text(encoding="utf-8"))
    payload["forbidden_paths"] = []
    allowlist.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ServingAgentBuildError):
        build_serving_agent(root, tmp_path / "serving-agent", allowlist)


def test_build_rejects_symlink_source_and_nonempty_destination(tmp_path):
    root = tmp_path / "agent"
    _write(root / "gateway/run.py")
    outside = tmp_path / "outside.py"
    _write(outside)
    (root / "gateway/link.py").symlink_to(outside)
    allowlist = _allowlist(root, ["gateway/run.py", "gateway/link.py"])

    with pytest.raises(ServingAgentBuildError):
        build_serving_agent(root, tmp_path / "serving-agent", allowlist)

    destination = tmp_path / "occupied"
    _write(destination / "existing")
    clean_allowlist = _allowlist(root, ["gateway/run.py"])
    with pytest.raises(ServingAgentBuildError):
        build_serving_agent(root, destination, clean_allowlist)


def test_build_preserves_executable_members_as_read_only_executables(tmp_path):
    root = tmp_path / "agent"
    entrypoint = root / "gateway/run.py"
    _write(entrypoint)
    entrypoint.chmod(0o755)
    allowlist = _allowlist(root, ["gateway/run.py"])

    result = build_serving_agent(root, tmp_path / "serving-agent", allowlist)

    assert (result.root / "gateway/run.py").stat().st_mode & 0o777 == 0o555
    assert verify_serving_agent(result.root)["status"] == "ready"


def test_verifier_rejects_added_or_modified_files(tmp_path):
    root = tmp_path / "agent"
    _write(root / "gateway/run.py")
    allowlist = _allowlist(root, ["gateway/run.py"])
    result = build_serving_agent(root, tmp_path / "serving-agent", allowlist)

    (result.root / "gateway/run.py").chmod(0o644)
    (result.root / "gateway/run.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ServingAgentBuildError):
        verify_serving_agent(result.root)


def test_verifier_rejects_writable_serving_members(tmp_path):
    root = tmp_path / "agent"
    _write(root / "gateway/run.py")
    allowlist = _allowlist(root, ["gateway/run.py"])
    result = build_serving_agent(root, tmp_path / "serving-agent", allowlist)

    (result.root / "gateway/run.py").chmod(0o644)
    with pytest.raises(ServingAgentBuildError):
        verify_serving_agent(result.root)

    (result.root / "gateway/run.py").write_text("pass\n", encoding="utf-8")
    result.root.chmod(0o755)
    _write(result.root / "unexpected.py")
    with pytest.raises(ServingAgentBuildError):
        verify_serving_agent(result.root)
