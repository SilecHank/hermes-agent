"""Build privacy-safe evidence sidecars for IVD answers."""

from __future__ import annotations

import re
from typing import Any, Iterable


FORMAL_AUTHORITIES = {"formal_authority", "formal_sop", "controlled_workflow"}
UNUSABLE_MARKERS = (
    "/_extracted/",
    "/matrices/",
    "pending_verify",
    "candidate_from_case",
    "sop-parameter-candidates.tsv",
    "case-mechanism-candidates.tsv",
)
NUMERIC_RE = re.compile(r"\d+(?:\.\d+)?")
NUMERIC_SHAPES = {"scalar_lookup", "scoped_scalar"}


def build_answer_sidecar(
    *,
    answer: str,
    answer_shape: str,
    validated_sources: Iterable[dict[str, Any]],
    product_scope: str,
    adopted_evidence_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Keep only explicitly adopted, formal, non-candidate evidence."""
    adopted = {str(item) for item in adopted_evidence_ids if str(item)}
    selected: list[dict[str, str]] = []
    for source in validated_sources:
        evidence_id = str(source.get("evidence_id") or "")
        source_path = _normalise_path(str(source.get("source_path") or ""))
        if evidence_id not in adopted:
            continue
        if str(source.get("status") or "") != "validated":
            continue
        if str(source.get("authority") or "") not in FORMAL_AUTHORITIES:
            continue
        if not source_path or _is_unusable(source_path):
            continue
        if not str(source.get("source_revision") or ""):
            continue
        selected.append(
            {
                "evidence_id": evidence_id,
                "authority": str(source.get("authority") or ""),
                "source_path": source_path,
                "source_revision": str(source.get("source_revision") or ""),
            }
        )

    evidence_ids = [item["evidence_id"] for item in selected]
    numeric_claim = bool(NUMERIC_RE.search(str(answer or "")))
    needs_evidence = answer_shape in NUMERIC_SHAPES or numeric_claim
    status = "validated" if selected or not needs_evidence else "needs_source"
    return {
        "schema_version": 1,
        "status": status,
        "answer_shape": str(answer_shape or "direct_fact"),
        "product_scope": str(product_scope or ""),
        "evidence_ids": evidence_ids,
        "sources": selected,
    }


def _normalise_path(value: str) -> str:
    return value.strip().replace("\\", "/").lstrip("./")


def _is_unusable(path: str) -> bool:
    lower = path.casefold()
    return any(marker in lower for marker in UNUSABLE_MARKERS)
