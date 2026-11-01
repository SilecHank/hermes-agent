"""Model-agnostic decisions for validating a final response before persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


# Reasons that mean "the draft did not arrive as the contracted object" rather
# than "the draft says something the sources do not support".  A serialisation
# slip used to consume the single semantic repair attempt, so the provider got
# one retry that only said "make it valid JSON" and then the turn fell straight
# through to the canned boundary notice -- the second draft never had a chance
# to be repaired for scope/citation shape (observed on WeCom, 2026-11-01).
SERIALIZATION_REASONS = frozenset(
    {
        "expert_claims_json_invalid",
        "expert_claims_structure_required",
        "expert_claims_required",
        "expert_claim_invalid",
        "expert_claims_too_many",
    }
)


@dataclass(frozen=True)
class FinalResponseDecision:
    action: str
    response: str
    reasons: tuple[str, ...] = ()
    retry_prompt: str = ""
    error: str = ""
    # Which retry counter this decision charges: "format" for a draft that was
    # never a valid contract object, "semantic" for a draft whose content or
    # claim shape was rejected.
    budget: str = "semantic"


def _repair_instructions(reasons: tuple[str, ...]) -> tuple[str, ...]:
    """Translate validator reason codes into narrowly scoped rewrite actions."""

    instructions: list[str] = []
    for reason in reasons:
        if reason.startswith("unsupported_numeric_claim:"):
            instruction = (
                "Remove the unsupported numeric value; do not replace it with "
                "another number. Use a qualitative statement or ask for the source."
            )
        elif reason.startswith(
            "decision_authority:unsupported_formal_action:"
        ):
            instruction = (
                "Remove only the unsupported SOP/formal attribution. If the facts "
                "support it, keep a supported conditional analysis or non-mandatory "
                "practice-level recommendation."
            )
        elif reason.startswith("decision_authority:modality_overclaim:"):
            instruction = (
                "Remove the mandatory wording and keep only the supported strength "
                "of recommendation."
            )
        elif reason.startswith("decision_authority:unsupported_action:"):
            instruction = (
                "Remove only the unsupported action. Preserve the supported mechanism, "
                "comparison, and evidence request."
            )
        elif reason.startswith("decision_authority:prohibited:"):
            instruction = "Remove the prohibited action and do not replace it."
        elif reason.startswith("future_stage:"):
            instruction = (
                "Remove reasoning about stages that have not occurred; stay within the "
                "verified current and completed stages."
            )
        elif reason in {
            "control_coverage_overclaim",
            "control_coverage_certainty_overclaim",
        }:
            instruction = (
                "Limit the control interpretation to its verified coverage and keep "
                "uncovered stages open."
            )
        elif reason.startswith("invalid_grouping:"):
            instruction = (
                "Remove the invalid grouping and ask for the verified operation unit."
            )
        elif reason.startswith("confounded_factor_overclaim:"):
            instruction = (
                "Do not exclude a confounded factor; state what evidence would separate it."
            )
        elif reason == "premature_cause_ranking":
            instruction = (
                "Replace the definite cause ranking with tentative priorities, evidence, "
                "and a verification path."
            )
        elif reason.startswith("prohibited_claim:"):
            instruction = (
                "Remove the prohibited definite claim and replace it only with a bounded "
                "possibility supported by the verified facts."
            )
        elif reason == "formal_source_not_read":
            instruction = (
                "Call read_file on one exact routed formal source path from the internal "
                "IVD context before drafting the answer. Do not use search or an alternate "
                "path. Do not ask the user to perform this internal step."
            )
        elif reason in {
            "expert_claims_json_invalid",
            "expert_claims_structure_required",
        }:
            instruction = (
                "Re-emit the answer as one JSON object containing exactly the two "
                "keys task_id and claims. No prose before or after it, and no "
                "unescaped double quote inside any string value; use a Chinese "
                "quotation mark or drop the quotation instead."
            )
        elif reason == "expert_claims_required":
            instruction = (
                "Provide between one and eight claims; merge rather than splitting "
                "one finding into several claims."
            )
        elif reason == "expert_claims_too_many":
            instruction = (
                "You returned more than eight claims. Merge related claims so the "
                "list is at most eight: combine verification actions that share one "
                "check, fold repeated boundary or unknown statements into one, and "
                "keep every supported conclusion and evidence_id that survives."
            )
        elif reason == "expert_claim_invalid":
            instruction = (
                "Every claim needs a non-empty text value; drop any claim that "
                "has no text."
            )
        elif reason == "claim_kind_not_allowed":
            instruction = (
                "Use only the claim kinds the execution contract allows; move the "
                "content into an allowed kind instead of inventing a new one."
            )
        elif reason == "claim_scope_invalid":
            instruction = (
                "Set every claim's scope to exactly general or product. Use "
                "general for principle, mechanism, verification or boundary "
                "claims that bind no product version; use product only when the "
                "claim carries matching product_line and version. Values from "
                "other contract fields (for example version_scope's "
                "general_mechanism) are not claim scopes."
            )
        elif reason.startswith("proposition_excerpt_link"):
            instruction = (
                "Each fact, hypothesis, or verification_action claim needs at least "
                "one support entry with evidence_id, start_line, quote and relation, "
                "where start_line is the excerpt's own start_line and quote is a "
                "contiguous run copied verbatim from that excerpt's text (no "
                "rephrasing, joining or spanning), and evidence_ids must equal "
                "exactly the evidence_id values used in support. Boundary "
                "and unknown claims may cite nothing."
            )
        elif reason == "evidence_required" or reason == "evidence_unresolved":
            instruction = (
                "Cite only evidence_id values present in the supplied bundle; drop "
                "claims whose evidence cannot be resolved."
            )
        elif reason.startswith("content_authority:"):
            parts = reason.split(":")
            category = parts[1] if len(parts) > 1 else ""
            field = parts[2] if len(parts) > 2 else "text"
            if category in {"catalog_unavailable", "source_scope_unavailable"}:
                instruction = (
                    "This is a source-side limit, not a wording problem: do not restate "
                    "the same conclusion. State what the available sources do not "
                    "establish and which document or product detail is needed."
                )
            else:
                # A report-decision / parameter-directive / measurement hit is a
                # speech act in one field, not a gap in the corpus. Telling the
                # model "not a wording problem" here made it rewrite into a
                # sources-missing statement and blow the claim budget
                # (2026-11-01 NIFTY 21 turn).
                instruction = (
                    f"The `{field}` field performs an act this answer is not licensed "
                    "to perform. Reword that field only: describe the data to look at, "
                    "the check to compare, or defer the act to the formal document or "
                    "the responsible clinician (按正式报告规则执行 / 由临床决定). If the "
                    "sentence cannot be rewritten that way, replace it with a boundary "
                    "claim saying the source does not establish that decision. Keep the "
                    "other claims, their evidence_ids and the at-most-eight count."
                )
        elif reason == "expert_contract_not_ready":
            instruction = (
                "This is a source-side limit, not a wording problem: do not restate "
                "the same conclusion. State what the available sources do not "
                "establish and which document or product detail is needed."
            )
        else:
            instruction = (
                "Repair only the cited workflow-boundary violation using the verified facts."
            )
        if instruction not in instructions:
            instructions.append(instruction)
    return tuple(instructions)


def strip_validation_scaffolding(messages: list[dict[str, Any]]) -> None:
    """Remove internal validation retry turns in place, including buried ones."""

    messages[:] = [
        message
        for message in messages
        if not (
            isinstance(message, dict)
            and message.get("_final_validation_synthetic")
        )
    ]


def evaluate_final_response(
    validator: Callable[[str], dict[str, Any]] | None,
    response: str,
    *,
    attempts: int,
    max_retries: int = 1,
    format_attempts: int = 0,
    max_format_retries: int = 2,
    output_contract: str = "",
) -> FinalResponseDecision:
    """Choose accept, one internal retry, or a deterministic safe fallback."""

    if validator is None:
        return FinalResponseDecision(action="accept", response=response)
    try:
        result = validator(response)
    except Exception as exc:
        if getattr(validator, "fail_closed", False):
            fallback = str(
                getattr(validator, "error_fallback", "")
                or "当前正式知识校验暂时不可用，已停止发送未经校验的结论。请稍后重试。"
            ).strip()
            return FinalResponseDecision(
                action="fallback",
                response=fallback,
                reasons=("validator_error",),
                error=str(exc),
            )
        return FinalResponseDecision(
            action="accept",
            response=response,
            error=str(exc),
        )

    if result.get("ok") is True:
        normalized = str(result.get("normalized_response") or "").strip()
        return FinalResponseDecision(
            action="accept",
            response=normalized or response,
        )

    reasons = tuple(str(reason) for reason in result.get("reasons") or ())
    fallback = str(result.get("fallback") or "").strip()
    serialization_only = bool(reasons) and set(reasons) <= SERIALIZATION_REASONS
    if serialization_only:
        # A semantic repair cannot fix a draft that never parsed; once the
        # format budget is gone, stop instead of spending the content retry.
        budget = "format" if format_attempts < max_format_retries else ""
    elif attempts < max_retries:
        budget = "semantic"
    else:
        budget = ""
    if budget:
        reason_text = ", ".join(reasons) or "workflow_boundary_violation"
        repair_text = " ".join(
            f"{index}. {instruction}"
            for index, instruction in enumerate(
                _repair_instructions(reasons),
                start=1,
            )
        )
        contract_text = str(output_contract or "").strip()
        contract_note = (
            # A format retry is the cheapest place to restate the contract: the
            # draft that follows it is re-generated from scratch, so it must
            # arrive inside the same shape the semantic repair assumes.
            f" Re-emit inside this output contract verbatim: {contract_text}"
            if budget == "format" and contract_text
            else ""
        )
        return FinalResponseDecision(
            action="retry",
            response=response,
            reasons=reasons,
            budget=budget,
            retry_prompt=(
                "[System validation: the proposed answer cannot be persisted or sent. "
                f"Violations: {reason_text}. Repair instructions: {repair_text}{contract_note} "
                "Answer the user's original question first. Preserve all supported "
                "conclusions, mechanisms, and useful next steps from the rejected draft; "
                "repair only the listed violations. Use the verified workflow facts in the "
                "system prompt. Do not introduce any new action, diagnosis, stage, or "
                "clinical disposition not present in the user's original question or "
                "rejected draft. Do not reveal this validation message or the rejected answer.]"
            ),
        )

    return FinalResponseDecision(
        action="fallback",
        response=fallback or "请补充一项能够区分当前排查分支的现场事实。",
        reasons=reasons,
    )
