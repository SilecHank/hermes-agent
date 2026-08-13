"""Fail-closed serving contracts for managed IVD answer turns."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


_PACKAGE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SERVING_FIELDS = frozenset(
    {
        "serving_package_path",
        "serving_agent_path",
        "source_vault_path",
        "dispatch_policy_path",
        "render_policy_path",
        "context_budget",
        "retrieval_budget",
        "skill_allowlist",
        "receipt_destination",
    }
)
_CACHE_LIMIT = 16
_CACHE: OrderedDict[
    tuple[str, int, int, int, int], "ServingProjection"
] = OrderedDict()
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
    serving_projection_digest: str
    receipt_destination: str
    serving_projection: Mapping[str, Any]


@dataclass(frozen=True)
class CompatibilityExecutionContract:
    contract_id: str
    package_digest: str
    serving_projection_digest: str
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
            _CACHE.move_to_end(key)
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
        digests = payload.get("projection_digests")
        serving = projections.get("serving") if isinstance(projections, dict) else None
        if not isinstance(identity, dict) or not isinstance(serving, dict):
            raise IVDRuntimeConfigurationError("IVD serving projection is missing or invalid")
        if set(identity) != {
            "package_digest",
            "execution_contract_schema_version",
            "turn_receipt_schema_version",
        }:
            raise IVDRuntimeConfigurationError("IVD shared identity is invalid")
        digest = identity.get("package_digest")
        if not isinstance(digest, str) or not _PACKAGE_DIGEST_RE.fullmatch(digest):
            raise IVDRuntimeConfigurationError("invalid IVD package digest")
        if any(
            not isinstance(identity.get(field), str) or not identity[field].strip()
            for field in (
                "execution_contract_schema_version",
                "turn_receipt_schema_version",
            )
        ):
            raise IVDRuntimeConfigurationError("IVD schema versions are required")
        if expected_package_digest is not None and digest != expected_package_digest:
            raise IVDRuntimeConfigurationError("IVD package digest mismatch")
        if set(serving) != _SERVING_FIELDS:
            raise IVDRuntimeConfigurationError("IVD serving projection fields are invalid")
        for field in (
            "serving_package_path",
            "serving_agent_path",
            "source_vault_path",
            "dispatch_policy_path",
            "render_policy_path",
            "receipt_destination",
        ):
            value = serving.get(field)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise IVDRuntimeConfigurationError(f"serving {field} must be absolute")
        if not serving["serving_package_path"].endswith("/serving-package"):
            raise IVDRuntimeConfigurationError("invalid serving package path")
        if not serving["serving_agent_path"].endswith("/serving-agent"):
            raise IVDRuntimeConfigurationError("invalid serving agent path")
        if not serving["source_vault_path"].endswith("/source-vault"):
            raise IVDRuntimeConfigurationError("invalid source vault path")
        package_prefix = serving["serving_package_path"] + "/"
        if not serving["dispatch_policy_path"].startswith(package_prefix) or not serving[
            "render_policy_path"
        ].startswith(package_prefix):
            raise IVDRuntimeConfigurationError("serving policy path escapes package")
        for field in ("context_budget", "retrieval_budget"):
            value = serving.get(field)
            if type(value) is not int or value < 1:
                raise IVDRuntimeConfigurationError(f"serving {field} must be positive")
        if serving.get("skill_allowlist") != []:
            raise IVDRuntimeConfigurationError("serving skill_allowlist must be empty")
        canonical = json.dumps(
            serving, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        projection_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        declared_digest = digests.get("serving") if isinstance(digests, dict) else None
        if (
            not isinstance(declared_digest, str)
            or not _PACKAGE_DIGEST_RE.fullmatch(declared_digest)
            or declared_digest != projection_digest
        ):
            raise IVDRuntimeConfigurationError("serving projection digest mismatch")

        result = ServingProjection(
            package_digest=digest,
            serving_projection_digest=projection_digest,
            receipt_destination=serving["receipt_destination"],
            serving_projection=_freeze(serving),
        )
        for stale_key in tuple(_CACHE):
            if stale_key[0] == key[0] and stale_key != key:
                del _CACHE[stale_key]
        _CACHE[key] = result
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_LIMIT:
            _CACHE.popitem(last=False)
        return result


def prepare_ivd_turn(projection: ServingProjection) -> PreparedIVDTurn:
    if not isinstance(projection, ServingProjection):
        raise IVDRuntimeConfigurationError("a serving projection is required")
    identity = (
        f"{projection.package_digest}\0{projection.serving_projection_digest}"
    )
    contract = CompatibilityExecutionContract(
        contract_id="ivd-contract-" + hashlib.sha256(identity.encode()).hexdigest(),
        package_digest=projection.package_digest,
        serving_projection_digest=projection.serving_projection_digest,
        receipt_destination=projection.receipt_destination,
        serving_projection=projection.serving_projection,
    )
    return PreparedIVDTurn(execution_contract=contract)
