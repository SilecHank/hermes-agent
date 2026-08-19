"""Regression tests for trusted numeric-claim evidence from formal sources.

Covers the fix where ``_trusted_tool_numeric_evidence_details`` trusts
read_file calls that read formal/controlled source trees (serving package,
source vault, KB checkout, material library) even when the file was
discovered dynamically rather than listed in the turn's pre-injected
``source_paths``. Without this, the LLM's dynamically-found DOC numbers
(e.g. 5 mL, ≥30 ng/µL) were rejected by an empty whitelist, forcing answers
to strip all concrete values.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

GUARD_PATH = Path(__file__).resolve().parents[2] / "gateway" / "after_sales_guard.py"
RELEASE_ROOT = Path("/home/slim/.hermes/ivd-state/releases")


def _current_release_validator() -> Path:
    """Resolve the after_sales_answer_validator.py in the active release."""
    try:
        real = Path(
            subprocess.check_output(
                ["readlink", "-f", "/home/slim/.hermes/ivd-state/current"],
                text=True,
            ).strip()
        )
    except Exception:
        return Path("")
    candidate = real / "serving-package" / "scripts" / "after_sales_answer_validator.py"
    if candidate.exists():
        return candidate
    # Fallback: any release with a validator script.
    if RELEASE_ROOT.is_dir():
        for rel in sorted(RELEASE_ROOT.iterdir()):
            candidate = rel / "serving-package" / "scripts" / "after_sales_answer_validator.py"
            if candidate.exists():
                return candidate
    return Path("")


@pytest.fixture(scope="module")
def validator():
    vpath = _current_release_validator()
    if not vpath.exists():
        pytest.skip("active release validator script not present")
    spec = importlib.util.spec_from_file_location("test_validator_mod", str(vpath))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_validator_mod"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("test_guard_mod", str(GUARD_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_guard_mod"] = module
    spec.loader.exec_module(module)
    return module


def _formal_doc_path() -> str:
    """A path under the active release's serving-package knowledge-base."""
    if RELEASE_ROOT.is_dir():
        for rel in sorted(RELEASE_ROOT.iterdir()):
            kb = rel / "serving-package" / "knowledge-base"
            if kb.is_dir():
                for p in kb.rglob("*.md"):
                    return str(p)
    return f"{RELEASE_ROOT}/0000/serving-package/knowledge-base/products/carrier-manual.md"


def _messages_for(doc_path: str, tool_content: str, call_id: str = "call_doc") -> list[dict]:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": doc_path}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": tool_content},
    ]


def test_formal_source_trusted_with_empty_source_paths(guard, validator):
    doc = _formal_doc_path()
    messages = _messages_for(
        doc,
        "采血量：5 mL EDTA 抗凝管；gDNA 浓度：≥30 ng/µL；全血：≥2 mL；唾液：≥10 mL",
    )
    claims, source_read, sources = guard._trusted_tool_numeric_evidence_details(
        messages, [], validator)
    assert source_read is True
    assert "5 mL" in claims
    assert "≥30 ng/µL" in claims
    assert "≥2 mL" in claims
    assert "≥10 mL" in claims


def test_formal_source_trusted_outside_preinjected_paths(guard, validator):
    doc = _formal_doc_path()
    unrelated = str(Path(doc).parent / "unrelated-other.md")
    messages = _messages_for(
        doc,
        "羊水体积：15 mL；DNA 浓度：≥3 ng/µL；总量：≥30 ng",
    )
    claims, source_read, sources = guard._trusted_tool_numeric_evidence_details(
        messages, [unrelated], validator)
    assert source_read is True
    assert "15 mL" in claims
    assert "≥30 ng" in claims


def test_non_formal_source_not_trusted(guard, validator):
    messages = _messages_for(
        "/tmp/random-user-file.txt", "数值 42 ng/µL 5 mL", call_id="call_tmp")
    claims, source_read, sources = guard._trusted_tool_numeric_evidence_details(
        messages, [], validator)
    assert source_read is False
    assert claims == ()


def test_is_formal_source_path(guard):
    assert guard._is_formal_source_path(
        "/home/slim/.hermes/ivd-state/releases/x/serving-package/knowledge-base/01_标准作业指导书_SOP/NIFTY/a.md")
    assert guard._is_formal_source_path(
        "/home/slim/.hermes/ivd-state/releases/x/source-vault/objects/sha256/abc")
    assert guard._is_formal_source_path(
        "/mnt/d/FileServer/文件材料库/01_标准作业指导书_SOP/耳聋/a.pdf")
    assert guard._is_formal_source_path(
        "/mnt/d/iCloud/iCloudDrive/Workspace/文件材料库/07_设备试剂耗材清单/a.xlsx")
    assert not guard._is_formal_source_path("/tmp/random.txt")
    assert not guard._is_formal_source_path("/home/slim/Downloads/report.docx")
