"""Model-agnostic decisions for validating a final response before persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class FinalResponseDecision:
    action: str
    response: str
    reasons: tuple[str, ...] = ()
    retry_prompt: str = ""
    error: str = ""


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
    if attempts < max_retries:
        reason_text = ", ".join(reasons) or "workflow_boundary_violation"
        repair_text = " ".join(
            f"{index}. {instruction}"
            for index, instruction in enumerate(
                _repair_instructions(reasons),
                start=1,
            )
        )
        return FinalResponseDecision(
            action="retry",
            response=response,
            reasons=reasons,
            retry_prompt=(
                "[System validation: the proposed answer cannot be persisted or sent. "
                f"Violations: {reason_text}. Repair instructions: {repair_text} "
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
