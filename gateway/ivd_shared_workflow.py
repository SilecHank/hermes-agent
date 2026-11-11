"""Shared, source-bound task and presentation helpers for IVD runtimes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any

def is_non_business_task(task_contract: Any) -> bool:
    """True when the resolver said this turn is not a customer case.

    The KB resolver publishes `task_kind` (social / meta / operator / business /
    unknown) precisely so the gateway can decide whether the after-sales evidence
    contract applies at all.  Only `social` used to be honoured, so a message
    that talked about the assistant itself, a maintenance instruction, or a
    runtime notice still entered the IVD engine and came back as a product
    clarification.
    """
    task = task_contract if isinstance(task_contract, dict) else {}
    return str(task.get("task_kind") or "") in {
        "social", "meta", "operator", "unknown",
    }



def _bind_workflow_scripts(path: Path) -> None:
    """Bind one process to one trusted KB scripts root before its first import.

    Never replace an existing namespace or module. A release change requires a
    new gateway process; an incompatible cache is a configuration error.
    """
    import os
    import importlib.machinery

    resolved = path.resolve(strict=True)
    root = resolved.parent.parent
    agent = Path(__file__).resolve().parents[1]
    if agent.name == "serving-agent":
        expected = agent.parent / "serving-package"
        staged = json.loads((agent.parent / ".staged.json").read_bytes())
        manifest = json.loads((expected / "package-manifest.json").read_bytes())
        if (root != expected or manifest.get("package_digest") != staged.get("package_digest")
                or not staged.get("knowledgehub_commit")):
            raise ImportError("workflow scripts release binding mismatch")
        members = manifest.get("members")
        if (not isinstance(members, dict)
                or "scripts/hermes_workflow_clarification.py" not in members
                or str(resolved.relative_to(root)) not in members):
            raise ImportError("workflow scripts package members unavailable")
        package_digest = hashlib.sha256(json.dumps(
            {"algorithm": "sha256-canonical-members-v1", "members": members},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if package_digest != staged["package_digest"]:
            raise ImportError("workflow scripts package digest mismatch")
        for relative, digest in members.items():
            if relative.startswith("scripts/"):
                member = root / relative
                if member.is_symlink() or hashlib.sha256(member.read_bytes()).hexdigest() != digest:
                    raise ImportError("workflow scripts package member mismatch")
    else:
        configured = os.environ.get("IVD_KB_ROOT")
        if not configured or root != Path(configured).resolve(strict=True) or not (root / ".git").exists():
            raise ImportError("workflow scripts explicit source binding required")
    scripts = root / "scripts"
    helper = scripts / "hermes_workflow_clarification.py"
    if not helper.is_file() or helper.is_symlink() or scripts.is_symlink():
        raise ImportError("workflow scripts dependency missing or linked")
    for name, module in list(sys.modules.items()):
        if name == "scripts":
            locations = getattr(module, "__path__", None)
            resolved_locations = [Path(p).resolve() for p in locations] if locations is not None else []
            # Existing source tests may have Python's implicit namespace spanning
            # the selected KB and this Agent's scripts directory. Only that
            # known empty namespace is narrowed, after all cached children pass.
            allowed_locations = {scripts, agent / "scripts"} if agent.name != "serving-agent" else {scripts}
            if (not resolved_locations
                    or any(p not in allowed_locations for p in resolved_locations)):
                raise ImportError(f"workflow scripts namespace conflict: {resolved_locations!r}")
        elif name.startswith("scripts."):
            file = getattr(module, "__file__", None)
            if not file or not Path(file).resolve().is_relative_to(scripts):
                # Source runner owns independent Agent scripts (e.g. environment
                # isolation). Preserve them only when no KB module shares that
                # name; never permit a foreign KB dependency or release module.
                relative = Path(*name.split(".")[1:]).with_suffix(".py")
                source_agent_module = (agent.name != "serving-agent" and file
                    and Path(file).resolve() == agent / "scripts" / relative
                    and not (scripts / relative).exists())
                if not source_agent_module:
                    raise ImportError(f"workflow scripts cached module conflict: {name}: {file}")
    if "scripts" in sys.modules:
        sys.modules["scripts"].__path__ = [str(scripts)]
        if sys.modules["scripts"].__spec__ is not None:
            sys.modules["scripts"].__spec__.submodule_search_locations = [str(scripts)]
    else:
        namespace = ModuleType("scripts")
        namespace.__path__ = [str(scripts)]
        namespace.__package__ = "scripts"
        namespace.__spec__ = importlib.machinery.ModuleSpec("scripts", loader=None, is_package=True)
        namespace.__spec__.submodule_search_locations = namespace.__path__
        sys.modules["scripts"] = namespace


def _load_module(path_text: str, mtime_ns: int) -> ModuleType:
    path = Path(path_text)
    if path.name in {"after_sales_workflow_gate.py", "hermes_fast_response_pipeline.py",
                     "hermes_context_envelope.py"}:
        _bind_workflow_scripts(path)
    return _load_module_cached(path_text, mtime_ns)


@lru_cache(maxsize=8)
def _load_module_cached(path_text: str, mtime_ns: int) -> ModuleType:
    path = Path(path_text)
    module_name = f"hermes_after_sales_{abs(hash((path_text, mtime_ns)))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load after-sales module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def resolve_after_sales_task(guard: dict[str, Any], message: str, *, actor_id: str = "",
                             pending_task: dict[str, Any] | None = None,
                             _loader=None) -> dict[str, Any]:
    """Load the sole task resolver from the verified workflow dependency root."""
    configured_text = str(guard.get("fast_response_module") or guard.get("workflow_module") or '').strip()
    if not configured_text:
        return {}
    configured = Path(configured_text)
    path = configured.with_name("hermes_context_envelope.py")
    if not path.is_file():
        return {}
    module = (_loader or _load_module)(str(path), path.stat().st_mtime_ns)
    return module.resolve_current_task(message, actor_id=actor_id, pending_task=pending_task)



def _load_presentation_policy(
    fast_response_module: str, *, platform: str, task_type: str = "general",
    detail_requested: bool = False, _loader=None,
) -> tuple[ModuleType | None, dict[str, Any]]:
    """Load the canonical code beside the configured release-owned pipeline."""
    if not fast_response_module or not Path(fast_response_module).is_file():
        return None, {}
    path = Path(fast_response_module).with_name("hermes_channel_presentation.py")
    if not path.is_file():
        return None, {}  # Compatibility with releases predating this contract.
    try:
        module = (_loader or _load_module)(str(path), path.stat().st_mtime_ns)
        if not all(callable(getattr(module, name, None)) for name in (
            "presentation_contract", "render_presentation_instructions", "prepare_user_text"
        )):
            return None, {}  # Old KB release: retain its existing guard/policy.
        contract = module.presentation_contract(platform, task_type=task_type,
                                                 detail_requested=detail_requested)
        return module, dict(contract)
    except Exception:
        # Presentation loading must not discard the numeric/source guard.
        return None, {}


def explicit_sop_version_mismatch(question: str, document: str, version: str) -> tuple[str, str] | None:
    """Identify a directly requested SOP revision that differs from a source."""
    import re

    parts = re.findall(r'[A-Za-z0-9]+', str(document or ''))
    if len(parts) < 3 or parts[0].upper() != 'SOP' or not str(version or '').strip():
        return None
    document_pattern = r'[-_\s]*'.join(re.escape(part) for part in parts)
    pattern = re.compile(r'(?<![A-Za-z0-9])' + document_pattern
                         + r'\s*(?:版本\s*)?([A-Za-z]\d+(?:\.\d+)?)\b', re.I)
    revisions = {match.group(1).upper() for match in pattern.finditer(str(question or ''))}
    selected = str(version).strip().upper()
    mismatches = sorted(rev for rev in revisions if rev != selected)
    return (mismatches[0], selected) if mismatches else None
