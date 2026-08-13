"""Turn-local limits for the IVD answer plane."""

from __future__ import annotations

import json
import os
import queue
import re
import threading
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gateway.ivd_execution_contract import (
    PreparedIVDTurn,
    load_serving_projection,
    prepare_ivd_turn,
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


_MAX_RECEIPT_BYTES = 4096
_RECEIPT_QUEUE: queue.Queue[tuple[str, bytes]] | None = queue.Queue(maxsize=256)
_RECEIPT_WORKER_STARTED = False
_RECEIPT_WORKER_LOCK = threading.Lock()


def _append_ivd_receipt(path: str, payload: bytes) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def _receipt_worker() -> None:
    assert _RECEIPT_QUEUE is not None
    while True:
        destination, payload = _RECEIPT_QUEUE.get()
        try:
            _append_ivd_receipt(destination, payload)
        except Exception:
            pass
        finally:
            _RECEIPT_QUEUE.task_done()


def _ensure_receipt_worker() -> None:
    global _RECEIPT_WORKER_STARTED
    if _RECEIPT_WORKER_STARTED:
        return
    with _RECEIPT_WORKER_LOCK:
        if not _RECEIPT_WORKER_STARTED:
            threading.Thread(
                target=_receipt_worker,
                name="ivd-receipt-writer",
                daemon=True,
            ).start()
            _RECEIPT_WORKER_STARTED = True


def enqueue_ivd_receipt(destination: str, receipt: dict[str, Any]) -> bool:
    """Enqueue one bounded receipt; full/unavailable queues drop without retry."""
    if _RECEIPT_QUEUE is None:
        return False
    payload = (
        json.dumps(receipt, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(payload) > _MAX_RECEIPT_BYTES:
        return False
    _ensure_receipt_worker()
    try:
        _RECEIPT_QUEUE.put_nowait((destination, payload))
        return True
    except queue.Full:
        return False


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
    if fast_path and source_paths and not _EVIDENCE_EXPANSION_RE.search(text):
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


_CURRENT_BUDGET: ContextVar[IVDTurnBudget | None] = ContextVar(
    "hermes_ivd_turn_budget",
    default=None,
)


def begin_ivd_answer_turn(
    *,
    max_searches: int | None = None,
    mode: str = "answer",
    policy: IVDRetrievalPolicy | None = None,
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
    )
    return _CURRENT_BUDGET.set(budget)


def end_ivd_answer_turn(token: Token) -> None:
    _CURRENT_BUDGET.reset(token)


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
