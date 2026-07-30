"""Deterministic IVD maintenance worker for the gateway command bus."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

from gateway.maintenance_command_bus import MaintenanceCommandLedger


@dataclass(frozen=True)
class WorkerStep:
    name: str
    argv: tuple[str, ...]
    allow_failure: bool = False


Runner = Callable[..., subprocess.CompletedProcess]


def resolve_ivd_kb_root() -> Path:
    return Path(os.environ.get("HERMES_IVD_KB_ROOT") or "/home/slim/IVD-KnowledgeHub")


def build_default_ivd_maintenance_steps(
    kb_root: Path,
    *,
    python_executable: str | None = None,
    run_date: str | None = None,
) -> tuple[WorkerStep, ...]:
    py = python_executable or sys.executable
    maintenance_date = run_date or time.strftime("%Y-%m-%d", time.localtime())
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
            (py, "scripts/hermes_incremental_conflict_scan.py", "--repo-root", "."),
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
        WorkerStep(
            "daily_maintenance_runner",
            (
                py,
                "scripts/hermes_daily_maintenance_runner.py",
                "--date",
                maintenance_date,
                "--repo-root",
                ".",
                "--execute",
            ),
        ),
        WorkerStep(
            "portable_state_sync",
            ("bash", "scripts/hermes-portable-state-sync.sh"),
            allow_failure=True,
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
    worker_lock_path: str | Path | None = None,
    worker_lock_timeout_seconds: float = 1.0,
    artifact_max_age_seconds: int = 14 * 24 * 3600,
) -> Path:
    root = Path(kb_root) if kb_root is not None else resolve_ivd_kb_root()
    artifact_dir = ledger.path.parent / "ivd-maintenance-results"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prune_ivd_worker_artifacts(artifact_dir, max_age_seconds=artifact_max_age_seconds)
    artifact = artifact_dir / f"{command_id}.json"
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
    lock_path = Path(worker_lock_path) if worker_lock_path is not None else ledger.path.with_suffix(".worker.lock")
    try:
        with _worker_locked(lock_path, timeout_seconds=worker_lock_timeout_seconds):
            ledger.mark_running(command_id)
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
    except TimeoutError:
        status = "failed"
        error = "worker_already_running"

    payload["finished_at"] = _utc_now()
    payload["status"] = status
    if error:
        payload["error"] = error
    artifact.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if status == "completed":
        ledger.mark_completed(command_id, artifact=str(artifact))
    else:
        ledger.mark_failed(command_id, error=error, artifact=str(artifact))
    return artifact


def prune_ivd_worker_artifacts(
    artifact_dir: str | Path,
    *,
    max_age_seconds: int = 14 * 24 * 3600,
    now_epoch: float | None = None,
) -> int:
    root = Path(artifact_dir)
    if not root.exists():
        return 0
    now = now_epoch if now_epoch is not None else time.time()
    removed = 0
    for path in root.glob("*.json"):
        try:
            if now - path.stat().st_mtime <= max_age_seconds:
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


@contextmanager
def _worker_locked(lock_path: Path, *, timeout_seconds: float) -> Iterator[None]:
    deadline = time.monotonic() + timeout_seconds
    fd: int | None = None
    while fd is None:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for IVD worker lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
