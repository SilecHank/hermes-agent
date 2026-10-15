"""Pure final validation for deterministic IVD package responses."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Mapping


METHOD_TERMS = (
    (r"手工|手动|\bmanual(?:_\w+)?\b", "manual"),
    (r"自动(?:化)?|纳磁|\bautomatic(?:_\w+)?\b|\bautomated(?:_\w+)?\b", "automatic"),
)


def confirmed_method_families(text: str) -> set[str]:
    """Recognize a current assertion/correction, never a mere method mention.

    The existing two-family vocabulary is unchanged. A method question,
    uncertainty, alternative, or contradictory assertion fails closed.
    """
    text = str(text or "")
    uncertainty = r"不确定|不清楚|不知道|不能确定|不能确认|无法确定|无法确认|未能确定|未能确认|没(?:有)?(?:确认|确定)|未确认|未确定|尚未确认|待(?:确认|确定)|不一定|应该|疑似|似乎|可能|也许|大概|是否|能否|可否|是不是|能不能|可不可以|吗|么"
    method_pattern = "(?:" + "|".join(pattern for pattern, _ in METHOD_TERMS) + ")"
    uncertainty_spans = list(re.finditer(uncertainty, text))
    resets = []
    for match in re.finditer(r"(?:已确认|确认是|确定是|明确是|实际是|改成|改为|改用|而是)\s*" + method_pattern, text, re.I):
        prefix = re.split(r"[，,。；;！!\n？?]", text[:match.start()])[-1]
        inside_doubt = any(span.start() <= match.start() < span.end() for span in uncertainty_spans)
        if not inside_doubt and not re.search(uncertainty, prefix) and not re.search(r"不|没|未|非|无法|不能", prefix):
            resets.append(match.start())
    quantity_scope = r"多少|用量|体积|浓度|时间|温度|多久|投入|循环|阈值|参数|毫升|微升|\b[µuμm]L\b"
    doubts = []
    for part in re.finditer(r"[^，,。；;！!\n？?]+", text):
        clause = part.group()
        for doubt in re.finditer(uncertainty, clause):
            following = clause[doubt.end():]
            next_method = re.search(method_pattern, following, re.I)
            next_quantity = re.search(quantity_scope, following, re.I)
            prior_methods = list(re.finditer(method_pattern, clause[:doubt.start()], re.I))
            prior_quantities = list(re.finditer(quantity_scope, clause[:doubt.start()], re.I))
            numeric_doubt = bool(next_quantity and (not next_method or next_quantity.start() < next_method.start()))
            numeric_doubt = numeric_doubt or bool(
                not next_method and prior_quantities
                and (not prior_methods or prior_quantities[-1].start() > prior_methods[-1].start())
            )
            if not numeric_doubt:
                doubts.append(part.start() + doubt.start())
    if doubts and (not resets or doubts[-1] > resets[-1]):
        return set()
    if resets:
        text = text[resets[-1]:]
    asserted: set[str] = set()
    denied: set[str] = set()
    pieces = re.split(r"([，,。；;！!\n？?])", text)
    for position in range(0, len(pieces), 2):
        clause = pieces[position]
        terminator = pieces[position + 1] if position + 1 < len(pieces) else ""
        occurrences = sorted(
            (match.start(), match.end(), family)
            for pattern, family in METHOD_TERMS
            for match in re.finditer(pattern, clause, re.I)
        )
        if not occurrences:
            continue
        if terminator in {"?", "？"} and not re.search(quantity_scope, clause, re.I):
            return set()
        if len({family for _, _, family in occurrences}) > 1 and re.search(r"和|与|及|/|、|或|还是", clause):
            return set()
        if re.search(r"(?:或(?:者)?|还是)\s*(?:其他|其它|别的|某种).{0,4}(?:方法|方式)", clause):
            return set()
        previous_end = 0
        for index, (start, end, family) in enumerate(occurrences):
            prefix = clause[previous_end:start]
            if re.search(r"(?:或(?:者)?|还是)\s*$", prefix):
                return set()
            next_start = occurrences[index + 1][0] if index + 1 < len(occurrences) else len(clause)
            suffix = clause[end:next_start]
            negated = bool(re.search(r"不是|并非|(?:不|未)(?:采用|使用|做)|无[须需]|不用|不要|不能|非", prefix))
            negated = negated or bool(re.match(r"\s*(?:方法|方式)?\s*(?:不是|不适用|不可用|不行|未确认)", suffix))
            replaced = index + 1 < len(occurrences) and bool(re.search(r"而是|改用|改为|改成", suffix))
            if negated:
                denied.add(family)
            elif not replaced:
                asserted.add(family)
            previous_end = end
    if len(asserted) != 1 or asserted & denied:
        return set()
    return asserted


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
        if str(getattr(hit, "knowledge_kind", "")) == "parameter":
            try:
                conditions = json.loads(str(getattr(hit, "conditions_json", "{}")))
            except (ValueError, TypeError):
                conditions = None
            if not isinstance(conditions, Mapping):
                reasons.append("parameter_conditions_invalid")
            else:
                required = {family for pattern, family in METHOD_TERMS
                            if re.search(pattern, str(conditions.get("method") or ""), re.I)}
                requested = confirmed_method_families(str(contract.get("question") or ""))
                if required and not requested:
                    reasons.append("method_scope_required")
                elif required and required != requested:
                    reasons.append("method_scope_mismatch")
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
