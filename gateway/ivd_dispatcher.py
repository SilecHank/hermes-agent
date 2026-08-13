"""Immutable, package-only dispatch for Hermes IVD answer turns."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, Protocol


_VOCABULARY_MEMBER = "indexes/dispatch-vocabulary-v1.json"
_RESOLUTION_ORDER = (
    "operation_intent",
    "product_identity",
    "semantic_intent",
    "answer_shape",
)
_MATCH_MODES = frozenset({"contains", "regex", "token"})


@dataclass(frozen=True)
class DecisionEnvelope:
    """One complete dispatch decision that cannot be rewritten in-place."""

    policy_version: str
    intent: str
    product_line: str | None
    product_variant: str | None
    workflow_stage: str | None
    knowledge_type: str
    risk_class: str
    answer_shape: str
    ambiguities: tuple[str, ...]
    clarifying_questions: tuple[str, ...]
    indexed_retrieval_budget: int
    model_call_budget: int
    envelope_count: int
    metadata: Mapping[str, object]


@dataclass(frozen=True)
class DispatchOutcome:
    envelope: DecisionEnvelope
    result: object | None


class _KnowledgeEngine(Protocol):
    def execute(self, **arguments: object) -> object: ...


def _matches(text: str, alias: str, mode: str) -> bool:
    if mode == "regex":
        return bool(re.search(alias, text, re.IGNORECASE))
    if mode == "token" and re.fullmatch(r"[a-z0-9_.+\- ]+", alias, re.IGNORECASE):
        return bool(
            re.search(
                rf"(?<![a-z0-9]){re.escape(alias.casefold())}(?![a-z0-9])",
                text.casefold(),
            )
        )
    return alias.casefold() in text.casefold()


def _validate_alias(alias: object) -> dict[str, str]:
    if not isinstance(alias, Mapping):
        raise ValueError("dispatch vocabulary alias invalid")
    value = str(alias.get("alias") or "").strip()
    mode = str(alias.get("match_mode") or "").strip()
    if not value or mode not in _MATCH_MODES:
        raise ValueError("dispatch vocabulary alias invalid")
    if mode == "regex":
        try:
            re.compile(value, re.IGNORECASE)
        except re.error as error:
            raise ValueError("dispatch vocabulary regex invalid") from error
    return {"alias": value, "match_mode": mode}


def _validate_intents(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise ValueError("dispatch vocabulary intents invalid")
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("dispatch vocabulary intent invalid")
        intent = str(item.get("intent") or "").strip()
        knowledge_type = str(item.get("knowledge_type") or "").strip()
        risk_class = str(item.get("risk_class") or "").strip()
        answer_shape = str(item.get("answer_shape") or "").strip()
        aliases = tuple(_validate_alias(alias) for alias in item.get("aliases", []))
        if not all((intent, knowledge_type, risk_class, answer_shape)):
            raise ValueError("dispatch vocabulary intent invalid")
        result.append(
            {
                "intent": intent,
                "priority": int(item.get("priority", 100)),
                "knowledge_type": knowledge_type,
                "risk_class": risk_class,
                "answer_shape": answer_shape,
                "aliases": aliases,
            }
        )
    return tuple(sorted(result, key=lambda item: (item["priority"], item["intent"])))


class IVDDispatcher:
    """Resolve exactly one decision from an immutable serving-package member."""

    def __init__(self, serving_package: str | Path) -> None:
        root = Path(serving_package)
        if not root.is_dir() or root.is_symlink():
            raise ValueError("dispatch vocabulary package invalid")
        pure = PurePosixPath(_VOCABULARY_MEMBER)
        member = root.joinpath(*pure.parts)
        ancestors = [root.joinpath(*pure.parts[:index]) for index in range(1, len(pure.parts))]
        if member.is_symlink() or any(path.is_symlink() for path in ancestors) or not member.is_file():
            raise ValueError("dispatch vocabulary member invalid")
        try:
            policy = json.loads(member.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("dispatch vocabulary invalid") from error
        if not isinstance(policy, Mapping):
            raise ValueError("dispatch vocabulary invalid")
        if policy.get("schema_version") != 1 or policy.get("policy_version") != "hermes-dispatch-v1":
            raise ValueError("dispatch vocabulary schema invalid")
        if tuple(policy.get("resolution_order", ())) != _RESOLUTION_ORDER:
            raise ValueError("dispatch vocabulary resolution invalid")

        self._policy_version = str(policy["policy_version"])
        self._operations = _validate_intents(policy.get("operation_intents"))
        self._semantics = _validate_intents(policy.get("semantic_intents"))
        if not any(item["intent"] == "product_fact" for item in self._semantics):
            raise ValueError("dispatch vocabulary default intent missing")
        self._platform_scopes = frozenset(map(str, policy.get("operation_platform_scopes", [])))
        self._product_required = frozenset(map(str, policy.get("product_required_intents", [])))
        clarifications = policy.get("clarifications")
        if not isinstance(clarifications, Mapping):
            raise ValueError("dispatch vocabulary clarifications invalid")
        self._product_question = str(clarifications.get("product_line") or "").strip()
        if not self._product_question:
            raise ValueError("dispatch vocabulary product clarification missing")
        self._products = self._validate_products(policy.get("product_aliases"))
        self._stages = self._validate_stages(policy.get("workflow_stages"))

    @staticmethod
    def _validate_products(value: object) -> tuple[dict[str, object], ...]:
        if not isinstance(value, list):
            raise ValueError("dispatch vocabulary products invalid")
        products: list[dict[str, object]] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise ValueError("dispatch vocabulary product invalid")
            product_line = str(item.get("product_line") or "").strip()
            alias = _validate_alias(item)
            if not product_line:
                raise ValueError("dispatch vocabulary product invalid")
            products.append(
                {
                    **alias,
                    "product_line": product_line,
                    "product_variant": (
                        str(item["product_variant"]).strip()
                        if item.get("product_variant") is not None
                        else None
                    ),
                }
            )
        return tuple(products)

    @staticmethod
    def _validate_stages(value: object) -> tuple[dict[str, object], ...]:
        if not isinstance(value, list):
            raise ValueError("dispatch vocabulary workflow stages invalid")
        stages: list[dict[str, object]] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise ValueError("dispatch vocabulary workflow stage invalid")
            stage = str(item.get("stage") or "").strip()
            aliases = tuple(str(alias).strip() for alias in item.get("aliases", []) if str(alias).strip())
            if not stage:
                raise ValueError("dispatch vocabulary workflow stage invalid")
            stages.append({"stage": stage, "aliases": aliases})
        return tuple(stages)

    @staticmethod
    def _match_intent(text: str, groups: tuple[dict[str, object], ...]) -> dict[str, object] | None:
        for group in groups:
            aliases = group["aliases"]
            if any(_matches(text, alias["alias"], alias["match_mode"]) for alias in aliases):
                return group
        return None

    def _match_product(
        self, text: str, *, excluded_scopes: frozenset[str]
    ) -> tuple[str | None, str | None, bool]:
        matches = [
            item
            for item in self._products
            if item["product_line"] not in excluded_scopes
            and _matches(text, str(item["alias"]), str(item["match_mode"]))
        ]
        identities = {
            (
                str(item["product_line"]),
                str(item["product_variant"])
                if item["product_variant"] is not None
                else None,
            )
            for item in matches
        }
        if len(identities) > 1:
            return None, None, True
        if not matches:
            return None, None, False
        selected = matches[0]
        return (
            str(selected["product_line"]),
            str(selected["product_variant"]) if selected["product_variant"] is not None else None,
            False,
        )

    def _workflow_stage(self, text: str) -> str | None:
        for item in self._stages:
            if any(alias.casefold() in text.casefold() for alias in item["aliases"]):
                return str(item["stage"])
        return None

    def dispatch(self, question: str) -> DecisionEnvelope:
        text = str(question or "").strip()
        operation = self._match_intent(text, self._operations)
        product_line, product_variant, product_conflict = self._match_product(
            text,
            excluded_scopes=self._platform_scopes if operation is not None else frozenset(),
        )
        semantic = self._match_intent(text, self._semantics)
        selected = operation or semantic or next(
            item for item in self._semantics if item["intent"] == "product_fact"
        )
        intent = str(selected["intent"])
        needs_product = intent in self._product_required and product_line is None
        ambiguous = product_conflict or needs_product
        return DecisionEnvelope(
            policy_version=self._policy_version,
            intent=intent,
            product_line=None if product_conflict else product_line,
            product_variant=None if product_conflict else product_variant,
            workflow_stage=self._workflow_stage(text),
            knowledge_type=str(selected["knowledge_type"]),
            risk_class=str(selected["risk_class"]),
            answer_shape="clarification" if ambiguous else str(selected["answer_shape"]),
            ambiguities=("product_line",) if ambiguous else (),
            clarifying_questions=(self._product_question,) if ambiguous else (),
            indexed_retrieval_budget=0,
            model_call_budget=0,
            envelope_count=1,
            metadata=MappingProxyType({"resolution_order": _RESOLUTION_ORDER}),
        )

    def execute(
        self,
        engine: _KnowledgeEngine,
        *,
        question: str,
        evidence: Mapping[str, object] | None = None,
    ) -> DispatchOutcome:
        envelope = self.dispatch(question)
        if envelope.clarifying_questions:
            return DispatchOutcome(envelope=envelope, result=None)
        result = engine.execute(
            question=question,
            product_line=envelope.product_line or "",
            evidence=dict(evidence or {}),
            allow_index_transaction=False,
        )
        return DispatchOutcome(envelope=envelope, result=result)
