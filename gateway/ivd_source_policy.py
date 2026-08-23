"""Runtime boundary for the IVD maintenance source workspace."""

from __future__ import annotations

from pathlib import Path


_AUXILIARY_MARKERS = (
    "formywin",
    "workspace-kb",
    "hermes-transfer",
    "recovered-sources-",
    "recovery-",
    "phase_",
    "debug_",
    "patch_",
)


def classify_runtime_kb_root(root: str | Path) -> str:
    """Return a stable source class without reading or executing the path."""
    normalized = str(Path(root).expanduser()).replace("\\", "/").casefold()
    if any(marker in normalized for marker in _AUXILIARY_MARKERS):
        return "auxiliary_workspace"
    return "authoritative_or_unclassified"


def runtime_kb_root_allowed(root: str | Path) -> bool:
    return classify_runtime_kb_root(root) != "auxiliary_workspace"
