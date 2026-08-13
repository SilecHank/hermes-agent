"""Fail-closed serving contracts for managed IVD answer turns."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


_PACKAGE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CACHE: dict[tuple[str, int, int, int, int], "ServingProjection"] = {}
_CACHE_LOCK = threading.Lock()


class IVDRuntimeConfigurationError(RuntimeError):
    """The managed IVD runtime cannot establish a serving contract."""


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
    receipt_destination: str
    serving_projection: Mapping[str, Any]


@dataclass(frozen=True)
class CompatibilityExecutionContract:
    contract_id: str
    package_digest: str
    receipt_destination: str
    serving_projection: Mapping[str, Any]
    trusted_legacy_answer_enabled: bool = True


@dataclass(frozen=True)
class PreparedIVDTurn:
    execution_contract: CompatibilityExecutionContract
    trusted_legacy_answer_enabled: bool = True
    execution_contract_count: int = 1

    def __post_init__(self) -> None:
        if self.execution_contract_count != 1:
            raise IVDRuntimeConfigurationError("an IVD turn requires exactly one contract")


def _stat_key(path: Path) -> tuple[str, int, int, int, int]:
    try:
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
    except OSError as error:
        raise IVDRuntimeConfigurationError(
            f"cannot stat IVD serving projection: {path}"
        ) from error
    return (
        str(resolved),
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_mtime_ns),
        int(stat.st_size),
    )


def load_serving_projection(
    manifest_path: str | Path,
    *,
    expected_package_digest: str | None = None,
) -> ServingProjection:
    """Load and cache only package identity plus ``projections.serving``."""
    path = Path(manifest_path).expanduser()
    key = _stat_key(path)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        if expected_package_digest and cached.package_digest != expected_package_digest:
            raise IVDRuntimeConfigurationError("IVD package digest mismatch")
        return cached

    try:
        payload = json.loads(Path(key[0]).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise IVDRuntimeConfigurationError(
            f"cannot load IVD serving projection: {path}"
        ) from error
    if not isinstance(payload, dict):
        raise IVDRuntimeConfigurationError("IVD projection envelope must be an object")
    identity = payload.get("shared_identity")
    projections = payload.get("projections")
    serving = projections.get("serving") if isinstance(projections, dict) else None
    if not isinstance(identity, dict) or not isinstance(serving, dict):
        raise IVDRuntimeConfigurationError("IVD serving projection is missing or invalid")
    digest = identity.get("package_digest")
    serving_digest = serving.get("package_digest")
    destination = serving.get("receipt_destination")
    if not isinstance(digest, str) or not _PACKAGE_DIGEST_RE.fullmatch(digest):
        raise IVDRuntimeConfigurationError("invalid IVD package digest")
    if serving_digest != digest:
        raise IVDRuntimeConfigurationError("serving projection package digest mismatch")
    if expected_package_digest is not None and digest != expected_package_digest:
        raise IVDRuntimeConfigurationError("IVD package digest mismatch")
    if not isinstance(destination, str) or not destination.strip():
        raise IVDRuntimeConfigurationError("serving receipt_destination is required")

    result = ServingProjection(
        package_digest=digest,
        receipt_destination=destination,
        serving_projection=_freeze(serving),
    )
    with _CACHE_LOCK:
        _CACHE[key] = result
    return result


def prepare_ivd_turn(projection: ServingProjection) -> PreparedIVDTurn:
    if not isinstance(projection, ServingProjection):
        raise IVDRuntimeConfigurationError("a serving projection is required")
    identity = f"{projection.package_digest}\0{projection.receipt_destination}"
    contract = CompatibilityExecutionContract(
        contract_id="ivd-contract-" + hashlib.sha256(identity.encode()).hexdigest(),
        package_digest=projection.package_digest,
        receipt_destination=projection.receipt_destination,
        serving_projection=projection.serving_projection,
    )
    return PreparedIVDTurn(execution_contract=contract)
