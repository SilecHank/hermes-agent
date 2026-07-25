"""Optional per-turn workflow-fact injection for after-sales channels."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any


MEASUREMENT_VALUE_RE = re.compile(
    r"(?:浓度|均值|平均(?:值)?|中位数|范围)[^0-9]{0,12}"
    r"(\d+(?:\.\d+)?(?:\s*[-~～]\s*\d+(?:\.\d+)?)?)"
)
MEASUREMENT_UNIT_RE = re.compile(r"ng\s*/\s*[µuμ]L|%", re.IGNORECASE)


@dataclass(frozen=True)
class AfterSalesTurn:
    context: str
    facts: dict[str, Any]
    validator: ModuleType
    allowed_numeric_claims: tuple[str, ...]

    def validate(
        self,
        answer: str,
        *,
        messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        allowed_numeric_claims = list(self.allowed_numeric_claims)
        allowed_numeric_claims.extend(
            _trusted_tool_numeric_claims(
                messages or [],
                self.facts,
                self.validator,
            )
        )
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
        }


def _trusted_tool_numeric_claims(
    messages: list[dict[str, Any]],
    facts: dict[str, Any],
    validator: ModuleType,
) -> tuple[str, ...]:
    trusted_paths = {
        str(Path(source.get("resolved_path", "")).expanduser().resolve())
        for source in facts.get("authoritative_sources", ())
        if source.get("resolved_path")
    }
    if not trusted_paths:
        return ()

    trusted_call_ids: set[str] = set()
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
                    trusted_call_ids.add(str(call_id))

    claims: list[str] = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        if str(message.get("tool_call_id") or "") not in trusted_call_ids:
            continue
        claims.extend(validator.extract_numeric_claims(str(message.get("content") or "")))
    return tuple(dict.fromkeys(claims))


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
) -> AfterSalesTurn | None:
    """Return verified per-turn facts when an enabled workflow card matches."""

    guard = config.get("after_sales_guard") or {}
    if not isinstance(guard, dict) or not guard.get("enabled", False):
        return None
    platforms = guard.get("platforms") or []
    if isinstance(platforms, str):
        platforms = [item.strip() for item in platforms.split(",") if item.strip()]
    if platform not in platforms:
        return None

    module_path = Path(str(guard.get("workflow_module") or ""))
    validator_path = Path(str(guard.get("validator_module") or ""))
    cards_dir = Path(str(guard.get("cards_dir") or ""))
    if not module_path.is_file() or not validator_path.is_file() or not cards_dir.is_dir():
        return None

    module = _load_module(str(module_path), module_path.stat().st_mtime_ns)
    validator = _load_module(str(validator_path), validator_path.stat().st_mtime_ns)
    match = module.match_case_facts(cards_dir, message=message, history=history)
    if match is None:
        return None
    context = module.render_fact_context(match)
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
    return AfterSalesTurn(
        context=context,
        facts=match["facts"],
        validator=validator,
        allowed_numeric_claims=tuple(dict.fromkeys(allowed_numeric_claims)),
    )
