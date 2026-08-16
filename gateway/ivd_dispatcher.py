"""Immutable, package-only dispatch for Hermes IVD answer turns."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, Protocol


_VOCABULARY_MEMBER = "indexes/dispatch-vocabulary-v1.json"
_MANIFEST_MEMBER = "package-manifest.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
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
        root_fd = self._open_directory_tree(root, "dispatch vocabulary package")
        try:
            manifest_bytes = self._read_regular_at(
                root_fd, _MANIFEST_MEMBER, "package manifest"
            )
            indexes_fd = self._open_directory_at(
                root_fd, "indexes", "dispatch vocabulary indexes"
            )
            try:
                vocabulary_bytes = self._read_regular_at(
                    indexes_fd,
                    "dispatch-vocabulary-v1.json",
                    "dispatch vocabulary member",
                )
            finally:
                os.close(indexes_fd)
        finally:
            os.close(root_fd)
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("package manifest invalid") from error
        if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
            raise ValueError("package manifest invalid")
        members = manifest.get("members")
        if not isinstance(members, Mapping) or _VOCABULARY_MEMBER not in members:
            raise ValueError("dispatch vocabulary member not declared")
        expected_digest = members[_VOCABULARY_MEMBER]
        if not isinstance(expected_digest, str) or not _SHA256_RE.fullmatch(expected_digest):
            raise ValueError("dispatch vocabulary digest invalid")
        if manifest.get("member_digest_algorithm") != "sha256-canonical-members-v1":
            raise ValueError("package digest algorithm invalid")
        package_digest = manifest.get("package_digest")
        canonical_members = json.dumps(
            {
                "algorithm": "sha256-canonical-members-v1",
                "members": dict(members),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if (
            not isinstance(package_digest, str)
            or not _SHA256_RE.fullmatch(package_digest)
            or hashlib.sha256(canonical_members).hexdigest() != package_digest
        ):
            raise ValueError("package digest mismatch")
        if hashlib.sha256(vocabulary_bytes).hexdigest() != expected_digest:
            raise ValueError("dispatch vocabulary digest mismatch")
        try:
            policy = json.loads(vocabulary_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
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
        if not self._product_question or not _CJK_RE.search(self._product_question):
            raise ValueError("dispatch vocabulary product clarification must be Chinese")
        self._products = self._validate_products(policy.get("product_aliases"))
        self._stages = self._validate_stages(policy.get("workflow_stages"))

    @staticmethod
    def _open_directory_tree(path: Path, label: str) -> int:
        absolute = Path(os.path.abspath(path))
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = None
        try:
            descriptor = os.open(absolute.anchor or os.sep, flags)
            for part in absolute.parts[1:]:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
        except OSError as error:
            raise ValueError(f"{label} invalid") from error
        assert descriptor is not None
        return descriptor

    @staticmethod
    def _open_directory_at(parent_fd: int, name: str, label: str) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            return os.open(name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise ValueError(f"{label} invalid") from error

    @staticmethod
    def _read_regular_at(parent_fd: int, name: str, label: str) -> bytes:
        if not name or name in {".", ".."} or "/" in name:
            raise ValueError(f"{label} path invalid")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise ValueError(f"{label} invalid") from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"{label} invalid")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        except OSError as error:
            raise ValueError(f"{label} invalid") from error
        finally:
            os.close(descriptor)

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

    def dispatch(self, question: str, *, context: str = "") -> DecisionEnvelope:
        text = str(question or "").strip()
        routing_text = f"{context} {text}".strip()
        operation = self._match_intent(text, self._operations)
        product_line, product_variant, product_conflict = self._match_product(
            routing_text,
            excluded_scopes=self._platform_scopes if operation is not None else frozenset(),
        )
        if product_conflict:
            current_line, current_variant, _ = self._match_product(
                text,
                excluded_scopes=self._platform_scopes if operation is not None else frozenset(),
            )
            if current_line is not None:
                product_line, product_variant, product_conflict = (
                    current_line,
                    current_variant,
                    False,
                )
        semantic = self._match_intent(text, self._semantics) or self._match_intent(
            routing_text, self._semantics
        )
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
            workflow_stage=self._workflow_stage(routing_text),
            knowledge_type=str(selected["knowledge_type"]),
            risk_class=str(selected["risk_class"]),
            answer_shape="clarification" if ambiguous else str(selected["answer_shape"]),
            ambiguities=("product_line",) if ambiguous else (),
            clarifying_questions=(self._product_question,) if ambiguous else (),
            indexed_retrieval_budget=0 if ambiguous else 1,
            model_call_budget=0,
            envelope_count=1,
            metadata=MappingProxyType({"resolution_order": _RESOLUTION_ORDER}),
        )

    def execute(
        self,
        engine: _KnowledgeEngine,
        *,
        question: str,
        context: str = "",
        evidence: Mapping[str, object] | None = None,
    ) -> DispatchOutcome:
        envelope = self.dispatch(question, context=context)
        if envelope.clarifying_questions:
            return DispatchOutcome(envelope=envelope, result=None)
        result = engine.execute(
            question=question,
            product_line=envelope.product_line or "",
            product_variant=envelope.product_variant or "",
            workflow_stage=envelope.workflow_stage or "",
            knowledge_type=envelope.knowledge_type,
            answer_shape=envelope.answer_shape,
            evidence=dict(evidence or {}),
            allow_index_transaction=envelope.indexed_retrieval_budget > 0,
        )
        return DispatchOutcome(envelope=envelope, result=result)
