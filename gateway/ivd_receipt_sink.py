"""One idempotent authoritative receipt sink for each served IVD turn."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any, Callable, Mapping


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


class AuthoritativeTurnReceiptSink:
    """Submit at most one durable authoritative outcome for a turn id."""

    def __init__(self, *, submitter: Callable[[dict[str, Any]], bool]) -> None:
        self._submitter = submitter
        self._claimed_turns: set[str] = set()
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
        with self._lock:
            if receipt.turn_id in self._claimed_turns:
                return True
            self._claimed_turns.add(receipt.turn_id)
        try:
            accepted = bool(self._submitter(receipt.to_event()))
        except Exception:
            accepted = False
        with self._lock:
            if accepted:
                self._authoritative_count += 1
            else:
                self._failed_count += 1
        return accepted

    def submit_after_handoff(self, answer: Any, receipt: TurnReceipt) -> Any:
        self.submit(receipt)
        return answer


_SINKS_BY_DESTINATION: dict[int, tuple[object, AuthoritativeTurnReceiptSink]] = {}
_SINKS_LOCK = threading.Lock()


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
