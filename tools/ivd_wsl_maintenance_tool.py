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
    confirmation_ttl_seconds: int = 300
    required_owner: str = "wsl-primary"
    required_generation: str = "10"


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


def _confirmation_path(policy: MaintenancePolicy, task_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{24}", task_id or ""):
        raise ValueError("invalid confirmation id")
    directory = policy.state_dir / "confirmations"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory / f"{task_id}.json"


def _write_json_atomic(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _new_confirmation(
    policy: MaintenancePolicy,
    identity: SessionIdentity,
    action: str,
    suite: Optional[str],
    now: float,
) -> dict[str, str]:
    chat_hash, user_hash = _audit_hashes(policy, identity)
    task_id = secrets.token_hex(12)
    record = {
        "schema_version": 1,
        "task_id": task_id,
        "action": action,
        "test_suite": suite,
        "profile": identity.profile,
        "platform": identity.platform,
        "chat_hash": chat_hash,
        "user_hash": user_hash,
        "owner": policy.required_owner,
        "generation": policy.required_generation,
        "created_at": now,
        "expires_at": now + policy.confirmation_ttl_seconds,
        "status": "waiting_confirmation",
    }
    _write_json_atomic(_confirmation_path(policy, task_id), record)
    return {
        "status": "waiting_confirmation",
        "message_zh": (
            f"该动作会修改 IVD 运行状态，正在等待你的确认。"
            f"请确认任务 {task_id} 后再执行。"
        ),
        "confirmation_task_id": task_id,
    }


def _load_confirmation(
    policy: MaintenancePolicy,
    identity: SessionIdentity,
    action: str,
    suite: Optional[str],
    task_id: str,
    now: float,
    preflight: Callable[[], tuple[str, str, bool]],
) -> tuple[Optional[Path], Optional[dict], Optional[dict[str, str]]]:
    try:
        path = _confirmation_path(policy, task_id)
        record = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError, json.JSONDecodeError):
        return None, None, {"status": "blocked", "message_zh": "确认任务不存在或已失效。"}
    if float(record.get("expires_at", 0)) < now:
        path.unlink(missing_ok=True)
        return None, None, {"status": "blocked", "message_zh": "确认任务已过期，请重新发起。"}
    chat_hash, user_hash = _audit_hashes(policy, identity)
    expected = (
        identity.profile,
        identity.platform,
        chat_hash,
        user_hash,
        action,
        suite,
        "waiting_confirmation",
    )
    actual = (
        record.get("profile"), record.get("platform"), record.get("chat_hash"),
        record.get("user_hash"), record.get("action"), record.get("test_suite"),
        record.get("status"),
    )
    if actual != expected:
        return None, None, {"status": "blocked", "message_zh": "确认信息与原任务不一致，已拒绝执行。"}
    try:
        owner, generation, mac_ivd_safe = preflight()
    except Exception:
        return None, None, {"status": "blocked", "message_zh": "维护前检查失败，未执行任何修改。"}
    if (
        owner != record.get("owner")
        or str(generation) != str(record.get("generation"))
        or not mac_ivd_safe
    ):
        return None, None, {"status": "blocked", "message_zh": "主机所有权、代次或 Mac 冷备状态已变化，未执行修改。"}
    record["status"] = "running"
    record["started_at"] = now
    _write_json_atomic(path, record)
    return path, record, None


def _acquire_lease(policy: MaintenancePolicy, task_id: str, now: float) -> bool:
    _ensure_state_dir(policy)
    path = policy.state_dir / "write.lease"
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if float(current.get("expires_at", 0)) >= now:
                return False
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        path.unlink(missing_ok=True)
    value = {
        "task_id": task_id,
        "pid": os.getpid(),
        "created_at": now,
        "expires_at": now + policy.lock_lease_seconds,
    }
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, separators=(",", ":"))
    return True


def _release_lease(policy: MaintenancePolicy, task_id: str) -> None:
    path = policy.state_dir / "write.lease"
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if current.get("task_id") == task_id:
            path.unlink(missing_ok=True)
    except (OSError, ValueError, json.JSONDecodeError):
        return


_HOST_RE = re.compile(r"\A(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9.-]*\Z", re.ASCII)


def _safe_client_path(raw: object) -> Path:
    path = Path(str(raw or "")).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("unsafe client path")
    if path.stat().st_mode & 0o022:
        raise ValueError("writable client path")
    return path.resolve()


def load_policy(config: Optional[Mapping[str, object]] = None) -> MaintenancePolicy:
    if config is None:
        from hermes_cli.config import load_config

        config = load_config() or {}
    raw = config.get("ivd_maintenance") if isinstance(config, Mapping) else None
    if not isinstance(raw, Mapping):
        raise ValueError("ivd maintenance is not configured")
    allowed = {
        "enabled", "profile", "admin_user_id_file", "windows_ssh_host",
        "ivd_wsl_path", "ivd_remote_path", "state_dir", "read_timeout_seconds",
        "test_timeout_seconds", "write_timeout_seconds", "lock_lease_seconds",
        "confirmation_ttl_seconds", "required_owner", "required_generation",
    }
    if set(raw) - allowed:
        raise ValueError("unknown policy fields")
    admin_file = Path(str(raw.get("admin_user_id_file") or "")).expanduser()
    if (
        not admin_file.is_absolute() or admin_file.is_symlink() or not admin_file.is_file()
        or admin_file.stat().st_mode & 0o077
    ):
        raise ValueError("unsafe admin allowlist")
    admin_ids = frozenset(
        line.strip() for line in admin_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if not admin_ids:
        raise ValueError("empty admin allowlist")
    host = str(raw.get("windows_ssh_host") or "")
    if not _HOST_RE.fullmatch(host):
        raise ValueError("invalid Windows host")
    state_dir = Path(str(raw.get("state_dir") or "")).expanduser()
    if not state_dir.is_absolute() or state_dir.is_symlink():
        raise ValueError("unsafe state directory")

    def bounded(name: str, default: int, low: int, high: int) -> int:
        value = int(raw.get(name, default))
        if not low <= value <= high:
            raise ValueError(f"invalid {name}")
        return value

    return MaintenancePolicy(
        enabled=raw.get("enabled") is True,
        profile=str(raw.get("profile") or ""),
        admin_user_ids=admin_ids,
        windows_ssh_host=host,
        ivd_wsl_path=_safe_client_path(raw.get("ivd_wsl_path")),
        ivd_remote_path=_safe_client_path(raw.get("ivd_remote_path")),
        state_dir=state_dir,
        read_timeout_seconds=bounded("read_timeout_seconds", 30, 5, 300),
        test_timeout_seconds=bounded("test_timeout_seconds", 900, 30, 3600),
        write_timeout_seconds=bounded("write_timeout_seconds", 900, 30, 3600),
        lock_lease_seconds=bounded("lock_lease_seconds", 1200, 60, 7200),
        confirmation_ttl_seconds=bounded("confirmation_ttl_seconds", 300, 30, 1800),
        required_owner=str(raw.get("required_owner") or "wsl-primary"),
        required_generation=str(raw.get("required_generation") or "10"),
    )


def _production_preflight(policy: MaintenancePolicy) -> tuple[str, str, bool]:
    env = {
        "PATH": SAFE_PATH,
        "HOME": str(Path.home()),
        "IVD_WINDOWS_SSH_HOST": policy.windows_ssh_host,
    }
    status = subprocess.run(
        [str(policy.ivd_remote_path), "status"], check=False, capture_output=True,
        text=True, timeout=policy.read_timeout_seconds, env=env,
    )
    if status.returncode != 0:
        raise RuntimeError("production status unavailable")
    text = status.stdout or ""
    owner_match = re.search(r'(?:active_host|owner)["\s:=]+([A-Za-z0-9._-]+)', text)
    generation_match = re.search(r'generation["\s:=]+([0-9]+)', text)
    if not owner_match or not generation_match:
        raise RuntimeError("production ownership unavailable")
    uid = os.getuid()
    disabled = subprocess.run(
        ["/bin/launchctl", "print-disabled", f"gui/{uid}"], check=False,
        capture_output=True, text=True, timeout=10, env={"PATH": SAFE_PATH},
    )
    loaded = subprocess.run(
        ["/bin/launchctl", "print", f"gui/{uid}/ai.hermes.gateway"], check=False,
        capture_output=True, text=True, timeout=10, env={"PATH": SAFE_PATH},
    )
    mac_safe = bool(
        disabled.returncode == 0
        and re.search(r'"ai\.hermes\.gateway"\s*=>\s*(?:true|disabled)', disabled.stdout or "")
        and loaded.returncode != 0
    )
    return owner_match.group(1), generation_match.group(1), mac_safe


def execute_action(
    args: Mapping[str, object],
    policy: MaintenancePolicy,
    identity: SessionIdentity,
    *,
    runner: Callable = subprocess.run,
    clock: Callable[[], float] = time.time,
    preflight: Optional[Callable[[], tuple[str, str, bool]]] = None,
) -> dict[str, str]:
    try:
        action, suite = _validate_request(args)
        spec = build_action_spec(action, suite, policy)
    except (TypeError, ValueError):
        return {"status": "blocked", "message_zh": "请求包含不支持的维护参数，已拒绝执行。"}
    if not _authorize(policy, identity):
        return {"status": "blocked", "message_zh": "当前会话无权执行此维护动作。"}

    confirmation_path = None
    confirmation_task_id = None
    if action in WRITE_ACTIONS:
        confirmation_task_id = args.get("confirmation_task_id")
        if not confirmation_task_id:
            return _new_confirmation(policy, identity, action, suite, clock())
        if not isinstance(confirmation_task_id, str):
            return {"status": "blocked", "message_zh": "确认任务格式无效。"}
        effective_preflight = preflight or (
            lambda: (policy.required_owner, policy.required_generation, True)
        )
        confirmation_path, _, blocked = _load_confirmation(
            policy, identity, action, suite, confirmation_task_id, clock(), effective_preflight
        )
        if blocked is not None:
            return blocked
        if not _acquire_lease(policy, confirmation_task_id, clock()):
            if confirmation_path is not None:
                record = json.loads(confirmation_path.read_text(encoding="utf-8"))
                record["status"] = "waiting_confirmation"
                record.pop("started_at", None)
                _write_json_atomic(confirmation_path, record)
            return {"status": "blocked", "message_zh": "已有维护任务正在执行，请稍后再试。"}

    started = clock()
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
    finally:
        if confirmation_task_id:
            _release_lease(policy, confirmation_task_id)

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


def _handle_ivd_wsl_maintenance(args, **_kwargs) -> str:
    from gateway.session_context import get_session_env

    identity = SessionIdentity(
        platform=get_session_env("HERMES_SESSION_PLATFORM"),
        profile=get_session_env("HERMES_SESSION_PROFILE"),
        chat_id=get_session_env("HERMES_SESSION_CHAT_ID"),
        user_id=get_session_env("HERMES_SESSION_USER_ID"),
        gateway_admin=get_session_env("HERMES_SESSION_IVD_ADMIN") == "1",
    )
    try:
        policy = load_policy()
    except Exception:
        return json.dumps(
            {"status": "blocked", "message_zh": "IVD 维护策略未就绪，未执行任何操作。"},
            ensure_ascii=False,
        )
    result = execute_action(
        args, policy, identity,
        preflight=lambda: _production_preflight(policy),
    )
    return json.dumps(result, ensure_ascii=False)


def _check_ivd_maintenance_requirements() -> bool:
    try:
        policy = load_policy()
        return bool(policy.enabled and policy.profile == "telegram")
    except Exception:
        return False


from tools.registry import registry

registry.register(
    name="ivd_wsl_maintenance",
    toolset="ivd_maintenance",
    schema=TOOL_SCHEMA,
    handler=_handle_ivd_wsl_maintenance,
    check_fn=_check_ivd_maintenance_requirements,
    description="执行受治理的 IVD WSL 状态检查或维护动作",
    emoji="🛠️",
    max_result_size_chars=12_000,
)
