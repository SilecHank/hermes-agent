"""Manifest-bound, fail-closed IVD Source Vault document delivery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Protocol

from gateway.ivd_execution_contract import (
    IVDRuntimeConfigurationError,
    validate_file_delivery_authorization,
)
from gateway.ivd_receipt_sink import effect_ledger_for_destination


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_FIELDS = {"schema_version", "originals", "source_vault_digest"}
_ORIGINAL_REQUIRED_FIELDS = {
    "document",
    "version",
    "logical_path",
    "locator",
    "source_sha256",
    "source_record_digest",
    "object_path",
    "object_digest",
    "size",
}
_ORIGINAL_ALLOWED_FIELDS = _ORIGINAL_REQUIRED_FIELDS
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_OBJECT_BYTES = 128 * 1024 * 1024


class DocumentDeliveryError(RuntimeError):
    """Delivery failed and must not be rerouted into search."""


class CapabilityDenied(DocumentDeliveryError):
    """The authenticated grant does not permit document delivery."""


class ReceiptSink(Protocol):
    def append(self, payload: bytes) -> bool: ...


@dataclass(frozen=True)
class DeliveredDocument:
    object_id: str
    digest: str
    logical_name: str
    content: bytes


@dataclass(frozen=True)
class DocumentEffectReceipt:
    object_id: str
    object_digest: str
    status: str
    contract_id: str
    package_digest: str
    grant_id: str


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _safe_relative(value: object) -> str:
    text = str(value or "")
    path = PurePosixPath(text)
    if (
        not text
        or "\\" in text
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise DocumentDeliveryError("manifest object path is invalid")
    return path.as_posix()


def _read_at(root_fd: int, relative: str, maximum: int) -> bytes:
    parts = PurePosixPath(_safe_relative(relative)).parts
    current_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(parts[-1], flags, dir_fd=current_fd)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
                raise DocumentDeliveryError("manifest object is not a bounded regular file")
            data = bytearray()
            while len(data) <= maximum:
                chunk = os.read(fd, min(1024 * 1024, maximum + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > maximum:
                raise DocumentDeliveryError("manifest object exceeds size limit")
            return bytes(data)
        finally:
            os.close(fd)
    except (OSError, ValueError) as error:
        raise DocumentDeliveryError("manifest object cannot be opened safely") from error
    finally:
        os.close(current_fd)


class ManifestBoundDocumentDelivery:
    """Resolve one immutable object ID and emit exactly one delivery effect."""

    def __init__(
        self,
        source_vault_root: str | Path,
        sender: Callable[[DeliveredDocument], object],
        receipt_sink: ReceiptSink,
    ) -> None:
        root = Path(source_vault_root)
        if root.is_symlink():
            raise DocumentDeliveryError("source vault root cannot be a symlink")
        try:
            root = root.resolve(strict=True)
            self._root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError as error:
            raise DocumentDeliveryError("source vault is unavailable") from error
        self._sender = sender
        self._receipt_sink = receipt_sink
        self._effect_ledger = effect_ledger_for_destination(receipt_sink)
        self._objects = self._load_manifest()

    def __del__(self) -> None:
        fd = getattr(self, "_root_fd", None)
        if isinstance(fd, int):
            try:
                os.close(fd)
            except OSError:
                pass
            self._root_fd = None

    def _load_manifest(self) -> dict[str, Mapping[str, object]]:
        raw = _read_at(self._root_fd, "source-vault-manifest.json", _MAX_MANIFEST_BYTES)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise DocumentDeliveryError("source vault manifest is invalid") from error
        if not isinstance(payload, Mapping) or set(payload) != _MANIFEST_FIELDS:
            raise DocumentDeliveryError("source vault manifest fields are invalid")
        if type(payload.get("schema_version")) is not int or payload["schema_version"] != 2:
            raise DocumentDeliveryError("source vault manifest schema is invalid")
        originals = payload.get("originals")
        if not isinstance(originals, list):
            raise DocumentDeliveryError("source vault originals are invalid")
        unsigned = {"schema_version": 2, "originals": originals}
        if payload.get("source_vault_digest") != hashlib.sha256(_canonical(unsigned)).hexdigest():
            raise DocumentDeliveryError("source vault manifest digest mismatch")
        objects: dict[str, Mapping[str, object]] = {}
        for item in originals:
            if (
                not isinstance(item, Mapping)
                or not _ORIGINAL_REQUIRED_FIELDS <= set(item) <= _ORIGINAL_ALLOWED_FIELDS
            ):
                raise DocumentDeliveryError("source vault entry fields are invalid")
            object_digest = str(item.get("object_digest") or "")
            if not _SHA256.fullmatch(object_digest):
                raise DocumentDeliveryError("source vault object digest is invalid")
            object_id = str(item.get("source_record_digest") or "")
            if not _SHA256.fullmatch(object_id):
                raise DocumentDeliveryError("source vault object ID is invalid")
            if item.get("source_sha256") != object_digest:
                raise DocumentDeliveryError("source and object digests differ")
            if type(item.get("size")) is not int or not 0 <= item["size"] <= _MAX_OBJECT_BYTES:
                raise DocumentDeliveryError("source vault object size is invalid")
            logical_path = _safe_relative(item.get("logical_path"))
            object_path = _safe_relative(item.get("object_path"))
            expected_path = f"objects/sha256/{object_digest[:2]}/{object_digest}"
            if object_path != expected_path:
                raise DocumentDeliveryError("source vault object path is not content-addressed")
            normalized = {**item, "logical_path": logical_path, "object_path": object_path}
            previous = objects.setdefault(object_id, normalized)
            if (
                previous.get("object_path") != item.get("object_path")
                or previous.get("object_digest") != item.get("object_digest")
                or previous.get("size") != item.get("size")
            ):
                raise DocumentDeliveryError("source vault object binding conflicts")
        return objects

    def send(
        self,
        *,
        object_id: str,
        expected_digest: str,
        contract: object,
        grant: object,
    ) -> DocumentEffectReceipt:
        try:
            trusted_contract, trusted_grant = validate_file_delivery_authorization(
                contract,
                grant,
                object_id=object_id,
            )
        except IVDRuntimeConfigurationError as error:
            raise CapabilityDenied("trusted deliver_file authorization is required") from error
        if not _SHA256.fullmatch(object_id) or not _SHA256.fullmatch(expected_digest):
            raise DocumentDeliveryError("document digest is invalid")
        entry = self._objects.get(object_id)
        if entry is None:
            raise DocumentDeliveryError("document object is not in the manifest")
        if entry.get("object_digest") != expected_digest:
            raise DocumentDeliveryError("requested document digest mismatch")

        def deliver_once() -> tuple[DocumentEffectReceipt, bytes]:
            data = _read_at(
                self._root_fd,
                str(entry["object_path"]),
                _MAX_OBJECT_BYTES,
            )
            digest = hashlib.sha256(data).hexdigest()
            if digest != expected_digest or len(data) != entry["size"]:
                raise DocumentDeliveryError("document object integrity mismatch")
            logical_name = PurePosixPath(str(entry["logical_path"])).name
            if not logical_name:
                raise DocumentDeliveryError("document logical name is invalid")
            document = DeliveredDocument(object_id, digest, logical_name, data)
            try:
                self._sender(document)
            except Exception as error:
                raise DocumentDeliveryError("document sender failed") from error
            receipt = DocumentEffectReceipt(
                object_id=object_id,
                object_digest=digest,
                status="delivered",
                contract_id=trusted_contract.contract_id,
                package_digest=trusted_contract.package_digest,
                grant_id=trusted_grant.grant_id,
            )
            payload = _canonical(
                {
                    "effect": "deliver_file",
                    "contract_id": receipt.contract_id,
                    "grant_id": receipt.grant_id,
                    "package_digest": receipt.package_digest,
                    "object_digest": receipt.object_digest,
                    "object_id": receipt.object_id,
                    "status": receipt.status,
                }
            ) + b"\n"
            return receipt, payload

        return self._effect_ledger.execute(
            key=(
                trusted_contract.contract_id,
                trusted_contract.package_digest,
                "deliver_file",
                object_id,
            ),
            digest=expected_digest,
            operation=deliver_once,
            receipt_submitter=self._receipt_sink.append,
        )
