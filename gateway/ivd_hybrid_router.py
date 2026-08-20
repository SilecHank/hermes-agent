"""Conservative selection between structured scalar answers and expert mode."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_EXPERT_TERMS = re.compile(
    r"为什么|为何|原理|机制|原因|排查|异常|失败|不过|不通过|怎么办|建议|处理|"
    r"解读|判读|判断|比较|对比|差异|区别|同批|混合|污染|嵌合|可能|why|mechanism|troubleshoot",
    re.IGNORECASE,
)
_SCALAR_SHAPES = frozenset({"scalar_lookup", "direct_fact", "scalar"})
_PARAMETER_INTENTS = frozenset({"parameter", "product_fact"})
_NUMERIC_CLAIM = re.compile(
    r"(?<![\w])(?:[<>≥≤]=?|>=|<=)?\s*\d+(?:\.\d+)?\s*"
    r"(?:ng|uL|μL|µL|ml|mL|%|bp|℃|°C|个|次|微升|纳克)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HybridRouteDecision:
    mode: str
    reason: str


def decide_hybrid_route(
    question: str,
    *,
    envelope: Any,
    result: Any,
) -> HybridRouteDecision:
    """Allow package output only for one unambiguous scalar fact.

    The package remains a deterministic fact source. It is deliberately not
    allowed to replace the expert plane for reasoning, diagnosis, or weak hits.
    """
    text = str(question or "").strip()
    if _EXPERT_TERMS.search(text):
        return HybridRouteDecision("expert", "expert_intent")
    if getattr(envelope, "ambiguities", ()):
        return HybridRouteDecision("expert", "ambiguous")
    if not str(getattr(envelope, "product_line", "") or "").strip():
        return HybridRouteDecision("expert", "product_unresolved")
    if str(getattr(envelope, "intent", "") or "") not in _PARAMETER_INTENTS:
        return HybridRouteDecision("expert", "non_parameter_intent")
    if str(getattr(envelope, "answer_shape", "") or "") not in _SCALAR_SHAPES:
        return HybridRouteDecision("expert", "non_scalar")
    if str(getattr(result, "outcome", "") or "") != "answer":
        return HybridRouteDecision("expert", "non_answer")
    if str(getattr(result, "answer_shape", "") or "") not in _SCALAR_SHAPES:
        return HybridRouteDecision("expert", "non_scalar")
    if len(tuple(getattr(result, "sources", ()) or ())) != 1:
        return HybridRouteDecision("expert", "source_not_unique")
    if len(_NUMERIC_CLAIM.findall(str(getattr(result, "text", "") or ""))) != 1:
        return HybridRouteDecision("expert", "scalar_not_unique")
    return HybridRouteDecision("package_scalar", "exact_scalar_fact")
