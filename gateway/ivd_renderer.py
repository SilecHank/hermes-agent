"""Pure, bounded rendering for immutable IVD serving-package results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class SourceReference:
    document: str
    version: str
    locator: str
    path: str
    sha256: str
    record_digest: str


@dataclass(frozen=True)
class RenderedAnswer:
    text: str
    answer_shape: str
    source: SourceReference | None = None


def source_reference(hit: object) -> SourceReference:
    return SourceReference(
        document=str(getattr(hit, "source_document_id", "") or ""),
        version=str(getattr(hit, "source_version", "") or ""),
        locator=str(getattr(hit, "source_locator", "") or ""),
        path=str(getattr(hit, "source_path", "") or ""),
        sha256=str(getattr(hit, "source_sha256", "") or ""),
        record_digest=str(getattr(hit, "source_record_digest", "") or ""),
    )


def source_reference_from_mapping(source: Mapping[str, object]) -> SourceReference:
    return SourceReference(
        document=str(source.get("document") or ""),
        version=str(source.get("version") or ""),
        locator=str(source.get("locator") or ""),
        path=str(source.get("path") or ""),
        sha256=str(source.get("source_sha256") or ""),
        record_digest=str(source.get("source_record_digest") or ""),
    )


class IVDRenderer:
    """Render only the answer shape selected by the deterministic engine."""

    def __init__(self, policy: Mapping[str, object]) -> None:
        if policy.get("schema_version") != 1:
            raise ValueError("render policy schema invalid")
        if policy.get("render_mode") != "strict":
            raise ValueError("render policy must be strict")
        self._policy = dict(policy)

    @property
    def fallback_request(self) -> str:
        return str(
            self._policy.get("fallback_request")
            or ""
        )

    def render_registry_hit(self, hit: object) -> RenderedAnswer:
        kind = str(getattr(hit, "knowledge_kind", ""))
        value = str(getattr(hit, "value", "") or "").strip()
        unit = str(getattr(hit, "unit", "") or "").strip()
        shape = {
            "parameter": "scalar",
            "process_fact": "process",
            "file": "file",
            "report_rule": "rule",
            "contact": "contact",
            "evidence": "evidence",
        }.get(kind)
        if shape is None or not value:
            raise ValueError("unsupported registry hit")
        if shape == "scalar":
            template = str(self._policy.get("scalar_template") or "{value} {unit}.")
            text = template.format(value=value, unit=unit).replace("  ", " ").strip()
        else:
            text = value
        return RenderedAnswer(text=text, answer_shape=shape, source=source_reference(hit))

    def render_diagnostic(self, evaluation: Mapping[str, object]) -> RenderedAnswer:
        outcome = str(evaluation.get("outcome") or "")
        if outcome == "direction":
            parts = [
                str(evaluation.get("first_direction") or "").strip(),
                str(evaluation.get("recommended_action") or "").strip(),
            ]
            text = "\n".join(part for part in parts if part)
        elif outcome == "needs_discriminator":
            questions = evaluation.get("questions")
            text = str(questions[0]).strip() if isinstance(questions, list) and questions else ""
        elif outcome == "stopped":
            text = "当前已满足停止条件，暂不继续扩展排查。"
        elif outcome == "recovered":
            text = "当前已满足恢复条件，无需继续扩展排查。"
        else:
            raise ValueError("unsupported diagnostic outcome")
        if not text:
            raise ValueError("diagnostic render text missing")
        return RenderedAnswer(text=text, answer_shape="diagnostic")

    def render_fallback(self) -> RenderedAnswer:
        return RenderedAnswer(text=self.fallback_request, answer_shape="fallback_request")
