"""Fail-closed serving contracts for trusted IVD answer turns."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


_PACKAGE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_NON_SERVING_PROJECTIONS = frozenset({"build", "control", "observability"})


class IVDRuntimeConfigurationError(RuntimeError):
    """The trusted IVD runtime cannot establish a serving contract."""


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise IVDRuntimeConfigurationError("serving projection is not valid JSON data")


@dataclass(frozen=True)
class ServingProjection:
    package_digest: str
    serving_projection: Mapping[str, Any]


@dataclass(frozen=True)
class CompatibilityExecutionContract:
    package_digest: str
    serving_projection: Mapping[str, Any]
    trusted_legacy_answer_enabled: bool = True


@dataclass(frozen=True)
class PreparedIVDTurn:
    contract: CompatibilityExecutionContract
    contract_count: int = 1

    def __post_init__(self) -> None:
        if self.contract_count != 1:
            raise IVDRuntimeConfigurationError("an IVD turn requires exactly one contract")


def load_serving_projection(
    manifest_path: str | Path,
    *,
    expected_package_digest: str | None = None,
) -> ServingProjection:
    """Load only ``projections.serving`` from a KnowledgeHub manifest."""
    path = Path(manifest_path).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise IVDRuntimeConfigurationError(
            f"cannot load IVD serving projection: {path}"
        ) from error

    if not isinstance(payload, dict):
        raise IVDRuntimeConfigurationError("IVD release manifest must be an object")
    identity = payload.get("shared_identity")
    projections = payload.get("projections")
    if not isinstance(identity, dict) or not isinstance(projections, dict):
        raise IVDRuntimeConfigurationError(
            "IVD release manifest lacks shared_identity or projections"
        )
    digest = identity.get("package_digest")
    if not isinstance(digest, str) or not _PACKAGE_DIGEST_RE.fullmatch(digest):
        raise IVDRuntimeConfigurationError("invalid IVD package digest")
    if expected_package_digest is not None and digest != expected_package_digest:
        raise IVDRuntimeConfigurationError("IVD package digest mismatch")

    serving = projections.get("serving")
    if not isinstance(serving, dict):
        raise IVDRuntimeConfigurationError("IVD serving projection is missing or invalid")
    projection_kind = serving.get("projection") or serving.get("projection_type")
    if projection_kind in _NON_SERVING_PROJECTIONS:
        raise IVDRuntimeConfigurationError(
            f"{projection_kind} projection cannot be used for serving"
        )
    serving_digest = serving.get("package_digest")
    if serving_digest is not None and serving_digest != digest:
        raise IVDRuntimeConfigurationError("serving projection package digest mismatch")

    return ServingProjection(
        package_digest=digest,
        serving_projection=_freeze(serving),
    )


def prepare_ivd_turn(projection: ServingProjection) -> PreparedIVDTurn:
    """Prepare exactly one immutable contract without generating user text."""
    if not isinstance(projection, ServingProjection):
        raise IVDRuntimeConfigurationError("a serving projection is required")
    return PreparedIVDTurn(
        contract=CompatibilityExecutionContract(
            package_digest=projection.package_digest,
            serving_projection=projection.serving_projection,
        )
    )
