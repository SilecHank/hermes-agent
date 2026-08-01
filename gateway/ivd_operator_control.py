"""Fixed, read-mostly operator bridge for `/ivd status` and `/ivd repair`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable


Runner = Callable[..., subprocess.CompletedProcess]


def resolve_ivd_paths() -> tuple[Path, Path, Path]:
    home = Path(os.environ.get("HOME") or Path.home()).expanduser()
    hermes_home = Path(os.environ.get("HERMES_HOME") or home / ".hermes").expanduser()
    pinned_sibling = Path(__file__).resolve().parents[1].parent / "knowledgehub"
    configured = os.environ.get("HERMES_IVD_RUNTIME_KB_ROOT")
    if configured:
        kb_root = Path(configured).expanduser()
    elif (pinned_sibling / "scripts/hermes_oob_entrypoint.py").is_file():
        kb_root = pinned_sibling
    else:
        kb_root = Path(os.environ.get("HERMES_IVD_KB_ROOT") or home / "IVD-KnowledgeHub").expanduser()
    return kb_root, hermes_home / "ivd-state", hermes_home / "ivd-live-data"


def _safe_json(path: Path) -> object:
    try:
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _cron_status(live_root: Path, hermes_home: Path) -> dict[str, object]:
    roots = (live_root / "cron", hermes_home / "cron")
    cron_root = next((item for item in roots if (item / "jobs.json").is_file()), roots[0])
    payload = _safe_json(cron_root / "jobs.json")
    if isinstance(payload, dict):
        jobs = payload.get("jobs", [])
    elif isinstance(payload, list):
        jobs = payload
    else:
        jobs = []
    if not isinstance(jobs, list):
        return {"status": "degraded", "jobs": 0}
    try:
        heartbeat = float((cron_root / "ticker_last_success").read_text(encoding="utf-8").strip())
        age = max(0.0, time.time() - heartbeat)
    except (OSError, UnicodeError, ValueError):
        age = None
    status = "healthy" if age is not None and age <= 180 else "degraded"
    return {"status": status, "jobs": len(jobs), "heartbeat_age_seconds": age}


def read_ivd_operator_status(
    *,
    kb_root: str | Path | None = None,
    state_root: str | Path | None = None,
    live_root: str | Path | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, object]:
    default_kb, default_state, default_live = resolve_ivd_paths()
    kb = Path(kb_root) if kb_root is not None else default_kb
    state = Path(state_root) if state_root is not None else default_state
    live = Path(live_root) if live_root is not None else default_live
    script = kb / "scripts" / "hermes_oob_entrypoint.py"
    try:
        result = runner(
            [sys.executable, "-B", str(script), "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        payload = json.loads(str(result.stdout or ""))
        if result.returncode != 0 or not isinstance(payload, dict):
            raise ValueError("status_probe_failed")
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return {"status": "blocked", "reason": "status_probe_failed", "detail": type(exc).__name__}

    marker = _safe_json(state / "current" / ".verified.json")
    if isinstance(marker, dict) and isinstance(marker.get("knowledge_release_digest"), str):
        payload["knowledge_release_digest"] = marker["knowledge_release_digest"]
    payload["cron"] = _cron_status(live, state.parent)
    return payload


def run_ivd_safe_repair(
    *, kb_root: str | Path | None = None, runner: Runner = subprocess.run
) -> dict[str, object]:
    default_kb, _, _ = resolve_ivd_paths()
    kb = Path(kb_root) if kb_root is not None else default_kb
    script = kb / "scripts" / "hermes_oob_entrypoint.py"
    try:
        result = runner(
            [sys.executable, "-B", str(script), "repair", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
        payload = json.loads(str(result.stdout or ""))
        if not isinstance(payload, dict):
            raise ValueError("repair_result_invalid")
        return payload
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return {"status": "blocked", "reason": "repair_failed", "operator_message": f"安全修复未完成（{type(exc).__name__}）。"}


def format_ivd_operator_status(report: dict[str, object]) -> str:
    if report.get("status") != "ready":
        return "IVD 状态暂时无法读取，未执行任何修改。请稍后重试；若连续失败，请交由 Codex 排查。"
    owner = str(report.get("active_host") or "未知")
    generation = report.get("active_generation", "未知")
    release = str(report.get("current_release") or "未知")
    knowledge = str(report.get("knowledge_release_digest") or "未记录")
    platform = report.get("platform_health") if isinstance(report.get("platform_health"), dict) else {}
    platform_ok = platform.get("status") == "healthy"
    platforms = platform.get("platforms") if isinstance(platform.get("platforms"), dict) else {}
    platform_detail = "、".join(f"{name}={state}" for name, state in sorted(platforms.items())) or "无明细"
    cron = report.get("cron") if isinstance(report.get("cron"), dict) else {}
    cron_ok = cron.get("status") == "healthy"
    owner_ok = owner == "wsl-primary"
    healthy = owner_ok and platform_ok and cron_ok
    action = "无需操作" if healthy else "建议执行 `/ivd repair`；若仍异常，请交由 Codex 排查"
    return "\n".join((
        f"IVD 状态：{'正常' if healthy else '异常'}",
        f"生产主机：{owner}（generation {generation}）",
        f"三平台：{'正常' if platform_ok else '异常'}（{platform_detail}）",
        f"组合 Release：{release[:12]}",
        f"Knowledge Release：{knowledge[:12]}",
        f"Cron：{'正常' if cron_ok else '异常'}（{int(cron.get('jobs') or 0)} 项）",
        f"结论：{action}。",
    ))
