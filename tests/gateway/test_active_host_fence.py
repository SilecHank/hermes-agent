"""Security and startup-order tests for the optional IVD active-host fence."""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


MANIFEST_SHA256 = "a" * 64


def _record(*, host_id: str = "wsl-primary", manifest: str = MANIFEST_SHA256):
    return {
        "schema_version": 1,
        "host_id": host_id,
        "generation": 4,
        "activated_at": datetime.now(timezone.utc).isoformat(),
        "reason": "initial_primary",
        "operator": "owner",
        "deployment_manifest_sha256": manifest,
    }


def _override(*, host_id: str = "wsl-primary", created_delta=timedelta(), lifetime=timedelta(minutes=5)):
    now = datetime.now(timezone.utc)
    created = now + created_delta
    return {
        "schema_version": 1,
        "host_id": host_id,
        "deployment_manifest_sha256": MANIFEST_SHA256,
        "created_at": created.isoformat(),
        "expires_at": (created + lifetime).isoformat(),
        "operator": "owner",
        "reason": "remote_control_plane_unavailable",
    }


def test_gateway_fence_blocks_wrong_host_and_unavailable_remote():
    from gateway.active_host_fence import evaluate_fence

    assert not evaluate_fence(_record(host_id="mac-standby"), local_host="wsl-primary").allowed
    assert evaluate_fence(None, local_host="wsl-primary", required=True).reason == "fence_unavailable"


def test_evaluator_strictly_validates_record_and_manifest():
    from gateway.active_host_fence import evaluate_fence

    assert evaluate_fence(_record(), local_host="wsl-primary", expected_manifest_sha256=MANIFEST_SHA256).reason == "owner_match"
    assert evaluate_fence(_record(manifest="b" * 64), local_host="wsl-primary", expected_manifest_sha256=MANIFEST_SHA256).reason == "manifest_mismatch"
    invalid = {**_record(), "unexpected": True}
    assert evaluate_fence(invalid, local_host="wsl-primary").reason == "fence_record_invalid"
    invalid = {**_record(), "generation": True}
    assert evaluate_fence(invalid, local_host="wsl-primary").reason == "fence_record_invalid"
    invalid = {**_record(), "host_id": ["wsl-primary"]}
    assert evaluate_fence(invalid, local_host="wsl-primary").reason == "fence_record_invalid"


def test_optional_mode_does_no_io(monkeypatch):
    import gateway.active_host_fence as fence

    monkeypatch.delenv("IVD_ACTIVE_HOST_FENCE_REQUIRED", raising=False)
    monkeypatch.setattr(fence, "fetch_active_host_record", lambda *a, **k: pytest.fail("network used"))
    decision = fence.assert_active_host_or_raise()
    assert decision.allowed
    assert decision.reason == "fence_disabled"


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        if size < 0:
            size = len(self.payload) - self.offset
        chunk = self.payload[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


def test_remote_fetch_uses_bounded_timeout_size_and_github_contents(monkeypatch, tmp_path):
    from gateway.active_host_fence import fetch_active_host_record

    captured = {}
    expected = _record()
    encoded = base64.b64encode(json.dumps(expected).encode()).decode()
    github_content = "\n".join(encoded[index : index + 32] for index in range(0, len(encoded), 32))
    wrapper = json.dumps(
        {"encoding": "base64", "content": github_content}
    ).encode()

    def fake_urlopen(request, timeout):
        captured["timeout"] = timeout
        captured["authorization"] = request.headers.get("Authorization")
        return _Response(wrapper)

    token_file = tmp_path / "credential"
    token_file.write_text("private-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = fetch_active_host_record(
        "https://api.github.com/repos/example/private/contents/active-host.json",
        timeout_seconds=1.5,
        max_response_bytes=4096,
        token_path=token_file,
        max_attempts=1,
    )
    assert result == expected
    assert captured == {"timeout": 1.5, "authorization": "Bearer private-token"}


def test_remote_fetch_rejects_oversize_invalid_json_and_token_permissions(monkeypatch, tmp_path):
    from gateway.active_host_fence import FenceError, fetch_active_host_record

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Response(b"x" * 65))
    with pytest.raises(FenceError, match="remote_response_too_large"):
        fetch_active_host_record("https://example.invalid", max_response_bytes=64, max_attempts=1)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Response(b"not-json"))
    with pytest.raises(FenceError, match="remote_json_invalid"):
        fetch_active_host_record("https://example.invalid", max_attempts=1)
    token_file = tmp_path / "credential"
    token_file.write_text("secret", encoding="utf-8")
    token_file.chmod(0o644)
    with pytest.raises(FenceError, match="credential_permissions_unsafe"):
        fetch_active_host_record("https://example.invalid", token_path=token_file, max_attempts=1)


def test_remote_fetch_rejects_symlink_credential(tmp_path):
    from gateway.active_host_fence import FenceError, fetch_active_host_record

    target = tmp_path / "real-token"
    target.write_text("secret", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "token"
    link.symlink_to(target)
    with pytest.raises(FenceError, match="credential_path_unsafe"):
        fetch_active_host_record("https://example.invalid", token_path=link, max_attempts=1)


def test_credential_read_is_parent_fd_anchored_during_swap(monkeypatch, tmp_path):
    from gateway.active_host_fence import _read_secure_credential

    credential_dir = tmp_path / "credentials"
    credential_dir.mkdir()
    token = credential_dir / "credential"
    token.write_text("original-token", encoding="utf-8")
    token.chmod(0o600)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "credential").write_text("outside-token", encoding="utf-8")
    (outside / "credential").chmod(0o600)
    original_dir = tmp_path / "credentials-original"
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if Path(path).name == "credential" and not swapped:
            swapped = True
            credential_dir.rename(original_dir)
            credential_dir.symlink_to(outside, target_is_directory=True)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapping_open)
    assert _read_secure_credential(token) == "original-token"


def test_required_remote_failure_exits_78_without_secret_leak(monkeypatch, tmp_path, caplog):
    import gateway.active_host_fence as fence

    token_file = tmp_path / "credential"
    token_file.write_text("do-not-log-me", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("IVD_ACTIVE_HOST_FENCE_REQUIRED", "true")
    monkeypatch.setenv("IVD_ACTIVE_HOST_ID", "wsl-primary")
    monkeypatch.setenv("IVD_ACTIVE_HOST_RECORD_URL", "https://example.invalid/active-host.json")
    monkeypatch.setenv("IVD_ACTIVE_HOST_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("IVD_DEPLOYMENT_MANIFEST_SHA256", MANIFEST_SHA256)
    monkeypatch.setattr(fence, "fetch_active_host_record", lambda *a, **k: (_ for _ in ()).throw(fence.FenceError("remote_unavailable")))
    with pytest.raises(SystemExit) as raised:
        fence.assert_active_host_or_raise()
    assert raised.value.code == 78
    assert "do-not-log-me" not in caplog.text


def test_offline_override_requires_two_switches_and_operator_file(monkeypatch, tmp_path):
    import gateway.active_host_fence as fence

    home = tmp_path / "hermes"
    override_path = home / "ivd-state" / "offline-override.json"
    override_path.parent.mkdir(parents=True)
    override_path.write_text(json.dumps(_override()), encoding="utf-8")
    override_path.chmod(0o600)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("IVD_ACTIVE_HOST_FENCE_REQUIRED", "true")
    monkeypatch.setenv("IVD_ACTIVE_HOST_ID", "wsl-primary")
    monkeypatch.setenv("IVD_DEPLOYMENT_MANIFEST_SHA256", MANIFEST_SHA256)
    monkeypatch.setattr(fence, "fetch_active_host_record", lambda *a, **k: (_ for _ in ()).throw(fence.FenceError("remote_unavailable")))
    for first, second, allowed in (("false", "false", False), ("true", "false", False), ("false", "true", False), ("true", "true", True)):
        monkeypatch.setenv("IVD_ACTIVE_HOST_OFFLINE_OVERRIDE", first)
        monkeypatch.setenv("IVD_ACTIVE_HOST_OFFLINE_OVERRIDE_CONFIRM", second)
        if allowed:
            assert fence.assert_active_host_or_raise().reason == "offline_override"
        else:
            with pytest.raises(SystemExit) as raised:
                fence.assert_active_host_or_raise()
            assert raised.value.code == 78


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (_override(host_id="mac-standby"), "override_host_mismatch"),
        (_override(created_delta=timedelta(minutes=-10), lifetime=timedelta(minutes=5)), "override_expired"),
        (_override(created_delta=timedelta(minutes=2)), "override_created_in_future"),
        (_override(lifetime=timedelta(hours=1)), "override_validity_too_long"),
        ({**_override(), "unexpected": True}, "override_schema_invalid"),
    ],
)
def test_offline_override_strict_validation(payload, reason):
    from gateway.active_host_fence import validate_offline_override

    decision = validate_offline_override(
        payload,
        local_host="wsl-primary",
        expected_manifest_sha256=MANIFEST_SHA256,
        max_validity_seconds=900,
        max_future_skew_seconds=60,
    )
    assert not decision.allowed
    assert decision.reason == reason


def test_offline_override_rejects_symlink_and_wide_permissions(tmp_path):
    from gateway.active_host_fence import read_offline_override

    real = tmp_path / "real.json"
    real.write_text(json.dumps(_override()), encoding="utf-8")
    real.chmod(0o600)
    link = tmp_path / "override.json"
    link.symlink_to(real)
    assert read_offline_override(link).reason == "override_path_unsafe"
    real.chmod(0o644)
    assert read_offline_override(real).reason == "override_permissions_unsafe"


def test_offline_override_rejects_symlink_parent_escape(tmp_path):
    from gateway.active_host_fence import read_offline_override

    outside = tmp_path / "outside"
    outside.mkdir()
    override = outside / "offline-override.json"
    override.write_text(json.dumps(_override()), encoding="utf-8")
    override.chmod(0o600)
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "ivd-state").symlink_to(outside, target_is_directory=True)
    assert read_offline_override(
        home / "ivd-state" / "offline-override.json",
        trusted_root=home,
    ).reason == "override_path_unsafe"


def test_offline_override_writes_sanitized_audit_event(monkeypatch, tmp_path):
    import gateway.active_host_fence as fence

    home = tmp_path / "hermes"
    override_path = home / "ivd-state" / "offline-override.json"
    override_path.parent.mkdir(parents=True)
    override_path.write_text(json.dumps(_override()), encoding="utf-8")
    override_path.chmod(0o600)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("IVD_ACTIVE_HOST_FENCE_REQUIRED", "true")
    monkeypatch.setenv("IVD_ACTIVE_HOST_ID", "wsl-primary")
    monkeypatch.setenv("IVD_DEPLOYMENT_MANIFEST_SHA256", MANIFEST_SHA256)
    monkeypatch.setenv("IVD_ACTIVE_HOST_OFFLINE_OVERRIDE", "true")
    monkeypatch.setenv("IVD_ACTIVE_HOST_OFFLINE_OVERRIDE_CONFIRM", "true")
    monkeypatch.setattr(fence, "fetch_active_host_record", lambda *a, **k: (_ for _ in ()).throw(fence.FenceError("remote_unavailable:secret-token")))
    assert fence.assert_active_host_or_raise().allowed
    audit = (home / "ivd-state" / "active-host-audit.jsonl").read_text(encoding="utf-8")
    event = json.loads(audit)
    assert event["event"] == "offline_override_used"
    assert event["host_id"] == "wsl-primary"
    assert "secret-token" not in audit


def test_override_audit_is_state_fd_anchored_during_swap(monkeypatch, tmp_path):
    from gateway.active_host_fence import _append_override_audit

    home = tmp_path / "hermes"
    state = home / "ivd-state"
    state.mkdir(parents=True)
    state.chmod(0o700)
    original_state = home / "ivd-state-original"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if Path(path).name == "active-host-audit.jsonl" and not swapped:
            swapped = True
            state.rename(original_state)
            state.symlink_to(outside, target_is_directory=True)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapping_open)
    _append_override_audit(home, _override())
    assert not (outside / "active-host-audit.jsonl").exists()
    assert (original_state / "active-host-audit.jsonl").is_file()


def test_override_audit_rejects_existing_wide_permissions(tmp_path):
    from gateway.active_host_fence import FenceError, _append_override_audit

    home = tmp_path / "hermes"
    state = home / "ivd-state"
    state.mkdir(parents=True)
    state.chmod(0o700)
    audit = state / "active-host-audit.jsonl"
    audit.write_text("", encoding="utf-8")
    audit.chmod(0o644)
    with pytest.raises(FenceError, match="audit_permissions_unsafe"):
        _append_override_audit(home, _override())


def test_gateway_fence_precedes_runtime_lock_and_embedded_cron():
    text = Path("gateway/run.py").read_text(encoding="utf-8")
    start = text.index("async def start_gateway")
    fence = text.index("assert_active_host_or_raise()", start)
    replace_marker = text.index("write_takeover_marker(existing_pid)", start)
    replace_signal = text.index("terminate_pid(existing_pid, force=False)", start)
    lock = text.index("acquire_gateway_runtime_lock()", start)
    cron = text.index("cron_provider.start", start)
    assert start < fence < replace_marker < replace_signal < lock < cron


def test_independent_ivd_cron_contract_is_rejected():
    from gateway.active_host_fence import validate_runtime_contract

    assert validate_runtime_contract({"mode": "embedded_gateway", "independent_ivd_service_allowed": False}).allowed
    assert validate_runtime_contract({"mode": "external", "independent_ivd_service_allowed": False}).reason == "independent_ivd_cron_forbidden"
    assert validate_runtime_contract({"mode": "embedded_gateway", "independent_ivd_service_allowed": True}).reason == "independent_ivd_cron_forbidden"
