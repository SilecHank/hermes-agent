"""Stable runtime contract for answers resolved from formal cards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ResolvedAnswer:
    """A deterministic answer that may bypass model generation."""

    text: str
    source_kind: str
    card_id: str = ""
    answer_shape: str = ""
    product_scope: str = ""
    product_variant: str = ""
    source_path: str = ""
    source_document_id: str = ""
    source_version: str = ""
    source_locator: str = ""
    evidence_ids: tuple[str, ...] = ()

    @classmethod
    def from_fast_result(cls, result: Mapping[str, Any]) -> "ResolvedAnswer | None":
        text = str(result.get("direct_response") or "").strip()
        card = result.get("answer_card") or {}
        contact = result.get("contact_fact") or {}
        source_kind = str(result.get("route_source") or "")
        if isinstance(contact, Mapping) and contact.get("status") == "resolved":
            source_kind = "contact"
        if not text and isinstance(contact, Mapping) and contact.get("status") == "resolved":
            text = str(contact.get("answer") or "").strip()
            source_kind = "contact"
        if not text and isinstance(card, Mapping) and card.get("stop_after_fast_path"):
            text = str(card.get("answer_text") or "").strip()
            source_kind = "formal_answer_card"
        if not text:
            return None
        if source_kind == "contact_fact_card":
            source_kind = "contact"
        return cls(
            text=text,
            source_kind=source_kind or "resolved_card",
            card_id=str(card.get("fact_key") or result.get("fact_key") or ""),
            answer_shape=str(result.get("answer_shape") or ""),
            product_scope=str(card.get("product_scope") or result.get("product_scope") or ""),
            product_variant=str(card.get("product_variant") or result.get("product_variant") or ""),
            source_path=str(card.get("source_path") or ""),
            source_document_id=str(card.get("source_document_id") or ""),
            source_version=str(card.get("source_version") or ""),
            source_locator=str(card.get("source_locator") or ""),
            evidence_ids=tuple(str(item) for item in result.get("direct_evidence_ids") or () if item),
        )
