"""Read-only execution of one immutable Hermes IVD serving-package."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
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

    def __init__(
        self,
        package_root: str | Path,
        *,
        expected_package_digest: str | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._closed = False
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
        observed_package_digest = hashlib.sha256(digest_payload).hexdigest()
        if observed_package_digest != manifest.get("package_digest"):
            raise PackageIntegrityError("package digest mismatch")
        if (
            expected_package_digest is not None
            and expected_package_digest != observed_package_digest
        ):
            raise PackageIntegrityError("expected package digest mismatch")
        self._package_digest = observed_package_digest

        self._graph = self._read_json("indexes/diagnostic-graph.json")
        self._renderer = IVDRenderer(self._read_json("renders/render-policy.json"))
        self._equipment_reagent_index: dict[str, object] | None = None
        _er_relative = "indexes/equipment-reagent-index.json"
        if _er_relative in members:
            self._equipment_reagent_index = self._read_json(_er_relative)
        database = self._member_path("database/registry.sqlite")
        uri = f"file:{quote(str(database.resolve()))}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            known_product_lines = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT product_line FROM products ORDER BY product_line"
                ).fetchall()
                if row[0]
            )
        except Exception:
            connection.close()
            raise
        self._database = connection
        self._known_product_lines = known_product_lines

    @property
    def package_digest(self) -> str:
        return self._package_digest

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
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
            SELECT x.alias AS matched_alias, e.entity_id, e.knowledge_kind,
                   p.product_line, p.product_variant,
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

    @staticmethod
    def _semantic_signature(value: str) -> tuple[str, frozenset[str]]:
        normalized = re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", value.casefold())
        for noise in (
            "请帮我", "麻烦", "请问", "需要多少", "是多少", "多少",
            "一下", "的", "呀", "呢", "吗",
        ):
            normalized = normalized.replace(noise, "")
        grams = frozenset(
            normalized[index : index + 2]
            for index in range(max(0, len(normalized) - 1))
        )
        return normalized, grams

    def _semantic_registry(
        self,
        question: str,
        product_line: str,
        product_variant: str | None,
        workflow_stage: str,
        knowledge_type: str,
    ) -> SimpleNamespace | None:
        query_text, query_grams = self._semantic_signature(question)
        if len(query_text) < 2 or len(question) > 256:
            return None
        clauses = ["a.effective_status='active'", "length(x.alias)>=2"]
        values: list[object] = []
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
            self._projection()
            + " WHERE "
            + " AND ".join(clauses)
            + " ORDER BY x.assertion_id, length(x.alias) DESC LIMIT 256",
            values,
        ).fetchall()
        if not rows:
            return None
        candidates: dict[tuple[str, str, str, str], tuple[float, sqlite3.Row]] = {}
        for row in rows:
            alias_text, alias_grams = self._semantic_signature(str(row["matched_alias"]))
            if not alias_text:
                continue
            if alias_text in query_text or query_text in alias_text:
                score = 1.0
            elif not alias_grams or not query_grams:
                score = 0.0
            else:
                score = len(alias_grams & query_grams) / min(
                    len(alias_grams), len(query_grams)
                )
            fact = (
                str(row["entity_id"]),
                str(row["fact_key"]),
                str(row["source_locator"]),
                str(row["value"]),
            )
            previous = candidates.get(fact)
            if previous is None or score > previous[0]:
                candidates[fact] = (score, row)
        ranked = sorted(candidates.values(), key=lambda item: item[0], reverse=True)
        if not ranked or ranked[0][0] < 0.55:
            return None
        if len(ranked) > 1 and ranked[1][0] >= ranked[0][0] - 0.12:
            return None
        return self._hit(ranked[0][1])

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

    def _equipment_reagent_lookup(self, normalized: str) -> "ExecutionResult | None":
        index = self._equipment_reagent_index
        if not isinstance(index, dict):
            return None
        groups = index.get("groups")
        if not isinstance(groups, list) or not groups:
            return None
        if not any(key in normalized for key in ("设备", "试剂", "耗材", "清单")):
            return None

        platforms = sorted({str(g.get("platform") or "") for g in groups if isinstance(g, dict) and g.get("platform")})
        sub_projects = sorted({str(g.get("sub_project") or "") for g in groups if isinstance(g, dict) and g.get("sub_project")})
        methods = sorted({str(g.get("method") or "") for g in groups if isinstance(g, dict) and g.get("method")})
        n = normalized.casefold()
        matched_platforms = [p for p in platforms if p and p.casefold() in n]
        if matched_platforms:
            matched_platforms = [max(matched_platforms, key=len)]
        matched_subs = [s for s in sub_projects if s and s.casefold() in n]
        if matched_subs and len(matched_subs) > 1:
            longest = max(matched_subs, key=len)
            matched_subs = [longest]
        elif not matched_subs:
            for token in re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", normalized):
                token_l = token.casefold()
                if len(token_l) < 2:
                    continue
                prefix_matches = [s for s in sub_projects if s and s.casefold().startswith(token_l)]
                if prefix_matches:
                    matched_subs = prefix_matches
                    break
        method_tokens = [
            token for token in re.split(r"[\s,，、]+", normalized)
            if token and any(k in token for k in ("提取", "建库", "杂交", "富集", "自动化", "纳磁", "磁珠", "PCR", "手动", "单纯化", "板式", "单管"))
        ]
        matched_methods = [
            m for m in methods
            if m and m != "未标注" and all(token.casefold() in m.casefold() for token in method_tokens)
        ] if method_tokens else []

        candidates = [g for g in groups if isinstance(g, dict)]
        if matched_platforms:
            candidates = [g for g in candidates if g.get("platform") in matched_platforms]
        if matched_subs:
            candidates = [g for g in candidates if g.get("sub_project") in matched_subs]
        if matched_methods:
            candidates = [g for g in candidates if g.get("method") in matched_methods]
        if not candidates:
            return None
        if len(candidates) > 8:
            distinct_methods = sorted({str(g.get("method") or "未标注") for g in candidates})
            scope = " · ".join(filter(None, [matched_subs[0] if matched_subs else "", matched_platforms[0] if matched_platforms else ""]))
            clarification = (
                "Workflow: equipment-reagent-selection.md\n\n"
                f"该产品（{scope}）存在多种实验方式，请先确认要哪一种：\n\n"
                + "\n".join(f"- {m}" for m in distinct_methods)
            )
            return ExecutionResult(clarification, "clarification", "clarification", 0, 0, 0, 0, None, ())

        want_equipment = any(k in normalized for k in ("设备",))
        want_reagent = any(k in normalized for k in ("试剂", "耗材"))
        if not want_equipment and not want_reagent:
            want_equipment = want_reagent = True

        lines = ["Workflow: equipment-reagent-selection.md", ""]
        for group in candidates:
            lines.append(f"## {group.get('sub_project')} · {group.get('platform')} · {group.get('method')}")
            lines.append("")
            if want_equipment:
                equip = group.get("equipment") if isinstance(group.get("equipment"), list) else []
                if equip:
                    lines.append("### 设备清单")
                    lines.append("")
                    lines.append("| 实验区 | 设备名称 | 参数要求 | 品牌/型号 | 数量 | 必选性 | 物料编码(SAP) |")
                    lines.append("|------|------|------|------|-----|------|------|")
                    for item in equip:
                        lines.append(
                            f"| {item.get('workflow_step') or '-'} | {item.get('item_name') or '-'} "
                            f"| {item.get('specification') or '-'} | {item.get('brand') or '-'} {item.get('model') or ''} "
                            f"| {item.get('quantity') or '-'} | {item.get('required_level') or '-'} "
                            f"| {item.get('sap_code') or '-'} |"
                        )
                    lines.append("")
            if want_reagent:
                reagents = group.get("reagents") if isinstance(group.get("reagents"), list) else []
                if reagents:
                    lines.append("### 试剂耗材清单")
                    lines.append("")
                    lines.append("| 物料名称 | 规格 | 单个样本使用量 | 单位 | SAP物料编码 | RM编码 |")
                    lines.append("|------|------|------|------|------|------|")
                    for item in reagents:
                        lines.append(
                            f"| {item.get('item_name') or '-'} | {item.get('specification') or '-'} "
                            f"| {item.get('quantity') or '-'} | {item.get('unit') or '-'} "
                            f"| {item.get('sap_code') or '-'} | {item.get('material_code') or '-'} |"
                        )
                    lines.append("")
        text = "\n".join(lines).strip()
        if not text:
            return None
        return ExecutionResult(text, "answer", "answer", 0, 0, 0, 0, None, ())

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
        with self._lock:
            if self._closed:
                raise PackageIntegrityError("knowledge engine is closed")
            return self._execute(
                question=question,
                product_line=product_line,
                product_variant=product_variant,
                workflow_stage=workflow_stage,
                knowledge_type=knowledge_type,
                answer_shape=answer_shape,
                evidence=evidence,
                allow_index_transaction=allow_index_transaction,
            )

    def _execute(
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

        equipment_reagent = self._equipment_reagent_lookup(normalized)
        if equipment_reagent is not None:
            return equipment_reagent

        fuzzy = None
        if allow_index_transaction:
            fuzzy = self._semantic_registry(
                normalized,
                product_line,
                product_variant,
                workflow_stage,
                knowledge_type,
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
