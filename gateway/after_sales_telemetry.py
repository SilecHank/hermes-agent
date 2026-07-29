"""Sanitized per-turn telemetry for the IVD answer plane."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


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
    answer_text: str = "",
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
    return {
        "schema_version": 1,
        "event_type": "ivd_answer_turn",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": str(platform),
        "session_hash": hashlib.sha256(str(session_key).encode("utf-8")).hexdigest()[:16],
        "product_scope": str(product_scope or ""),
        "route_id": str(route_id or "standard"),
        "route_version": str(route_version or ""),
        "fast_path": bool(fast_path),
        "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 3),
        "api_calls": max(0, int(api_calls)),
        "tool_names": tools,
        "tool_count": len(tools),
        "source_paths": sources,
        "validation_status": str(validation_status or "unknown"),
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
