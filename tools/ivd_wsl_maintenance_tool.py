"""Governed, enum-only bridge to the IVD WSL maintenance channel."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from typing import Callable, Mapping, Optional


READ_ACTIONS = frozenset({"status", "git_status", "logs", "release_check"})
WRITE_ACTIONS = frozenset({"sync", "deploy", "repair"})
TEST_SUITES = frozenset({"quick", "runtime", "full"})
ALL_ACTIONS = READ_ACTIONS | WRITE_ACTIONS | {"test"}
SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"

TOOL_SCHEMA = {
    "name": "ivd_wsl_maintenance",
    "description": "执行受治理的 IVD WSL 状态检查、测试或维护动作。",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(ALL_ACTIONS)},
            "test_suite": {"type": "string", "enum": sorted(TEST_SUITES)},
            "confirmation_task_id": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class MaintenancePolicy:
    enabled: bool
    profile: str
    admin_user_ids: frozenset[str]
    windows_ssh_host: str
    ivd_wsl_path: Path
    ivd_remote_path: Path
    state_dir: Path
    read_timeout_seconds: int = 30
    test_timeout_seconds: int = 900
    write_timeout_seconds: int = 900
    lock_lease_seconds: int = 1200


@dataclass(frozen=True)
class SessionIdentity:
    platform: str
    profile: str
    chat_id: str
    user_id: str
    gateway_admin: bool


@dataclass(frozen=True)
class ActionSpec:
    argv: tuple[str, ...]
    timeout_seconds: int
    shell: bool = False


_FIXED_WSL_COMMANDS = {
    "git_status": "git -C /home/slim/IVD-KnowledgeHub status --short --branch",
    "release_check": (
        "cd /home/slim/IVD-KnowledgeHub && python3 -B -c 'import json; "
        "from pathlib import Path; from scripts.hermes_transactional_deploy import "
        "inspect_verified_release; print(json.dumps(inspect_verified_release("
        "(Path.home()/\".hermes/ivd-state/current\").resolve()), ensure_ascii=False))'"
    ),
    "test:quick": (
        "cd /home/slim/.hermes/ivd-state/current/knowledgehub && "
        "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -q "
        "scripts.test_hermes_structural_guardrails"
    ),
    "test:runtime": (
        "cd /home/slim/.hermes/ivd-state/current/knowledgehub && "
        "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -q "
        "scripts.test_hermes_runtime_integration scripts.test_hermes_wsl_command_channel"
    ),
    "test:full": (
        "cd /home/slim/.hermes/ivd-state/current/knowledgehub && "
        "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -q "
        "scripts.test_hermes_structural_guardrails scripts.test_hermes_experience_consistency "
        "scripts.test_hermes_live_answer_quality_gate scripts.test_hermes_runtime_integration "
        "scripts.test_hermes_wsl_command_channel"
    ),
}


def build_action_spec(
    action: str, test_suite: Optional[str], policy: MaintenancePolicy
) -> ActionSpec:
    if action not in ALL_ACTIONS:
        raise ValueError("unsupported action")
    if action == "test":
        if test_suite not in TEST_SUITES:
            raise ValueError("unsupported test suite")
        return ActionSpec(
            (str(policy.ivd_wsl_path), "--timeout", str(policy.test_timeout_seconds), "--", _FIXED_WSL_COMMANDS[f"test:{test_suite}"]),
            policy.test_timeout_seconds + 30,
        )
    if test_suite is not None:
        raise ValueError("test_suite is only valid for test")
    if action in {"git_status", "release_check"}:
        return ActionSpec(
            (str(policy.ivd_wsl_path), "--timeout", str(policy.read_timeout_seconds), "--", _FIXED_WSL_COMMANDS[action]),
            policy.read_timeout_seconds + 15,
        )
    timeout = policy.read_timeout_seconds if action in READ_ACTIONS else policy.write_timeout_seconds
    return ActionSpec((str(policy.ivd_remote_path), action), timeout)


_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_HEADER_SECRET_RE = re.compile(
    r"(?im)^(\s*(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*).*$"
)
_ASSIGNMENT_SECRET_RE = re.compile(
    r"(?im)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY)[A-Z0-9_]*\s*=\s*)[^\s]+"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")


def redact_text(value: str) -> str:
    text = _PRIVATE_KEY_RE.sub("[已隐藏私钥]", value or "")
    text = _HEADER_SECRET_RE.sub(r"\1[已隐藏]", text)
    text = _ASSIGNMENT_SECRET_RE.sub(r"\1[已隐藏]", text)
    return _JWT_RE.sub("[已隐藏令牌]", text)


def _validate_request(args: Mapping[str, object]) -> tuple[str, Optional[str]]:
    if not isinstance(args, Mapping) or set(args) - {
        "action", "test_suite", "confirmation_task_id"
    }:
        raise ValueError("unsupported fields")
    action = args.get("action")
    suite = args.get("test_suite")
    if not isinstance(action, str):
        raise ValueError("missing action")
    if suite is not None and not isinstance(suite, str):
        raise ValueError("invalid suite")
    return action, suite


def _authorize(policy: MaintenancePolicy, identity: SessionIdentity) -> bool:
    return bool(
        policy.enabled
        and policy.profile == "telegram"
        and identity.platform == "telegram"
        and identity.profile == "telegram"
        and identity.gateway_admin
        and identity.chat_id
        and identity.user_id
        and identity.user_id in policy.admin_user_ids
    )


def _ensure_state_dir(policy: MaintenancePolicy) -> None:
    policy.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(policy.state_dir, 0o700)


def _audit_hashes(policy: MaintenancePolicy, identity: SessionIdentity) -> tuple[str, str]:
    _ensure_state_dir(policy)
    salt_path = policy.state_dir / "audit.salt"
    if not salt_path.exists():
        fd = os.open(salt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(secrets.token_hex(32))
    os.chmod(salt_path, 0o600)
    salt = salt_path.read_text(encoding="ascii").strip()
    digest = lambda value: sha256(f"{salt}:{value}".encode()).hexdigest()
    return digest(identity.chat_id), digest(identity.user_id)


def _append_audit(
    policy: MaintenancePolicy,
    identity: SessionIdentity,
    *,
    action: str,
    suite: Optional[str],
    started_at: float,
    status: str,
    exit_code: Optional[int],
    summary: str,
) -> None:
    chat_hash, user_hash = _audit_hashes(policy, identity)
    record = {
        "schema_version": 1,
        "action": action,
        "test_suite": suite,
        "profile": identity.profile,
        "platform": identity.platform,
        "chat_hash": chat_hash,
        "user_hash": user_hash,
        "started_at": started_at,
        "completed_at": time.time(),
        "duration_seconds": round(max(0.0, time.time() - started_at), 3),
        "status": status,
        "exit_code": exit_code,
        "summary_hash": sha256(summary.encode("utf-8")).hexdigest(),
    }
    audit_path = policy.state_dir / "audit.jsonl"
    fd = os.open(audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.chmod(audit_path, 0o600)


def execute_action(
    args: Mapping[str, object],
    policy: MaintenancePolicy,
    identity: SessionIdentity,
    *,
    runner: Callable = subprocess.run,
) -> dict[str, str]:
    try:
        action, suite = _validate_request(args)
        spec = build_action_spec(action, suite, policy)
    except (TypeError, ValueError):
        return {"status": "blocked", "message_zh": "请求包含不支持的维护参数，已拒绝执行。"}
    if not _authorize(policy, identity):
        return {"status": "blocked", "message_zh": "当前会话无权执行此维护动作。"}

    started = time.time()
    try:
        completed = runner(
            list(spec.argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=spec.timeout_seconds,
            env={
                "PATH": SAFE_PATH,
                "HOME": str(Path.home()),
                "IVD_WINDOWS_SSH_HOST": policy.windows_ssh_host,
            },
        )
    except subprocess.TimeoutExpired:
        result = {"status": "timed_out", "message_zh": "执行已超时，远程任务已停止。"}
        _append_audit(policy, identity, action=action, suite=suite, started_at=started,
                      status="timed_out", exit_code=None, summary=result["message_zh"])
        return result
    except Exception:
        result = {"status": "failed", "message_zh": "维护通道执行失败，请检查本机通道状态。"}
        _append_audit(policy, identity, action=action, suite=suite, started_at=started,
                      status="failed", exit_code=None, summary=result["message_zh"])
        return result

    output = redact_text(((completed.stdout or "") + "\n" + (completed.stderr or "")).strip())[:8000]
    if completed.returncode == 0:
        if action == "git_status" and re.search(r"(?m)^\s*[MADRCU?!]{1,2}\s", output):
            message = "检查完成：WSL 工作区存在未提交改动，未执行任何修改。"
        else:
            message = "检查完成。" if action in READ_ACTIONS else "维护动作执行完成。"
        if output:
            message += "\n\n" + output
        status = "completed"
    else:
        status = "failed"
        message = f"执行未成功（退出码 {completed.returncode}）。"
        if output:
            message += "\n\n" + output
    _append_audit(policy, identity, action=action, suite=suite, started_at=started,
                  status=status, exit_code=completed.returncode, summary=message)
    return {"status": status, "message_zh": message}

