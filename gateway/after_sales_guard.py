"""Optional per-turn workflow-fact injection for after-sales channels."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from gateway.ivd_evidence import build_runtime_evidence_id
from gateway.ivd_verified_facts import VerifiedFactService


MEASUREMENT_VALUE_RE = re.compile(
    r"(?:浓度|均值|平均(?:值)?|中位数|范围)[^0-9]{0,12}"
    r"(\d+(?:\.\d+)?(?:\s*[-~～]\s*\d+(?:\.\d+)?)?)"
)
MEASUREMENT_UNIT_RE = re.compile(r"ng\s*/\s*[µuμ]L|%", re.IGNORECASE)


@dataclass(frozen=True)
class AfterSalesTurn:
    context: str
    facts: dict[str, Any]
    validator: ModuleType | None
    allowed_numeric_claims: tuple[str, ...]
    product_scope: str = ""
    product_variant: str = ""
    fast_path: bool = False
    route_id: str = ""
    route_version: str = ""
    source_paths: tuple[str, ...] = ()
    requires_source_validation: bool = False
    preflight_decision: str = ""
    preflight_action: str = ""
    preflight_issues: tuple[str, ...] = ()
    answer_shape: str = "diagnostic"
    verified_fact_hit: bool = False
    verified_fact_status: str = "off"
    direct_response: str = ""
    direct_evidence_ids: tuple[str, ...] = ()
    fact_key: str = ""
    source_revisions: dict[str, str] = field(default_factory=dict)
    evidence_sidecar_enabled: bool = False

    @property
    def blocks_answer_generation(self) -> bool:
        return self.preflight_decision == "block" or self.preflight_action in {
            "stop_before_answer_generation",
            "stop_before_final_answer",
        }

    @property
    def has_validator(self) -> bool:
        return self.validator is not None and (
            bool(self.facts) or self.requires_source_validation
        )

    def validate(
        self,
        answer: str,
        *,
        messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self.has_validator:
            return {"ok": True, "reasons": [], "fallback": ""}
        assert self.validator is not None
        if self.requires_source_validation and not self.facts:
            trusted_claims, source_read, claim_sources = _trusted_tool_numeric_evidence_details(
                messages or [],
                self.source_paths,
                self.validator,
            )
            if not source_read:
                return {
                    "ok": False,
                    "reasons": ["formal_source_not_read"],
                    "fallback": (
                        "当前未能从当前产品的正式来源核实该参数，暂不提供数值结论。"
                        "请补充产品版本或SOP编号，以便继续核实。"
                    ),
                }
            allowed = tuple(self.allowed_numeric_claims) + trusted_claims
            allowed_normalized = {_normalize_numeric_claim(item) for item in allowed}
            unsupported = [
                claim
                for claim in self.validator.extract_numeric_claims(answer)
                if _normalize_numeric_claim(claim) not in allowed_normalized
            ]
            if unsupported:
                return {
                    "ok": False,
                    "reasons": [
                        f"unsupported_numeric_claim:{claim}" for claim in unsupported
                    ],
                    "fallback": (
                        "当前回答中的参数与已核实的正式来源不一致，已停止发送该数值结论。"
                        "请补充产品版本或SOP编号，以便继续核实。"
                    ),
                }
            adopted_claims = self._runtime_adopted_claims(answer, claim_sources)
            return {
                "ok": True,
                "reasons": [],
                "fallback": "",
                "adopted_claims": adopted_claims,
            }
        allowed_numeric_claims = list(self.allowed_numeric_claims)
        trusted_claims, _ = _trusted_tool_numeric_evidence(
            messages or [],
            (
                source.get("resolved_path", "")
                for source in self.facts.get("authoritative_sources", ())
                if source.get("resolved_path")
            ),
            self.validator,
        )
        allowed_numeric_claims.extend(trusted_claims)
        result = self.validator.validate_answer(
            answer,
            self.facts,
            allowed_numeric_claims=tuple(dict.fromkeys(allowed_numeric_claims)),
        )
        return {
            "ok": result.ok,
            "reasons": result.reasons,
            "fallback": ""
            if result.ok
            else self.validator.build_safe_clarification(result, self.facts),
            "adopted_claims": list(getattr(result, "adopted_claims", ()) or ()),
        }

    def _runtime_adopted_claims(
        self,
        answer: str,
        claim_sources: dict[str, str],
    ) -> list[dict[str, Any]]:
        if not self.evidence_sidecar_enabled or not self.fact_key:
            return []
        assert self.validator is not None
        claims = list(self.validator.extract_numeric_claims(answer))
        if len(claims) != 1:
            return []
        claim = claims[0]
        source_path = claim_sources.get(_normalize_numeric_claim(claim), "")
        relative_path = _relative_source_path(source_path, self.source_revisions)
        revision = self.source_revisions.get(relative_path, "")
        if not relative_path or not revision:
            return []
        value, unit = _split_numeric_claim(claim)
        if not value:
            return []
        return [
            {
                "fact_key": self.fact_key,
                "value": value,
                "unit": unit,
                "conditions": [],
                "evidence_id": build_runtime_evidence_id(
                    source_revision=revision,
                    source_path=relative_path,
                    locator="tool:read_file",
                    adopted_excerpt=claim,
                ),
                "source_path": relative_path,
                "source_revision": revision,
                "locator": "tool:read_file",
            }
        ]


def build_preflight_block_result(
    turn: AfterSalesTurn,
    message: str,
) -> dict[str, Any]:
    """Build a direct Chinese response without invoking the model."""
    fallback = (
        "当前检索计划包含待验证或非正式来源，暂不能据此给出结论。"
        "请补充产品名称、版本或SOP编号，我会改用正式来源继续核实。"
    )
    return {
        "final_response": fallback,
        "messages": [
            {"role": "user", "content": str(message or "")},
            {"role": "assistant", "content": fallback},
        ],
        "api_calls": 0,
        "completed": True,
        "partial": False,
        "interrupted": False,
        "error": None,
        "history_offset": 0,
        "last_prompt_tokens": 0,
        "agent_persisted": False,
        "preflight_blocked": True,
    }


def build_direct_fact_result(turn: AfterSalesTurn, message: str) -> dict[str, Any]:
    """Build the standard zero-model result for an exact active fact hit."""
    if not turn.direct_response:
        raise ValueError("direct fact result requires a validated response")
    return {
        "final_response": turn.direct_response,
        "messages": [
            {"role": "user", "content": str(message or "")},
            {"role": "assistant", "content": turn.direct_response},
        ],
        "api_calls": 0,
        "completed": True,
        "partial": False,
        "interrupted": False,
        "error": None,
        "history_offset": 0,
        "last_prompt_tokens": 0,
        "agent_persisted": False,
        "verified_fact_reused": True,
        "evidence_ids": list(turn.direct_evidence_ids),
    }


def activate_validated_fact(
    session_db: Any,
    turn: AfterSalesTurn,
    *,
    question: str,
    validation: dict[str, Any],
) -> bool:
    """Activate exactly one adopted scalar claim after final validation."""
    if not validation.get("ok") or turn.answer_shape not in {"scalar_lookup", "direct_fact"}:
        return False
    if not turn.product_scope:
        return False
    claims = list(validation.get("adopted_claims") or [])
    if len(claims) != 1:
        return False
    claim = claims[0]
    fact_key = str(claim.get("fact_key") or _fact_key_for_question(question))
    if not fact_key or not claim.get("evidence_id") or not claim.get("source_path"):
        return False
    revision = str(claim.get("source_revision") or "")
    if not revision:
        return False
    unit = str(claim.get("unit") or "")
    template = "{value} {unit}。" if unit else "{value}。"
    fact_id_payload = "\x1f".join(
        (
            turn.product_scope,
            turn.product_variant,
            fact_key,
            json.dumps(claim.get("conditions") or [], ensure_ascii=False, sort_keys=True),
        )
    )
    import hashlib

    fact_id = "fact-" + hashlib.sha256(fact_id_payload.encode("utf-8")).hexdigest()[:24]
    return VerifiedFactService(session_db).activate(
        {
            "fact_id": fact_id,
            "product_scope": turn.product_scope,
            "product_variant": turn.product_variant,
            "question_type": turn.answer_shape,
            "fact_key": fact_key,
            "value": str(claim.get("value") or ""),
            "unit": unit,
            "conditions": list(claim.get("conditions") or []),
            "answer_template": template,
            "evidence": [
                {
                    "evidence_id": str(claim["evidence_id"]),
                    "source_path": str(claim["source_path"]),
                    "source_revision": revision,
                }
            ],
            "source_revision": revision,
            "status": "active",
        }
    )


@dataclass(frozen=True)
class CriticalAfterSalesValidator:
    """Callable policy that prevents unvalidated formal answers from escaping."""

    turn: AfterSalesTurn
    messages_provider: Callable[[], list[dict[str, Any]]]
    fail_closed: bool = True
    error_fallback: str = (
        "当前正式知识校验暂时不可用，已停止发送未经校验的结论。请稍后重试；"
        "如问题紧急，请提供产品、流程阶段和异常指标后转人工确认。"
    )

    def __call__(self, answer: str) -> dict[str, Any]:
        return self.turn.validate(answer, messages=self.messages_provider())


def _normalize_numeric_claim(value: str) -> str:
    return (
        re.sub(r"\s+", "", value)
        .replace("μ", "µ")
        .replace(">=", "≥")
        .replace("<=", "≤")
        .lower()
    )


def _canonical_preflight_action(gate: dict[str, Any]) -> str:
    decision = str(gate.get("decision") or "")
    if decision == "block":
        return "stop_before_answer_generation"
    if decision in {"trim", "trim_context"}:
        return "trim_context_before_answer_generation"
    if decision == "allow":
        return "continue_answer_generation"
    return str(gate.get("pipeline_action") or "")


def _render_answer_experience_context(
    guard: dict[str, Any],
    *,
    platform: str,
) -> str:
    """Load the shared IVD answer policy beside the configured KB pipeline."""
    fast_response_path = Path(str(guard.get("fast_response_module") or ""))
    if not fast_response_path.is_file():
        return ""
    policy_path = fast_response_path.with_name("after_sales_platform_policy.py")
    if not policy_path.is_file():
        return ""
    try:
        module = _load_module(str(policy_path), policy_path.stat().st_mtime_ns)
        render = getattr(module, "render_answer_experience_context")
        return str(render(platform=platform) or "").strip()
    except Exception:
        return ""


def _trusted_tool_numeric_evidence(
    messages: list[dict[str, Any]],
    source_paths: Any,
    validator: ModuleType,
) -> tuple[tuple[str, ...], bool]:
    claims, source_read, _ = _trusted_tool_numeric_evidence_details(
        messages, source_paths, validator
    )
    return claims, source_read


def _trusted_tool_numeric_evidence_details(
    messages: list[dict[str, Any]],
    source_paths: Any,
    validator: ModuleType,
) -> tuple[tuple[str, ...], bool, dict[str, str]]:
    trusted_paths = {
        str(Path(str(path)).expanduser().resolve())
        for path in source_paths
        if path
    }
    if not trusted_paths:
        return (), False, {}

    trusted_call_ids: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or ():
            function = call.get("function") or {}
            if function.get("name") != "read_file":
                continue
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    continue
            if not isinstance(arguments, dict) or not arguments.get("path"):
                continue
            candidate = str(Path(str(arguments["path"])).expanduser().resolve())
            if candidate in trusted_paths:
                call_id = call.get("id") or call.get("call_id")
                if call_id:
                    trusted_call_ids[str(call_id)] = candidate

    claims: list[str] = []
    claim_sources: dict[str, str] = {}
    source_read = False
    for message in messages:
        if message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id") or "")
        if call_id not in trusted_call_ids:
            continue
        source_read = True
        for claim in validator.extract_numeric_claims(str(message.get("content") or "")):
            claims.append(claim)
            claim_sources.setdefault(_normalize_numeric_claim(claim), trusted_call_ids[call_id])
    return tuple(dict.fromkeys(claims)), source_read, claim_sources


@lru_cache(maxsize=8)
def _load_module(path_text: str, mtime_ns: int) -> ModuleType:
    path = Path(path_text)
    module_name = f"hermes_after_sales_{abs(hash((path_text, mtime_ns)))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load after-sales module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def prepare_after_sales_turn(
    config: dict[str, Any],
    *,
    platform: str,
    message: str,
    history: list[dict[str, Any]],
    session_db: Any | None = None,
) -> AfterSalesTurn | None:
    """Return verified per-turn facts when an enabled workflow card matches."""

    guard = config.get("after_sales_guard") or {}
    if not isinstance(guard, dict) or not guard.get("enabled", False):
        return None
    normalized_platform = str(platform or "").strip().lower()
    platforms = guard.get("platforms") or []
    if isinstance(platforms, str):
        platforms = [item.strip() for item in platforms.split(",") if item.strip()]
    enabled_platforms = {
        str(item or "").strip().lower()
        for item in platforms
        if str(item or "").strip()
    }
    if normalized_platform not in enabled_platforms:
        return None

    module_path = Path(str(guard.get("workflow_module") or ""))
    validator_path = Path(str(guard.get("validator_module") or ""))
    cards_dir = Path(str(guard.get("cards_dir") or ""))
    match = None
    validator = None
    if module_path.is_file() and validator_path.is_file() and cards_dir.is_dir():
        module = _load_module(str(module_path), module_path.stat().st_mtime_ns)
        validator = _load_module(str(validator_path), validator_path.stat().st_mtime_ns)
        match = module.match_case_facts(cards_dir, message=message, history=history)

    experience_context = _render_answer_experience_context(
        guard,
        platform=normalized_platform,
    )
    fast_result = _render_fast_response_context(guard, message=message, match=match)
    fast_context = str(fast_result.get("context") or "")
    if match is None:
        combined_context = "\n\n".join(
            item for item in (experience_context, fast_context) if item
        )
        if not combined_context:
            return None
        route_id = str(fast_result.get("route_id") or "fast_preflight")
        source_paths = tuple(fast_result.get("source_paths") or ())
        requires_source_validation = route_id == "sop_parameter_short_answer"
        source_text = " ".join(
            str(item.get("content", ""))
            for item in history[-12:]
            if item.get("role") == "user"
        ) + f" {message}"
        return _attach_verified_fact(
            AfterSalesTurn(
            context=combined_context,
            facts={},
            validator=validator if requires_source_validation else None,
            allowed_numeric_claims=(
                tuple(validator.extract_numeric_claims(source_text))
                if validator is not None and requires_source_validation
                else ()
            ),
            product_scope=str(fast_result.get("product_scope") or ""),
            product_variant=str(fast_result.get("product_variant") or ""),
            fast_path=True,
            route_id=route_id,
            route_version=str(fast_result.get("route_version") or ""),
            source_paths=source_paths,
            requires_source_validation=requires_source_validation,
            preflight_decision=str(fast_result.get("preflight_decision") or ""),
            preflight_action=str(fast_result.get("preflight_action") or ""),
            preflight_issues=tuple(fast_result.get("preflight_issues") or ()),
            answer_shape=str(fast_result.get("answer_shape") or "direct_fact"),
            ),
            guard=guard,
            question=message,
            session_db=session_db,
        )

    context = module.render_fact_context(match)
    if experience_context:
        context = f"{experience_context}\n\n{context}"
    if fast_context:
        context = f"{context}\n\n{fast_context}"
    recent_user_text = " ".join(
        str(item.get("content", ""))
        for item in history[-12:]
        if item.get("role") == "user" and isinstance(item.get("content"), str)
    )
    source_text = f"{recent_user_text} {message}"
    allowed_numeric_claims = list(
        validator.extract_numeric_claims(f"{source_text} {context}")
    )
    measurement_rule = match["facts"].get("measurement_rule") or {}
    unit_match = MEASUREMENT_UNIT_RE.search(
        str(measurement_rule.get("release_rule") or "")
    )
    if unit_match:
        unit = unit_match.group(0)
        for value_match in MEASUREMENT_VALUE_RE.finditer(source_text):
            allowed_numeric_claims.append(f"{value_match.group(1)} {unit}")
    return _attach_verified_fact(
        AfterSalesTurn(
        context=context,
        facts=match["facts"],
        validator=validator,
        allowed_numeric_claims=tuple(dict.fromkeys(allowed_numeric_claims)),
        product_scope=(
            str(fast_result.get("product_scope") or "")
            or str(match["facts"].get("product") or "")
        ),
        product_variant=str(fast_result.get("product_variant") or ""),
        fast_path=bool(fast_context),
        route_id=str(fast_result.get("route_id") or match["facts"].get("workflow_id") or "facts"),
        route_version=str(fast_result.get("route_version") or ""),
        source_paths=tuple(
            fast_result.get("source_paths")
            or (
                source.get("resolved_path", "")
                for source in match["facts"].get("authoritative_sources", ())
                if source.get("resolved_path")
            )
        ),
        preflight_decision=str(fast_result.get("preflight_decision") or ""),
        preflight_action=str(fast_result.get("preflight_action") or ""),
        preflight_issues=tuple(fast_result.get("preflight_issues") or ()),
        answer_shape=str(fast_result.get("answer_shape") or "diagnostic"),
        ),
        guard=guard,
        question=message,
        session_db=session_db,
    )


def _attach_verified_fact(
    turn: AfterSalesTurn,
    *,
    guard: dict[str, Any],
    question: str,
    session_db: Any | None,
) -> AfterSalesTurn:
    fact_key = _fact_key_for_question(question)
    source_revisions = _load_source_revisions(guard.get("knowledge_release_manifest"))
    turn = replace(
        turn,
        fact_key=fact_key,
        source_revisions=source_revisions,
        evidence_sidecar_enabled=bool(guard.get("evidence_sidecar_enabled", False)),
    )
    mode = str(guard.get("verified_fact_reuse_mode") or "off").strip().lower()
    if mode not in {"off", "shadow", "active"}:
        mode = "off"
    if mode == "off" or session_db is None:
        return turn
    if turn.answer_shape not in {"scalar_lookup", "direct_fact"} or not turn.product_scope:
        return turn
    if not fact_key:
        return turn
    if not source_revisions:
        return turn
    match = VerifiedFactService(session_db).lookup(
        product_scope=turn.product_scope,
        product_variant=turn.product_variant,
        fact_key=fact_key,
        conditions=[],
        source_revisions=source_revisions,
    )
    if match is None:
        return turn
    evidence_ids = tuple(
        str(item.get("evidence_id") or "")
        for item in match.get("evidence") or []
        if item.get("evidence_id")
    )
    return replace(
        turn,
        verified_fact_hit=True,
        verified_fact_status="active",
        direct_response=str(match.get("rendered_answer") or "") if mode == "active" else "",
        direct_evidence_ids=evidence_ids,
    )


def _load_source_revisions(path_value: Any) -> dict[str, str]:
    path = Path(str(path_value or "")).expanduser()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if payload.get("status") != "ready":
        return {}
    revisions = payload.get("source_revisions") or {}
    return {str(path): str(revision) for path, revision in revisions.items() if path and revision}


def _fact_key_for_question(question: str) -> str:
    text = str(question or "")
    rules = (
        (r"DNA.{0,8}(?:起始|投入)|(?:起始|投入).{0,8}DNA|建库投入", "dna_starting_input"),
        (r"循环数|几个循环|多少循环", "pcr_cycles"),
        (r"温度|多少度", "temperature"),
        (r"时长|多长时间|多少分钟|多少小时", "duration"),
        (r"体积|多少[μu]?L", "volume"),
        (r"浓度|阈值|标准", "threshold"),
        (r"通量|数据量|reads?", "throughput"),
    )
    for pattern, fact_key in rules:
        if re.search(pattern, text, re.I):
            return fact_key
    return ""


def _relative_source_path(source_path: str, revisions: dict[str, str]) -> str:
    normalized = str(source_path or "").replace("\\", "/")
    matches = [path for path in revisions if normalized == path or normalized.endswith("/" + path)]
    return matches[0] if len(matches) == 1 else ""


def _split_numeric_claim(claim: str) -> tuple[str, str]:
    match = re.search(r"(\d+(?:\.\d+)?(?:\s*[-~～]\s*\d+(?:\.\d+)?)?)\s*(.*)", claim)
    if not match:
        return "", ""
    return match.group(1).strip(), match.group(2).strip()


def _render_fast_response_context(
    guard: dict[str, Any],
    *,
    message: str,
    match: dict[str, Any] | None,
) -> dict[str, Any]:
    module_path = Path(str(guard.get("fast_response_module") or ""))
    if not module_path.is_file():
        return {}
    try:
        kb_root = str(module_path.parent.parent)
        if kb_root not in sys.path:
            sys.path.insert(0, kb_root)
        module = _load_module(str(module_path), module_path.stat().st_mtime_ns)
        question_type = _question_type_from_match(match)
        plan = module.build_fast_response_plan(message, question_type=question_type)
    except Exception:
        return {}
    runtime_preflight = plan.get("runtime_preflight") or {}
    if not runtime_preflight.get("eligible", False):
        return {}
    template = plan.get("answer_template") or {}
    gate = plan.get("preflight_gate") or {}
    kb_root = module_path.parent.parent
    initial_files = []
    for raw_path in plan.get("initial_files", [])[:3]:
        if "candidate" in str(raw_path).casefold():
            continue
        path = Path(str(raw_path)).expanduser()
        if not path.is_absolute():
            path = kb_root / path
        initial_files.append(str(path.resolve()))
    lines = [
        "[快速回答管线]",
        f"路由版本：{runtime_preflight.get('route_version', '')}",
        f"回答风格：{template.get('style', 'short_first')}",
        f"预算动作：{gate.get('pipeline_action', 'continue_final_answer')}",
        f"首轮文件数：{len(initial_files)}",
    ]
    product_identity = plan.get("product_identity") or {}
    if product_identity.get("product_scope_confirmed"):
        lines.append(f"已识别产品：{product_identity.get('product_scope', '')}")
        if product_identity.get("product_variant"):
            lines.append(f"产品变体：{product_identity.get('product_variant')}")
    if initial_files:
        lines.append("首轮只读正式来源：" + "；".join(initial_files))
    lines.append("默认先给结论、要点、下一步、边界/来源；用户追问时再展开。")
    fast_path = plan.get("fast_path") or {}
    answer_shape = plan.get("answer_shape") or {}
    return {
        "context": "\n".join(lines),
        "route_id": fast_path.get("route_id") or fast_path.get("answer_shape") or "fast_preflight",
        "route_version": runtime_preflight.get("route_version", ""),
        "source_paths": tuple(initial_files),
        "product_scope": str(product_identity.get("product_scope") or "")
        if product_identity.get("product_scope_confirmed")
        else "",
        "product_variant": str(product_identity.get("product_variant") or "")
        if product_identity.get("product_scope_confirmed")
        else "",
        "preflight_decision": str(gate.get("decision") or ""),
        "preflight_action": _canonical_preflight_action(gate),
        "preflight_issues": tuple(str(item) for item in (gate.get("issues") or ())),
        "answer_shape": str(answer_shape.get("answer_shape") or "direct_fact"),
    }


def _question_type_from_match(match: dict[str, Any] | None) -> str:
    facts = match.get("facts") if isinstance(match, dict) else {}
    workflow = str((facts or {}).get("workflow_id") or "")
    if "report" in workflow:
        return "report_interpretation"
    if "operation" in workflow or "platform" in workflow:
        return "platform_operation"
    return "wet_lab"
