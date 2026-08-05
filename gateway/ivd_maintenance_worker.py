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
from typing import Any, Callable, Iterator, Sequence

from gateway.maintenance_command_bus import MaintenanceCommandLedger


@dataclass(frozen=True)
class WorkerStep:
    name: str
    argv: tuple[str, ...]
    allow_failure: bool = False


Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class LiveManagementResult:
    status: str
    reason: str
    report: dict[str, Any]
    message: str
    executed: bool = False
    queued: bool = False


def read_live_or_last_known(
    status_reader: Callable[[], dict[str, Any]],
    *,
    last_known_status: dict[str, Any] | None = None,
) -> LiveManagementResult:
    report = status_reader()
    if report.get("status") == "ready" and report.get("active_host") == "wsl-primary":
        return LiveManagementResult("ready", "live_status", report, "已读取 WSL 实时状态。")
    cached = dict(last_known_status or {})
    return LiveManagementResult(
        "degraded",
        "last_known_status",
        cached,
        "当前无法连接 WSL，只能显示最近一次状态；未执行也未排队任何修改。",
    )


def run_live_management_write(
    status_reader: Callable[[], dict[str, Any]],
    action: Callable[[], Any],
) -> LiveManagementResult:
    report = status_reader()
    if report.get("status") != "ready" or report.get("active_host") != "wsl-primary":
        return LiveManagementResult(
            "blocked",
            "live_preflight_unavailable",
            report,
            "当前无法连接 WSL，未执行也未排队任何修改。",
        )
    action()
    return LiveManagementResult(
        "ready", "write_executed", report, "维护操作已执行。", executed=True,
    )


def resolve_ivd_kb_root() -> Path:
    configured = os.environ.get("HERMES_IVD_RUNTIME_KB_ROOT")
    if configured:
        return Path(configured).expanduser()
    pinned_sibling = Path(__file__).resolve().parents[1].parent / "knowledgehub"
    if (pinned_sibling / "scripts/hermes-self-maintenance.py").is_file():
        return pinned_sibling
    return Path(os.environ.get("HERMES_IVD_KB_ROOT") or Path.home() / "IVD-KnowledgeHub").expanduser()


def build_default_ivd_maintenance_steps(
    kb_root: Path,
    *,
    python_executable: str | None = None,
    run_date: str | None = None,
    scope: str = "default",
) -> tuple[WorkerStep, ...]:
    py = python_executable or sys.executable
    maintenance_date = run_date or time.strftime("%Y-%m-%d", time.localtime())
    return (
        WorkerStep(
            "isolated_self_maintenance",
            (
                py,
                "-B",
                "scripts/hermes-self-maintenance.py",
                "run",
                "--scope",
                scope,
                "--date",
                maintenance_date,
                "--json",
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
            for step in steps or build_default_ivd_maintenance_steps(root, scope=scope):
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
