"""Fail-closed serving contracts for managed IVD answer turns."""

from __future__ import annotations

import atexit
import hashlib
import hmac
import json
import os
import re
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised by monkeypatch on POSIX CI
    fcntl = None  # type: ignore[assignment]


_PACKAGE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "release_id",
        "shared_identity",
        "projections",
        "projection_digests",
        "authority_owners",
        "manifest_digest",
    }
)
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
_CONTRACT_AUTHORITY = object()
_FILE_GRANT_ISSUER = object()
_FILE_GRANT_SECRET = os.urandom(32)


def _release_manifest_payload(raw: object) -> dict[str, Any] | None:
    """Unwrap the compiler envelope while retaining legacy direct manifests."""
    if not isinstance(raw, dict):
        return None
    if {"status", "reason", "manifest", "digest"} <= set(raw):
        manifest = raw.get("manifest")
        if (
            raw.get("status") != "ready"
            or raw.get("reason") != "release_manifest_ready"
            or not isinstance(manifest, dict)
            or raw.get("digest") != manifest.get("manifest_digest")
        ):
            return None
        return manifest
    return raw


class IVDRuntimeConfigurationError(RuntimeError):
    """The managed IVD runtime cannot establish a serving contract."""


class FileDeliveryCapabilityGrant:
    """A process-issued grant bound to one contract, package, and object set."""

    __slots__ = (
        "contract_id",
        "package_digest",
        "allowed_object_ids",
        "allowed_capabilities",
        "grant_id",
        "_signature",
    )

    def __init__(
        self,
        *,
        contract_id: str,
        package_digest: str,
        allowed_object_ids: tuple[str, ...],
        issuer: object,
    ) -> None:
        if issuer is not _FILE_GRANT_ISSUER:
            raise IVDRuntimeConfigurationError("file delivery grant issuer is invalid")
        if not contract_id or not _PACKAGE_DIGEST_RE.fullmatch(package_digest):
            raise IVDRuntimeConfigurationError("file delivery grant identity is invalid")
        normalized = tuple(sorted(set(allowed_object_ids)))
        if not normalized or any(not _PACKAGE_DIGEST_RE.fullmatch(item) for item in normalized):
            raise IVDRuntimeConfigurationError("file delivery grant objects are invalid")
        identity = "\0".join((contract_id, package_digest, *normalized)).encode("utf-8")
        signature = hmac.new(_FILE_GRANT_SECRET, identity, hashlib.sha256).hexdigest()
        object.__setattr__(self, "contract_id", contract_id)
        object.__setattr__(self, "package_digest", package_digest)
        object.__setattr__(self, "allowed_object_ids", normalized)
        object.__setattr__(self, "allowed_capabilities", ("deliver_file",))
        object.__setattr__(self, "grant_id", "ivd-file-grant-" + signature)
        object.__setattr__(self, "_signature", signature)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("file delivery grants are immutable")

    def verify(self) -> bool:
        if (
            self.allowed_capabilities != ("deliver_file",)
            or self.grant_id != "ivd-file-grant-" + self._signature
            or not self.contract_id
            or not _PACKAGE_DIGEST_RE.fullmatch(self.package_digest)
            or not self.allowed_object_ids
            or any(
                not _PACKAGE_DIGEST_RE.fullmatch(item)
                for item in self.allowed_object_ids
            )
        ):
            return False
        identity = "\0".join(
            (self.contract_id, self.package_digest, *self.allowed_object_ids)
        ).encode("utf-8")
        expected = hmac.new(_FILE_GRANT_SECRET, identity, hashlib.sha256).hexdigest()
        return hmac.compare_digest(self._signature, expected)


class AppendOnlyReceiptSink:
    """A startup-opened append target that never re-resolves its path."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._lock = threading.Lock()
        self._closed = False
        atexit.register(self.close)

    @classmethod
    def open(
        cls,
        destination: str | Path,
        *,
        release_root: str | Path,
    ) -> "AppendOnlyReceiptSink":
        if fcntl is None:
            raise IVDRuntimeConfigurationError(
                "managed IVD receipts require process-safe file locking"
            )
        target = Path(destination)
        if not target.is_absolute() or target.suffix.lower() != ".jsonl":
            raise IVDRuntimeConfigurationError(
                "receipt destination must be an absolute .jsonl file"
            )
        try:
            canonical_release_root = Path(release_root).resolve(strict=False)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            canonical_parent = target.parent.resolve(strict=True)
            observability_root = (
                canonical_release_root / "observability"
            ).resolve(strict=True)
        except OSError as error:
            raise IVDRuntimeConfigurationError(
                "cannot prepare IVD receipt destination"
            ) from error
        if (
            not observability_root.is_relative_to(canonical_release_root)
            or not canonical_parent.is_relative_to(observability_root)
        ):
            raise IVDRuntimeConfigurationError(
                "receipt destination must be inside release observability"
            )

        directory_fd = None
        try:
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(canonical_parent, directory_flags)
            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
            flags |= getattr(os, "O_NOFOLLOW", 0)
            if os.open in os.supports_dir_fd:
                fd = os.open(target.name, flags, 0o600, dir_fd=directory_fd)
            else:
                fd = os.open(canonical_parent / target.name, flags, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise IVDRuntimeConfigurationError(
                    "receipt destination must be a regular file"
                )
            os.set_inheritable(fd, False)
        except IVDRuntimeConfigurationError:
            raise
        except OSError as error:
            raise IVDRuntimeConfigurationError(
                "cannot open IVD receipt destination"
            ) from error
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
        return cls(fd)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def append(self, payload: bytes) -> bool:
        with self._lock:
            if self._closed:
                return False
            assert fcntl is not None
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX)
                original_size = os.lseek(self._fd, 0, os.SEEK_END)
                try:
                    written = os.write(self._fd, payload)
                except OSError:
                    os.ftruncate(self._fd, original_size)
                    return False
                if written != len(payload):
                    os.ftruncate(self._fd, original_size)
                    return False
                return True
            except OSError:
                return False
            finally:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                except OSError:
                    pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            os.close(self._fd)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, AppendOnlyReceiptSink):
        return value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise IVDRuntimeConfigurationError("serving projection is not valid JSON data")


@dataclass(frozen=True)
class ServingProjection:
    package_digest: str
    serving_projection_digest: str
    receipt_destination: AppendOnlyReceiptSink
    serving_projection: Mapping[str, Any]


@dataclass(frozen=True)
class CompatibilityExecutionContract:
    contract_id: str
    package_digest: str
    serving_projection_digest: str
    receipt_destination: AppendOnlyReceiptSink
    serving_projection: Mapping[str, Any]
    trusted_legacy_answer_enabled: bool = True
    _authority_proof: object | None = field(
        default=None, repr=False, compare=False
    )

    def issue_file_delivery_grant(
        self, allowed_object_ids: tuple[str, ...]
    ) -> FileDeliveryCapabilityGrant:
        if self._authority_proof is not _CONTRACT_AUTHORITY:
            raise IVDRuntimeConfigurationError(
                "file delivery grants require a trusted execution contract"
            )
        return FileDeliveryCapabilityGrant(
            contract_id=self.contract_id,
            package_digest=self.package_digest,
            allowed_object_ids=allowed_object_ids,
            issuer=_FILE_GRANT_ISSUER,
        )


def validate_file_delivery_authorization(
    contract: object,
    grant: object,
    *,
    object_id: str,
) -> tuple[CompatibilityExecutionContract, FileDeliveryCapabilityGrant]:
    """Validate exact process-issued contract and grant identities."""
    if (
        type(contract) is not CompatibilityExecutionContract
        or contract._authority_proof is not _CONTRACT_AUTHORITY
    ):
        raise IVDRuntimeConfigurationError(
            "file delivery requires a trusted execution contract"
        )
    if type(grant) is not FileDeliveryCapabilityGrant or not grant.verify():
        raise IVDRuntimeConfigurationError(
            "file delivery requires a trusted capability grant"
        )
    if (
        grant.contract_id != contract.contract_id
        or grant.package_digest != contract.package_digest
        or object_id not in grant.allowed_object_ids
    ):
        raise IVDRuntimeConfigurationError(
            "file delivery grant binding does not match the requested object"
        )
    return contract, grant


@dataclass(frozen=True)
class PreparedIVDTurn:
    execution_contract: CompatibilityExecutionContract
    trusted_legacy_answer_enabled: bool = True
    execution_contract_count: int = 1

    def __post_init__(self) -> None:
        if self.execution_contract_count != 1:
            raise IVDRuntimeConfigurationError("an IVD turn requires exactly one contract")

    def close(self) -> None:
        self.execution_contract.receipt_destination.close()


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
            if cached.receipt_destination.closed:
                del _CACHE[key]
            else:
                _CACHE.move_to_end(key)
                if (
                    expected_package_digest
                    and cached.package_digest != expected_package_digest
                ):
                    raise IVDRuntimeConfigurationError("IVD package digest mismatch")
                return cached

        try:
            payload = json.loads(Path(key[0]).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise IVDRuntimeConfigurationError(
                f"cannot load IVD serving projection: {path}"
            ) from error
        payload = _release_manifest_payload(payload)
        if payload is None:
            raise IVDRuntimeConfigurationError("IVD projection envelope must be an object")
        if set(payload) != _MANIFEST_FIELDS:
            raise IVDRuntimeConfigurationError("IVD release manifest fields are invalid")
        declared_manifest_digest = payload.get("manifest_digest")
        unsigned_manifest = dict(payload)
        unsigned_manifest.pop("manifest_digest")
        canonical_manifest = json.dumps(
            unsigned_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        calculated_manifest_digest = hashlib.sha256(
            canonical_manifest.encode("utf-8")
        ).hexdigest()
        if (
            not isinstance(declared_manifest_digest, str)
            or not _PACKAGE_DIGEST_RE.fullmatch(declared_manifest_digest)
            or declared_manifest_digest != calculated_manifest_digest
        ):
            raise IVDRuntimeConfigurationError("IVD release manifest digest mismatch")
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
        canonical_serving = dict(serving)
        for field in (
            "serving_package_path",
            "serving_agent_path",
            "source_vault_path",
            "dispatch_policy_path",
            "render_policy_path",
            "receipt_destination",
        ):
            canonical_serving[field] = str(Path(serving[field]).resolve(strict=False))
        package_path = Path(canonical_serving["serving_package_path"])
        if package_path.name != "serving-package":
            raise IVDRuntimeConfigurationError("invalid serving package path")
        if Path(canonical_serving["serving_agent_path"]).name != "serving-agent":
            raise IVDRuntimeConfigurationError("invalid serving agent path")
        if Path(canonical_serving["source_vault_path"]).name != "source-vault":
            raise IVDRuntimeConfigurationError("invalid source vault path")
        if any(
            not Path(canonical_serving[field]).is_relative_to(package_path)
            for field in ("dispatch_policy_path", "render_policy_path")
        ):
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

        receipt_sink = AppendOnlyReceiptSink.open(
            canonical_serving["receipt_destination"],
            release_root=package_path.parent,
        )
        canonical_serving["receipt_destination"] = receipt_sink

        result = ServingProjection(
            package_digest=digest,
            serving_projection_digest=projection_digest,
            receipt_destination=receipt_sink,
            serving_projection=_freeze(canonical_serving),
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
        f"\0{os.urandom(32).hex()}"
    )
    contract = CompatibilityExecutionContract(
        contract_id="ivd-contract-" + hashlib.sha256(identity.encode()).hexdigest(),
        package_digest=projection.package_digest,
        serving_projection_digest=projection.serving_projection_digest,
        receipt_destination=projection.receipt_destination,
        serving_projection=projection.serving_projection,
        _authority_proof=_CONTRACT_AUTHORITY,
    )
    return PreparedIVDTurn(execution_contract=contract)
