"""Turn-local limits for the IVD answer plane."""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import re
import threading
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from gateway.ivd_execution_contract import (
    AppendOnlyReceiptSink,
    PreparedIVDTurn,
    load_serving_projection,
    prepare_ivd_turn,
)
from gateway.ivd_dispatcher import IVDDispatcher
from gateway.ivd_knowledge_engine import IVDKnowledgeEngine

logger = logging.getLogger(__name__)


def _strip_gateway_internal_prefix(question: str) -> str:
    """Remove the gateway sender-identity prefix from the engine question."""
    if not isinstance(question, str):
        return str(question or "")
    return re.sub(r"^\[Gateway[^\]]*\]\s*", "", question).strip()


@dataclass(frozen=True)
class ExclusiveIVDResult:
    """One fully validated package answer selected by one dispatch."""

    text: str
    answer_shape: str
    outcome: str
    model_calls: int
    index_transactions: int
    filesystem_scans: int
    effect_count: int
    sources: tuple[object, ...]
    dispatch_count: int = 1
    final_validation_count: int = 1


@dataclass
class _CachedEngineGeneration:
    package_digest: str
    engine: IVDKnowledgeEngine
    active_leases: int = 0
    retired: bool = False


class _IVDKnowledgeEngineLease:
    def __init__(
        self,
        cache: "_IVDKnowledgeEngineCache",
        generation: _CachedEngineGeneration,
    ) -> None:
        self._cache = cache
        self._generation = generation
        self._released = False

    def __enter__(self) -> IVDKnowledgeEngine:
        return self._generation.engine

    def __exit__(self, *_args: object) -> None:
        if not self._released:
            self._released = True
            self._cache.release(self._generation)


class _IVDKnowledgeEngineCache:
    """Keep one digest-bound engine active and retire generations safely."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current: _CachedEngineGeneration | None = None

    def acquire(
        self,
        package_root: str | Path,
        package_digest: str,
    ) -> _IVDKnowledgeEngineLease:
        if not re.fullmatch(r"[0-9a-f]{64}", package_digest):
            from gateway.ivd_execution_contract import IVDRuntimeConfigurationError

            raise IVDRuntimeConfigurationError("IVD package digest is invalid")
        close_after_swap: IVDKnowledgeEngine | None = None
        with self._lock:
            generation = self._current
            if generation is None or generation.package_digest != package_digest:
                candidate = IVDKnowledgeEngine(
                    package_root,
                    expected_package_digest=package_digest,
                )
                generation = _CachedEngineGeneration(package_digest, candidate)
                prior = self._current
                self._current = generation
                if prior is not None:
                    prior.retired = True
                    if prior.active_leases == 0:
                        close_after_swap = prior.engine
            generation.active_leases += 1
        if close_after_swap is not None:
            close_after_swap.close()
        return _IVDKnowledgeEngineLease(self, generation)

    def release(self, generation: _CachedEngineGeneration) -> None:
        close_retired: IVDKnowledgeEngine | None = None
        with self._lock:
            if generation.active_leases <= 0:
                return
            generation.active_leases -= 1
            if generation.retired and generation.active_leases == 0:
                close_retired = generation.engine
        if close_retired is not None:
            close_retired.close()

    def close(self) -> None:
        close_current: IVDKnowledgeEngine | None = None
        with self._lock:
            generation = self._current
            self._current = None
            if generation is not None:
                generation.retired = True
                if generation.active_leases == 0:
                    close_current = generation.engine
        if close_current is not None:
            close_current.close()


_IVD_ENGINE_CACHE = _IVDKnowledgeEngineCache()


def _bounded_user_context(
    history: list[dict[str, Any]] | None,
    *,
    budget: int,
) -> str:
    if budget <= 0:
        return ""
    selected: list[str] = []
    remaining = budget
    for item in reversed((history or [])[-6:]):
        if item.get("role") != "user" or not isinstance(item.get("content"), str):
            continue
        content = str(item["content"]).strip()
        if not content:
            continue
        separator = 1 if selected else 0
        if remaining <= separator:
            break
        chunk = content[: remaining - separator]
        selected.append(chunk)
        remaining -= len(chunk) + separator
        if remaining <= 0:
            break
    return " ".join(reversed(selected))


def close_ivd_knowledge_engine_cache() -> None:
    _IVD_ENGINE_CACHE.close()


atexit.register(close_ivd_knowledge_engine_cache)


def ivd_engine_mode(config: Mapping[str, Any], *, platform: str) -> str:
    guard = _enabled_ivd_guard(dict(config), platform)
    if guard is None:
        return "disabled"
    mode = str(guard.get("engine_mode") or "compatibility").strip().lower()
    if mode not in {"compatibility", "package"}:
        from gateway.ivd_execution_contract import IVDRuntimeConfigurationError

        raise IVDRuntimeConfigurationError("invalid IVD engine mode")
    return mode


def execute_exclusive_ivd_turn(
    prepared: PreparedIVDTurn | None,
    *,
    question: str,
    history: list[dict[str, Any]] | None = None,
    evidence: Mapping[str, object] | None = None,
) -> ExclusiveIVDResult:
    """Run the immutable package without constructing any legacy answer plane."""
    if prepared is None:
        from gateway.ivd_execution_contract import IVDRuntimeConfigurationError

        raise IVDRuntimeConfigurationError("package contract is required")
    projection = prepared.execution_contract.serving_projection
    package_root = str(projection.get("serving_package_path") or "")
    if not package_root:
        from gateway.ivd_execution_contract import IVDRuntimeConfigurationError

        raise IVDRuntimeConfigurationError("package contract has no serving package")
    package_digest = str(
        getattr(prepared.execution_contract, "package_digest", "") or ""
    )
    dispatcher = IVDDispatcher(package_root)
    context_budget = int(projection.get("context_budget") or 0)
    recent_context = _bounded_user_context(
        history,
        budget=context_budget,
    )
    with _IVD_ENGINE_CACHE.acquire(package_root, package_digest) as engine:
        outcome = dispatcher.execute(
            engine,
            question=_strip_gateway_internal_prefix(question),
            context=recent_context,
            evidence=evidence,
        )
        logger.info(
            "IVD dispatch diagnostic: question=%r context=%r intent=%s product=%s "
            "variant=%s stage=%s knowledge=%s shape=%s ambiguities=%s budget=%s",
            question,
            recent_context,
            outcome.envelope.intent,
            outcome.envelope.product_line,
            outcome.envelope.product_variant,
            outcome.envelope.workflow_stage,
            outcome.envelope.knowledge_type,
            outcome.envelope.answer_shape,
            outcome.envelope.ambiguities,
            outcome.envelope.indexed_retrieval_budget,
        )
        result = outcome.result
        if result is None:
            questions = tuple(outcome.envelope.clarifying_questions)
            text = questions[0] if questions else "请补充产品名称、版本或SOP编号。"
            return ExclusiveIVDResult(
                text=text,
                answer_shape="clarification",
                outcome="clarification",
                model_calls=0,
                index_transactions=0,
                filesystem_scans=0,
                effect_count=0,
                sources=(),
            )
        if int(result.model_calls) > int(outcome.envelope.model_call_budget):
            from gateway.ivd_execution_contract import IVDRuntimeConfigurationError

            raise IVDRuntimeConfigurationError("IVD model-call budget exceeded")
        if int(result.index_transactions) > int(
            outcome.envelope.indexed_retrieval_budget
        ):
            from gateway.ivd_execution_contract import IVDRuntimeConfigurationError

            raise IVDRuntimeConfigurationError(
                "IVD index-transaction budget exceeded"
            )
        return ExclusiveIVDResult(
            text=str(result.text),
            answer_shape=str(result.answer_shape),
            outcome=str(result.outcome),
            model_calls=int(result.model_calls),
            index_transactions=int(result.index_transactions),
            filesystem_scans=int(result.filesystem_scans),
            effect_count=int(result.effect_count),
            sources=tuple(result.sources),
        )


def _enabled_ivd_guard(config: dict[str, Any], platform: str) -> dict[str, Any] | None:
    guard = config.get("after_sales_guard") or {}
    if not isinstance(guard, dict) or not guard.get("enabled", False):
        return None
    platforms = guard.get("platforms") or []
    if isinstance(platforms, str):
        platforms = [item.strip() for item in platforms.split(",") if item.strip()]
    if str(platform or "").strip().lower() not in {
        str(item or "").strip().lower() for item in platforms
    }:
        return None
    return guard


def prepare_enabled_ivd_turn(
    config: dict[str, Any], *, platform: str
) -> PreparedIVDTurn | None:
    """Prepare the trusted serving contract before legacy after-sales work."""
    guard = _enabled_ivd_guard(config, platform)
    if guard is None:
        return None
    path = guard.get("serving_projection_path")
    if not isinstance(path, str) or not path.strip():
        from gateway.ivd_execution_contract import IVDRuntimeConfigurationError

        raise IVDRuntimeConfigurationError(
            "managed IVD platform requires serving_projection_path"
        )
    projection = load_serving_projection(
        path,
        expected_package_digest=guard.get("package_digest"),
    )
    return prepare_ivd_turn(projection)


def preload_enabled_ivd_contracts(
    config: dict[str, Any],
) -> Mapping[str, PreparedIVDTurn]:
    """Load the release-pinned IVD contract once during gateway startup."""
    guard = config.get("after_sales_guard") or {}
    if not isinstance(guard, dict) or not guard.get("enabled", False):
        return MappingProxyType({})
    platforms = guard.get("platforms") or []
    if isinstance(platforms, str):
        platforms = [item.strip() for item in platforms.split(",") if item.strip()]
    normalized_platforms = tuple(
        dict.fromkeys(
            str(item or "").strip().lower()
            for item in platforms
            if str(item or "").strip()
        )
    )
    if not normalized_platforms:
        return MappingProxyType({})
    path = guard.get("serving_projection_path")
    if not isinstance(path, str) or not path.strip():
        from gateway.ivd_execution_contract import IVDRuntimeConfigurationError

        raise IVDRuntimeConfigurationError(
            "managed IVD platform requires serving_projection_path"
        )
    projection = load_serving_projection(
        path,
        expected_package_digest=guard.get("package_digest"),
    )
    prepared = prepare_ivd_turn(projection)
    return MappingProxyType(
        {platform: prepared for platform in normalized_platforms}
    )


_MAX_RECEIPT_BYTES = 4096
_RECEIPT_WRITE_ATTEMPTS = 3
_RECEIPT_ENQUEUE_TIMEOUT_SECONDS = 0.25
_RECEIPT_STOP = object()


@dataclass
class _ReceiptWriteTask:
    destination: AppendOnlyReceiptSink
    payload: bytes
    completed: threading.Event = field(default_factory=threading.Event)
    accepted: bool = False


_RECEIPT_QUEUE: queue.Queue[_ReceiptWriteTask | object] | None = queue.Queue(
    maxsize=256
)
_RECEIPT_WORKER_STARTED = False
_RECEIPT_WORKER: threading.Thread | None = None
_RECEIPT_ACCEPTING = True
_RECEIPT_WORKER_LOCK = threading.RLock()


def _append_ivd_receipt(sink: AppendOnlyReceiptSink, payload: bytes) -> bool:
    return sink.append(payload)


def _receipt_worker(receipt_queue: queue.Queue[_ReceiptWriteTask | object]) -> None:
    while True:
        task = receipt_queue.get()
        try:
            if task is _RECEIPT_STOP:
                return
            assert isinstance(task, _ReceiptWriteTask)
            try:
                for _attempt in range(_RECEIPT_WRITE_ATTEMPTS):
                    try:
                        if _append_ivd_receipt(task.destination, task.payload):
                            task.accepted = True
                            break
                    except Exception:
                        continue
            finally:
                task.completed.set()
        finally:
            receipt_queue.task_done()


def _ensure_receipt_worker() -> bool:
    global _RECEIPT_WORKER, _RECEIPT_WORKER_STARTED
    with _RECEIPT_WORKER_LOCK:
        if not _RECEIPT_ACCEPTING or _RECEIPT_QUEUE is None:
            return False
        if _RECEIPT_WORKER_STARTED:
            return bool(_RECEIPT_WORKER and _RECEIPT_WORKER.is_alive())
        worker = threading.Thread(
            target=_receipt_worker,
            args=(_RECEIPT_QUEUE,),
            name="ivd-receipt-writer",
            daemon=True,
        )
        worker.start()
        _RECEIPT_WORKER = worker
        _RECEIPT_WORKER_STARTED = True
        return True


def enqueue_ivd_receipt(
    destination: AppendOnlyReceiptSink, receipt: dict[str, Any]
) -> bool:
    """Return success only after one bounded receipt is durably appended."""
    if _RECEIPT_QUEUE is None:
        return False
    payload = (
        json.dumps(receipt, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(payload) > _MAX_RECEIPT_BYTES:
        return False
    task = _ReceiptWriteTask(destination=destination, payload=payload)
    with _RECEIPT_WORKER_LOCK:
        if not _ensure_receipt_worker() or _RECEIPT_QUEUE is None:
            return False
        try:
            _RECEIPT_QUEUE.put(task, timeout=_RECEIPT_ENQUEUE_TIMEOUT_SECONDS)
        except queue.Full:
            return False
    task.completed.wait()
    return task.accepted


def drain_ivd_receipts(timeout: float = 5.0) -> bool:
    """Stop accepting receipt work and drain every task accepted before shutdown."""
    global _RECEIPT_ACCEPTING, _RECEIPT_WORKER, _RECEIPT_WORKER_STARTED
    with _RECEIPT_WORKER_LOCK:
        _RECEIPT_ACCEPTING = False
        receipt_queue = _RECEIPT_QUEUE
        worker = _RECEIPT_WORKER
        if not _RECEIPT_WORKER_STARTED:
            return receipt_queue is None or receipt_queue.empty()
        if worker is None or receipt_queue is None:
            return False
        try:
            receipt_queue.put(_RECEIPT_STOP, timeout=max(0.0, float(timeout)))
        except queue.Full:
            return False
    worker.join(timeout=max(0.0, float(timeout)))
    stopped = not worker.is_alive()
    if stopped:
        with _RECEIPT_WORKER_LOCK:
            if _RECEIPT_WORKER is worker:
                _RECEIPT_WORKER = None
                _RECEIPT_WORKER_STARTED = False
    return stopped


atexit.register(drain_ivd_receipts)


@dataclass(frozen=True)
class IVDRetrievalPolicy:
    profile: str
    stages: tuple[str, ...]
    max_searches: int
    hard_limit: int = 4


DIRECT_POLICY = IVDRetrievalPolicy("direct", (), 0)
INDEX_FALLBACK_POLICY = IVDRetrievalPolicy(
    "index_fallback", ("index_fallback",), 2
)
EVIDENCE_SUPPLEMENT_POLICY = IVDRetrievalPolicy(
    "evidence_supplement",
    ("index_fallback", "evidence_supplement"),
    2,
)
COMPLEX_DIAGNOSIS_POLICY = IVDRetrievalPolicy(
    "complex_diagnosis",
    ("product_lookup", "conflict_branch", "evidence_supplement"),
    3,
)

_COMPLEX_INTENT_RE = re.compile(
    r"跨产品|不同产品|多个产品|多产品|混杂|叠加|多个异常|"
    r"版本冲突|版本不一致|新旧版本|"
    r"同批.{0,20}(?:不同|多个).{0,12}(?:产品|异常)",
    re.IGNORECASE,
)
_EVIDENCE_INTENT_RE = re.compile(
    r"为什么|原理|机制|文献|指南|共识|证据|依据|引用|版本|"
    r"principle|mechanism|literature|guideline|evidence|version",
    re.IGNORECASE,
)
_EVIDENCE_EXPANSION_RE = re.compile(
    r"文献|指南|共识|证据|依据|引用|版本|"
    r"literature|guideline|evidence|version",
    re.IGNORECASE,
)


def resolve_ivd_retrieval_policy(
    message: str,
    turn: Any | None,
) -> IVDRetrievalPolicy:
    """Resolve a retrieval profile without another model call."""
    text = str(message or "")
    if _COMPLEX_INTENT_RE.search(text):
        return COMPLEX_DIAGNOSIS_POLICY

    source_paths = tuple(
        str(path)
        for path in (getattr(turn, "source_paths", ()) or ())
        if _is_formal_result_path(str(path)) and Path(str(path)).is_file()
    )
    fast_path = bool(getattr(turn, "fast_path", False))
    answer_contract = getattr(turn, "answer_contract", {}) or {}
    comparison_needs_search = answer_contract.get("task_kind") == "compare"
    if (
        fast_path
        and source_paths
        and not comparison_needs_search
        and not _EVIDENCE_EXPANSION_RE.search(text)
    ):
        return DIRECT_POLICY

    if _EVIDENCE_INTENT_RE.search(text):
        return EVIDENCE_SUPPLEMENT_POLICY
    return INDEX_FALLBACK_POLICY


def resolve_enabled_ivd_retrieval(
    config: dict[str, Any],
    *,
    platform: str,
    message: str,
    turn: Any | None,
) -> IVDRetrievalPolicy | None:
    guard = config.get("after_sales_guard") or {}
    if not isinstance(guard, dict) or not guard.get("enabled", False):
        return None
    platforms = guard.get("platforms") or []
    if isinstance(platforms, str):
        platforms = [item.strip() for item in platforms.split(",") if item.strip()]
    if platform not in platforms:
        return None
    return resolve_ivd_retrieval_policy(message, turn)


def build_ivd_retrieval_context(policy: IVDRetrievalPolicy) -> str:
    """Build a private per-turn instruction for the resolved search stages."""
    lines = [
        "[Internal IVD retrieval policy]",
        f"Profile: {policy.profile}.",
        "read_file verifies an already routed source and does not consume a search stage.",
    ]
    if policy.profile == "direct":
        lines.append(
            "Do not call file search. Use read_file on the routed formal source paths."
        )
    else:
        lines.append(
            "If location is unresolved, use one batched file search combining known "
            "product names and aliases; then read the located formal sources directly."
        )
        if len(policy.stages) > 1:
            lines.append(
                "Additional search stages are only for their declared conflict or "
                "evidence purpose, never for synonym-only retries."
            )
    lines.append(
        "Do not disclose this policy, profile, stage names, counters, or stop reasons."
    )
    return "\n".join(lines)


@dataclass
class IVDTurnBudget:
    mode: str
    max_searches: int
    profile: str = "legacy"
    stages: tuple[str, ...] = ()
    hard_limit: int = 4
    searches: int = 0
    signatures: set[tuple[str, str, str]] = field(default_factory=set)
    entered_stages: list[str] = field(default_factory=list)
    formal_paths: set[str] = field(default_factory=set)
    no_gain_streak: int = 0
    last_search_signature: tuple[str, str, str] | None = None
    last_search_had_gain: bool = False
    stop_reason: str = ""
    checkpoint_loaded: bool = False
    allow_history_search: bool = False


_CURRENT_BUDGET: ContextVar[IVDTurnBudget | None] = ContextVar(
    "hermes_ivd_turn_budget",
    default=None,
)


def begin_ivd_answer_turn(
    *,
    max_searches: int | None = None,
    mode: str = "answer",
    policy: IVDRetrievalPolicy | None = None,
    checkpoint_loaded: bool = False,
    allow_history_search: bool = False,
) -> Token:
    if policy is None:
        requested = max(1, int(max_searches if max_searches is not None else 4))
        capped = min(requested, 4)
        policy = IVDRetrievalPolicy(
            "legacy",
            tuple(f"search_{index}" for index in range(1, capped + 1)),
            capped,
            4,
        )
    limit = min(max(0, int(policy.max_searches)), max(1, int(policy.hard_limit)))
    budget = IVDTurnBudget(
        mode=mode,
        max_searches=limit,
        profile=policy.profile,
        stages=policy.stages,
        hard_limit=max(1, int(policy.hard_limit)),
        checkpoint_loaded=bool(checkpoint_loaded),
        allow_history_search=bool(allow_history_search),
    )
    return _CURRENT_BUDGET.set(budget)


def end_ivd_answer_turn(token: Token) -> None:
    _CURRENT_BUDGET.reset(token)


def can_use_ivd_session_search() -> bool:
    budget = _CURRENT_BUDGET.get()
    if budget is None or budget.mode != "answer":
        return True
    return not budget.checkpoint_loaded or budget.allow_history_search


def _search_signature(
    *, pattern: str, path: str, target: str
) -> tuple[str, str, str] | None:
    if not pattern and path == "." and target == "content":
        return None
    normalized_pattern = re.sub(r"\s+", " ", str(pattern or "")).strip().casefold()
    normalized_path = os.path.normpath(os.path.expanduser(str(path or "."))).casefold()
    return str(target or "content").casefold(), normalized_path, normalized_pattern


def consume_ivd_search(
    *, pattern: str = "", path: str = ".", target: str = "content"
) -> tuple[bool, int, int]:
    budget = _CURRENT_BUDGET.get()
    if budget is None or budget.mode != "answer":
        return True, 0, 0

    next_number = budget.searches + 1
    if budget.stop_reason:
        return False, next_number, budget.max_searches

    signature = _search_signature(pattern=pattern, path=path, target=target)
    if signature is not None and signature in budget.signatures:
        budget.stop_reason = "duplicate"
        return False, next_number, budget.max_searches

    if (
        budget.profile == "evidence_supplement"
        and budget.last_search_had_gain
        and signature is not None
        and budget.last_search_signature is not None
        and signature[1] == budget.last_search_signature[1]
    ):
        budget.stop_reason = "duplicate_intent"
        return False, next_number, budget.max_searches

    if budget.searches >= budget.max_searches:
        if budget.profile == "direct":
            budget.stop_reason = "direct"
        elif budget.searches >= budget.hard_limit:
            budget.stop_reason = "hard_limit"
        else:
            budget.stop_reason = "profile_limit"
        return False, next_number, budget.max_searches

    if signature is not None:
        budget.signatures.add(signature)
    budget.last_search_signature = signature
    budget.last_search_had_gain = False
    stage = (
        budget.stages[budget.searches]
        if budget.searches < len(budget.stages)
        else f"search_{next_number}"
    )
    budget.entered_stages.append(stage)
    budget.searches += 1
    return True, budget.searches, budget.max_searches


_NON_FORMAL_PATH_PARTS = {
    "_extracted",
    "_wechat-mirror",
    "matrices",
    "evaluation",
    "archive",
    "deprecated",
    "superseded",
}


def _is_formal_result_path(path: str) -> bool:
    normalized = os.path.normpath(os.path.expanduser(str(path or "")))
    parts = {part.casefold() for part in normalized.split(os.sep) if part}
    name = os.path.basename(normalized).casefold()
    return (
        bool(normalized)
        and "ivd-knowledgehub" in parts
        and "knowledge-base" in parts
        and not (parts & _NON_FORMAL_PATH_PARTS or "candidate" in name)
    )


def record_ivd_search_result(
    *,
    pattern: str,
    path: str,
    target: str,
    result_paths: tuple[str, ...] | list[str],
) -> None:
    budget = _CURRENT_BUDGET.get()
    if budget is None or budget.mode != "answer" or budget.searches <= 0:
        return

    formal_paths = {
        os.path.normpath(os.path.expanduser(str(candidate)))
        for candidate in result_paths
        if _is_formal_result_path(str(candidate))
    }
    novel = formal_paths - budget.formal_paths
    budget.last_search_had_gain = bool(novel)
    if novel:
        budget.formal_paths.update(novel)
        budget.no_gain_streak = 0
        if budget.profile == "index_fallback":
            budget.stop_reason = "formal_source_found"
        return

    budget.no_gain_streak += 1
    if budget.no_gain_streak >= 2:
        budget.stop_reason = "no_gain"


def get_ivd_retrieval_snapshot() -> dict[str, object]:
    budget = _CURRENT_BUDGET.get()
    if budget is None or budget.mode != "answer":
        return {
            "active": False,
            "profile": "inactive",
            "stages": [],
            "searches": 0,
            "signature_count": 0,
            "formal_source_count": 0,
            "no_gain_streak": 0,
            "stop_reason": "",
        }
    return {
        "active": True,
        "profile": budget.profile,
        "stages": list(budget.entered_stages),
        "searches": budget.searches,
        "signature_count": len(budget.signatures),
        "formal_source_count": len(budget.formal_paths),
        "no_gain_streak": budget.no_gain_streak,
        "stop_reason": budget.stop_reason,
    }
