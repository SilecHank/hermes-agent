import json
import subprocess
import time
from pathlib import Path

import pytest

from tools.ivd_wsl_maintenance_tool import (
    MaintenancePolicy,
    SessionIdentity,
    TOOL_SCHEMA,
    build_action_spec,
    execute_action,
    redact_text,
)


@pytest.fixture
def policy(tmp_path):
    ivd_wsl = tmp_path / "ivd-wsl"
    ivd_remote = tmp_path / "ivd-remote"
    ivd_wsl.write_text("client", encoding="utf-8")
    ivd_remote.write_text("client", encoding="utf-8")
    ivd_wsl.chmod(0o700)
    ivd_remote.chmod(0o700)
    return MaintenancePolicy(
        enabled=True,
        profile="telegram",
        admin_user_ids=frozenset({"owner"}),
        windows_ssh_host="slim@wsl-host.tailnet.ts.net",
        ivd_wsl_path=ivd_wsl,
        ivd_remote_path=ivd_remote,
        state_dir=tmp_path / "state",
    )


@pytest.fixture
def identity():
    return SessionIdentity("telegram", "telegram", "chat", "owner", True)


def test_schema_has_no_arbitrary_command_fields():
    props = TOOL_SCHEMA["parameters"]["properties"]
    assert set(props) == {"action", "test_suite", "confirmation_task_id"}
    assert TOOL_SCHEMA["parameters"]["additionalProperties"] is False
    assert {"command", "shell", "path", "cwd", "ssh_options"}.isdisjoint(props)


@pytest.mark.parametrize("action", ["status", "git_status", "logs", "release_check"])
def test_read_only_actions_map_to_fixed_argv(action, policy):
    spec = build_action_spec(action, None, policy)
    assert isinstance(spec.argv, tuple)
    assert spec.shell is False


def test_action_validation_rejects_model_supplied_command(policy, identity):
    calls = []
    result = execute_action(
        {"action": "status", "command": "id"}, policy, identity,
        runner=lambda *a, **k: calls.append((a, k)),
    )
    assert result["status"] == "blocked"
    assert calls == []
    assert "不支持" in result["message_zh"]


@pytest.mark.parametrize(
    "args",
    [
        {"action": "unknown"},
        {"action": "status", "test_suite": "quick"},
        {"action": "test"},
        {"action": "test", "test_suite": "other"},
    ],
)
def test_invalid_action_combinations_are_blocked(args, policy, identity):
    calls = []
    result = execute_action(args, policy, identity, runner=lambda *a, **k: calls.append(1))
    assert result["status"] == "blocked"
    assert calls == []


def test_runner_never_uses_shell(policy, identity):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="owner=wsl-primary generation=10", stderr="")

    result = execute_action({"action": "status"}, policy, identity, runner=runner)
    assert result["status"] == "completed"
    assert calls[0][0] == [str(policy.ivd_remote_path), "status"]
    assert calls[0][1].get("shell", False) is False


def test_timeout_returns_chinese_terminal_state(policy, identity):
    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    result = execute_action({"action": "status"}, policy, identity, runner=runner)
    assert result == {"status": "timed_out", "message_zh": "执行已超时，远程任务已停止。"}


def test_redaction_removes_credentials_and_private_key_text():
    raw = (
        "Authorization: Bearer secret-value\nTOKEN=secret-value\n"
        "Cookie: session=secret-value\n-----BEGIN PRIVATE KEY-----\nsecret-value\n"
        "-----END PRIVATE KEY-----"
    )
    clean = redact_text(raw)
    assert "secret-value" not in clean
    assert "PRIVATE KEY" not in clean


def test_audit_has_no_raw_command_output_or_identity(policy, identity):
    secret = "Authorization: Bearer secret-value"

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=secret, stderr="")

    execute_action({"action": "status"}, policy, identity, runner=runner)
    record = json.loads((policy.state_dir / "audit.jsonl").read_text().splitlines()[-1])
    forbidden = {"command", "stdout", "stderr", "user_id", "chat_id", "user_task"}
    assert forbidden.isdisjoint(record)
    assert record["user_hash"] != identity.user_id
    assert record["chat_hash"] != identity.chat_id
    assert "secret-value" not in json.dumps(record)
    assert (policy.state_dir / "audit.jsonl").stat().st_mode & 0o777 == 0o600


def test_dirty_git_status_is_read_only(policy, identity):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=" M user-file.md", stderr="")

    result = execute_action({"action": "git_status"}, policy, identity, runner=runner)
    assert result["status"] == "completed"
    assert "工作区存在未提交改动" in result["message_zh"]
    assert len(calls) == 1


def test_user_messages_are_plain_chinese(policy, identity):
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="failure")

    result = execute_action({"action": "status"}, policy, identity, runner=runner)
    text = result["message_zh"]
    assert text
    assert "Reply with the number" not in text
    assert "Working — clarify" not in text
    assert "Traceback" not in text


def test_write_action_first_call_returns_waiting_confirmation(policy, identity):
    calls = []
    result = execute_action(
        {"action": "sync"}, policy, identity,
        runner=lambda *a, **k: calls.append(1),
    )
    assert result["status"] == "waiting_confirmation"
    assert "等待你的确认" in result["message_zh"]
    assert result["confirmation_task_id"]
    assert calls == []


@pytest.mark.parametrize("mismatch", ["profile", "chat", "user", "action", "generation"])
def test_confirmation_requires_exact_identity_action_and_generation(
    policy, identity, mismatch,
):
    first = execute_action({"action": "sync"}, policy, identity)
    task_id = first["confirmation_task_id"]
    confirm_identity = identity
    args = {"action": "sync", "confirmation_task_id": task_id}
    preflight = lambda: ("wsl-primary", "10", True)
    if mismatch == "profile":
        confirm_identity = SessionIdentity("telegram", "other", "chat", "owner", True)
    elif mismatch == "chat":
        confirm_identity = SessionIdentity("telegram", "telegram", "other", "owner", True)
    elif mismatch == "user":
        confirm_identity = SessionIdentity("telegram", "telegram", "chat", "other", True)
    elif mismatch == "action":
        args["action"] = "deploy"
    elif mismatch == "generation":
        preflight = lambda: ("wsl-primary", "11", True)
    calls = []
    result = execute_action(
        args, policy, confirm_identity, runner=lambda *a, **k: calls.append(1),
        preflight=preflight,
    )
    assert result["status"] == "blocked"
    assert calls == []


def test_expired_confirmation_is_blocked_and_removed(policy, identity):
    now = [1000.0]
    first = execute_action({"action": "repair"}, policy, identity, clock=lambda: now[0])
    now[0] += policy.confirmation_ttl_seconds + 1
    result = execute_action(
        {"action": "repair", "confirmation_task_id": first["confirmation_task_id"]},
        policy, identity, clock=lambda: now[0],
    )
    assert result["status"] == "blocked"
    assert not (policy.state_dir / "confirmations" / f"{first['confirmation_task_id']}.json").exists()


def test_gateway_restart_does_not_replay_pending_write(policy, identity):
    first = execute_action({"action": "deploy"}, policy, identity)
    calls = []
    second = execute_action({"action": "deploy"}, policy, identity, runner=lambda *a, **k: calls.append(1))
    assert second["status"] == "waiting_confirmation"
    assert second["confirmation_task_id"] != first["confirmation_task_id"]
    assert calls == []


def test_second_writer_is_blocked_until_lease_expires(policy, identity):
    first = execute_action({"action": "sync"}, policy, identity)
    lease = policy.state_dir / "write.lease"
    lease.write_text(json.dumps({"task_id": "other", "expires_at": time.time() + 60}), encoding="utf-8")
    result = execute_action(
        {"action": "sync", "confirmation_task_id": first["confirmation_task_id"]},
        policy, identity,
    )
    assert result["status"] == "blocked"
    assert "维护任务" in result["message_zh"]


@pytest.mark.parametrize("returncode", [0, 3])
def test_terminal_state_releases_matching_lease(policy, identity, returncode):
    first = execute_action({"action": "sync"}, policy, identity)

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout="done", stderr="")

    result = execute_action(
        {"action": "sync", "confirmation_task_id": first["confirmation_task_id"]},
        policy, identity, runner=runner,
    )
    assert result["status"] in {"completed", "failed"}
    assert not (policy.state_dir / "write.lease").exists()
