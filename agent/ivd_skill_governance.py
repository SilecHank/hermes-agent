"""Deterministic Skill governance for the IVD after-sales profile."""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Mapping


IVD_SKILL_CATALOG = frozenset(
    {
        "ivd-knowledge-delivery",
        "ivd-system-operations",
        "hermes-latency-troubleshooting",
        "ngs-workflow-router",
        "ngs-eval-maintainer",
    }
)

_BUSINESS_SKILLS = frozenset({"ivd-knowledge-delivery", "ngs-workflow-router"})
_OPERATIONS_SKILLS = frozenset(
    {"ivd-system-operations", "hermes-latency-troubleshooting"}
)
_EVALUATION_SKILLS = frozenset({"ngs-eval-maintainer"})
_IVD_PLATFORMS = frozenset({"weixin", "wecom", "qqbot"})

_OPERATIONS_RE = re.compile(
    r"Hermes|Gateway|网关|发布|部署|上线|回滚|代理|网络|系统运维|"
    r"响应(?:慢|延迟)|延迟排查|上下文膨胀|三平台|掉线|进程|服务",
    re.I,
)
_EVALUATION_RE = re.compile(
    r"golden|fixture|eval|回归测试|质量评测|质量回顾|测试样本|评测维护",
    re.I,
)


@dataclass(frozen=True)
class SkillLoadDecision:
    allowed: bool
    reason: str


@dataclass
class IVDTurnSkills:
    task_domain: str
    governance_mode: str
    loaded_names: list[str] = field(default_factory=list)
    body_chars: int = 0
    blocked_loads: int = 0
    shadow_would_block: int = 0
    unused_loads: int = 0
    max_concurrent: int = 0


_CURRENT_SKILL_TURN: ContextVar[IVDTurnSkills | None] = ContextVar(
    "hermes_ivd_skill_turn", default=None
)


def _platforms(value: Any) -> set[str]:
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    return {str(item).strip() for item in (value or ()) if str(item).strip()}


def resolve_ivd_skill_catalog(
    platform: str, config: Mapping[str, Any]
) -> frozenset[str] | None:
    """Return the focused catalog only when active IVD governance applies."""
    guard = config.get("after_sales_guard") or {}
    if not isinstance(guard, Mapping) or not guard.get("enabled", False):
        return None
    if str(platform or "") not in _platforms(guard.get("platforms")):
        return None
    if str(guard.get("skill_governance_mode") or "off") != "active":
        return None
    return IVD_SKILL_CATALOG


def classify_skill_task_domain(question: str) -> str:
    text = str(question or "")
    if _EVALUATION_RE.search(text):
        return "evaluation"
    if _OPERATIONS_RE.search(text):
        return "operations"
    return "business"


def begin_ivd_skill_turn(*, question: str, governance_mode: str) -> Token:
    mode = str(governance_mode or "off")
    return _CURRENT_SKILL_TURN.set(
        IVDTurnSkills(
            task_domain=classify_skill_task_domain(question),
            governance_mode=mode if mode in {"shadow", "active"} else "off",
        )
    )


def end_ivd_skill_turn(token: Token) -> None:
    _CURRENT_SKILL_TURN.reset(token)


def _role(name: str) -> str:
    if name in _BUSINESS_SKILLS:
        return "business"
    if name in _OPERATIONS_SKILLS:
        return "operations"
    if name in _EVALUATION_SKILLS:
        return "evaluation"
    return "other"


def _active_decision(turn: IVDTurnSkills, name: str) -> SkillLoadDecision:
    role = _role(name)
    if role == "other":
        # Explicit discovery remains available for skills outside the focused
        # catalog. They are hidden from the fixed prompt, not globally disabled.
        return SkillLoadDecision(True, "explicit_skill_allowed")
    if role != turn.task_domain:
        return SkillLoadDecision(False, "task_domain_mismatch")
    if name == "hermes-latency-troubleshooting" and turn.task_domain == "operations":
        return SkillLoadDecision(False, "canonical_skill_required")
    if name in turn.loaded_names:
        return SkillLoadDecision(False, "duplicate_skill_load")
    if turn.task_domain == "business" and any(
        loaded in _BUSINESS_SKILLS for loaded in turn.loaded_names
    ):
        return SkillLoadDecision(False, "business_skill_limit")
    if turn.task_domain == "operations" and any(
        loaded in _OPERATIONS_SKILLS for loaded in turn.loaded_names
    ):
        return SkillLoadDecision(False, "operations_skill_limit")
    return SkillLoadDecision(True, "allowed")


def decide_ivd_skill_load(name: str) -> SkillLoadDecision:
    turn = _CURRENT_SKILL_TURN.get()
    if turn is None or turn.governance_mode == "off":
        return SkillLoadDecision(True, "governance_inactive")
    decision = _active_decision(turn, str(name or ""))
    if turn.governance_mode == "shadow" and not decision.allowed:
        return SkillLoadDecision(True, f"shadow_would_block:{decision.reason}")
    return decision


def record_ivd_skill_load(name: str, *, body_chars: int, reason: str) -> None:
    turn = _CURRENT_SKILL_TURN.get()
    if turn is None or turn.governance_mode == "off":
        return
    resolved = str(name or "")
    if resolved not in turn.loaded_names:
        turn.loaded_names.append(resolved)
    turn.body_chars += max(0, int(body_chars or 0))
    turn.max_concurrent = max(turn.max_concurrent, len(turn.loaded_names))
    if reason.startswith("shadow_would_block:"):
        turn.shadow_would_block += 1
        turn.unused_loads += 1


def record_ivd_skill_block(reason: str) -> None:
    turn = _CURRENT_SKILL_TURN.get()
    if turn is not None and turn.governance_mode == "active":
        turn.blocked_loads += 1


def evaluate_ivd_skill_load(name: str, *, body_chars: int) -> SkillLoadDecision:
    decision = decide_ivd_skill_load(name)
    if decision.allowed:
        record_ivd_skill_load(name, body_chars=body_chars, reason=decision.reason)
    else:
        record_ivd_skill_block(decision.reason)
    return decision


def get_ivd_skill_snapshot() -> dict[str, Any]:
    turn = _CURRENT_SKILL_TURN.get()
    if turn is None:
        return {
            "skill_load_count": 0,
            "skill_body_chars": 0,
            "skill_unused_loads": 0,
            "skill_blocked_loads": 0,
            "skill_shadow_would_block": 0,
            "skill_max_concurrent": 0,
            "skill_names": [],
        }
    return {
        "skill_load_count": len(turn.loaded_names),
        "skill_body_chars": turn.body_chars,
        "skill_unused_loads": turn.unused_loads,
        "skill_blocked_loads": turn.blocked_loads,
        "skill_shadow_would_block": turn.shadow_would_block,
        "skill_max_concurrent": turn.max_concurrent,
        "skill_names": list(turn.loaded_names),
    }
