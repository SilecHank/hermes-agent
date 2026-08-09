import json
from pathlib import Path

import pytest

from tools.ivd_standby_status_tool import (
    StandbyStatusPolicy,
    StandbyStatusSession,
    TOOL_SCHEMA,
    read_standby_status,
)


@pytest.fixture
def policy(tmp_path):
    return StandbyStatusPolicy(
        enabled=True,
        profile="telegram",
        receipt_path=tmp_path / "latest-verification.json",
    )


@pytest.fixture
def admin():
    return StandbyStatusSession(platform="telegram", profile="telegram", gateway_admin=True)


def _write_receipt(path: Path, **updates):
    payload = {
        "schema_version": 1,
        "status": "ready",
        "tag": "standby-2026-08-09",
        "mac_identity_decrypt": "ready",
        "recovery_identity_decrypt": "ready",
        "verified_at": "2026-08-09T04:00:00Z",
        "freshness": "fresh",
    }
    payload.update(updates)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def test_schema_is_zero_argument():
    parameters = TOOL_SCHEMA["parameters"]
    assert parameters == {"type": "object", "properties": {}, "additionalProperties": False}


def test_ready_receipt_returns_concise_chinese(policy, admin):
    _write_receipt(policy.receipt_path)
    result = read_standby_status({}, policy, admin)
    assert result["status"] == "ready"
    assert "灾备验收通过" in result["message_zh"]
    assert "standby-2026-08-09" in result["message_zh"]
    assert set(result) == {"status", "tag", "mac_identity_decrypt", "recovery_identity_decrypt", "verified_at", "freshness", "message_zh"}


@pytest.mark.parametrize(
    "session",
    [
        StandbyStatusSession("qqbot", "telegram", True),
        StandbyStatusSession("telegram", "default", True),
        StandbyStatusSession("telegram", "telegram", False),
    ],
)
def test_non_admin_or_wrong_profile_is_blocked_without_reading(policy, session):
    result = read_standby_status({}, policy, session)
    assert result == {"status": "blocked", "message_zh": "当前会话无权查看灾备验收状态。"}


@pytest.mark.parametrize("args", [{"action": "verify"}, {"path": "/tmp/x"}, {"command": "id"}])
def test_any_argument_is_rejected(policy, admin, args):
    result = read_standby_status(args, policy, admin)
    assert result == {"status": "blocked", "message_zh": "此状态工具不接受任何参数。"}


def test_missing_malformed_and_unknown_fields_fail_closed(policy, admin):
    assert read_standby_status({}, policy, admin)["status"] == "blocked"
    policy.receipt_path.write_text("not-json", encoding="utf-8")
    policy.receipt_path.chmod(0o600)
    assert read_standby_status({}, policy, admin)["status"] == "blocked"
    _write_receipt(policy.receipt_path, secret="must-not-pass")
    assert read_standby_status({}, policy, admin)["status"] == "blocked"


def test_symlink_and_non_private_receipt_fail_closed(policy, admin, tmp_path):
    target = tmp_path / "target.json"
    _write_receipt(target)
    policy.receipt_path.symlink_to(target)
    assert read_standby_status({}, policy, admin)["status"] == "blocked"
    policy.receipt_path.unlink()
    _write_receipt(policy.receipt_path)
    policy.receipt_path.chmod(0o644)
    assert read_standby_status({}, policy, admin)["status"] == "blocked"


def test_tool_module_has_no_process_execution_surface():
    source = Path("tools/ivd_standby_status_tool.py").read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "os.system" not in source
    assert "popen" not in source.lower()
    assert "shell=" not in source
