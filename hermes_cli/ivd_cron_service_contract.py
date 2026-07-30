"""Opt-in installer contract that rejects independent scheduled IVD jobs."""

from __future__ import annotations

import os
import plistlib
import re
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


@dataclass(frozen=True)
class FenceDecision:
    allowed: bool
    reason: str


class IndependentIvdCronServiceError(RuntimeError):
    """A separate IVD scheduler would bypass the gateway ownership fence."""

    def __init__(self, path: Path | str) -> None:
        self.reason = "independent_ivd_cron_forbidden"
        self.path = str(path)
        super().__init__(f"{self.reason}:{self.path}")


class IvdCronServiceDiscoveryError(RuntimeError):
    """An effective service scope could not be inspected safely."""

    def __init__(self, reason: str, path: Path | str) -> None:
        self.reason = reason
        self.path = str(path)
        super().__init__(f"{self.reason}:{self.path}")


MAX_SERVICE_DEFINITION_BYTES = 128 * 1024
MAX_SYSTEMD_DROPIN_LEVELS = 64
MAX_SYSTEMD_UNIT_NAME_BYTES = 255
_SYSTEMD_UNIT_SUFFIXES = frozenset(
    (
        "service",
        "socket",
        "device",
        "mount",
        "automount",
        "swap",
        "target",
        "path",
        "timer",
        "slice",
        "scope",
    )
)
_SYSTEMD_DEPENDENCY_DIRECTORY_SUFFIXES = ("wants", "requires", "upholds")
SYSTEMD_ANALYZE_CANDIDATES = (
    Path("/usr/bin/systemd-analyze"),
    Path("/bin/systemd-analyze"),
    Path("/usr/local/bin/systemd-analyze"),
    Path("/run/current-system/sw/bin/systemd-analyze"),
)
_AUTO_SYSTEMD_ANALYZE = object()


def validate_runtime_contract(cron: object) -> FenceDecision:
    if not isinstance(cron, dict):
        return FenceDecision(False, "runtime_contract_invalid")
    if cron.get("mode") != "embedded_gateway" or cron.get("independent_ivd_service_allowed") is not False:
        return FenceDecision(False, "independent_ivd_cron_forbidden")
    return FenceDecision(True, "embedded_cron_owned")


_IVD_IDENTITY = re.compile(r"(?<![a-z0-9])(?:ivd|after[-_ ]sales)(?![a-z0-9])", re.IGNORECASE)
_IVD_RUNNERS = (
    "hermes_daily_maintenance_runner",
    "ivd_daily_maintenance",
    "ivd-maintenance-runner",
)
_SYSTEMD_PERIODIC_KEYS = frozenset(
    (
        "OnActiveSec",
        "OnBootSec",
        "OnStartupSec",
        "OnUnitActiveSec",
        "OnUnitInactiveSec",
        "OnCalendar",
        "OnClockChange",
        "OnTimezoneChange",
    )
)
_LAUNCHD_PERIODIC_KEYS = frozenset(("StartInterval", "StartCalendarInterval"))


def _has_explicit_ivd_identity(text: str) -> bool:
    lowered = text.lower()
    return _IVD_IDENTITY.search(lowered) is not None or any(
        marker in lowered for marker in _IVD_RUNNERS
    )


def _systemd_definition_is_independent_ivd_cron(name: str, text: str) -> bool:
    periodic, _, identity = _systemd_timer_metadata(name, text)
    return periodic and identity


def _launchd_payload(name: str, text: str | bytes) -> dict[object, object]:
    try:
        payload = plistlib.loads(text.encode("utf-8") if isinstance(text, str) else text)
    except (ValueError, TypeError, IndexError, plistlib.InvalidFileException) as exc:
        raise IvdCronServiceDiscoveryError("launchd_plist_invalid", name) from exc
    if not isinstance(payload, dict):
        raise IvdCronServiceDiscoveryError("launchd_plist_invalid", name)
    label = payload.get("Label")
    program = payload.get("Program")
    arguments = payload.get("ProgramArguments")
    valid_program = isinstance(program, str) and bool(program.strip())
    valid_arguments = (
        isinstance(arguments, list)
        and bool(arguments)
        and all(isinstance(item, str) and bool(item) for item in arguments)
    )
    interval = payload.get("StartInterval")
    calendar = payload.get("StartCalendarInterval")
    valid_calendar = (
        calendar is None
        or isinstance(calendar, dict)
        or (
            isinstance(calendar, list)
            and all(isinstance(item, dict) for item in calendar)
        )
    )
    if (
        not isinstance(label, str)
        or not label.strip()
        or not (valid_program or valid_arguments)
        or (interval is not None and (type(interval) is not int or interval <= 0))
        or not valid_calendar
    ):
        raise IvdCronServiceDiscoveryError("launchd_plist_invalid", name)
    return payload


def _launchd_definition_is_independent_ivd_cron(name: str, text: str | bytes) -> bool:
    payload = _launchd_payload(name, text)
    label = payload.get("Label") if isinstance(payload.get("Label"), str) else ""
    arguments = payload.get("ProgramArguments")
    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
        arguments = []
    program = payload.get("Program") if isinstance(payload.get("Program"), str) else ""
    command_text = " ".join((program, *arguments)).lower()
    identity = f"{name} {label} {command_text}".lower()
    if "gateway run" in command_text or "gateway serve" in command_text:
        return False
    periodic = any(key in payload for key in _LAUNCHD_PERIODIC_KEYS)
    return periodic and _has_explicit_ivd_identity(identity)


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
    return _has_explicit_ivd_identity(name)


def _read_bounded_service_definition(path: Path, *, dir_fd: int | None = None) -> bytes:
    open_path: str | Path = path.name if dir_fd is not None else path
    descriptor = os.open(
        open_path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=dir_fd,
    )
    try:
        metadata = os.fstat(descriptor)
        limit = MAX_SERVICE_DEFINITION_BYTES
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


def _map_service_scope(scope_root: Path, logical_path: Path) -> Path:
    if not logical_path.is_absolute():
        raise ValueError("service_scope_path_not_absolute")
    logical_path = Path(os.path.normpath(os.fspath(logical_path)))
    root = Path(os.path.normpath(os.fspath(scope_root)))
    if root == Path("/"):
        return logical_path
    return root.joinpath(*logical_path.parts[1:])


def _deduplicate_paths(paths: list[Path]) -> tuple[Path, ...]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(os.path.normpath(os.fspath(path)))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return tuple(unique)


MAX_SYSTEMD_SERVICE_SCOPES = 64
MAX_SYSTEMD_ANALYZE_OUTPUT_BYTES = 64 * 1024
MAX_SYSTEMD_ANALYZE_PATH_BYTES = 4096
SYSTEMD_ANALYZE_TIMEOUT_SECONDS = 2.0


def validate_systemd_analyze_binary(path: Path) -> Path:
    """Resolve a fixed candidate through trusted directories to a root-owned inode."""
    candidate = Path(path)
    if not candidate.is_absolute():
        raise IvdCronServiceDiscoveryError(
            "systemd_analyze_binary_untrusted", candidate
        )
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise IvdCronServiceDiscoveryError(
            "systemd_analyze_binary_untrusted", candidate
        ) from exc
    current_user = os.geteuid() if hasattr(os, "geteuid") else None

    def assert_trusted_directory(component: Path) -> None:
        try:
            component_metadata = component.stat()
        except OSError as exc:
            raise IvdCronServiceDiscoveryError(
                "systemd_analyze_binary_untrusted", candidate
            ) from exc
        mode = stat.S_IMODE(component_metadata.st_mode)
        user_mutable = (
            current_user not in (None, 0)
            and component_metadata.st_uid == current_user
            and bool(mode & stat.S_IWUSR)
        )
        if (
            not stat.S_ISDIR(component_metadata.st_mode)
            or mode & 0o022
            or user_mutable
        ):
            raise IvdCronServiceDiscoveryError(
                "systemd_analyze_binary_untrusted", candidate
            )

    current = Path("/")
    assert_trusted_directory(current)
    for component_name in candidate.parent.parts[1:]:
        try:
            current = (current / component_name).resolve(strict=True)
        except OSError as exc:
            raise IvdCronServiceDiscoveryError(
                "systemd_analyze_binary_untrusted", candidate
            ) from exc
        assert_trusted_directory(current)
    for component in reversed((resolved.parent, *resolved.parent.parents)):
        assert_trusted_directory(component)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not stat.S_IMODE(metadata.st_mode) & 0o111
    ):
        raise IvdCronServiceDiscoveryError(
            "systemd_analyze_binary_untrusted", candidate
        )
    return resolved


def find_trusted_systemd_analyze(
    *,
    candidates: tuple[Path, ...] | None = None,
) -> Path:
    for candidate in SYSTEMD_ANALYZE_CANDIDATES if candidates is None else candidates:
        try:
            return validate_systemd_analyze_binary(candidate)
        except IvdCronServiceDiscoveryError:
            continue
    raise IvdCronServiceDiscoveryError(
        "systemd_analyze_binary_untrusted", "systemd-analyze"
    )


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass
    if process.poll() is None:
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass


def _systemd_analyze_environment(environ: Mapping[str, str]) -> dict[str, str]:
    safe = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LC_ALL": "C",
    }
    allowed = (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_CONFIG_DIRS",
        "XDG_DATA_HOME",
        "XDG_DATA_DIRS",
        "XDG_RUNTIME_DIR",
        "SYSTEMD_UNIT_PATH",
        "SYSTEMD_USER_UNIT_PATH",
    )
    for name in allowed:
        value = environ.get(name)
        if isinstance(value, str):
            safe[name] = value
    return safe


def run_systemd_analyze_unit_paths(
    *,
    binary_path: Path,
    user: bool,
    timeout_seconds: float = SYSTEMD_ANALYZE_TIMEOUT_SECONDS,
    max_output_bytes: int = MAX_SYSTEMD_ANALYZE_OUTPUT_BYTES,
    environ: Mapping[str, str] | None = None,
) -> bytes:
    """Run one bounded unit-paths query without a shell or inherited loader hooks."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= SYSTEMD_ANALYZE_TIMEOUT_SECONDS
        or type(max_output_bytes) is not int
        or not 1 <= max_output_bytes <= MAX_SYSTEMD_ANALYZE_OUTPUT_BYTES
    ):
        raise IvdCronServiceDiscoveryError(
            "systemd_unit_paths_invalid", binary_path
        )
    command = [os.fspath(binary_path)]
    if user:
        command.append("--user")
    command.append("unit-paths")
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    output = bytearray()
    deadline = time.monotonic() + float(timeout_seconds)
    try:
        process = subprocess.Popen(
            command,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_systemd_analyze_environment(os.environ if environ is None else environ),
            start_new_session=True,
            close_fds=True,
        )
        if process.stdout is None:
            raise OSError("systemd_analyze_stdout_unavailable")
        descriptor = process.stdout.fileno()
        os.set_blocking(descriptor, False)
        selector.register(descriptor, selectors.EVENT_READ)
        stdout_open = True
        while stdout_open:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_group(process)
                raise IvdCronServiceDiscoveryError(
                    "systemd_unit_paths_timeout", binary_path
                )
            events = selector.select(min(remaining, 0.1))
            if not events:
                continue
            for key, _ in events:
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fd)
                    stdout_open = False
                    break
                output.extend(chunk)
                if len(output) > max_output_bytes:
                    _terminate_process_group(process)
                    raise IvdCronServiceDiscoveryError(
                        "systemd_unit_paths_output_limit", binary_path
                    )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process_group(process)
            raise IvdCronServiceDiscoveryError(
                "systemd_unit_paths_timeout", binary_path
            )
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(process)
            raise IvdCronServiceDiscoveryError(
                "systemd_unit_paths_timeout", binary_path
            ) from exc
        if return_code != 0:
            raise IvdCronServiceDiscoveryError(
                "systemd_unit_paths_command_failed", binary_path
            )
        return bytes(output)
    except IvdCronServiceDiscoveryError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if process is not None:
            _terminate_process_group(process)
        raise IvdCronServiceDiscoveryError(
            "systemd_unit_paths_command_failed", binary_path
        ) from exc
    finally:
        selector.close()
        if process is not None and process.stdout is not None:
            process.stdout.close()


def _parse_systemd_unit_paths(raw: bytes, *, source: Path) -> tuple[Path, ...]:
    if len(raw) > MAX_SYSTEMD_ANALYZE_OUTPUT_BYTES:
        raise IvdCronServiceDiscoveryError("systemd_unit_paths_output_limit", source)
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise IvdCronServiceDiscoveryError("systemd_unit_paths_invalid", source) from exc
    lines = text.splitlines()
    if not lines or len(lines) > MAX_SYSTEMD_SERVICE_SCOPES:
        raise IvdCronServiceDiscoveryError("systemd_unit_paths_invalid", source)
    paths: list[Path] = []
    seen: set[str] = set()
    for line in lines:
        if (
            not line
            or "\x00" in line
            or len(line.encode("utf-8")) > MAX_SYSTEMD_ANALYZE_PATH_BYTES
        ):
            raise IvdCronServiceDiscoveryError("systemd_unit_paths_invalid", source)
        path = Path(line)
        normalized = os.path.normpath(line)
        if not path.is_absolute() or normalized != line or normalized in seen:
            raise IvdCronServiceDiscoveryError("systemd_unit_paths_invalid", source)
        seen.add(normalized)
        paths.append(path)
    return tuple(paths)


def _discover_systemd_unit_paths(
    *,
    binary_path: Path,
    runner: Callable[..., bytes],
    environ: Mapping[str, str],
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    trusted_binary = validate_systemd_analyze_binary(binary_path)
    outputs: list[tuple[Path, ...]] = []
    for user in (False, True):
        try:
            raw = runner(
                binary_path=trusted_binary,
                user=user,
                timeout_seconds=SYSTEMD_ANALYZE_TIMEOUT_SECONDS,
                max_output_bytes=MAX_SYSTEMD_ANALYZE_OUTPUT_BYTES,
                environ=environ,
            )
        except IvdCronServiceDiscoveryError:
            raise
        except Exception as exc:
            raise IvdCronServiceDiscoveryError(
                "systemd_unit_paths_command_failed", trusted_binary
            ) from exc
        if not isinstance(raw, bytes):
            raise IvdCronServiceDiscoveryError(
                "systemd_unit_paths_invalid", trusted_binary
            )
        outputs.append(_parse_systemd_unit_paths(raw, source=trusted_binary))
    system_paths, user_paths = outputs
    return user_paths, system_paths


def _absolute_env_path(value: str, *, source: str) -> Path:
    path = Path(value)
    if "\x00" in value or not path.is_absolute():
        raise IvdCronServiceDiscoveryError("service_scope_env_invalid", source)
    return Path(os.path.normpath(os.fspath(path)))


def _bounded_absolute_components(value: str, *, source: str) -> tuple[Path, ...]:
    paths: list[Path] = []
    for component in value.split(":"):
        if not component:
            continue
        if len(paths) >= MAX_SYSTEMD_SERVICE_SCOPES:
            raise IvdCronServiceDiscoveryError("service_scope_limit", source)
        paths.append(_absolute_env_path(component, source=source))
    return tuple(paths)


def _xdg_home_path(
    environ: Mapping[str, str],
    name: str,
    default: Path,
) -> Path:
    value = environ.get(name, "")
    return default if not value else _absolute_env_path(value, source=name)


def _xdg_directory_list(
    environ: Mapping[str, str],
    name: str,
    defaults: tuple[Path, ...],
) -> tuple[Path, ...]:
    value = environ.get(name, "")
    if not value:
        return defaults
    paths = _bounded_absolute_components(value, source=name)
    return paths or defaults


def _systemd_user_directory(base: Path) -> Path:
    if base.parts[-2:] == ("systemd", "user"):
        return base
    return base / "systemd" / "user"


def _systemd_path_override(
    environ: Mapping[str, str],
    name: str,
    defaults: tuple[Path, ...],
) -> tuple[Path, ...]:
    if name not in environ:
        return defaults
    value = environ[name]
    if value == "":
        return ()
    append_defaults = value.endswith(":")
    paths = list(_bounded_absolute_components(value, source=name))
    if append_defaults:
        paths.extend(defaults)
    return _deduplicate_paths(paths)


def _systemd_service_scope_groups(
    *,
    target_path: Path,
    scope_root: Path,
    home: Path,
    environ: Mapping[str, str],
    uid: int | None,
    discovered_user: tuple[Path, ...] = (),
    discovered_system: tuple[Path, ...] = (),
) -> tuple[tuple[Path, ...], ...]:
    config_home = _xdg_home_path(environ, "XDG_CONFIG_HOME", home / ".config")
    config_dirs = _xdg_directory_list(
        environ,
        "XDG_CONFIG_DIRS",
        (Path("/etc/xdg"),),
    )
    data_home = _xdg_home_path(environ, "XDG_DATA_HOME", home / ".local" / "share")
    data_dirs = _xdg_directory_list(
        environ,
        "XDG_DATA_DIRS",
        (Path("/usr/local/share"), Path("/usr/share")),
    )
    runtime_value = environ.get("XDG_RUNTIME_DIR", "")
    if runtime_value:
        runtime_home = _absolute_env_path(runtime_value, source="XDG_RUNTIME_DIR")
    else:
        effective_uid = uid
        if effective_uid is None and hasattr(os, "getuid"):
            effective_uid = os.getuid()
        runtime_home = Path("/run/user") / str(effective_uid) if effective_uid is not None else None
    user_logical = [
        config_home / "systemd" / "user.control",
    ]
    if runtime_home is not None:
        user_logical.extend(
            (
                runtime_home / "systemd" / "user.control",
                runtime_home / "systemd" / "transient",
                runtime_home / "systemd" / "generator.early",
            )
        )
    user_logical.extend(
        (
            config_home / "systemd" / "user",
            *(_systemd_user_directory(path) for path in config_dirs),
            Path("/etc/systemd/user"),
        )
    )
    if runtime_home is not None:
        user_logical.append(runtime_home / "systemd" / "user")
    user_logical.append(Path("/run/systemd/user"))
    if runtime_home is not None:
        user_logical.append(runtime_home / "systemd" / "generator")
    user_logical.extend(
        (
            data_home / "systemd" / "user",
            *(_systemd_user_directory(path) for path in data_dirs),
            Path("/usr/local/lib/systemd/user"),
            Path("/usr/lib/systemd/user"),
        )
    )
    if runtime_home is not None:
        user_logical.append(runtime_home / "systemd" / "generator.late")
    system_logical = (
        Path(path)
        for path in (
            "/etc/systemd/system.control",
            "/run/systemd/system.control",
            "/run/systemd/transient",
            "/run/systemd/generator.early",
            "/etc/systemd/system",
            "/etc/systemd/system.attached",
            "/run/systemd/system",
            "/run/systemd/system.attached",
            "/run/systemd/generator",
            "/usr/local/lib/systemd/system",
            "/usr/lib/systemd/system",
            "/run/systemd/generator.late",
        )
    )
    default_user = _deduplicate_paths([Path(path) for path in user_logical])
    default_system = _deduplicate_paths(list(system_logical))
    active_user = _systemd_path_override(
        environ,
        "SYSTEMD_USER_UNIT_PATH",
        default_user,
    )
    active_system = _systemd_path_override(
        environ,
        "SYSTEMD_UNIT_PATH",
        default_system,
    )
    active_user = _deduplicate_paths([*discovered_user, *active_user])
    active_system = _deduplicate_paths([*discovered_system, *active_system])
    root = Path(scope_root)
    default_user_scopes = _deduplicate_paths(
        [_map_service_scope(root, path) for path in default_user]
    )
    default_system_scopes = _deduplicate_paths(
        [_map_service_scope(root, path) for path in default_system]
    )
    user_scopes = _deduplicate_paths([_map_service_scope(root, path) for path in active_user])
    system_scopes = _deduplicate_paths([_map_service_scope(root, path) for path in active_system])
    target_scope = Path(target_path).parent
    groups: list[tuple[Path, ...]] = []
    if target_scope not in default_user_scopes and target_scope not in default_system_scopes:
        groups.append((target_scope,))
    groups.extend((user_scopes, system_scopes))
    return tuple(group for group in groups if group)


def _effective_systemd_service_scope_groups(
    *,
    target_path: Path,
    scope_root: Path,
    home: Path,
    environ: Mapping[str, str],
    uid: int | None,
    systemd_analyze_path: Path | None,
    systemd_analyze_runner: Callable[..., bytes] | None,
) -> tuple[tuple[Path, ...], ...]:
    discovered_user: tuple[Path, ...] = ()
    discovered_system: tuple[Path, ...] = ()
    if systemd_analyze_path is not None:
        discovered_user, discovered_system = _discover_systemd_unit_paths(
            binary_path=Path(systemd_analyze_path),
            runner=systemd_analyze_runner or run_systemd_analyze_unit_paths,
            environ=environ,
        )
    return _systemd_service_scope_groups(
        target_path=target_path,
        scope_root=scope_root,
        home=home,
        environ=environ,
        uid=uid,
        discovered_user=discovered_user,
        discovered_system=discovered_system,
    )


def discover_ivd_cron_service_scopes(
    kind: str,
    *,
    target_path: Path,
    scope_root: Path = Path("/"),
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    uid: int | None = None,
    max_scopes: int = MAX_SYSTEMD_SERVICE_SCOPES,
    systemd_analyze_path: Path | None = None,
    systemd_analyze_runner: Callable[..., bytes] | None = None,
) -> tuple[Path, ...]:
    """Return bounded effective service scopes, optionally mapped under a test root."""
    if kind not in {"systemd", "launchd"}:
        raise ValueError("service_kind_invalid")
    environment = os.environ if environ is None else environ
    home_path = Path.home() if home is None else Path(home)
    if kind == "systemd":
        groups = _effective_systemd_service_scope_groups(
            target_path=target_path,
            scope_root=Path(scope_root),
            home=home_path,
            environ=environment,
            uid=uid,
            systemd_analyze_path=systemd_analyze_path,
            systemd_analyze_runner=systemd_analyze_runner,
        )
        scopes = _deduplicate_paths([scope for group in groups for scope in group])
    else:
        logical_paths = (
            home_path / "Library" / "LaunchAgents",
            Path("/Library/LaunchAgents"),
            Path("/Library/LaunchDaemons"),
            Path("/System/Library/LaunchAgents"),
            Path("/System/Library/LaunchDaemons"),
        )
        scopes = _deduplicate_paths(
            [Path(target_path).parent]
            + [_map_service_scope(Path(scope_root), path) for path in logical_paths]
        )
    scope_limit = min(max_scopes, MAX_SYSTEMD_SERVICE_SCOPES)
    if scope_limit < 1 or len(scopes) > scope_limit:
        raise IvdCronServiceDiscoveryError("service_scope_limit", target_path)
    return scopes


@dataclass(frozen=True)
class _SystemdUnitRecord:
    name: str
    path: Path
    raw: bytes | None = None
    alias_target: str | None = None
    read_error: bool = False


@dataclass(frozen=True)
class _SynthesizedSystemdTimer:
    record: _SystemdUnitRecord
    source_path: Path


def _resolve_systemd_scope_alias(
    scope: Path,
    *,
    allowed_scopes: frozenset[Path],
    scope_root: Path,
    max_hops: int = 8,
) -> Path | None:
    current = scope
    visited: set[Path] = set()
    for hop in range(max_hops + 1):
        if current in visited:
            raise IvdCronServiceDiscoveryError("service_scope_symlink", scope)
        visited.add(current)
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if hop == 0:
                return None
            raise IvdCronServiceDiscoveryError("service_scope_unreadable", scope)
        except OSError as exc:
            raise IvdCronServiceDiscoveryError("service_scope_unreadable", scope) from exc
        if not stat.S_ISLNK(metadata.st_mode):
            if not stat.S_ISDIR(metadata.st_mode):
                raise IvdCronServiceDiscoveryError("service_scope_unreadable", scope)
            return current
        try:
            target = Path(os.readlink(current))
        except OSError as exc:
            raise IvdCronServiceDiscoveryError("service_scope_unreadable", scope) from exc
        if target.is_absolute() and scope_root != Path("/") and not target.is_relative_to(scope_root):
            target = _map_service_scope(scope_root, target)
        elif not target.is_absolute():
            target = current.parent / target
        current = Path(os.path.normpath(os.fspath(target)))
        if current not in allowed_scopes:
            raise IvdCronServiceDiscoveryError("service_scope_symlink", scope)
    raise IvdCronServiceDiscoveryError("service_scope_symlink", scope)


def _collect_systemd_scope(scope: Path, *, max_entries: int) -> dict[str, _SystemdUnitRecord]:
    try:
        metadata = scope.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise IvdCronServiceDiscoveryError("service_scope_unreadable", scope) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise IvdCronServiceDiscoveryError("service_scope_symlink", scope)
    if not stat.S_ISDIR(metadata.st_mode):
        raise IvdCronServiceDiscoveryError("service_scope_unreadable", scope)

    descriptor = -1
    try:
        descriptor = os.open(
            scope,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("service_scope_not_directory")
        with os.scandir(descriptor) as entries:
            names: list[str] = []
            for entry in entries:
                names.append(entry.name)
                if len(names) > max_entries:
                    raise IvdCronServiceDiscoveryError("service_scope_entry_limit", scope)
        records: dict[str, _SystemdUnitRecord] = {}
        for name in sorted(names):
            if not name.endswith((".service", ".timer")):
                continue
            path = scope / name
            try:
                entry_stat = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(entry_stat.st_mode):
                    records[name] = _SystemdUnitRecord(
                        name=name,
                        path=path,
                        alias_target=os.readlink(name, dir_fd=descriptor),
                    )
                else:
                    records[name] = _SystemdUnitRecord(
                        name=name,
                        path=path,
                        raw=_read_bounded_service_definition(path, dir_fd=descriptor),
                    )
            except OSError:
                records[name] = _SystemdUnitRecord(name=name, path=path, read_error=True)
        return records
    except (IndependentIvdCronServiceError, IvdCronServiceDiscoveryError):
        raise
    except OSError as exc:
        raise IvdCronServiceDiscoveryError("service_scope_unreadable", scope) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _effective_systemd_units(
    scopes: tuple[Path, ...],
    *,
    max_entries: int,
    scope_root: Path,
) -> tuple[dict[str, _SystemdUnitRecord], dict[Path, _SystemdUnitRecord]]:
    effective: dict[str, _SystemdUnitRecord] = {}
    by_path: dict[Path, _SystemdUnitRecord] = {}
    allowed_scopes = frozenset(scopes)
    visited_scopes: set[Path] = set()
    for scope in scopes:
        resolved_scope = _resolve_systemd_scope_alias(
            scope,
            allowed_scopes=allowed_scopes,
            scope_root=scope_root,
        )
        if resolved_scope is None or resolved_scope in visited_scopes:
            continue
        visited_scopes.add(resolved_scope)
        records = _collect_systemd_scope(resolved_scope, max_entries=max_entries)
        by_path.update((record.path, record) for record in records.values())
        for name, record in records.items():
            effective.setdefault(name, record)
    return effective, by_path


def _resolve_systemd_unit_alias(
    record: _SystemdUnitRecord,
    *,
    records_by_path: Mapping[Path, _SystemdUnitRecord],
    allowed_scopes: frozenset[Path],
    scope_root: Path,
    max_hops: int = 8,
) -> _SystemdUnitRecord | None:
    current = record
    visited: set[Path] = set()
    for _ in range(max_hops + 1):
        if current.path in visited:
            raise IvdCronServiceDiscoveryError("systemd_unit_alias_cycle", current.path)
        visited.add(current.path)
        if current.read_error:
            raise IvdCronServiceDiscoveryError("service_definition_unreadable", current.path)
        if current.alias_target is None:
            return current
        if current.alias_target == "/dev/null":
            return None
        target = Path(current.alias_target)
        if target.is_absolute() and scope_root != Path("/") and not target.is_relative_to(scope_root):
            target = _map_service_scope(scope_root, target)
        elif not target.is_absolute():
            target = current.path.parent / target
        target = Path(os.path.normpath(os.fspath(target)))
        if target.parent not in allowed_scopes:
            raise IvdCronServiceDiscoveryError("systemd_unit_alias_escape", current.path)
        next_record = records_by_path.get(target)
        if next_record is None:
            return None
        current = next_record
    raise IvdCronServiceDiscoveryError("systemd_unit_alias_limit", record.path)


def _systemd_directives(text: str) -> list[tuple[str, str, str]]:
    directives: list[tuple[str, str, str]] = []
    section = ""
    logical_lines: list[str] = []
    continued = ""
    for raw_line in text.splitlines():
        line = continued + (raw_line.lstrip() if continued else raw_line)
        if line.endswith("\\"):
            continued = line[:-1] + " "
            continue
        logical_lines.append(line)
        continued = ""
    if continued:
        logical_lines.append(continued)
    for raw_line in logical_lines:
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        directives.append((section, key.strip(), value.strip()))
    return directives


@dataclass(frozen=True)
class _SystemdDropInRecord:
    path: Path
    raw: bytes


def _collect_systemd_dropin_directory(
    directory: Path,
    *,
    max_entries: int,
) -> dict[str, _SystemdDropInRecord]:
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise IvdCronServiceDiscoveryError(
            "systemd_dropin_unreadable", directory
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise IvdCronServiceDiscoveryError("systemd_dropin_symlink", directory)
    if not stat.S_ISDIR(metadata.st_mode):
        raise IvdCronServiceDiscoveryError("systemd_dropin_unreadable", directory)

    descriptor = -1
    try:
        descriptor = os.open(
            directory,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        names: list[str] = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > max_entries:
                    raise IvdCronServiceDiscoveryError(
                        "systemd_dropin_entry_limit", directory
                    )
        records: dict[str, _SystemdDropInRecord] = {}
        for name in sorted(names):
            if not name.endswith(".conf"):
                continue
            path = directory / name
            try:
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise OSError("systemd_dropin_unsafe")
                raw = _read_bounded_service_definition(path, dir_fd=descriptor)
            except OSError as exc:
                raise IvdCronServiceDiscoveryError(
                    "systemd_dropin_unreadable", path
                ) from exc
            records[name] = _SystemdDropInRecord(path=path, raw=raw)
        return records
    except IvdCronServiceDiscoveryError:
        raise
    except OSError as exc:
        raise IvdCronServiceDiscoveryError(
            "systemd_dropin_unreadable", directory
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _effective_systemd_dropins(
    unit_name: str,
    scopes: tuple[Path, ...],
    *,
    max_entries: int,
    scope_root: Path,
) -> tuple[_SystemdDropInRecord, ...]:
    directory_names = _systemd_dropin_directory_names(unit_name)
    selected: dict[str, _SystemdDropInRecord] = {}
    allowed_scopes = frozenset(scopes)
    visited: set[Path] = set()
    resolved_scopes: list[Path] = []
    for scope in scopes:
        resolved_scope = _resolve_systemd_scope_alias(
            scope,
            allowed_scopes=allowed_scopes,
            scope_root=scope_root,
        )
        if resolved_scope is None or resolved_scope in visited:
            continue
        visited.add(resolved_scope)
        resolved_scopes.append(resolved_scope)

    # Specific unit/template directories beat prefix and type-wide directories.
    # Scope precedence only breaks ties within the same specificity level.
    for directory_name in directory_names:
        for resolved_scope in resolved_scopes:
            records = _collect_systemd_dropin_directory(
                resolved_scope / directory_name,
                max_entries=max_entries,
            )
            for name, record in records.items():
                selected.setdefault(name, record)
    return tuple(selected[name] for name in sorted(selected))


def _systemd_template_unit_name(unit_name: str) -> str | None:
    stem, separator, unit_type = unit_name.rpartition(".")
    if not separator or "@" not in stem:
        return None
    template_stem, instance = stem.split("@", 1)
    if not template_stem or not instance:
        return None
    return f"{template_stem}@.{unit_type}"


_SYSTEMD_UNIT_NAME_PART = re.compile(
    r"(?:[A-Za-z0-9:_.-]|\\x[0-9A-Fa-f]{2})+"
)


def _valid_systemd_unit_name_part(value: str) -> bool:
    return (
        value not in {".", ".."}
        and _SYSTEMD_UNIT_NAME_PART.fullmatch(value) is not None
    )


def _valid_systemd_unit_stem(value: str, *, template_allowed: bool) -> bool:
    if "@" not in value:
        return _valid_systemd_unit_name_part(value)
    if value.count("@") != 1:
        return False
    prefix, instance = value.split("@", 1)
    return _valid_systemd_unit_name_part(prefix) and (
        _valid_systemd_unit_name_part(instance)
        if instance
        else template_allowed
    )


def _parse_systemd_template_timer_instance(
    unit_name: str,
    source_path: Path,
) -> tuple[str, str]:
    try:
        encoded_size = len(unit_name.encode("utf-8"))
    except UnicodeError as exc:
        raise IvdCronServiceDiscoveryError(
            "systemd_timer_instance_invalid", source_path
        ) from exc
    if (
        encoded_size > MAX_SYSTEMD_UNIT_NAME_BYTES
        or not unit_name.endswith(".timer")
        or unit_name.count("@") != 1
    ):
        raise IvdCronServiceDiscoveryError(
            "systemd_timer_instance_invalid", source_path
        )
    stem = unit_name[: -len(".timer")]
    template_prefix, instance = stem.split("@", 1)
    if not (
        _valid_systemd_unit_name_part(template_prefix)
        and _valid_systemd_unit_name_part(instance)
    ):
        raise IvdCronServiceDiscoveryError(
            "systemd_timer_instance_invalid", source_path
        )
    return unit_name, f"{template_prefix}@.timer"


def _validate_systemd_timer_template_name(
    unit_name: str,
    source_path: Path,
) -> None:
    if not unit_name.endswith("@.timer"):
        raise IvdCronServiceDiscoveryError(
            "systemd_timer_instance_invalid", source_path
        )
    template_prefix = unit_name[: -len("@.timer")]
    if (
        len(unit_name.encode("utf-8")) > MAX_SYSTEMD_UNIT_NAME_BYTES
        or not _valid_systemd_unit_name_part(template_prefix)
    ):
        raise IvdCronServiceDiscoveryError(
            "systemd_timer_instance_invalid", source_path
        )


def _systemd_dropin_directory_names(unit_name: str) -> tuple[str, ...]:
    stem, separator, unit_type = unit_name.rpartition(".")
    if not separator or unit_type not in {"timer", "service"}:
        raise IvdCronServiceDiscoveryError("systemd_unit_name_invalid", unit_name)

    names = [f"{unit_name}.d"]
    template_name = _systemd_template_unit_name(unit_name)
    hierarchy_stem = stem
    if template_name is not None:
        names.append(f"{template_name}.d")
        hierarchy_stem = template_name.rpartition(".")[0]
    if hierarchy_stem.endswith("@"):
        hierarchy_stem = hierarchy_stem[:-1]

    dash_positions = [
        position
        for position, character in enumerate(hierarchy_stem)
        if character == "-"
    ]
    for index in reversed(dash_positions):
        names.append(f"{hierarchy_stem[: index + 1]}.{unit_type}.d")
    names.append(f"{unit_type}.d")

    deduplicated = tuple(dict.fromkeys(names))
    if len(deduplicated) > MAX_SYSTEMD_DROPIN_LEVELS:
        raise IvdCronServiceDiscoveryError("systemd_dropin_level_limit", unit_name)
    return deduplicated


def _effective_systemd_unit_text(
    unit_name: str,
    record: _SystemdUnitRecord,
    scopes: tuple[Path, ...],
    *,
    max_entries: int,
    scope_root: Path,
) -> str:
    parts = [_decode_systemd_record(record)]
    for dropin in _effective_systemd_dropins(
        unit_name,
        scopes,
        max_entries=max_entries,
        scope_root=scope_root,
    ):
        try:
            parts.append(dropin.raw.decode("utf-8"))
        except UnicodeError as exc:
            raise IvdCronServiceDiscoveryError(
                "systemd_dropin_unreadable", dropin.path
            ) from exc
    return "\n".join(parts)


def _register_synthesized_timer(
    candidates: dict[str, _SynthesizedSystemdTimer],
    effective: Mapping[str, _SystemdUnitRecord],
    *,
    unit_name: str,
    template_name: str,
    source_path: Path,
    max_entries: int,
) -> None:
    if unit_name in effective or unit_name in candidates:
        return
    template_record = effective.get(template_name)
    if template_record is None:
        return
    if len(candidates) >= max_entries:
        raise IvdCronServiceDiscoveryError(
            "systemd_timer_instance_limit", source_path
        )
    candidates[unit_name] = _SynthesizedSystemdTimer(
        record=template_record,
        source_path=source_path,
    )


def _validate_systemd_instance_dropin(
    scope: Path,
    name: str,
    *,
    max_entries: int,
) -> tuple[str, str] | None:
    source_path = scope / name
    unit_name = name[: -len(".d")]
    if unit_name.endswith("@.timer"):
        _validate_systemd_timer_template_name(unit_name, source_path)
        return None
    parsed = _parse_systemd_template_timer_instance(unit_name, source_path)
    _collect_systemd_dropin_directory(source_path, max_entries=max_entries)
    return parsed


def _parse_systemd_dependency_directory(
    name: str,
    source_path: Path,
) -> tuple[str, str] | None:
    dependency = next(
        (
            suffix
            for suffix in _SYSTEMD_DEPENDENCY_DIRECTORY_SUFFIXES
            if name.endswith(f".{suffix}")
        ),
        None,
    )
    if dependency is None:
        return None
    source_unit = name[: -len(f".{dependency}")]
    unit_stem, separator, unit_suffix = source_unit.rpartition(".")
    try:
        source_unit_size = len(source_unit.encode("utf-8"))
    except UnicodeError as exc:
        raise IvdCronServiceDiscoveryError(
            "systemd_wants_directory_invalid", source_path
        ) from exc
    if (
        Path(name).name != name
        or source_unit_size > MAX_SYSTEMD_UNIT_NAME_BYTES
        or not separator
        or unit_suffix not in _SYSTEMD_UNIT_SUFFIXES
        or not _valid_systemd_unit_stem(unit_stem, template_allowed=True)
    ):
        raise IvdCronServiceDiscoveryError(
            "systemd_wants_directory_invalid", source_path
        )
    return source_unit, dependency


def _mapped_systemd_dependency_target(
    raw_target: str,
    *,
    dependency_directory: Path,
    scope_root: Path,
) -> Path:
    target = Path(raw_target)
    if target.is_absolute():
        if scope_root != Path("/") and not target.is_relative_to(scope_root):
            target = _map_service_scope(scope_root, target)
    else:
        target = dependency_directory / target
    return Path(os.path.normpath(os.fspath(target)))


def _scan_systemd_dependency_directory(
    scope: Path,
    directory_name: str,
    *,
    effective: Mapping[str, _SystemdUnitRecord],
    records_by_path: Mapping[Path, _SystemdUnitRecord],
    candidates: dict[str, _SynthesizedSystemdTimer],
    max_entries: int,
    remaining_entries: int,
    scope_root: Path,
) -> int:
    directory = scope / directory_name
    try:
        metadata = directory.lstat()
    except OSError as exc:
        raise IvdCronServiceDiscoveryError(
            "systemd_wants_directory_invalid", directory
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise IvdCronServiceDiscoveryError(
            "systemd_wants_directory_invalid", directory
        )

    descriptor = -1
    try:
        descriptor = os.open(
            directory,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        names: list[str] = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > max_entries:
                    raise IvdCronServiceDiscoveryError(
                        "systemd_wants_entry_limit", directory
                    )
        if len(names) > remaining_entries:
            raise IvdCronServiceDiscoveryError(
                "systemd_wants_entry_limit", directory
            )

        allowed_unit_scopes = frozenset(
            record.path.parent for record in records_by_path.values()
        )
        normalized_root = Path(os.path.abspath(scope_root))
        for name in sorted(names):
            if not name.endswith(".timer") or "@" not in name:
                continue
            source_path = directory / name
            unit_name, template_name = _parse_systemd_template_timer_instance(
                name,
                source_path,
            )
            try:
                entry_stat = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISLNK(entry_stat.st_mode):
                    raise OSError("systemd_wants_entry_not_symlink")
                raw_target = os.readlink(name, dir_fd=descriptor)
            except OSError as exc:
                raise IvdCronServiceDiscoveryError(
                    "systemd_wants_entry_invalid", source_path
                ) from exc

            normalized_source = Path(os.path.abspath(source_path))
            if not normalized_source.is_relative_to(normalized_root):
                raise IvdCronServiceDiscoveryError(
                    "systemd_wants_entry_invalid", source_path
                )
            target_path = _mapped_systemd_dependency_target(
                raw_target,
                dependency_directory=directory,
                scope_root=scope_root,
            )
            if (
                scope_root != Path("/")
                and not target_path.is_relative_to(scope_root)
            ):
                raise IvdCronServiceDiscoveryError(
                    "systemd_wants_target_invalid", source_path
                )
            target_record = records_by_path.get(target_path)
            expected_names = {unit_name, template_name}
            if target_record is None or target_record.name not in expected_names:
                raise IvdCronServiceDiscoveryError(
                    "systemd_wants_target_invalid", source_path
                )
            resolved_target = _resolve_systemd_unit_alias(
                target_record,
                records_by_path=records_by_path,
                allowed_scopes=allowed_unit_scopes,
                scope_root=scope_root,
            )
            if resolved_target is None or resolved_target.name not in expected_names:
                raise IvdCronServiceDiscoveryError(
                    "systemd_wants_target_invalid", source_path
                )
            _register_synthesized_timer(
                candidates,
                effective,
                unit_name=unit_name,
                template_name=template_name,
                source_path=source_path,
                max_entries=max_entries,
            )
        return len(names)
    except IvdCronServiceDiscoveryError:
        raise
    except OSError as exc:
        raise IvdCronServiceDiscoveryError(
            "systemd_wants_directory_invalid", directory
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _discover_systemd_template_timer_instances(
    scopes: tuple[Path, ...],
    *,
    effective: Mapping[str, _SystemdUnitRecord],
    records_by_path: Mapping[Path, _SystemdUnitRecord],
    max_entries: int,
    scope_root: Path,
) -> dict[str, _SynthesizedSystemdTimer]:
    candidates: dict[str, _SynthesizedSystemdTimer] = {}
    dependency_entries = 0
    allowed_scopes = frozenset(scopes)
    visited: set[Path] = set()
    for scope in scopes:
        resolved_scope = _resolve_systemd_scope_alias(
            scope,
            allowed_scopes=allowed_scopes,
            scope_root=scope_root,
        )
        if resolved_scope is None or resolved_scope in visited:
            continue
        visited.add(resolved_scope)

        descriptor = -1
        try:
            descriptor = os.open(
                resolved_scope,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            names: list[str] = []
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    names.append(entry.name)
                    if len(names) > max_entries:
                        raise IvdCronServiceDiscoveryError(
                            "service_scope_entry_limit", resolved_scope
                        )
            for name in sorted(names):
                source_path = resolved_scope / name
                if name.endswith(".timer.d") and "@" in name:
                    parsed = _validate_systemd_instance_dropin(
                        resolved_scope,
                        name,
                        max_entries=max_entries,
                    )
                    if parsed is not None:
                        unit_name, template_name = parsed
                        _register_synthesized_timer(
                            candidates,
                            effective,
                            unit_name=unit_name,
                            template_name=template_name,
                            source_path=source_path,
                            max_entries=max_entries,
                        )
                elif _parse_systemd_dependency_directory(name, source_path):
                    scanned_entries = _scan_systemd_dependency_directory(
                        resolved_scope,
                        name,
                        effective=effective,
                        records_by_path=records_by_path,
                        candidates=candidates,
                        max_entries=max_entries,
                        remaining_entries=max_entries - dependency_entries,
                        scope_root=scope_root,
                    )
                    dependency_entries += scanned_entries
        except IvdCronServiceDiscoveryError:
            raise
        except OSError as exc:
            raise IvdCronServiceDiscoveryError(
                "service_scope_unreadable", resolved_scope
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    return candidates


def _systemd_identity(name: str, text: str) -> bool:
    identity_values = [name]
    descriptions: list[str] = []
    commands: dict[str, list[str]] = {}
    for section, key, value in _systemd_directives(text):
        if section == "Unit" and key == "Description":
            descriptions = [value] if value else []
        if section == "Service" and key.startswith("Exec"):
            values = commands.setdefault(key, [])
            if value:
                values.append(value)
            else:
                values.clear()
    effective_commands = [value for values in commands.values() for value in values]
    identity_values.extend(descriptions)
    identity_values.extend(effective_commands)
    if effective_commands and all(
        "gateway run" in command.lower() or "gateway serve" in command.lower()
        for command in effective_commands
    ):
        return False
    return _has_explicit_ivd_identity(" ".join(identity_values))


def _systemd_timer_metadata(name: str, text: str) -> tuple[bool, str | None, bool]:
    schedules = {key: [] for key in _SYSTEMD_PERIODIC_KEYS}
    unit_name: str | None = None
    for section, key, value in _systemd_directives(text):
        if section != "Timer":
            continue
        if key in _SYSTEMD_PERIODIC_KEYS:
            if value:
                schedules[key].append(value)
            else:
                schedules[key].clear()
        elif key == "Unit":
            unit_name = value or None
    return any(schedules.values()), unit_name, _systemd_identity(name, text)


_SYSTEMD_SERVICE_NAME = re.compile(r"[A-Za-z0-9_.:@-]+\.service")


def _linked_systemd_service_name(timer_name: str, configured_unit: str | None, path: Path) -> str | None:
    if configured_unit is None:
        return f"{timer_name[:-len('.timer')]}.service"
    if (
        "/" in configured_unit
        or "\\" in configured_unit
        or ".." in configured_unit
        or any(character.isspace() for character in configured_unit)
    ):
        raise IvdCronServiceDiscoveryError("systemd_timer_unit_invalid", path)
    if not configured_unit.endswith(".service"):
        return None
    if _SYSTEMD_SERVICE_NAME.fullmatch(configured_unit) is None:
        raise IvdCronServiceDiscoveryError("systemd_timer_unit_invalid", path)
    return configured_unit


def _decode_systemd_record(record: _SystemdUnitRecord) -> str:
    if record.raw is None:
        raise IvdCronServiceDiscoveryError("service_definition_unreadable", record.path)
    try:
        return record.raw.decode("utf-8")
    except UnicodeError as exc:
        raise IvdCronServiceDiscoveryError("service_definition_unreadable", record.path) from exc


def _assert_systemd_timer_service_contract(
    groups: tuple[tuple[Path, ...], ...],
    *,
    max_entries: int,
    scope_root: Path,
) -> None:
    for scopes in groups:
        effective, records_by_path = _effective_systemd_units(
            scopes,
            max_entries=max_entries,
            scope_root=scope_root,
        )
        synthesized = _discover_systemd_template_timer_instances(
            scopes,
            effective=effective,
            records_by_path=records_by_path,
            max_entries=max_entries,
            scope_root=scope_root,
        )
        allowed_scopes = frozenset(scopes)
        timer_names = {
            name for name in effective if name.endswith(".timer")
        } | synthesized.keys()
        for timer_name in sorted(timer_names):
            if timer_name in effective:
                timer_record = effective[timer_name]
                timer_source_path = timer_record.path
            else:
                candidate = synthesized[timer_name]
                timer_record = candidate.record
                timer_source_path = candidate.source_path
            if timer_record.alias_target is not None and _has_explicit_ivd_identity(timer_name):
                raise IndependentIvdCronServiceError(timer_source_path)
            resolved_timer = _resolve_systemd_unit_alias(
                timer_record,
                records_by_path=records_by_path,
                allowed_scopes=allowed_scopes,
                scope_root=scope_root,
            )
            if resolved_timer is None:
                continue
            timer_text = _effective_systemd_unit_text(
                timer_name,
                resolved_timer,
                scopes,
                max_entries=max_entries,
                scope_root=scope_root,
            )
            periodic, configured_unit, timer_is_ivd = _systemd_timer_metadata(
                timer_name,
                timer_text,
            )
            if not periodic:
                continue
            if timer_is_ivd:
                raise IndependentIvdCronServiceError(timer_source_path)
            service_name = _linked_systemd_service_name(
                timer_name,
                configured_unit,
                timer_source_path,
            )
            if service_name is None:
                continue
            service_record = effective.get(service_name)
            if service_record is None:
                template_service_name = _systemd_template_unit_name(service_name)
                if template_service_name is not None:
                    service_record = effective.get(template_service_name)
            if service_record is None:
                continue
            resolved_service = _resolve_systemd_unit_alias(
                service_record,
                records_by_path=records_by_path,
                allowed_scopes=allowed_scopes,
                scope_root=scope_root,
            )
            if resolved_service is None:
                continue
            service_text = _effective_systemd_unit_text(
                service_name,
                resolved_service,
                scopes,
                max_entries=max_entries,
                scope_root=scope_root,
            )
            if _systemd_identity(service_name, service_text):
                raise IndependentIvdCronServiceError(timer_source_path)


def _scan_service_scope(
    *,
    kind: str,
    scope: Path,
    max_entries: int,
    definition_reader: Callable[[Path], bytes] | None = None,
) -> None:
    try:
        metadata = scope.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise IvdCronServiceDiscoveryError("service_scope_unreadable", scope) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise IvdCronServiceDiscoveryError("service_scope_symlink", scope)
    if not stat.S_ISDIR(metadata.st_mode):
        raise IvdCronServiceDiscoveryError("service_scope_unreadable", scope)

    descriptor = -1
    try:
        descriptor = os.open(
            scope,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("service_scope_not_directory")
        with os.scandir(descriptor) as entries:
            names: list[str] = []
            for entry in entries:
                names.append(entry.name)
                if len(names) > max_entries:
                    raise IvdCronServiceDiscoveryError("service_scope_entry_limit", scope)
        suffixes = (".service", ".timer") if kind == "systemd" else (".plist",)
        for name in sorted(names):
            if not name.endswith(suffixes):
                continue
            path = scope / name
            try:
                raw = (
                    definition_reader(path)
                    if definition_reader is not None
                    else _read_bounded_service_definition(path, dir_fd=descriptor)
                )
                if not isinstance(raw, bytes):
                    raise OSError("service_definition_reader_invalid")
            except OSError as exc:
                if kind == "launchd":
                    raise IvdCronServiceDiscoveryError(
                        "launchd_plist_unreadable", path
                    ) from exc
                if _suspicious_ivd_cron_name(name):
                    raise IndependentIvdCronServiceError(path)
                continue
            if _definition_is_independent_ivd_cron(kind, name, raw):
                raise IndependentIvdCronServiceError(path)
    except (IndependentIvdCronServiceError, IvdCronServiceDiscoveryError):
        raise
    except OSError as exc:
        raise IvdCronServiceDiscoveryError("service_scope_unreadable", scope) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def assert_embedded_ivd_cron_service_contract(
    *,
    kind: str,
    service_dir: Path,
    target_path: Path,
    candidate_definition: str | None = None,
    scope_root: Path = Path("/"),
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    uid: int | None = None,
    max_entries_per_scope: int = 512,
    max_scopes: int = MAX_SYSTEMD_SERVICE_SCOPES,
    systemd_analyze_path: Path | None | object = _AUTO_SYSTEMD_ANALYZE,
    systemd_analyze_runner: Callable[..., bytes] | None = None,
    definition_reader: Callable[[Path], bytes] | None = None,
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
    if kind == "systemd":
        if systemd_analyze_path is _AUTO_SYSTEMD_ANALYZE:
            systemd_analyze_path = find_trusted_systemd_analyze()
        groups = _effective_systemd_service_scope_groups(
            target_path=target_path,
            scope_root=Path(scope_root),
            home=Path.home() if home is None else Path(home),
            environ=os.environ if environ is None else environ,
            uid=uid,
            systemd_analyze_path=(
                None if systemd_analyze_path is None else Path(systemd_analyze_path)
            ),
            systemd_analyze_runner=systemd_analyze_runner,
        )
        scopes = _deduplicate_paths([scope for group in groups for scope in group])
        scope_limit = min(max_scopes, MAX_SYSTEMD_SERVICE_SCOPES)
        if scope_limit < 1 or len(scopes) > scope_limit:
            raise IvdCronServiceDiscoveryError("service_scope_limit", target_path)
        grouped_scopes = {scope for group in groups for scope in group}
        if service_dir not in grouped_scopes:
            groups = ((service_dir,), *groups)
        _assert_systemd_timer_service_contract(
            groups,
            max_entries=max_entries_per_scope,
            scope_root=Path(scope_root),
        )
    else:
        scopes = discover_ivd_cron_service_scopes(
            kind,
            target_path=target_path,
            scope_root=scope_root,
            home=home,
            environ=environ,
            uid=uid,
            max_scopes=max_scopes,
            systemd_analyze_path=systemd_analyze_path,
            systemd_analyze_runner=systemd_analyze_runner,
        )
        if service_dir not in scopes:
            scopes = _deduplicate_paths([service_dir, *scopes])
        for scope in scopes:
            _scan_service_scope(
                kind=kind,
                scope=scope,
                max_entries=max_entries_per_scope,
                definition_reader=definition_reader,
            )

    if candidate_definition is not None and _definition_is_independent_ivd_cron(
        kind, Path(target_path).name, candidate_definition
    ):
        raise IndependentIvdCronServiceError(f"candidate:{target_path}")
    return contract
