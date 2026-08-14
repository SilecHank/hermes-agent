"""One idempotent authoritative receipt sink for each served IVD turn."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import threading
from typing import Any, Callable, Mapping, TypeVar


_T = TypeVar("_T")


class EffectConflict(RuntimeError):
    """An idempotency key was reused for a different immutable effect."""


@dataclass
class _EffectEntry:
    digest: str
    state: str = "running"
    result: Any = None
    error: BaseException | None = None
    receipt_payload: bytes | None = None
    receipt_persisted: bool = False
    receipt_running: bool = False


class IdempotentEffectLedger:
    """Execute and receipt one external effect exactly once per immutable key."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, ...], _EffectEntry] = {}
        self._condition = threading.Condition()

    def execute(
        self,
        *,
        key: tuple[str, ...],
        digest: str,
        operation: Callable[[], tuple[_T, bytes]],
        receipt_submitter: Callable[[bytes], bool],
    ) -> _T:
        with self._condition:
            entry = self._entries.get(key)
            if entry is None:
                entry = _EffectEntry(digest=digest)
                self._entries[key] = entry
                owner = True
            else:
                if entry.digest != digest:
                    raise EffectConflict("effect digest conflicts with prior execution")
                owner = False
                while entry.state == "running":
                    self._condition.wait()
                if entry.state == "failed":
                    assert entry.error is not None
                    raise entry.error

        if owner:
            try:
                result, payload = operation()
            except BaseException as error:
                with self._condition:
                    entry.state = "failed"
                    entry.error = error
                    self._condition.notify_all()
                raise
            with self._condition:
                entry.result = result
                entry.receipt_payload = payload
                entry.state = "complete"
                self._condition.notify_all()

        self._persist_receipt(entry, receipt_submitter)
        return entry.result

    def _persist_receipt(
        self,
        entry: _EffectEntry,
        receipt_submitter: Callable[[bytes], bool],
    ) -> None:
        with self._condition:
            while entry.receipt_running:
                self._condition.wait()
            if entry.receipt_persisted:
                return
            entry.receipt_running = True
            payload = entry.receipt_payload
        accepted = False
        try:
            accepted = bool(payload is not None and receipt_submitter(payload))
        except Exception:
            accepted = False
        finally:
            with self._condition:
                entry.receipt_running = False
                entry.receipt_persisted = accepted
                self._condition.notify_all()
        if not accepted:
            raise ReceiptPersistenceError("effect receipt persistence failed")


_ALLOWED_SPANS = {
    "dispatch_ms",
    "lookup_ms",
    "model_ms",
    "delivery_ms",
    "validation_ms",
    "first_response_ms",
    "total_ms",
}
_ALLOWED_COUNTERS = {
    "model_calls",
    "index_calls",
    "filesystem_calls",
    "skill_calls",
}


def _bounded_metrics(
    values: Mapping[str, int | float], allowed: set[str]
) -> dict[str, int | float]:
    bounded: dict[str, int | float] = {}
    for key, value in values.items():
        if key not in allowed or isinstance(value, bool):
            continue
        numeric = max(0, value)
        bounded[key] = round(numeric, 3) if isinstance(numeric, float) else numeric
    return bounded


@dataclass(frozen=True)
class TurnReceipt:
    turn_id: str
    contract_id: str
    event_id: str
    package_digest: str
    serving_projection_digest: str
    validation_status: str
    child_spans: Mapping[str, int | float] = field(default_factory=dict)
    counters: Mapping[str, int | float] = field(default_factory=dict)

    def to_event(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "event_type": "ivd_turn_receipt",
            "authoritative": True,
            "turn_id": str(self.turn_id),
            "contract_id": str(self.contract_id),
            "event_id": str(self.event_id),
            "package_digest": str(self.package_digest),
            "serving_projection_digest": str(self.serving_projection_digest),
            "validation_status": str(self.validation_status),
            "child_spans": _bounded_metrics(self.child_spans, _ALLOWED_SPANS),
            "counters": _bounded_metrics(self.counters, _ALLOWED_COUNTERS),
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_event()[key]

    def __contains__(self, key: object) -> bool:
        return key in self.to_event()


class ReceiptPersistenceError(RuntimeError):
    """The authoritative turn outcome was not durably persisted."""


class ReceiptConflictError(ReceiptPersistenceError):
    """One turn id was reused for a different authoritative outcome."""


@dataclass
class _PendingReceipt:
    digest: str
    completed: threading.Event = field(default_factory=threading.Event)
    accepted: bool = False


class AuthoritativeTurnReceiptSink:
    """Submit at most one durable authoritative outcome for a turn id."""

    def __init__(self, *, submitter: Callable[[dict[str, Any]], bool]) -> None:
        self._submitter = submitter
        self._claimed_turns: dict[str, str] = {}
        self._pending_turns: dict[str, _PendingReceipt] = {}
        self._lock = threading.Lock()
        self._authoritative_count = 0
        self._failed_count = 0

    @property
    def authoritative_count(self) -> int:
        with self._lock:
            return self._authoritative_count

    @property
    def failed_count(self) -> int:
        with self._lock:
            return self._failed_count

    def submit(self, receipt: TurnReceipt) -> bool:
        event = receipt.to_event()
        digest = hashlib.sha256(
            json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        while True:
            with self._lock:
                claimed = self._claimed_turns.get(receipt.turn_id)
                if claimed is not None:
                    if claimed != digest:
                        raise ReceiptConflictError(
                            "turn receipt conflicts with persisted outcome"
                        )
                    return True
                pending = self._pending_turns.get(receipt.turn_id)
                if pending is None:
                    pending = _PendingReceipt(digest=digest)
                    self._pending_turns[receipt.turn_id] = pending
                    owner = True
                else:
                    if pending.digest != digest:
                        raise ReceiptConflictError(
                            "turn receipt conflicts with pending outcome"
                        )
                    owner = False
            if owner:
                break
            pending.completed.wait()
            if pending.accepted:
                return True
        try:
            accepted = bool(self._submitter(event))
        except Exception:
            accepted = False
        with self._lock:
            self._pending_turns.pop(receipt.turn_id, None)
            pending.accepted = accepted
            if accepted:
                self._claimed_turns[receipt.turn_id] = digest
                self._authoritative_count += 1
            else:
                self._failed_count += 1
            pending.completed.set()
        if not accepted:
            raise ReceiptPersistenceError("turn receipt persistence failed")
        return True

    def submit_after_handoff(self, answer: Any, receipt: TurnReceipt) -> Any:
        self.submit(receipt)
        return answer


_SINKS_BY_DESTINATION: dict[int, tuple[object, AuthoritativeTurnReceiptSink]] = {}
_SINKS_LOCK = threading.Lock()
_EFFECT_LEDGERS: dict[int, tuple[object, IdempotentEffectLedger]] = {}


def sink_for_destination(
    destination: object,
    *,
    submitter: Callable[[dict[str, Any]], bool],
) -> AuthoritativeTurnReceiptSink:
    """Return one process-local idempotency boundary per startup-opened sink."""
    if isinstance(destination, (str, bytes, int, float, tuple, frozenset)):
        return AuthoritativeTurnReceiptSink(submitter=submitter)
    identity = id(destination)
    with _SINKS_LOCK:
        existing = _SINKS_BY_DESTINATION.get(identity)
        if existing is not None and existing[0] is destination:
            return existing[1]
        sink = AuthoritativeTurnReceiptSink(submitter=submitter)
        _SINKS_BY_DESTINATION[identity] = (destination, sink)
        return sink


def effect_ledger_for_destination(destination: object) -> IdempotentEffectLedger:
    """Return one process-local effect ledger for a startup-opened destination."""
    if isinstance(destination, (str, bytes, int, float, tuple, frozenset)):
        return IdempotentEffectLedger()
    identity = id(destination)
    with _SINKS_LOCK:
        existing = _EFFECT_LEDGERS.get(identity)
        if existing is not None and existing[0] is destination:
            return existing[1]
        ledger = IdempotentEffectLedger()
        _EFFECT_LEDGERS[identity] = (destination, ledger)
        return ledger
