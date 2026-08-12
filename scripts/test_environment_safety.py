#!/usr/bin/env python3
"""Create and verify the canonical pytest home sandbox."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Mapping


MARKER_NAME = ".hermes-test-sandbox.json"
SCHEMA_VERSION = 1
REQUIRED_ENV = (
    "HOME",
    "HERMES_HOME",
    "HERMES_TEST_SANDBOX",
    "HERMES_TEST_SANDBOX_TOKEN",
)


def _identity(path: Path) -> str:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"test sandbox is not a real directory: {path}")
    return f"{metadata.st_dev}:{metadata.st_ino}:{metadata.st_uid}"


def _write_marker(path: Path, payload: dict[str, object]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        content = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        os.write(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def initialize_test_sandbox(root: str | Path) -> dict[str, str]:
    sandbox = Path(root).resolve(strict=True)
    if any(sandbox.iterdir()):
        raise RuntimeError(f"test sandbox must start empty: {sandbox}")
    identity = _identity(sandbox)
    if hasattr(os, "getuid") and sandbox.stat().st_uid != os.getuid():
        raise RuntimeError(f"test sandbox owner mismatch: {sandbox}")
    token = secrets.token_urlsafe(32)
    (sandbox / ".hermes").mkdir(mode=0o700)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "root": str(sandbox),
        "identity": identity,
        "token": token,
    }
    _write_marker(sandbox / MARKER_NAME, payload)
    return {"root": str(sandbox), "identity": identity, "token": token}


def _marker_payload(root: Path) -> dict[str, object]:
    marker = root / MARKER_NAME
    metadata = marker.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("test sandbox marker is not a regular file")
    if metadata.st_size > 4096:
        raise RuntimeError("test sandbox marker is too large")
    payload = json.loads(marker.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("test sandbox marker is malformed")
    return payload


def test_environment_issues(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> list[str]:
    env = os.environ if environ is None else environ
    system = os.name if platform is None else platform
    if any(not env.get(name) for name in REQUIRED_ENV):
        return ["test_sandbox_contract_missing"]

    sandbox = Path(env["HERMES_TEST_SANDBOX"]).resolve(strict=False)
    home = Path(env["HOME"]).resolve(strict=False)
    hermes_home = Path(env["HERMES_HOME"]).resolve(strict=False)
    issues: list[str] = []
    if home != sandbox:
        issues.append("home_outside_test_sandbox")
    if hermes_home == sandbox or not hermes_home.is_relative_to(sandbox):
        issues.append("hermes_home_outside_test_sandbox")
    if system == "nt":
        userprofile = env.get("USERPROFILE")
        if not userprofile or Path(userprofile).resolve(strict=False) != sandbox:
            issues.append("userprofile_outside_test_sandbox")

    try:
        identity = _identity(sandbox)
        payload = _marker_payload(sandbox)
    except (FileNotFoundError, OSError, RuntimeError, json.JSONDecodeError):
        issues.append("test_sandbox_marker_invalid")
        return sorted(set(issues))
    if payload.get("schema_version") != SCHEMA_VERSION:
        issues.append("test_sandbox_schema_invalid")
    if payload.get("root") != str(sandbox):
        issues.append("test_sandbox_root_mismatch")
    if payload.get("identity") != identity:
        issues.append("test_sandbox_identity_mismatch")
    if payload.get("token") != env["HERMES_TEST_SANDBOX_TOKEN"]:
        issues.append("test_sandbox_token_mismatch")
    return sorted(set(issues))


def assert_safe_test_environment() -> None:
    issues = test_environment_issues()
    if issues:
        raise RuntimeError(
            "Refusing to collect tests outside the canonical isolated sandbox: "
            f"{', '.join(issues)}. Run scripts/run_tests.sh."
        )


def pytest_configure(config: object) -> None:
    assert_safe_test_environment()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    initialize = subparsers.add_parser("initialize")
    initialize.add_argument("--root", required=True)
    args = parser.parse_args()
    if args.command == "initialize":
        contract = initialize_test_sandbox(args.root)
        print(contract["token"])
        return 0
    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
