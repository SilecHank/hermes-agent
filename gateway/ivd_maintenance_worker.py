"""Deterministic IVD maintenance worker for the gateway command bus."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from gateway.maintenance_command_bus import MaintenanceCommandLedger


@dataclass(frozen=True)
class WorkerStep:
    name: str
    argv: tuple[str, ...]
    allow_failure: bool = False


Runner = Callable[..., subprocess.CompletedProcess]


def resolve_ivd_kb_root() -> Path:
    return Path(os.environ.get("HERMES_IVD_KB_ROOT") or "/home/slim/IVD-KnowledgeHub")


def build_default_ivd_maintenance_steps(kb_root: Path, *, python_executable: str | None = None) -> tuple[WorkerStep, ...]:
    py = python_executable or sys.executable
    out_dir = kb_root / "knowledge-base" / "_extracted" / "hermes-review-inbox"
    return (
        WorkerStep(
            "candidate_promotion_queue",
            (
                py,
                "scripts/hermes_candidate_promotion_queue.py",
                "--out-dir",
                str(out_dir),
                "--limit",
                "300",
                "--json",
            ),
        ),
        WorkerStep(
            "kb_conflict_detection",
            (py, "scripts/detect-kb-conflicts.py", "knowledge-base", "--json"),
            allow_failure=True,
        ),
        WorkerStep(
            "review_inbox_archive_plan",
            (py, "scripts/review-inbox-maintenance.py", "--out-dir", str(out_dir)),
        ),
        WorkerStep(
            "runtime_config_validation",
            (py, "scripts/hermes_runtime_config.py", "knowledge-base/config/hermes-runtime-concurrency.json"),
        ),
    )


def run_ivd_maintenance_worker(
    ledger: MaintenanceCommandLedger,
    command_id: str,
    *,
    kb_root: str | Path | None = None,
    scope: str = "default",
    runner: Runner = subprocess.run,
    steps: Sequence[WorkerStep] | None = None,
) -> Path:
    root = Path(kb_root) if kb_root is not None else resolve_ivd_kb_root()
    artifact_dir = ledger.path.parent / "ivd-maintenance-results"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifact_dir / f"{command_id}.json"
    ledger.mark_running(command_id)
    payload = {
        "command_id": command_id,
        "scope": scope,
        "kb_root": str(root),
        "started_at": _utc_now(),
        "status": "running",
        "steps": [],
    }

    status = "completed"
    error = ""
    for step in steps or build_default_ivd_maintenance_steps(root):
        started = time.monotonic()
        try:
            result = runner(
                list(step.argv),
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=300,
            )
            returncode = int(getattr(result, "returncode", 1))
            stdout = str(getattr(result, "stdout", "") or "")[-4000:]
            stderr = str(getattr(result, "stderr", "") or "")[-4000:]
        except Exception as exc:
            returncode = 1
            stdout = ""
            stderr = str(exc)
        payload["steps"].append(
            {
                "name": step.name,
                "argv": list(step.argv),
                "returncode": returncode,
                "allow_failure": step.allow_failure,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "stdout": stdout,
                "stderr": stderr,
            }
        )
        if returncode != 0 and not step.allow_failure:
            status = "failed"
            error = f"{step.name} failed"
            break

    payload["finished_at"] = _utc_now()
    payload["status"] = status
    artifact.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if status == "completed":
        ledger.mark_completed(command_id, artifact=str(artifact))
    else:
        ledger.mark_failed(command_id, error=error, artifact=str(artifact))
    return artifact


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
