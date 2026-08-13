"""Sanitized per-turn telemetry for the IVD answer plane."""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, TypeVar


REDACTED_QUESTION = "[content redacted]"
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_IDENTITY_RE = re.compile(r"(?<!\d)(?:\d{15}|\d{17}[0-9Xx])(?!\d)")
_NAME_RE = re.compile(r"((?:患者)?姓名\s*[:：]?\s*)[\u4e00-\u9fff·]{2,8}")
_SAMPLE_RE = re.compile(
    r"((?:样本号?|条码|sample\s*id|barcode)\s*[:：]?\s*)"
    r"[A-Za-z0-9][A-Za-z0-9._/-]{3,}",
    re.IGNORECASE,
)
_MISS_STOP_REASONS = {"no_gain", "profile_limit", "hard_limit"}
_PARTIAL_STOP_REASONS = {"duplicate", "duplicate_intent", "no_gain", "profile_limit", "hard_limit"}
_METADATA_ENUM_KEY_RE = re.compile(
    r"^(?:comparison_status|(?:served_|shadow_)?(?:route_id|route_version|"
    r"validation_status|retrieval_outcome|retrieval_profile|stop_reason))$"
)
_METADATA_BOOL_KEY_RE = re.compile(
    r"^(?:match|exact_match|(?:is|has)_[a-z0-9_]+|[a-z0-9_]+_changed)$"
)
_METADATA_NUMBER_KEY_RE = re.compile(
    r"^[a-z][a-z0-9_]*(?:_count|_seconds|_milliseconds|_ms|_score|_ratio|_delta)$"
)
_METADATA_ENUM_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_METADATA_ITEMS_LIMIT = 16

_AnswerT = TypeVar("_AnswerT")


@dataclass(frozen=True)
class ShadowReplayStats:
    submitted: int
    completed: int
    failed: int
    cancelled: int
    rejected: int
    recording_dropped: int


def default_runtime_event_path() -> Path:
    """Return the profile-aware mutable telemetry path outside every Release."""
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "ivd-live-data/telemetry/runtime-events.jsonl"


def sanitize_question_preview(question_text: str, *, limit: int = 120) -> str:
    """Return a bounded local replay preview or a fail-closed marker."""
    if not str(question_text or "").strip():
        return ""
    try:
        from agent.redact import redact_sensitive_text

        preview = redact_sensitive_text(str(question_text), force=True)
        preview = _EMAIL_RE.sub("<redacted:email>", preview)
        preview = _PHONE_RE.sub("<redacted:phone>", preview)
        preview = _IDENTITY_RE.sub("<redacted:id>", preview)
        preview = _NAME_RE.sub(r"\1<redacted:name>", preview)
        preview = _SAMPLE_RE.sub(r"\1<redacted:sample>", preview)
        preview = re.sub(r"\s+", " ", preview).strip()
        return preview[: max(0, int(limit))]
    except Exception:
        return REDACTED_QUESTION


def question_fingerprint(preview: str) -> str:
    if not preview or preview == REDACTED_QUESTION:
        return ""
    normalized = re.sub(r"\s+", "", preview).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def classify_retrieval_outcome(snapshot: dict[str, object]) -> str:
    profile = str(snapshot.get("profile") or "inactive")
    stop_reason = str(snapshot.get("stop_reason") or "")
    formal_sources = max(0, int(snapshot.get("formal_source_count") or 0))
    if profile == "direct":
        return "not_needed"
    if formal_sources:
        if profile in {"evidence_supplement", "complex_diagnosis"} and stop_reason in _PARTIAL_STOP_REASONS:
            return "partial"
        return "hit"
    if stop_reason in _MISS_STOP_REASONS:
        return "miss"
    return "partial"


def build_runtime_event(
    *,
    platform: str,
    session_key: str,
    product_scope: str,
    route_id: str,
    route_version: str,
    fast_path: bool,
    elapsed_seconds: float,
    api_calls: int,
    tool_names: Iterable[str],
    source_paths: Iterable[str],
    validation_status: str,
    product_variant: str = "",
    answer_text: str = "",
    question_text: str = "",
    retrieval_snapshot: dict[str, object] | None = None,
    preflight_decision: str = "",
    preflight_action: str = "",
    preflight_issues: Iterable[str] = (),
) -> dict[str, object]:
    del answer_text
    tools = [str(name) for name in tool_names if str(name)]
    sources = [str(path) for path in source_paths if str(path)]
    retrieval = retrieval_snapshot or {}
    retrieval_stages = [
        str(stage)[:64]
        for stage in (retrieval.get("stages") or [])
        if str(stage)
    ][:4]
    gate_issues = [str(item)[:64] for item in preflight_issues if str(item)][:8]
    preview = sanitize_question_preview(question_text)
    event = {
        "schema_version": 2,
        "event_type": "ivd_answer_turn",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": str(platform),
        "session_hash": hashlib.sha256(str(session_key).encode("utf-8")).hexdigest()[:16],
        "product_scope": str(product_scope or ""),
        "product_variant": str(product_variant or ""),
        "route_id": str(route_id or "standard"),
        "route_version": str(route_version or ""),
        "fast_path": bool(fast_path),
        "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 3),
        "api_calls": max(0, int(api_calls)),
        "tool_names": tools,
        "tool_count": len(tools),
        "source_paths": sources,
        "validation_status": str(validation_status or "unknown"),
        "question_preview": preview,
        "question_fingerprint": question_fingerprint(preview),
        "retrieval_outcome": classify_retrieval_outcome(retrieval),
        "retrieval_profile": str(retrieval.get("profile") or "inactive")[:64],
        "retrieval_stages": retrieval_stages,
        "retrieval_searches": max(0, int(retrieval.get("searches") or 0)),
        "retrieval_signature_count": max(
            0, int(retrieval.get("signature_count") or 0)
        ),
        "retrieval_formal_source_count": max(
            0, int(retrieval.get("formal_source_count") or 0)
        ),
        "retrieval_no_gain_streak": max(
            0, int(retrieval.get("no_gain_streak") or 0)
        ),
        "retrieval_stop_reason": str(retrieval.get("stop_reason") or "")[:64],
    }
    if preflight_decision or preflight_action or gate_issues:
        event["pre_answer_budget_gate"] = {
            "decision": str(preflight_decision or "unknown")[:32],
            "pipeline_action": str(preflight_action or "unknown")[:64],
            "issues": gate_issues,
        }
    return event


def append_runtime_event(path: str | Path, event: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    payload = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, payload)
    finally:
        os.close(fd)


def sanitize_shadow_comparison_metadata(
    metadata: Mapping[str, object] | None,
) -> dict[str, object]:
    """Return a fail-closed allowlist of low-risk comparison scalars."""
    sanitized: dict[str, object] = {}
    for raw_key, value in list((metadata or {}).items())[:_METADATA_ITEMS_LIMIT]:
        key = str(raw_key)[:64]
        if _METADATA_ENUM_KEY_RE.fullmatch(key) and isinstance(value, str):
            clean_value = sanitize_question_preview(value, limit=64)
            if _METADATA_ENUM_VALUE_RE.fullmatch(clean_value):
                sanitized[key] = clean_value
        elif _METADATA_BOOL_KEY_RE.fullmatch(key) and isinstance(value, bool):
            sanitized[key] = value
        elif (
            _METADATA_NUMBER_KEY_RE.fullmatch(key)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        ):
            sanitized[key] = value
    return sanitized


def build_shadow_replay_event(
    *,
    outcome: str,
    comparison_metadata: Mapping[str, object] | None = None,
    error_type: str = "",
) -> dict[str, object]:
    """Build a privacy-bounded shadow comparison or isolation event."""
    completed = outcome == "completed"
    event: dict[str, object] = {
        "schema_version": 2,
        "event_type": (
            "ivd_shadow_replay_comparison"
            if completed
            else "ivd_shadow_replay_isolation"
        ),
        "outcome": str(outcome)[:32],
    }
    metadata = sanitize_shadow_comparison_metadata(comparison_metadata)
    if metadata:
        event["comparison_metadata"] = metadata
    if error_type:
        event["error_type"] = str(error_type)[:64]
    return event


class ShadowReplaySubmitter:
    """Run bounded shadow replays without affecting the served answer path."""

    def __init__(
        self,
        *,
        recorder: Callable[[dict[str, object]], None],
        max_workers: int = 1,
        queue_capacity: int = 8,
    ) -> None:
        worker_count = int(max_workers)
        queued_count = int(queue_capacity)
        if worker_count < 1:
            raise ValueError("max_workers must be at least 1")
        if queued_count < 0:
            raise ValueError("queue_capacity must not be negative")

        self._recorder = recorder
        self._executor = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="hermes-shadow-replay",
        )
        self._capacity = threading.BoundedSemaphore(worker_count + queued_count)
        self._condition = threading.Condition()
        self._active_tasks = 0
        self._closed = False
        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._cancelled = 0
        self._rejected = 0
        self._recording_dropped = 0
        self._record_pending = 0
        self._record_accepting = True
        self._record_queue: queue.Queue[
            tuple[str, Mapping[str, object] | None, str]
        ] = queue.Queue(maxsize=max(1, worker_count + queued_count))
        self._record_stop = threading.Event()
        self._record_thread = threading.Thread(
            target=self._record_events,
            name="hermes-shadow-replay-recorder",
        )
        self._record_thread.start()

    @property
    def stats(self) -> ShadowReplayStats:
        with self._condition:
            return ShadowReplayStats(
                submitted=self._submitted,
                completed=self._completed,
                failed=self._failed,
                cancelled=self._cancelled,
                rejected=self._rejected,
                recording_dropped=self._recording_dropped,
            )

    def submit(
        self,
        served_answer: _AnswerT,
        replay: Callable[[], object],
        *,
        comparison_metadata: Mapping[str, object] | None = None,
    ) -> _AnswerT:
        """Submit replay work if capacity exists and return served_answer unchanged."""
        with self._condition:
            if self._closed:
                self._rejected += 1
                rejection = "closed"
            elif not self._capacity.acquire(blocking=False):
                self._rejected += 1
                rejection = "queue_full"
            else:
                rejection = ""
                self._active_tasks += 1
                try:
                    future = self._executor.submit(
                        self._run_replay,
                        replay,
                        comparison_metadata,
                    )
                except Exception as exc:
                    self._capacity.release()
                    self._active_tasks -= 1
                    self._rejected += 1
                    self._enqueue_record(
                        "submission_failed",
                        comparison_metadata,
                        type(exc).__name__,
                    )
                    self._condition.notify_all()
                else:
                    self._submitted += 1
                    future.add_done_callback(self._finish_future)

        if rejection:
            self._enqueue_record(rejection)
        return served_answer

    def _run_replay(
        self,
        replay: Callable[[], object],
        comparison_metadata: Mapping[str, object] | None,
    ) -> None:
        try:
            replay_result = replay()
        except Exception as exc:
            with self._condition:
                self._failed += 1
            self._enqueue_record(
                "execution_failed",
                comparison_metadata,
                type(exc).__name__,
            )
        else:
            with self._condition:
                self._completed += 1
            completed_metadata = dict(comparison_metadata or {})
            if isinstance(replay_result, Mapping):
                for key, value in replay_result.items():
                    completed_metadata[str(key)] = value
            self._enqueue_record("completed", completed_metadata)

    def _finish_future(self, future: Future[None]) -> None:
        self._capacity.release()
        with self._condition:
            if future.cancelled():
                self._cancelled += 1
            self._active_tasks -= 1
            should_stop_recorder = self._closed and self._active_tasks == 0
            self._condition.notify_all()
        if future.cancelled():
            self._enqueue_record("cancelled")
        if should_stop_recorder:
            self._stop_recorder()

    def _enqueue_record(
        self,
        outcome: str,
        comparison_metadata: Mapping[str, object] | None = None,
        error_type: str = "",
    ) -> None:
        with self._condition:
            if not self._record_accepting:
                self._recording_dropped += 1
                return
            self._record_pending += 1
        try:
            self._record_queue.put_nowait(
                (outcome, comparison_metadata, error_type)
            )
        except queue.Full:
            with self._condition:
                self._record_pending -= 1
                self._recording_dropped += 1
                self._condition.notify_all()

    def _record_events(self) -> None:
        while not self._record_stop.is_set() or self._record_pending:
            try:
                outcome, comparison_metadata, error_type = self._record_queue.get(
                    timeout=0.05
                )
            except queue.Empty:
                continue
            try:
                event = build_shadow_replay_event(
                    outcome=outcome,
                    comparison_metadata=comparison_metadata,
                    error_type=error_type,
                )
                self._recorder(event)
            except Exception:
                pass
            finally:
                self._record_queue.task_done()
                with self._condition:
                    self._record_pending -= 1
                    self._condition.notify_all()

    def _stop_recorder(self) -> None:
        with self._condition:
            self._record_accepting = False
            self._record_stop.set()

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        """Wait until accepted replay and recording work finishes."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            return self._condition.wait_for(
                lambda: self._active_tasks == 0 and self._record_pending == 0,
                timeout=(
                    None
                    if deadline is None
                    else max(0.0, deadline - time.monotonic())
                ),
            )

    def close(self, *, wait_for_tasks: bool = True) -> None:
        """Reject future submissions and shut down all executor threads."""
        with self._condition:
            self._closed = True
            should_stop_recorder = self._active_tasks == 0
        self._executor.shutdown(wait=wait_for_tasks, cancel_futures=not wait_for_tasks)
        if should_stop_recorder:
            self._stop_recorder()
        if wait_for_tasks:
            self._stop_recorder()
            self._record_thread.join()

    def __enter__(self) -> ShadowReplaySubmitter:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()
