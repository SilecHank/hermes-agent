"""Validated service boundary for rebuildable IVD facts."""

from __future__ import annotations

from typing import Any, Iterable


ALLOWED_TEMPLATES = {
    "{value} {unit}。",
    "{value}。",
}
ALLOWED_STATUSES = {"active"}
ALLOWED_QUESTION_TYPES = {"scalar_lookup", "direct_fact"}


class VerifiedFactService:
    def __init__(self, session_db: Any):
        self.session_db = session_db

    def activate(self, record: dict[str, Any]) -> bool:
        if not self._valid_record(record):
            return False
        normalized = dict(record)
        normalized["conditions"] = sorted(str(item) for item in record.get("conditions") or [])
        normalized["status"] = "active"
        try:
            return bool(self.session_db.upsert_ivd_verified_fact(normalized))
        except Exception:
            return False

    def lookup(
        self,
        *,
        product_scope: str,
        product_variant: str,
        fact_key: str,
        conditions: list[str],
        source_revisions: dict[str, str],
    ) -> dict[str, Any] | None:
        if not product_scope or not fact_key or not source_revisions:
            return None
        try:
            record = self.session_db.find_ivd_verified_fact(
                product_scope=product_scope,
                product_variant=product_variant or "",
                fact_key=fact_key,
                conditions=sorted(str(item) for item in conditions or []),
                source_revisions=source_revisions,
            )
        except Exception:
            return None
        if not record or record.get("status") not in ALLOWED_STATUSES:
            return None
        template = str(record.get("answer_template") or "")
        if template not in ALLOWED_TEMPLATES:
            return None
        rendered = template.format(value=record["value"], unit=record.get("unit", ""))
        result = dict(record)
        result["rendered_answer"] = " ".join(rendered.split())
        return result

    def mark_for_revalidation(self, changed_paths: Iterable[str]) -> int:
        try:
            return int(self.session_db.mark_ivd_facts_for_revalidation(changed_paths))
        except Exception:
            return 0

    def revoke(self, fact_id: str, reason: str) -> bool:
        try:
            return bool(self.session_db.revoke_ivd_verified_fact(fact_id, reason))
        except Exception:
            return False

    @staticmethod
    def _valid_record(record: dict[str, Any]) -> bool:
        required = (
            "fact_id",
            "product_scope",
            "question_type",
            "fact_key",
            "value",
            "answer_template",
            "evidence",
            "source_revision",
        )
        if any(not record.get(key) for key in required):
            return False
        if record.get("question_type") not in ALLOWED_QUESTION_TYPES:
            return False
        if record.get("answer_template") not in ALLOWED_TEMPLATES:
            return False
        evidence = record.get("evidence") or []
        return bool(evidence) and all(
            item.get("evidence_id") and item.get("source_path") and item.get("source_revision")
            for item in evidence
        )
