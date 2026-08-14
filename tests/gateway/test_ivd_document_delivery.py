from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from gateway.ivd_document_delivery import (
    CapabilityDenied,
    DocumentDeliveryError,
    ManifestBoundDocumentDelivery,
)


@dataclass(frozen=True)
class Grant:
    allowed_capabilities: tuple[str, ...]


class Sink:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def append(self, payload: bytes) -> bool:
        self.payloads.append(payload)
        return True


def _vault(
    root: Path,
    *,
    relative: str | None = None,
    logical_path: str = "01_SOP/document.pdf",
) -> tuple[str, str]:
    content = b"formal SOP content"
    digest = hashlib.sha256(content).hexdigest()
    object_id = "b" * 64
    relative = relative or f"objects/sha256/{digest[:2]}/{digest}"
    safe_relative = f"objects/sha256/{digest[:2]}/{digest}"
    target = root / safe_relative
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    payload = {
        "schema_version": 2,
        "originals": [
            {
                "document": "SOP-JL-100",
                "version": "A1",
                "logical_path": logical_path,
                "locator": "1-2",
                "source_sha256": digest,
                "source_record_digest": object_id,
                "object_path": relative,
                "object_digest": digest,
                "size": len(content),
            }
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["source_vault_digest"] = hashlib.sha256(canonical.encode()).hexdigest()
    (root / "source-vault-manifest.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return object_id, digest


def test_delivery_requires_manifest_object_capability_and_digest(tmp_path):
    object_id, digest = _vault(tmp_path)
    sent = []
    sink = Sink()
    delivery = ManifestBoundDocumentDelivery(tmp_path, sent.append, sink)

    with pytest.raises(CapabilityDenied):
        delivery.send(object_id=object_id, expected_digest=digest, grant=Grant(("serve_public",)))
    with pytest.raises(DocumentDeliveryError):
        delivery.send(object_id="c" * 64, expected_digest="c" * 64, grant=Grant(("deliver_file",)))
    with pytest.raises(DocumentDeliveryError):
        delivery.send(object_id=object_id, expected_digest="d" * 64, grant=Grant(("deliver_file",)))
    assert sent == []
    assert sink.payloads == []


def test_delivery_sends_one_verified_object_and_records_one_effect(tmp_path):
    object_id, digest = _vault(tmp_path)
    sent = []
    sink = Sink()
    delivery = ManifestBoundDocumentDelivery(tmp_path, sent.append, sink)

    receipt = delivery.send(object_id=object_id, expected_digest=digest, grant=Grant(("deliver_file",)))

    assert len(sent) == 1
    assert sent[0].content == b"formal SOP content"
    assert sent[0].object_id == object_id
    assert sent[0].logical_name == "document.pdf"
    assert receipt.status == "delivered"
    assert receipt.object_id == object_id
    assert len(sink.payloads) == 1
    assert json.loads(sink.payloads[0]) == {
        "effect": "deliver_file",
        "object_digest": digest,
        "object_id": object_id,
        "status": "delivered",
    }


@pytest.mark.parametrize(
    "relative",
    ["../outside", "/tmp/outside", r"objects\\escape", "objects/../escape"],
)
def test_delivery_rejects_manifest_path_traversal_without_search_fallback(tmp_path, relative):
    object_id, digest = _vault(tmp_path, relative=relative)
    with pytest.raises(DocumentDeliveryError):
        delivery = ManifestBoundDocumentDelivery(tmp_path, lambda _: None, Sink())
        delivery.send(object_id=object_id, expected_digest=digest, grant=Grant(("deliver_file",)))


@pytest.mark.parametrize(
    "logical_path",
    ["../secret.pdf", "/tmp/secret.pdf", r"01_SOP\secret.pdf", "."],
)
def test_delivery_rejects_unsafe_logical_name_without_sender_fallback(
    tmp_path, logical_path
):
    _vault(tmp_path, logical_path=logical_path)
    sent = []
    sink = Sink()

    with pytest.raises(DocumentDeliveryError):
        ManifestBoundDocumentDelivery(tmp_path, sent.append, sink)

    assert sent == []
    assert sink.payloads == []


def test_delivery_rejects_symlink_and_changed_content(tmp_path):
    object_id, digest = _vault(tmp_path)
    relative = f"objects/sha256/{digest[:2]}/{digest}"
    target = tmp_path / relative
    target.unlink()
    outside = tmp_path.parent / "outside-document"
    outside.write_bytes(b"formal SOP content")
    target.symlink_to(outside)
    delivery = ManifestBoundDocumentDelivery(tmp_path, lambda _: None, Sink())
    with pytest.raises(DocumentDeliveryError):
        delivery.send(object_id=object_id, expected_digest=digest, grant=Grant(("deliver_file",)))

    target.unlink()
    target.write_bytes(b"changed")
    delivery = ManifestBoundDocumentDelivery(tmp_path, lambda _: None, Sink())
    with pytest.raises(DocumentDeliveryError):
        delivery.send(object_id=object_id, expected_digest=digest, grant=Grant(("deliver_file",)))


def test_receipt_failure_is_terminal_and_does_not_resend(tmp_path):
    object_id, digest = _vault(tmp_path)
    sent = []

    class FailedSink:
        def append(self, payload: bytes) -> bool:
            return False

    delivery = ManifestBoundDocumentDelivery(tmp_path, sent.append, FailedSink())
    with pytest.raises(DocumentDeliveryError):
        delivery.send(object_id=object_id, expected_digest=digest, grant=Grant(("deliver_file",)))
    assert len(sent) == 1
