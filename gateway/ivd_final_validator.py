"""Pure final validation for deterministic IVD package responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


NON_SERVING_STATUSES = {"candidate", "candidate_from_case", "pending", "pending_verify", "blocked"}
SOURCE_FIELDS = (
    "source_document_id",
    "source_version",
    "source_locator",
    "source_path",
    "source_sha256",
    "source_record_digest",
)
INTERNAL_MARKERS = (
    "请先读取当前产品路由",
    "fast_path",
    "pending_verify",
    "candidate_from_case",
    "workflow_mismatch",
)


@dataclass(frozen=True)
class FinalValidationDecision:
    allowed: bool
    text: str
    reasons: tuple[str, ...]


def validate_final_response(
    *, text: str, contract: Mapping[str, object], effect_receipt: Mapping[str, object]
) -> FinalValidationDecision:
    """Validate text and receipts without searching, rerouting, or model calls."""
    reasons: list[str] = []
    hit = effect_receipt.get("hit")
    answer_shape = str(contract.get("answer_shape") or "")
    product_line = str(contract.get("product_line") or "")

    if hit is not None:
        hit_product = str(getattr(hit, "product_line", "") or "")
        if product_line and hit_product != product_line:
            reasons.append("product_scope_mismatch")
        status = str(getattr(hit, "effective_status", "") or "").lower()
        if status != "active" or status in NON_SERVING_STATUSES:
            reasons.append("non_serving_knowledge")
        if answer_shape == "scalar" and any(
            not str(getattr(hit, field, "") or "").strip() for field in SOURCE_FIELDS
        ):
            reasons.append("formal_source_incomplete")

    pattern = effect_receipt.get("diagnostic_pattern")
    if isinstance(pattern, Mapping):
        pattern_product = str(pattern.get("product_line") or "")
        if product_line and pattern_product != product_line:
            reasons.append("product_scope_mismatch")
        pattern_status = str(pattern.get("effective_status") or "active").lower()
        if pattern_status != "active" or pattern_status in NON_SERVING_STATUSES:
            reasons.append("non_serving_knowledge")
        sources = pattern.get("formal_source_ids")
        required = (
            "document", "version", "path", "locator",
            "source_sha256", "source_record_digest",
        )
        if not isinstance(sources, list) or not sources or any(
            not isinstance(source, Mapping)
            or any(not str(source.get(field) or "").strip() for field in required)
            for source in sources
        ):
            reasons.append("formal_source_incomplete")

    known_products = contract.get("known_product_lines")
    if isinstance(known_products, (list, tuple, set)):
        text_folded = text.casefold()
        if any(
            str(candidate).strip()
            and str(candidate) != product_line
            and str(candidate).casefold() in text_folded
            for candidate in known_products
        ):
            reasons.append("product_text_mismatch")

    if answer_shape in {"scalar", "process", "file", "diagnostic"}:
        if int(effect_receipt.get("model_calls") or 0):
            reasons.append("unexpected_model_call")
        if int(effect_receipt.get("filesystem_scans") or 0):
            reasons.append("unexpected_filesystem_scan")
    if int(effect_receipt.get("index_transactions") or 0) > int(
        contract.get("max_index_transactions") or 0
    ):
        reasons.append("unexpected_index_transaction")
    if any(marker.casefold() in text.casefold() for marker in INTERNAL_MARKERS):
        reasons.append("internal_instruction_leak")
    if not text.strip():
        reasons.append("empty_response")

    return FinalValidationDecision(not reasons, text.strip(), tuple(dict.fromkeys(reasons)))
