"""Fixed, read-mostly operator bridge for `/ivd status` and `/ivd repair`."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from gateway.ivd_source_policy import runtime_kb_root_allowed


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
    if not runtime_kb_root_allowed(kb):
        return {"status": "blocked", "reason": "auxiliary_workspace_forbidden"}
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


_VECTOR_QUESTION = re.compile(
    r"(?:请问|现在|目前|材料库(?:的)?|售后系统(?:的)?)*"
    r"全量向量(?:知识库|库|检索)(?:现在|目前|已经|是否|可以)*"
    r"(?:上线|启用|开启|开放|接入|可用|能用)(?:了吗|了没有|没有|了么|吗|了)?"
)
_RELEASE_CHANGES_QUESTION = re.compile(
    r"(?:请问)?(?:最近(?:一次)?|最新(?:一次)?|这次|本次|刚才)?售后系统(?:的)?"
    r"(?:最近(?:一次)?|最新(?:一次)?|这次|本次|刚才)?(?:发布|更新|版本)"
    r"(?:改了什么|更新了什么|有哪些改动|有哪些变化|提升了什么|有什么改进)"
)
_RELEASE_STATUS_QUESTION = re.compile(
    r"(?:请问)?售后系统(?:现在|当前|目前)?"
    r"(?:运行的是什么版本|运行的是哪个版本|线上版本是什么|当前版本是什么|上线了吗)"
)


def operator_question_kind(question: str) -> str:
    from gateway.turn_scaffolding import user_authored_text

    authored = user_authored_text(question).strip()
    if len(authored) > 120:
        return ""
    text = re.sub(r"[^\S\n]+", "", authored).rstrip("？?。！!")
    for pattern, kind in (
        (_VECTOR_QUESTION, "vector_status"),
        (_RELEASE_CHANGES_QUESTION, "release_changes"),
        (_RELEASE_STATUS_QUESTION, "release_status"),
    ):
        if pattern.fullmatch(text):
            return kind
    return {
        "应该还上线了一个全量向量知识库吧": "vector_status",
        "发布完成，提升了哪个方面": "release_changes",
        "这次发布提升了什么": "release_changes",
    }.get(text, "")


def _vector_status_text(config: object) -> str:
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return "当前全量向量检索未启用。"
    if config.get("scope") != "full_catalog":
        return "当前向量检索未开放全量范围。"
    root, originals = config.get("root"), config.get("originals")
    if not isinstance(root, str) or not isinstance(originals, str):
        return "已配置全量向量检索，但索引路径配置不完整。"
    index, source = Path(root), Path(originals)
    files = ("catalog.sqlite", "vectors.sqlite", "qwen.gguf", "build/bin/llama-embedding")
    if (not index.is_absolute() or not source.is_absolute() or not source.is_dir()
            or not all((index / name).is_file() for name in files)
            or not os.access(index / files[-1], os.X_OK)):
        return "已配置全量向量检索，但索引、原件目录或检索程序当前不可用。"
    snapshot = f"索引快照为 {index.name}。" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", index.name) else ""
    return ("全量向量检索已启用，索引和检索程序均可见。"
            + snapshot + "专家模式会按需检索材料。本次状态检查未执行检索。")


def format_operator_question(kind: str, guard: dict[str, object]) -> str:
    evidence = read_release_question_evidence()
    if evidence.get("status") != "verified":
        return "当前运行状态暂时无法确认，请稍后再查。"
    if kind == "vector_status":
        return _vector_status_text(guard.get("open_document_evidence"))
    release = evidence["release"]
    if not evidence.get("deployment_receipt_verified"):
        return f"当前网关运行版本为 {release}，对应部署回执暂未确认。"
    if kind == "release_status":
        channel = "渠道状态正常。" if evidence.get("channels_healthy") else "渠道健康状态暂未确认。"
        return f"当前网关运行版本为 {release}，部署回执已核对。{channel}"
    summary = evidence.get("change_summary")
    return (f"最近一次已核验发布为 {release}。" + str(summary)
            if summary else f"最近一次已核验发布为 {release}。发布回执暂无具体变更摘要。")


def _read_release_changes(state: Path, staged: dict[str, object]) -> list[str]:
    """Read an operator-authored note bound to this staged release."""
    notes = state / "oob" / "release-notes"
    path = notes / f"{staged['release_id']}.json"
    if notes.is_symlink() or path.is_symlink():
        return []
    note = _safe_json(path)
    if not isinstance(note, dict) or type(note.get("schema_version")) is not int or note["schema_version"] != 1:
        return []
    if note.get("release_id") != staged["release_id"]:
        return []
    for key in ("hermes_agent_commit", "knowledgehub_commit"):
        commit = staged.get(key)
        if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit) or note.get(key) != commit:
            return []
    changes = note.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= 5:
        return []
    if not all(isinstance(item, str) and 1 <= len(item) <= 160
               and item == item.strip() and item.isprintable() for item in changes):
        return []
    return changes


def read_release_question_evidence() -> dict[str, object]:
    """Return only bounded operating facts with an exact release binding."""
    status = read_ivd_operator_status()
    release = status.get("current_release")
    gateway = status.get("gateway")
    if (status.get("status") != "ready" or not isinstance(release, str)
            or not re.fullmatch(r"[a-f0-9]{64}", release)
            or not isinstance(gateway, dict) or gateway.get("runtime_release") != release
            or gateway.get("process_verified") is not True):
        return {"status": "unverified"}
    _, state, _ = resolve_ivd_paths()
    candidates = sorted((state / "oob/receipts").glob(f"*-{release[:12]}.json"), reverse=True)
    receipt = _safe_json(candidates[0]) if candidates else {}
    public = receipt.get("public_evidence") if isinstance(receipt, dict) else None
    matched = (
        isinstance(receipt, dict) and receipt.get("release_id") == release
        and receipt.get("status") == "ready" and receipt.get("reason") == "deploy_ready"
        and isinstance(public, dict) and public.get("current_release") == release
    )
    current = _safe_json(state / "current" / ".staged.json")
    summary_parts = []
    if matched and isinstance(current, dict) and current.get("release_id") == release:
        for key, name in (("hermes_agent_commit", "Agent"), ("knowledgehub_commit", "知识库")):
            commit = current.get(key)
            if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
                continue
            summary_parts.append(f"{name} 版本为 {commit[:8]}。")
        plan = receipt.get("change_plan")
        count = plan.get("changed_path_count") if isinstance(plan, dict) else None
        if type(count) is int and count > 0:
            summary_parts.append(f"发布计划记录了 {count} 处路径变更。")
        changes = _read_release_changes(state, current)
        if changes:
            summary_parts.append("\n本次功能改动\n" + "\n".join(f"• {change}" for change in changes))
        else:
            summary_parts.append("部署回执未记录具体功能改动。")
    health = status.get("platform_health")
    return {
        "status": "verified",
        "release": release[:12],
        "channels_healthy": isinstance(health, dict) and health.get("status") == "healthy",
        "deployment_receipt_verified": matched,
        "change_summary": "".join(summary_parts),
    }


def run_ivd_safe_repair(
    *, kb_root: str | Path | None = None, runner: Runner = subprocess.run
) -> dict[str, object]:
    default_kb, _, _ = resolve_ivd_paths()
    kb = Path(kb_root) if kb_root is not None else default_kb
    if not runtime_kb_root_allowed(kb):
        return {"status": "blocked", "reason": "auxiliary_workspace_forbidden"}
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
