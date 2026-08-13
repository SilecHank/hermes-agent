"""Read-only execution of one immutable Hermes IVD serving-package."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping
from urllib.parse import quote
from pathlib import PurePosixPath

from gateway.ivd_final_validator import validate_final_response
from gateway.ivd_renderer import IVDRenderer, SourceReference, source_reference_from_mapping


class PackageIntegrityError(ValueError):
    pass


@dataclass(frozen=True)
class ExecutionResult:
    text: str
    answer_shape: str
    outcome: str
    model_calls: int
    index_transactions: int
    filesystem_scans: int
    effect_count: int
    source: SourceReference | None = None
    sources: tuple[SourceReference, ...] = ()


def _normalize(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "").strip()).rstrip("?？。！!")


class IVDKnowledgeEngine:
    """Execute exact Registry and diagnostic lookups without mutable I/O."""

    _MEMBERS = (
        "database/registry.sqlite",
        "indexes/diagnostic-graph.json",
        "renders/render-policy.json",
    )

    def __init__(self, package_root: str | Path) -> None:
        self._root = Path(package_root)
        if not self._root.is_dir() or self._root.is_symlink():
            raise PackageIntegrityError("serving package root invalid")
        manifest = self._read_json("package-manifest.json")
        if manifest.get("schema_version") != 1:
            raise PackageIntegrityError("package manifest schema invalid")
        members = manifest.get("members")
        if not isinstance(members, Mapping):
            raise PackageIntegrityError("package members missing")
        if any(relative not in members for relative in self._MEMBERS):
            raise PackageIntegrityError("required package member missing")
        for relative, expected in sorted(members.items()):
            if not isinstance(relative, str) or not self._valid_relative(relative):
                raise PackageIntegrityError("member path invalid")
            path = self._member_path(relative)
            if not isinstance(expected, str) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise PackageIntegrityError(f"member digest mismatch: {relative}")
        digest_payload = json.dumps(
            {
                "algorithm": "sha256-canonical-members-v1",
                "members": dict(sorted(members.items())),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(digest_payload).hexdigest() != manifest.get("package_digest"):
            raise PackageIntegrityError("package digest mismatch")

        self._graph = self._read_json("indexes/diagnostic-graph.json")
        self._renderer = IVDRenderer(self._read_json("renders/render-policy.json"))
        database = self._member_path("database/registry.sqlite")
        uri = f"file:{quote(str(database.resolve()))}?mode=ro&immutable=1"
        self._database = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._database.row_factory = sqlite3.Row
        self._database.execute("PRAGMA query_only=ON")
        self._known_product_lines = tuple(
            row[0]
            for row in self._database.execute(
                "SELECT DISTINCT product_line FROM products ORDER BY product_line"
            ).fetchall()
            if row[0]
        )

    def close(self) -> None:
        self._database.close()

    def __enter__(self) -> "IVDKnowledgeEngine":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _member_path(self, relative: str) -> Path:
        if not self._valid_relative(relative):
            raise PackageIntegrityError("member path invalid")
        path = self._root.joinpath(*relative.split("/"))
        relative_path = Path(relative)
        ancestors = [self._root.joinpath(*relative_path.parts[:index]) for index in range(1, len(relative_path.parts))]
        if path.is_symlink() or any(item.is_symlink() for item in ancestors) or not path.is_file():
            raise PackageIntegrityError(f"package member invalid: {relative}")
        if path.resolve().parent != self._root.joinpath(*relative.split("/")[:-1]).resolve():
            raise PackageIntegrityError(f"package member escaped root: {relative}")
        return path

    @staticmethod
    def _valid_relative(relative: str) -> bool:
        pure = PurePosixPath(relative)
        return bool(relative) and not pure.is_absolute() and all(
            part not in {"", ".", ".."} for part in pure.parts
        )

    def _read_json(self, relative: str) -> dict[str, object]:
        path = self._member_path(relative)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PackageIntegrityError(f"package JSON invalid: {relative}") from error
        if not isinstance(value, dict):
            raise PackageIntegrityError(f"package JSON must be object: {relative}")
        return value

    @staticmethod
    def _projection() -> str:
        return """
            SELECT e.entity_id, e.knowledge_kind, p.product_line, p.product_variant,
                   s.source_document_id, v.source_version, l.source_locator,
                   v.source_path, v.source_sha256, v.source_record_digest,
                   e.workflow_stage, e.step_id, e.object_name, e.fact_key,
                   typed.value, typed.unit, e.conditions_json, a.effective_status
            FROM aliases x
            JOIN assertions a ON a.assertion_id=x.assertion_id
            JOIN entities e ON e.entity_id=a.entity_id
            JOIN products p ON p.product_id=e.product_id
            JOIN locators l ON l.locator_id=a.locator_id
            JOIN versions v ON v.version_id=l.version_id
            JOIN sources s ON s.source_id=v.source_id
            JOIN entity_values typed ON typed.assertion_id=a.assertion_id
        """

    @staticmethod
    def _hit(row: sqlite3.Row) -> SimpleNamespace:
        return SimpleNamespace(**dict(row))

    @staticmethod
    def _registry_kinds(knowledge_type: str) -> tuple[str, ...]:
        return {
            "parameter": ("parameter",),
            "process": ("process_fact",),
            "operation": ("process_fact",),
            "file": ("file",),
            "report_rule": ("report_rule",),
            "principle": ("evidence",),
            "evidence": ("evidence",),
        }.get(knowledge_type, ())

    def _exact_registry(
        self,
        question: str,
        product_line: str,
        product_variant: str | None,
        workflow_stage: str,
        knowledge_type: str,
    ) -> SimpleNamespace | None:
        clauses = ["x.alias=?", "a.effective_status='active'"]
        values: list[object] = [question]
        if product_line:
            clauses.append("p.product_line=?")
            values.append(product_line)
        if product_variant is not None:
            clauses.append("p.product_variant=?")
            values.append(product_variant)
        if workflow_stage:
            clauses.append("e.workflow_stage=?")
            values.append(workflow_stage)
        if knowledge_type:
            kinds = self._registry_kinds(knowledge_type)
            if not kinds:
                return None
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"e.knowledge_kind IN ({placeholders})")
            values.extend(kinds)
        rows = self._database.execute(
            self._projection() + " WHERE " + " AND ".join(clauses) + " LIMIT 2", values
        ).fetchall()
        return self._hit(rows[0]) if len(rows) == 1 else None

    def _fts_registry(
        self,
        question: str,
        product_line: str,
        product_variant: str | None,
        workflow_stage: str,
        knowledge_type: str,
    ) -> SimpleNamespace | None:
        tokens = re.findall(r"[^\W_]+", question, flags=re.UNICODE)
        if not tokens or len(tokens) > 16 or len(question) > 256:
            return None
        expression = " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
        clauses = ["aliases_fts MATCH ?", "a.effective_status='active'"]
        values: list[object] = [expression]
        if product_line:
            clauses.append("p.product_line=?")
            values.append(product_line)
        if product_variant is not None:
            clauses.append("p.product_variant=?")
            values.append(product_variant)
        if workflow_stage:
            clauses.append("e.workflow_stage=?")
            values.append(workflow_stage)
        if knowledge_type:
            kinds = self._registry_kinds(knowledge_type)
            if not kinds:
                return None
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"e.knowledge_kind IN ({placeholders})")
            values.extend(kinds)
        rows = self._database.execute(
            """
            SELECT DISTINCT x.alias, x.assertion_id
            FROM aliases_fts x
            JOIN assertions a ON a.assertion_id=x.assertion_id
            JOIN entities e ON e.entity_id=a.entity_id
            JOIN products p ON p.product_id=e.product_id
            WHERE """ + " AND ".join(clauses) + " ORDER BY x.assertion_id LIMIT 2",
            values,
        ).fetchall()
        if len(rows) != 1:
            return None
        return self._exact_registry(
            str(rows[0]["alias"]),
            product_line,
            product_variant,
            workflow_stage,
            knowledge_type,
        )

    def _diagnostic(
        self,
        question: str,
        product_line: str,
        product_variant: str | None,
        workflow_stage: str,
        evidence: Mapping[str, object],
    ) -> dict[str, object] | None:
        service = self._graph.get("service_graph")
        patterns = service.get("patterns") if isinstance(service, Mapping) else None
        if not isinstance(patterns, list):
            return None
        matches = []
        for pattern in patterns:
            if not isinstance(pattern, Mapping):
                continue
            aliases = {_normalize(alias).casefold() for alias in pattern.get("symptom_aliases", [])}
            if _normalize(question).casefold() not in aliases:
                continue
            if product_line and str(pattern.get("product_line") or "") != product_line:
                continue
            if (
                product_variant is not None
                and str(pattern.get("product_variant") or "") != product_variant
            ):
                continue
            pattern_stage = str(pattern.get("workflow_stage") or "")
            if workflow_stage and pattern_stage and pattern_stage != workflow_stage:
                continue
            matches.append(pattern)
        if len(matches) != 1:
            return None
        match = matches[0]
        base = {"lookup_count": 1, "effect_count": 0, "pattern": match}
        if any(bool(evidence.get(str(item))) for item in match.get("stop_condition", [])):
            return {**base, "outcome": "stopped"}
        if any(bool(evidence.get(str(item))) for item in match.get("recovery_condition", [])):
            return {**base, "outcome": "recovered"}
        supporting = any(bool(evidence.get(str(item))) for item in match.get("supporting_evidence", []))
        contradicting = any(bool(evidence.get(str(item))) for item in match.get("contradicting_evidence", []))
        missing = [item for item in match.get("required_evidence", []) if not bool(evidence.get(str(item)))]
        if (supporting and contradicting) or missing:
            discriminator = match.get("next_discriminator")
            question_text = str(discriminator.get("question") or "") if isinstance(discriminator, Mapping) else ""
            return {**base, "outcome": "needs_discriminator", "questions": [question_text] if question_text else []}
        return {
            **base,
            "outcome": "direction",
            "effect_count": 1,
            "first_direction": match.get("first_direction", ""),
            "recommended_action": match.get("recommended_action", ""),
            "source_ids": match.get("formal_source_ids", []),
        }

    def execute(
        self,
        *,
        question: str,
        product_line: str = "",
        product_variant: str | None = None,
        workflow_stage: str = "",
        knowledge_type: str = "",
        answer_shape: str = "",
        evidence: Mapping[str, object] | None = None,
        allow_index_transaction: bool = False,
    ) -> ExecutionResult:
        normalized = _normalize(question)
        evidence = dict(evidence or {})
        hit = self._exact_registry(
            normalized,
            product_line,
            product_variant,
            workflow_stage,
            knowledge_type,
        )
        if hit is not None:
            rendered = self._renderer.render_registry_hit(hit)
            if answer_shape and rendered.answer_shape != answer_shape:
                raise PackageIntegrityError("answer_shape_mismatch")
            receipt = {
                "hit": hit, "model_calls": 0, "index_transactions": 0,
                "filesystem_scans": 0,
            }
            decision = validate_final_response(
                text=rendered.text,
                contract={
                    "product_line": product_line or hit.product_line,
                    "known_product_lines": self._known_product_lines,
                    "answer_shape": rendered.answer_shape,
                    "max_index_transactions": 0,
                },
                effect_receipt=receipt,
            )
            if not decision.allowed:
                raise PackageIntegrityError("final validation failed: " + ",".join(decision.reasons))
            return ExecutionResult(
                decision.text, rendered.answer_shape, "answer", 0, 0, 0, 0,
                rendered.source, (rendered.source,) if rendered.source else (),
            )

        diagnostic = (
            self._diagnostic(
                normalized,
                product_line,
                product_variant,
                workflow_stage,
                evidence,
            )
            if not knowledge_type or knowledge_type == "diagnostic_pattern"
            else None
        )
        if diagnostic is not None:
            rendered = self._renderer.render_diagnostic(diagnostic)
            if answer_shape and rendered.answer_shape != answer_shape:
                raise PackageIntegrityError("answer_shape_mismatch")
            decision = validate_final_response(
                text=rendered.text,
                contract={
                    "product_line": product_line or str(diagnostic["pattern"].get("product_line") or ""),
                    "known_product_lines": self._known_product_lines,
                    "answer_shape": rendered.answer_shape,
                    "max_index_transactions": 0,
                },
                effect_receipt={
                    "diagnostic_pattern": diagnostic["pattern"],
                    "model_calls": 0,
                    "index_transactions": 0,
                    "filesystem_scans": 0,
                },
            )
            if not decision.allowed:
                raise PackageIntegrityError("final validation failed: " + ",".join(decision.reasons))
            sources = tuple(
                source_reference_from_mapping(source)
                for source in diagnostic["pattern"].get("formal_source_ids", [])
                if isinstance(source, Mapping)
            )
            return ExecutionResult(
                decision.text, rendered.answer_shape, str(diagnostic["outcome"]),
                0, 0, 0, int(diagnostic.get("effect_count") or 0),
                sources[0] if len(sources) == 1 else None, sources,
            )

        fuzzy = (
            self._fts_registry(
                normalized,
                product_line,
                product_variant,
                workflow_stage,
                knowledge_type,
            )
            if allow_index_transaction
            else None
        )
        if fuzzy is not None:
            rendered = self._renderer.render_registry_hit(fuzzy)
            if answer_shape and rendered.answer_shape != answer_shape:
                raise PackageIntegrityError("answer_shape_mismatch")
            decision = validate_final_response(
                text=rendered.text,
                contract={
                    "product_line": product_line or fuzzy.product_line,
                    "known_product_lines": self._known_product_lines,
                    "answer_shape": rendered.answer_shape,
                    "max_index_transactions": 1,
                },
                effect_receipt={
                    "hit": fuzzy, "model_calls": 0,
                    "index_transactions": 1, "filesystem_scans": 0,
                },
            )
            if not decision.allowed:
                raise PackageIntegrityError("final validation failed: " + ",".join(decision.reasons))
            return ExecutionResult(
                decision.text, rendered.answer_shape, "answer", 0, 1, 0, 0,
                rendered.source, (rendered.source,) if rendered.source else (),
            )
        fallback = self._renderer.render_fallback()
        return ExecutionResult(
            fallback.text, fallback.answer_shape, "fallback_request", 0,
            1 if allow_index_transaction else 0, 0, 0, None
        )
