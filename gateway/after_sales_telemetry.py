"""Sanitized per-turn telemetry for the IVD answer plane."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


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
    preview = sanitize_question_preview(question_text)
    return {
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


def append_runtime_event(path: str | Path, event: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
