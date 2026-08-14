#!/usr/bin/env python3
"""Build and verify the explicit, development-free IVD serving distribution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping


MANIFEST_NAME = "serving-agent-manifest.json"
_ALLOWLIST_FIELDS = {"schema_version", "entrypoint", "paths", "forbidden_paths"}
_MANIFEST_FIELDS = {
    "schema_version",
    "entrypoint",
    "files",
    "content_manifest_digest",
}
_FILE_FIELDS = {"path", "sha256", "size", "executable"}
_MANDATORY_FORBIDDEN_PATHS = (".git", "skills", "tests", "tools/search_files.py")


class ServingAgentBuildError(RuntimeError):
    """The serving distribution cannot be proven closed and immutable."""


@dataclass(frozen=True)
class ServingAgentBuildResult:
    root: Path
    manifest_digest: str
    file_count: int


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(value: object) -> str:
    text = str(value or "")
    path = PurePosixPath(text)
    if (
        not text
        or "\\" in text
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ServingAgentBuildError("serving allowlist path is unsafe")
    return path.as_posix()


def _is_forbidden(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _load_allowlist(path: Path) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ServingAgentBuildError("cannot read serving allowlist") from error
    if not isinstance(payload, Mapping) or set(payload) != _ALLOWLIST_FIELDS:
        raise ServingAgentBuildError("serving allowlist fields are invalid")
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ServingAgentBuildError("serving allowlist schema is invalid")
    if not isinstance(payload.get("paths"), list) or not isinstance(
        payload.get("forbidden_paths"), list
    ):
        raise ServingAgentBuildError("serving allowlist members are invalid")
    paths = tuple(_relative(item) for item in payload["paths"])
    declared_forbidden = tuple(
        _relative(item) for item in payload["forbidden_paths"]
    )
    entrypoint = _relative(payload.get("entrypoint"))
    if len(set(paths)) != len(paths) or len(set(declared_forbidden)) != len(
        declared_forbidden
    ):
        raise ServingAgentBuildError("serving allowlist contains duplicates")
    forbidden = tuple(
        sorted(set(declared_forbidden).union(_MANDATORY_FORBIDDEN_PATHS))
    )
    if entrypoint not in paths or any(_is_forbidden(item, forbidden) for item in paths):
        raise ServingAgentBuildError("serving allowlist violates its boundary")
    return entrypoint, tuple(sorted(paths)), forbidden


def _source_file(root: Path, relative: str) -> Path:
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise ServingAgentBuildError(f"allowlisted source is missing: {relative}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ServingAgentBuildError(f"allowlisted source is a symlink: {relative}")
    if not current.is_file():
        raise ServingAgentBuildError(f"allowlisted source is not a file: {relative}")
    return current


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            raise ServingAgentBuildError("serving distribution contains a symlink")
        if path.is_dir():
            path.chmod(0o555)
        else:
            executable = bool(path.stat().st_mode & 0o111)
            path.chmod(0o555 if executable else 0o444)
    root.chmod(0o555)


def build_serving_agent(
    agent_root: str | Path,
    destination: str | Path,
    allowlist_path: str | Path | None = None,
) -> ServingAgentBuildResult:
    root = Path(agent_root)
    destination = Path(destination)
    if root.is_symlink():
        raise ServingAgentBuildError("agent root cannot be a symlink")
    try:
        root = root.resolve(strict=True)
    except OSError as error:
        raise ServingAgentBuildError("agent root is unavailable") from error
    allowlist = Path(allowlist_path) if allowlist_path else (
        root / "knowledge-base/ivd-serving-agent-allowlist.json"
    )
    entrypoint, paths, _ = _load_allowlist(allowlist)
    if destination.exists():
        raise ServingAgentBuildError("serving destination must not already exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".ivd-serving-agent.", dir=destination.parent))
    try:
        files: list[dict[str, object]] = []
        for relative in paths:
            source = _source_file(root, relative)
            data = source.read_bytes()
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            executable = bool(source.stat().st_mode & 0o111)
            target.chmod(0o755 if executable else 0o644)
            files.append(
                {
                    "path": relative,
                    "sha256": _digest(data),
                    "size": len(data),
                    "executable": executable,
                }
            )
        unsigned = {"schema_version": 1, "entrypoint": entrypoint, "files": files}
        manifest_digest = _digest(_canonical(unsigned))
        manifest = {**unsigned, "content_manifest_digest": manifest_digest}
        (staging / MANIFEST_NAME).write_bytes(_canonical(manifest))
        _make_read_only(staging)
        os.replace(staging, destination)
    except Exception:
        if staging.exists():
            for path in staging.rglob("*"):
                if not path.is_symlink():
                    try:
                        path.chmod(0o755 if path.is_dir() else 0o644)
                    except OSError:
                        pass
            staging.chmod(0o755)
            shutil.rmtree(staging)
        raise
    verify_serving_agent(destination)
    return ServingAgentBuildResult(destination, manifest_digest, len(files))


def verify_serving_agent(root: str | Path) -> dict[str, object]:
    root = Path(root)
    if root.is_symlink():
        raise ServingAgentBuildError("serving root cannot be a symlink")
    try:
        root = root.resolve(strict=True)
        payload = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ServingAgentBuildError("cannot read serving manifest") from error
    if not isinstance(payload, Mapping) or set(payload) != _MANIFEST_FIELDS:
        raise ServingAgentBuildError("serving manifest fields are invalid")
    files = payload.get("files")
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ServingAgentBuildError("serving manifest schema is invalid")
    if not isinstance(files, list):
        raise ServingAgentBuildError("serving manifest files are invalid")
    expected: set[str] = {MANIFEST_NAME}
    normalized: list[dict[str, object]] = []
    for item in files:
        if not isinstance(item, Mapping) or set(item) != _FILE_FIELDS:
            raise ServingAgentBuildError("serving manifest file entry is invalid")
        relative = _relative(item.get("path"))
        if relative in expected:
            raise ServingAgentBuildError("serving manifest contains duplicates")
        if type(item.get("size")) is not int or item["size"] < 0:
            raise ServingAgentBuildError("serving manifest size is invalid")
        if type(item.get("executable")) is not bool:
            raise ServingAgentBuildError("serving manifest mode is invalid")
        target = _source_file(root, relative)
        mode = stat.S_IMODE(target.stat().st_mode)
        expected_mode = 0o555 if item.get("executable") else 0o444
        if mode != expected_mode:
            raise ServingAgentBuildError("serving file permissions are invalid")
        data = target.read_bytes()
        if len(data) != item["size"] or _digest(data) != item.get("sha256"):
            raise ServingAgentBuildError("serving file digest mismatch")
        expected.add(relative)
        normalized.append(dict(item))
    if [item["path"] for item in normalized] != sorted(item["path"] for item in normalized):
        raise ServingAgentBuildError("serving manifest order is invalid")
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ServingAgentBuildError("serving distribution contains a symlink")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != expected:
        raise ServingAgentBuildError("serving distribution has unexpected files")
    if stat.S_IMODE(root.stat().st_mode) != 0o555:
        raise ServingAgentBuildError("serving root permissions are invalid")
    if any(
        stat.S_IMODE(path.stat().st_mode) != 0o555
        for path in root.rglob("*")
        if path.is_dir()
    ):
        raise ServingAgentBuildError("serving directory permissions are invalid")
    unsigned = {
        "schema_version": payload["schema_version"],
        "entrypoint": payload.get("entrypoint"),
        "files": normalized,
    }
    calculated = _digest(_canonical(unsigned))
    if payload.get("content_manifest_digest") != calculated:
        raise ServingAgentBuildError("serving manifest digest mismatch")
    if payload.get("entrypoint") not in {item["path"] for item in normalized}:
        raise ServingAgentBuildError("serving entrypoint is absent")
    return {"status": "ready", "manifest_digest": calculated, "files": len(files)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-root", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--allowlist")
    args = parser.parse_args()
    result = build_serving_agent(args.agent_root, args.destination, args.allowlist)
    print(json.dumps({"status": "ready", "root": str(result.root), "manifest_digest": result.manifest_digest, "files": result.file_count}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
