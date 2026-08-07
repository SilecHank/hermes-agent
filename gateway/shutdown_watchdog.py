"""Out-of-loop shutdown and event-loop liveness backstops (#66892, #69089).

When the asyncio loop freezes mid-drain, every asyncio-based recovery path is
structurally unable to fire: the drain deadline, status rewrites, and forensics
all need the same loop that is stuck. launchd/systemd KeepAlive only restarts a
*dead* process, so a wedged-but-alive gateway sits as a zombie until manual
SIGKILL.

This module provides:

1. A plain OS-thread shutdown watchdog armed at ``stop()``. If shutdown has not
   completed within ``restart_drain_timeout + grace``, it dumps all-thread
   stacks via ``faulthandler`` plus a metadata snapshot, then ``os._exit`` so
   the service manager can revive the process.
2. An event-loop heartbeat file at ``<HERMES_HOME>/state/gateway.heartbeat`` so
   external supervision can distinguish "process alive" from "loop frozen"
   (``gateway_state.json`` alone can't — it only rewrites on transitions/turns).
3. A lifetime thread watchdog that can still diagnose and hard-exit when the
   event loop is too frozen to run its own heartbeat or timeout callbacks.
4. A self-rescheduling floor timer that keeps the loop selector's timeout
   finite, giving existing async recovery tasks a chance to resume.
"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import logging
import os
import stat
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE
from gateway.status import get_process_start_time
from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

# Extra leash beyond ``agent.restart_drain_timeout`` so a slow-but-progressing
# drain is not cut short. Matches the issue #66892 suggested hardening.
DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S = 60.0
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0
DEFAULT_LOOP_FLOOR_TIMER_INTERVAL_S = 5.0
DEFAULT_LOOP_WATCHDOG_INTERVAL_S = 30.0
DEFAULT_LOOP_WATCHDOG_TIMEOUT_S = 10.0
DEFAULT_LOOP_WATCHDOG_MAX_STRIKES = 3
_HEARTBEAT_RELATIVE = ("state", "gateway.heartbeat")
_WATCHDOG_DUMP_RELATIVE = ("logs", "gateway-shutdown-watchdog.log")
_RUNTIME_STATUS_FILENAME = "gateway_state.json"
_MAX_RUNTIME_STATUS_BYTES = 1024 * 1024
_PLATFORM_STATES = frozenset(
    {"connected", "connecting", "disconnected", "disabled", "fatal", "paused", "retrying"}
)
_HEARTBEAT_PROTECTED_FIELDS = frozenset(
    {
        "pid",
        "process_start_time",
        "app_start_time",
        "start_time",
        "boot_id",
        "updated_at",
        "monotonic",
        "platforms",
        "platforms_observed_at",
        "platforms_observation_valid",
        "platforms_observation_reason",
    }
)


class _LoopFloorTimerHandle:
    """Cancelable owner for the currently scheduled selector floor timer."""

    def __init__(self, loop: asyncio.AbstractEventLoop, interval: float):
        self._loop = loop
        self._interval = interval
        self._cancelled = False
        self._timer: Optional[asyncio.TimerHandle] = None
        self._schedule()

    def _schedule(self) -> None:
        self._timer = self._loop.call_later(self._interval, self._tick)

    def _tick(self) -> None:
        if not self._cancelled:
            self._schedule()

    def cancel(self) -> None:
        self._cancelled = True
        if self._timer is not None:
            self._timer.cancel()


class _LoopLivenessWatchdogHandle:
    """Small lifecycle handle for the daemon liveness thread."""

    def __init__(self, stop_event: threading.Event, thread: threading.Thread):
        self._stop_event = stop_event
        self._thread = thread

    def stop(self) -> None:
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()


def _arm_loop_floor_timer(
    loop: asyncio.AbstractEventLoop,
    interval: float = DEFAULT_LOOP_FLOOR_TIMER_INTERVAL_S,
) -> _LoopFloorTimerHandle:
    """Keep at least one timer pending so selector waits remain bounded."""
    try:
        resolved_interval = float(interval)
        if resolved_interval <= 0:
            raise ValueError
    except (TypeError, ValueError):
        resolved_interval = DEFAULT_LOOP_FLOOR_TIMER_INTERVAL_S
    return _LoopFloorTimerHandle(loop, resolved_interval)


def start_loop_liveness_watchdog(
    loop: asyncio.AbstractEventLoop,
    *,
    probe_interval: float = DEFAULT_LOOP_WATCHDOG_INTERVAL_S,
    probe_timeout: float = DEFAULT_LOOP_WATCHDOG_TIMEOUT_S,
    max_strikes: int = DEFAULT_LOOP_WATCHDOG_MAX_STRIKES,
    exit_code: int = GATEWAY_SERVICE_RESTART_EXIT_CODE,
) -> Optional[_LoopLivenessWatchdogHandle]:
    """Start an out-of-loop watchdog that hard-exits after missed probes.

    The guard is on by default; operators opt out with
    ``gateway.loop_watchdog: false`` in config.yaml (enforced by the caller,
    ``GatewayRunner._start_loop_liveness_guards`` — this module stays
    config-agnostic so bare-loop tests can drive it directly).
    """
    interval = probe_interval
    timeout = probe_timeout
    strikes_limit = max_strikes
    stop_event = threading.Event()

    def _wait_for_probe(probe_event: threading.Event) -> Optional[bool]:
        deadline = time.monotonic() + timeout
        while True:
            if stop_event.is_set():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return probe_event.is_set()
            if probe_event.wait(timeout=min(remaining, 0.05)):
                return True

    def _watchdog() -> None:
        strikes = 0
        while not stop_event.wait(timeout=interval):
            probe_event = threading.Event()
            try:
                loop.call_soon_threadsafe(probe_event.set)
            except RuntimeError:
                # A normally closed loop cannot be probed and no longer needs
                # a process-liveness backstop.
                return
            except Exception:
                logger.debug(
                    "Failed to schedule gateway loop liveness probe", exc_info=True
                )
                return

            responded = _wait_for_probe(probe_event)
            if responded is None:
                return
            if responded:
                strikes = 0
                continue

            if stop_event.is_set():
                return
            strikes += 1
            if strikes < strikes_limit:
                continue

            if stop_event.is_set():
                return
            try:
                logger.critical(
                    "Gateway event loop missed %d consecutive liveness probes; "
                    "dumping all thread stacks and exiting with code %d so the "
                    "service supervisor can restart it.",
                    strikes,
                    exit_code,
                )
            except Exception:
                pass
            try:
                faulthandler.dump_traceback(all_threads=True)
            except Exception:
                logger.debug("Loop liveness faulthandler dump failed", exc_info=True)
            if stop_event.is_set():
                return
            os._exit(exit_code)
            return

    thread = threading.Thread(
        target=_watchdog,
        daemon=True,
        name="gateway-loop-liveness-watchdog",
    )
    try:
        thread.start()
    except Exception:
        logger.debug("Failed to start gateway loop liveness watchdog", exc_info=True)
        return None
    return _LoopLivenessWatchdogHandle(stop_event, thread)


def _process_hermes_home() -> Path:
    """HERMES_HOME for process-level identity files (ignore profile overrides)."""
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        return Path(val)
    return get_hermes_home()


def get_loop_heartbeat_path(home: Optional[Path] = None) -> Path:
    """Return ``<HERMES_HOME>/state/gateway.heartbeat``."""
    base = home if home is not None else _process_hermes_home()
    return base.joinpath(*_HEARTBEAT_RELATIVE)


def get_shutdown_watchdog_dump_path(home: Optional[Path] = None) -> Path:
    """Return the faulthandler / metadata dump path for a fired watchdog."""
    base = home if home is not None else _process_hermes_home()
    return base.joinpath(*_WATCHDOG_DUMP_RELATIVE)


def _read_linux_boot_id(
    path: str = "/proc/sys/kernel/random/boot_id",
) -> Optional[str]:
    """Read the Linux boot identifier without following a substituted link."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, 129)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        value = raw.decode("ascii").strip()
        parsed = uuid.UUID(value)
    except (UnicodeDecodeError, ValueError):
        return None
    return str(parsed) if value.lower() == str(parsed) else None


def _runtime_status_path(home: Optional[Path]) -> Path:
    base = home if home is not None else _process_hermes_home()
    return base / _RUNTIME_STATUS_FILENAME


def _read_runtime_status_for_heartbeat(
    home: Optional[Path],
) -> tuple[Optional[Dict[str, Any]], str]:
    """Read one bounded regular runtime snapshot without following symlinks."""
    path = _runtime_status_path(home)
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            return None, "runtime_status_symlink"
    except FileNotFoundError:
        return None, "runtime_status_missing"
    except OSError:
        return None, "runtime_status_invalid"

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, "runtime_status_missing"
    except OSError:
        try:
            if path.is_symlink():
                return None, "runtime_status_symlink"
        except OSError:
            pass
        return None, "runtime_status_invalid"
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            return None, "runtime_status_invalid"
        raw = os.read(fd, _MAX_RUNTIME_STATUS_BYTES + 1)
    except OSError:
        return None, "runtime_status_invalid"
    finally:
        os.close(fd)
    if len(raw) > _MAX_RUNTIME_STATUS_BYTES:
        return None, "runtime_status_invalid"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "runtime_status_invalid"
    if not isinstance(payload, dict):
        return None, "runtime_status_invalid"
    return payload, "runtime_status_ready"


def _platform_observation(
    *,
    pid: int,
    process_start_time: Optional[int],
    home: Optional[Path],
    observed_at: str,
) -> Dict[str, Any]:
    runtime, reason = _read_runtime_status_for_heartbeat(home)
    if runtime is None:
        return {
            "platforms_observation_valid": False,
            "platforms_observation_reason": reason,
        }

    runtime_pid = runtime.get("pid")
    runtime_start = runtime.get("start_time")
    if (
        type(runtime_pid) is not int
        or runtime_pid != pid
        or isinstance(runtime_start, bool)
        or runtime_start != process_start_time
    ):
        return {
            "platforms_observation_valid": False,
            "platforms_observation_reason": "runtime_process_mismatch",
        }

    runtime_platforms = runtime.get("platforms")
    if not isinstance(runtime_platforms, dict):
        return {
            "platforms_observation_valid": False,
            "platforms_observation_reason": "runtime_platforms_invalid",
        }

    platforms: Dict[str, Dict[str, str]] = {}
    for name, details in runtime_platforms.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(details, dict)
            or not isinstance(details.get("state"), str)
            or details["state"] not in _PLATFORM_STATES
        ):
            return {
                "platforms_observation_valid": False,
                "platforms_observation_reason": "runtime_platforms_invalid",
            }
        platforms[name] = {"state": details["state"]}

    return {
        "platforms_observation_valid": True,
        "platforms_observed_at": observed_at,
        "platforms": platforms,
    }


def write_loop_heartbeat(
    *,
    pid: Optional[int] = None,
    start_time: Optional[float] = None,
    home: Optional[Path] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """Atomically rewrite the loop-liveness heartbeat file.

    ``start_time`` remains the application-start epoch accepted by existing
    callers and is persisted as ``app_start_time`` plus the deprecated
    ``start_time`` compatibility alias. ``process_start_time`` is independently
    sampled from the kernel for PID-reuse protection.

    Platform health is copied from the runtime status only when that file
    identifies the same live process. Best-effort — never raises.
    """
    path = get_loop_heartbeat_path(home)
    resolved_pid = int(pid if pid is not None else os.getpid())
    process_start_time = get_process_start_time(resolved_pid)
    observed_at = datetime.now(timezone.utc).isoformat()
    payload: Dict[str, Any] = {
        "pid": resolved_pid,
        "process_start_time": process_start_time,
        "updated_at": observed_at,
        "monotonic": time.monotonic(),
    }
    if start_time is not None:
        app_start_time = float(start_time)
        payload["app_start_time"] = app_start_time
        payload["start_time"] = app_start_time
    boot_id = _read_linux_boot_id()
    if boot_id is not None:
        payload["boot_id"] = boot_id
    if extra:
        payload.update(
            {key: value for key, value in extra.items() if key not in _HEARTBEAT_PROTECTED_FIELDS}
        )
    payload.update(
        _platform_observation(
            pid=resolved_pid,
            process_start_time=process_start_time,
            home=home,
            observed_at=observed_at,
        )
    )
    try:
        atomic_json_write(path, payload, indent=None)
    except Exception:
        logger.debug("Failed to write gateway loop heartbeat", exc_info=True)
    return path


def resolve_shutdown_watchdog_delay(
    drain_timeout: float,
    *,
    grace_s: float = DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S,
) -> float:
    """Return the wall-clock leash for the shutdown watchdog thread."""
    try:
        drain = max(float(drain_timeout), 0.0)
    except (TypeError, ValueError):
        drain = 0.0
    try:
        grace = max(float(grace_s), 0.0)
    except (TypeError, ValueError):
        grace = DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    return drain + grace


def _write_watchdog_dump(
    dump_path: Path,
    *,
    delay_s: float,
    snapshot: Optional[Dict[str, Any]],
) -> None:
    """Best-effort faulthandler + metadata dump before hard-exit."""
    try:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    header = {
        "event": "shutdown_watchdog_fired",
        "pid": os.getpid(),
        "delay_s": delay_s,
        "fired_at": datetime.now(timezone.utc).isoformat(),
        "snapshot": snapshot or {},
    }
    try:
        with open(dump_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(header, default=str) + "\n")
            fh.write("--- faulthandler dump (all threads) ---\n")
            fh.flush()
            try:
                faulthandler.dump_traceback(file=fh, all_threads=True)
            except Exception:
                fh.write("(faulthandler.dump_traceback failed)\n")
            fh.write("--- end dump ---\n")
            fh.flush()
    except Exception:
        pass

    # Also dump to stderr so journald/launchd capture it even if the file
    # write failed (wedged disk was one of the #66892 hypotheses).
    try:
        sys.stderr.write(
            f"Gateway shutdown watchdog fired after {delay_s:.0f}s "
            f"(pid={os.getpid()}); dumping all thread stacks.\n"
        )
        sys.stderr.flush()
        faulthandler.dump_traceback(all_threads=True)
    except Exception:
        pass


def arm_shutdown_watchdog(
    delay_s: float,
    *,
    done_event: Optional[threading.Event] = None,
    snapshot_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    exit_code: int = 1,
    dump_path: Optional[Path] = None,
    name: str = "gateway-shutdown-watchdog",
) -> threading.Event:
    """Arm a daemon-thread hard-exit backstop for a wedged shutdown path.

    If ``done_event`` is set before ``delay_s`` elapses, the thread exits
    quietly (normal / progressing shutdown completed). Otherwise it dumps
    diagnostics and calls ``os._exit(exit_code)``.

    Never raises. Returns the ``done_event`` (creating one when omitted) so
    the caller can disarm on successful completion.
    """
    done = done_event if done_event is not None else threading.Event()
    try:
        delay = max(float(delay_s), 0.0)
    except (TypeError, ValueError):
        delay = DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S

    if delay <= 0:
        return done

    def _watchdog() -> None:
        # Wait with interruptible chunks so a late disarm doesn't need the
        # full remaining sleep to observe done_event.
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if done.wait(timeout=min(remaining, 1.0)):
                return
        if done.is_set():
            return

        snapshot: Optional[Dict[str, Any]] = None
        if snapshot_fn is not None:
            try:
                snapshot = snapshot_fn()
            except Exception as exc:
                snapshot = {"snapshot_error": repr(exc)}

        target = dump_path if dump_path is not None else get_shutdown_watchdog_dump_path()
        _write_watchdog_dump(target, delay_s=delay, snapshot=snapshot)

        try:
            logger.critical(
                "Shutdown watchdog fired after %.0fs — forcing process exit "
                "(asyncio drain path appears wedged; see %s)",
                delay,
                target,
            )
        except Exception:
            pass

        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        # Mirror _exit_after_graceful_shutdown: release PID file + runtime
        # lock BEFORE the log drain (locks must never be stranded), then
        # drain the async log queue so the logger.critical above actually
        # reaches the file before os._exit bypasses atexit. (#66892)
        try:
            from gateway.status import remove_pid_file, release_gateway_runtime_lock
            remove_pid_file()
            release_gateway_runtime_lock()
        except Exception:
            pass
        try:
            from hermes_logging import drain_log_queue
            drain_log_queue(timeout=1.0)
        except Exception:
            pass
        os._exit(exit_code)

    try:
        threading.Thread(target=_watchdog, daemon=True, name=name).start()
    except Exception:
        logger.debug("Failed to arm shutdown watchdog", exc_info=True)
    return done


async def loop_heartbeat_forever(
    *,
    interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    start_time: Optional[float] = None,
    home: Optional[Path] = None,
    should_continue: Optional[Callable[[], bool]] = None,
) -> None:
    """Rewrite the loop heartbeat file on a cadence until cancelled / gated off.

    Runs as an asyncio task on the gateway loop — if the loop freezes, this
    task stops and the file mtime/updated_at goes stale for external monitors.
    """
    try:
        interval = max(float(interval_s), 1.0)
    except (TypeError, ValueError):
        interval = DEFAULT_HEARTBEAT_INTERVAL_S

    # Immediate first write so monitors see a fresh file as soon as the
    # gateway is running, not after the first interval.
    write_loop_heartbeat(start_time=start_time, home=home)
    while True:
        if should_continue is not None and not should_continue():
            return
        await asyncio.sleep(interval)
        if should_continue is not None and not should_continue():
            return
        write_loop_heartbeat(start_time=start_time, home=home)
