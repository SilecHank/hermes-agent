"""Optional fail-closed active-host fence for the IVD gateway runtime."""

from __future__ import annotations

import base64
import json
import logging
import os
import plistlib
import re
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)
HOST_IDS = frozenset(("wsl-primary", "mac-standby"))
SHA256 = re.compile(r"[0-9a-f]{64}")
RECORD_FIELDS = frozenset(
    (
        "schema_version",
        "host_id",
        "generation",
        "activated_at",
        "reason",
        "operator",
        "deployment_manifest_sha256",
    )
)
OVERRIDE_FIELDS = frozenset(
    (
        "schema_version",
        "host_id",
        "deployment_manifest_sha256",
        "created_at",
        "expires_at",
        "operator",
        "reason",
    )
)
DEFAULT_RECORD_URL = "https://api.github.com/repos/SilecHank/ivd-hermes-standby/contents/active-host.json"
MAX_RECORD_BYTES = 64 * 1024
EXIT_CONFIG = 78


@dataclass(frozen=True)
class FenceDecision:
    allowed: bool
    reason: str


class FenceError(RuntimeError):
    """A stable, log-safe fence failure."""


class IndependentIvdCronServiceError(RuntimeError):
    """A separate IVD scheduler would bypass the gateway ownership fence."""

    def __init__(self, path: Path | str) -> None:
        self.reason = "independent_ivd_cron_forbidden"
        self.path = str(path)
        super().__init__(f"{self.reason}:{self.path}")


class _UnsafePathError(OSError):
    pass


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Stop before urllib creates any redirected request with copied headers."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FenceError("remote_redirect_forbidden")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _bounded_text(value: object, maximum: int = 160) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) <= maximum


def _valid_record(payload: object) -> bool:
    if not isinstance(payload, dict) or set(payload) != RECORD_FIELDS:
        return False
    generation = payload.get("generation")
    digest = payload.get("deployment_manifest_sha256")
    host_id = payload.get("host_id")
    return (
        type(payload.get("schema_version")) is int
        and payload["schema_version"] == 1
        and isinstance(host_id, str)
        and host_id in HOST_IDS
        and type(generation) is int
        and generation >= 1
        and _utc_datetime(payload.get("activated_at")) is not None
        and _bounded_text(payload.get("reason"))
        and _bounded_text(payload.get("operator"))
        and isinstance(digest, str)
        and SHA256.fullmatch(digest) is not None
    )


def evaluate_fence(
    payload: dict[str, Any] | None,
    *,
    local_host: str,
    required: bool = True,
    expected_manifest_sha256: str | None = None,
) -> FenceDecision:
    if payload is None:
        return FenceDecision(not required, "fence_unavailable" if required else "fence_optional")
    if not _valid_record(payload):
        return FenceDecision(False, "fence_record_invalid")
    if payload["host_id"] != local_host:
        return FenceDecision(False, "owner_mismatch")
    if expected_manifest_sha256 and payload["deployment_manifest_sha256"] != expected_manifest_sha256:
        return FenceDecision(False, "manifest_mismatch")
    return FenceDecision(True, "owner_match")


def _read_secure_credential(path: Path) -> str:
    path = Path(path)
    parent_fd = -1
    descriptor = -1
    try:
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        parent_metadata = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_IMODE(parent_metadata.st_mode) & 0o022:
            raise FenceError("credential_path_unsafe")
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise FenceError("credential_path_unsafe")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise FenceError("credential_permissions_unsafe")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise FenceError("credential_owner_invalid")
        raw = os.read(descriptor, 4097)
        if len(raw) > 4096:
            raise FenceError("credential_too_large")
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeError as exc:
            raise FenceError("credential_invalid") from exc
        if not value or "\n" in value or "\r" in value:
            raise FenceError("credential_invalid")
        return value
    except FenceError:
        raise
    except FileNotFoundError as exc:
        raise FenceError("credential_unavailable") from exc
    except OSError as exc:
        raise FenceError("credential_path_unsafe") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)


def _decode_remote_payload(raw: bytes) -> dict[str, Any]:
    try:
        outer = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FenceError("remote_json_invalid") from exc
    if isinstance(outer, dict) and outer.get("encoding") == "base64" and isinstance(outer.get("content"), str):
        try:
            compact_content = re.sub(r"[ \t\r\n]", "", outer["content"])
            decoded = base64.b64decode(compact_content, validate=True)
            if len(decoded) > MAX_RECORD_BYTES:
                raise FenceError("remote_record_too_large")
            outer = json.loads(decoded.decode("utf-8"))
        except FenceError:
            raise
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise FenceError("remote_contents_invalid") from exc
    if not isinstance(outer, dict):
        raise FenceError("remote_record_invalid")
    return outer


def _open_remote_no_redirect(request: urllib.request.Request, timeout: float):
    opener = urllib.request.build_opener(_RejectRedirectHandler())
    return opener.open(request, timeout=timeout)


def fetch_active_host_record(
    url: str,
    *,
    timeout_seconds: float = 3.0,
    max_response_bytes: int = MAX_RECORD_BYTES,
    token_path: Path | None = None,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """Fetch one bounded owner record without exposing its credential."""
    if not isinstance(url, str) or not url.startswith("https://"):
        raise FenceError("remote_url_invalid")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0.1 <= timeout_seconds <= 10:
        raise FenceError("remote_timeout_invalid")
    if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= MAX_RECORD_BYTES:
        raise FenceError("remote_response_limit_invalid")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise FenceError("remote_attempts_invalid")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "hermes-active-host-fence/1"}
    if token_path is not None:
        headers["Authorization"] = f"Bearer {_read_secure_credential(Path(token_path))}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    last_error: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            with _open_remote_no_redirect(request, float(timeout_seconds)) as response:
                raw = response.read(max_response_bytes + 1)
            if len(raw) > max_response_bytes:
                raise FenceError("remote_response_too_large")
            return _decode_remote_payload(raw)
        except FenceError:
            raise
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise FenceError("remote_redirect_forbidden") from exc
            last_error = exc
            if attempt + 1 < max_attempts:
                time.sleep(min(0.1 * (attempt + 1), 0.2))
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt + 1 < max_attempts:
                time.sleep(min(0.1 * (attempt + 1), 0.2))
    raise FenceError("remote_unavailable") from last_error


def _open_readonly_beneath(path: Path, trusted_root: Path) -> int:
    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(trusted_root))
    try:
        relative_parent = path.parent.relative_to(root)
    except ValueError as exc:
        raise OSError("path_escape") from exc
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(root, directory_flags)
    except OSError as exc:
        raise _UnsafePathError("trusted_root_unsafe") from exc
    try:
        root_metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_IMODE(root_metadata.st_mode) & 0o022:
            raise _UnsafePathError("trusted_root_permissions_unsafe")
        for component in relative_parent.parts:
            try:
                next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            except OSError as exc:
                raise _UnsafePathError("parent_path_unsafe") from exc
            os.close(directory_fd)
            directory_fd = next_fd
            metadata = os.fstat(directory_fd)
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o022:
                raise _UnsafePathError("parent_permissions_unsafe")
        try:
            return os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise _UnsafePathError("record_path_unsafe") from exc
    finally:
        os.close(directory_fd)


def read_offline_override(
    path: Path,
    *,
    trusted_root: Path | None = None,
) -> dict[str, Any] | FenceDecision:
    path = Path(path)
    try:
        if path.is_symlink():
            return FenceDecision(False, "override_path_unsafe")
        descriptor = (
            _open_readonly_beneath(path, trusted_root)
            if trusted_root is not None
            else os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        )
    except _UnsafePathError:
        return FenceDecision(False, "override_path_unsafe")
    except OSError:
        return FenceDecision(False, "override_unavailable")
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return FenceDecision(False, "override_path_unsafe")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            return FenceDecision(False, "override_permissions_unsafe")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            return FenceDecision(False, "override_owner_invalid")
        raw = os.read(descriptor, MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            return FenceDecision(False, "override_too_large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return FenceDecision(False, "override_json_invalid")
        return payload if isinstance(payload, dict) else FenceDecision(False, "override_schema_invalid")
    finally:
        os.close(descriptor)


def validate_offline_override(
    payload: object,
    *,
    local_host: str,
    expected_manifest_sha256: str,
    max_validity_seconds: int = 900,
    max_future_skew_seconds: int = 60,
    now: datetime | None = None,
) -> FenceDecision:
    if not isinstance(payload, dict) or set(payload) != OVERRIDE_FIELDS:
        return FenceDecision(False, "override_schema_invalid")
    digest = payload.get("deployment_manifest_sha256")
    if (
        type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or not isinstance(payload.get("host_id"), str)
        or payload["host_id"] not in HOST_IDS
        or not isinstance(digest, str)
        or SHA256.fullmatch(digest) is None
        or not _bounded_text(payload.get("operator"))
        or not _bounded_text(payload.get("reason"))
    ):
        return FenceDecision(False, "override_schema_invalid")
    if payload["host_id"] != local_host:
        return FenceDecision(False, "override_host_mismatch")
    if digest != expected_manifest_sha256:
        return FenceDecision(False, "override_manifest_mismatch")
    created_at = _utc_datetime(payload.get("created_at"))
    expires_at = _utc_datetime(payload.get("expires_at"))
    if created_at is None or expires_at is None or expires_at <= created_at:
        return FenceDecision(False, "override_schema_invalid")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if (expires_at - created_at).total_seconds() > max_validity_seconds:
        return FenceDecision(False, "override_validity_too_long")
    if created_at.timestamp() > current.timestamp() + max_future_skew_seconds:
        return FenceDecision(False, "override_created_in_future")
    if expires_at <= current:
        return FenceDecision(False, "override_expired")
    return FenceDecision(True, "offline_override")


def _append_override_audit(home: Path, payload: dict[str, Any]) -> None:
    home_fd = -1
    state_fd = -1
    descriptor = -1
    try:
        home_fd = os.open(
            Path(home),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        home_metadata = os.fstat(home_fd)
        if not stat.S_ISDIR(home_metadata.st_mode) or stat.S_IMODE(home_metadata.st_mode) & 0o022:
            raise FenceError("audit_path_unsafe")
        try:
            os.mkdir("ivd-state", mode=0o700, dir_fd=home_fd)
            os.fsync(home_fd)
        except FileExistsError:
            pass
        state_fd = os.open(
            "ivd-state",
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=home_fd,
        )
        state_metadata = os.fstat(state_fd)
        if not stat.S_ISDIR(state_metadata.st_mode) or stat.S_IMODE(state_metadata.st_mode) & 0o022:
            raise FenceError("audit_path_unsafe")
        descriptor = os.open(
            "active-host-audit.jsonl",
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=state_fd,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise FenceError("audit_path_unsafe")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise FenceError("audit_permissions_unsafe")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise FenceError("audit_owner_invalid")
        event = {
            "schema_version": 1,
            "event": "offline_override_used",
            "host_id": payload["host_id"],
            "operator": payload["operator"],
            "override_expires_at": payload["expires_at"],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        data = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise FenceError("audit_write_failed")
            view = view[written:]
        os.fsync(descriptor)
        os.fsync(state_fd)
    except FenceError:
        raise
    except OSError as exc:
        raise FenceError("audit_unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if state_fd >= 0:
            os.close(state_fd)
        if home_fd >= 0:
            os.close(home_fd)


def validate_runtime_contract(cron: object) -> FenceDecision:
    if not isinstance(cron, dict):
        return FenceDecision(False, "runtime_contract_invalid")
    if cron.get("mode") != "embedded_gateway" or cron.get("independent_ivd_service_allowed") is not False:
        return FenceDecision(False, "independent_ivd_cron_forbidden")
    return FenceDecision(True, "embedded_cron_owned")


def _systemd_definition_is_independent_ivd_cron(name: str, text: str) -> bool:
    commands: list[str] = []
    schedule: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in {"ExecStart", "ExecStartPre", "ExecStartPost"}:
            commands.append(value.strip())
        elif key.strip() in {"OnCalendar", "OnBootSec", "OnUnitActiveSec", "OnStartupSec"}:
            schedule.append(value.strip())
    command_text = " ".join(commands).lower()
    identity = f"{name} {command_text}".lower()
    if "gateway run" in command_text or "gateway serve" in command_text:
        return False
    exact_runner = any(
        marker in command_text
        for marker in (
            "hermes_daily_maintenance_runner",
            "ivd_daily_maintenance",
            "ivd-maintenance-runner",
        )
    )
    ivd_named = any(marker in identity for marker in ("ivd", "after-sales", "after_sales"))
    scheduled_job = bool(schedule) or name.lower().endswith(".timer")
    cron_named = any(marker in identity for marker in ("cron", "maintenance", "daily", "schedule"))
    return exact_runner or (ivd_named and cron_named and (scheduled_job or bool(commands)))


def _launchd_definition_is_independent_ivd_cron(name: str, text: str | bytes) -> bool:
    try:
        payload = plistlib.loads(text.encode("utf-8") if isinstance(text, str) else text)
    except (ValueError, TypeError, plistlib.InvalidFileException):
        return False
    if not isinstance(payload, dict):
        return False
    label = payload.get("Label") if isinstance(payload.get("Label"), str) else ""
    arguments = payload.get("ProgramArguments")
    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
        arguments = []
    program = payload.get("Program") if isinstance(payload.get("Program"), str) else ""
    command_text = " ".join((program, *arguments)).lower()
    identity = f"{name} {label} {command_text}".lower()
    if "gateway run" in command_text or "gateway serve" in command_text:
        return False
    exact_runner = any(
        marker in command_text
        for marker in (
            "hermes_daily_maintenance_runner",
            "ivd_daily_maintenance",
            "ivd-maintenance-runner",
        )
    )
    ivd_named = any(marker in identity for marker in ("ivd", "after-sales", "after_sales"))
    scheduled_job = any(
        key in payload
        for key in ("StartInterval", "StartCalendarInterval", "StartOnMount")
    )
    cron_named = any(marker in identity for marker in ("cron", "maintenance", "daily", "schedule"))
    return exact_runner or (ivd_named and cron_named and (scheduled_job or bool(arguments)))


def _definition_is_independent_ivd_cron(kind: str, name: str, text: str | bytes) -> bool:
    if kind == "systemd":
        if isinstance(text, bytes):
            try:
                text = text.decode("utf-8")
            except UnicodeError:
                return False
        return _systemd_definition_is_independent_ivd_cron(name, text)
    if kind == "launchd":
        return _launchd_definition_is_independent_ivd_cron(name, text)
    raise ValueError("service_kind_invalid")


def _suspicious_ivd_cron_name(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in ("ivd", "after-sales", "after_sales")) and any(
        marker in lowered for marker in ("cron", "maintenance", "daily", "schedule")
    )


def _read_bounded_service_definition(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        limit = MAX_RECORD_BYTES * 2
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise OSError("service_definition_unsafe")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > limit:
            raise OSError("service_definition_too_large")
        return raw
    finally:
        os.close(descriptor)


def assert_embedded_ivd_cron_service_contract(
    *,
    kind: str,
    service_dir: Path,
    target_path: Path,
    candidate_definition: str | None = None,
) -> FenceDecision:
    """Reject only independent IVD schedulers, preserving unrelated cron jobs."""
    contract = validate_runtime_contract(
        {"mode": "embedded_gateway", "independent_ivd_service_allowed": False}
    )
    if not contract.allowed:
        raise IndependentIvdCronServiceError(target_path)
    if kind not in {"systemd", "launchd"}:
        raise ValueError("service_kind_invalid")

    service_dir = Path(service_dir)
    patterns = ("*.service", "*.timer") if kind == "systemd" else ("*.plist",)
    if service_dir.is_dir():
        for pattern in patterns:
            for path in sorted(service_dir.glob(pattern)):
                try:
                    raw = _read_bounded_service_definition(path)
                except IndependentIvdCronServiceError:
                    raise
                except OSError:
                    if _suspicious_ivd_cron_name(path.name):
                        raise IndependentIvdCronServiceError(path)
                    continue
                if _definition_is_independent_ivd_cron(kind, path.name, raw):
                    raise IndependentIvdCronServiceError(path)

    if candidate_definition is not None and _definition_is_independent_ivd_cron(
        kind, Path(target_path).name, candidate_definition
    ):
        raise IndependentIvdCronServiceError(f"candidate:{target_path}")
    return contract


def assert_active_host_or_raise() -> FenceDecision:
    """Enforce the fence before runtime lock acquisition; default is inert."""
    if not _truthy(os.environ.get("IVD_ACTIVE_HOST_FENCE_REQUIRED")):
        return FenceDecision(True, "fence_disabled")

    local_host = os.environ.get("IVD_ACTIVE_HOST_ID", "").strip()
    expected_manifest = os.environ.get("IVD_DEPLOYMENT_MANIFEST_SHA256", "").strip()
    url = os.environ.get("IVD_ACTIVE_HOST_RECORD_URL", DEFAULT_RECORD_URL).strip()
    token_value = os.environ.get("IVD_ACTIVE_HOST_TOKEN_FILE", "").strip()
    token_path = Path(token_value) if token_value else None
    if local_host not in HOST_IDS or SHA256.fullmatch(expected_manifest) is None:
        logger.error("IVD active-host fence blocked startup: fence_configuration_invalid")
        raise SystemExit(EXIT_CONFIG)

    remote_failure = False
    try:
        payload = fetch_active_host_record(
            url,
            timeout_seconds=3.0,
            max_response_bytes=MAX_RECORD_BYTES,
            token_path=token_path,
            max_attempts=2,
        )
    except FenceError:
        remote_failure = True
        payload = None
    if not remote_failure:
        decision = evaluate_fence(
            payload,
            local_host=local_host,
            required=True,
            expected_manifest_sha256=expected_manifest,
        )
        if decision.allowed:
            return decision
        logger.error("IVD active-host fence blocked startup: %s", decision.reason)
        raise SystemExit(EXIT_CONFIG)

    override_enabled = _truthy(os.environ.get("IVD_ACTIVE_HOST_OFFLINE_OVERRIDE"))
    override_confirmed = _truthy(os.environ.get("IVD_ACTIVE_HOST_OFFLINE_OVERRIDE_CONFIRM"))
    if override_enabled and override_confirmed:
        home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
        override = read_offline_override(
            home / "ivd-state" / "offline-override.json",
            trusted_root=home,
        )
        if isinstance(override, dict):
            decision = validate_offline_override(
                override,
                local_host=local_host,
                expected_manifest_sha256=expected_manifest,
            )
            if decision.allowed:
                try:
                    _append_override_audit(home, override)
                except FenceError:
                    logger.error("IVD active-host fence blocked startup: override_audit_failed")
                    raise SystemExit(EXIT_CONFIG)
                logger.warning("IVD active-host offline override accepted for host %s", local_host)
                return decision
            override_reason = decision.reason
        else:
            override_reason = override.reason
        logger.error("IVD active-host fence blocked startup: %s", override_reason)
    else:
        logger.error("IVD active-host fence blocked startup: fence_unavailable")
    raise SystemExit(EXIT_CONFIG)
