from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading

import pytest

from gateway.ivd_document_delivery import (
    CapabilityDenied,
    DocumentDeliveryError,
    ManifestBoundDocumentDelivery,
)
from gateway.ivd_execution_contract import ServingProjection, prepare_ivd_turn
from gateway.ivd_receipt_sink import EffectConflict
from gateway.ivd_receipt_sink import ReceiptPersistenceError


class Sink:
    def __init__(self, *, failures: int = 0) -> None:
        self.payloads: list[bytes] = []
        self.append_calls = 0
        self.failures = failures

    def append(self, payload: bytes) -> bool:
        self.append_calls += 1
        if self.failures:
            self.failures -= 1
            return False
        self.payloads.append(payload)
        return True


def _authorization(
    object_id: str,
    *,
    package_digest: str = "a" * 64,
    projection_digest: str = "c" * 64,
    allowed_object_ids: tuple[str, ...] | None = None,
):
    projection = ServingProjection(
        package_digest=package_digest,
        serving_projection_digest=projection_digest,
        receipt_destination=object(),
        serving_projection={},
    )
    contract = prepare_ivd_turn(projection).execution_contract
    grant = contract.issue_file_delivery_grant(
        allowed_object_ids or (object_id,)
    )
    return contract, grant


def _vault(
    root: Path,
    *,
    content: bytes = b"formal SOP content",
    relative: str | None = None,
    logical_path: str = "01_SOP/document.pdf",
) -> tuple[str, str]:
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
    contract, grant = _authorization(object_id)

    with pytest.raises(CapabilityDenied):
        delivery.send(
            object_id=object_id,
            expected_digest=digest,
            contract=contract,
            grant=type("DuckGrant", (), {"allowed_capabilities": ("deliver_file",)})(),
        )
    with pytest.raises(DocumentDeliveryError):
        delivery.send(
            object_id="c" * 64,
            expected_digest="c" * 64,
            contract=contract,
            grant=grant,
        )
    with pytest.raises(DocumentDeliveryError):
        delivery.send(
            object_id=object_id,
            expected_digest="d" * 64,
            contract=contract,
            grant=grant,
        )
    assert sent == []
    assert sink.payloads == []


def test_delivery_rejects_contract_package_and_object_binding_mismatch(tmp_path):
    object_id, digest = _vault(tmp_path)
    sink = Sink()
    delivery = ManifestBoundDocumentDelivery(tmp_path, lambda _: None, sink)
    contract, grant = _authorization(object_id)
    other_contract, _ = _authorization(
        object_id,
        package_digest="d" * 64,
        projection_digest="e" * 64,
    )
    _, wrong_object_grant = _authorization(
        object_id,
        allowed_object_ids=("f" * 64,),
    )

    for candidate_contract, candidate_grant in (
        (type("DuckContract", (), {"contract_id": contract.contract_id})(), grant),
        (other_contract, grant),
        (contract, wrong_object_grant),
    ):
        with pytest.raises(CapabilityDenied):
            delivery.send(
                object_id=object_id,
                expected_digest=digest,
                contract=candidate_contract,
                grant=candidate_grant,
            )

    assert sink.payloads == []


def test_delivery_rejects_tampered_trusted_grant_before_send(tmp_path):
    object_id, digest = _vault(tmp_path)
    sent = []
    sink = Sink()
    delivery = ManifestBoundDocumentDelivery(tmp_path, sent.append, sink)
    contract, grant = _authorization(object_id)
    object.__setattr__(grant, "grant_id", "ivd-file-grant-" + "0" * 64)

    with pytest.raises(CapabilityDenied):
        delivery.send(
            object_id=object_id,
            expected_digest=digest,
            contract=contract,
            grant=grant,
        )

    assert sent == []
    assert sink.payloads == []


def test_delivery_sends_one_verified_object_and_records_one_effect(tmp_path):
    object_id, digest = _vault(tmp_path)
    sent = []
    sink = Sink()
    delivery = ManifestBoundDocumentDelivery(tmp_path, sent.append, sink)
    contract, grant = _authorization(object_id)

    receipt = delivery.send(
        object_id=object_id,
        expected_digest=digest,
        contract=contract,
        grant=grant,
    )

    assert len(sent) == 1
    assert sent[0].content == b"formal SOP content"
    assert sent[0].object_id == object_id
    assert sent[0].logical_name == "document.pdf"
    assert receipt.status == "delivered"
    assert receipt.object_id == object_id
    assert len(sink.payloads) == 1
    assert json.loads(sink.payloads[0]) == {
        "effect": "deliver_file",
        "contract_id": contract.contract_id,
        "grant_id": grant.grant_id,
        "package_digest": contract.package_digest,
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
    contract, grant = _authorization(object_id)
    with pytest.raises(DocumentDeliveryError):
        delivery = ManifestBoundDocumentDelivery(tmp_path, lambda _: None, Sink())
        delivery.send(
            object_id=object_id,
            expected_digest=digest,
            contract=contract,
            grant=grant,
        )


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
    contract, grant = _authorization(object_id)
    relative = f"objects/sha256/{digest[:2]}/{digest}"
    target = tmp_path / relative
    target.unlink()
    outside = tmp_path.parent / "outside-document"
    outside.write_bytes(b"formal SOP content")
    target.symlink_to(outside)
    delivery = ManifestBoundDocumentDelivery(tmp_path, lambda _: None, Sink())
    with pytest.raises(DocumentDeliveryError):
        delivery.send(
            object_id=object_id,
            expected_digest=digest,
            contract=contract,
            grant=grant,
        )

    target.unlink()
    target.write_bytes(b"changed")
    delivery = ManifestBoundDocumentDelivery(tmp_path, lambda _: None, Sink())
    with pytest.raises(DocumentDeliveryError):
        delivery.send(
            object_id=object_id,
            expected_digest=digest,
            contract=contract,
            grant=grant,
        )


def test_receipt_failure_and_retry_return_existing_result_without_resend(tmp_path):
    object_id, digest = _vault(tmp_path)
    sent = []
    sink = Sink(failures=1)
    delivery = ManifestBoundDocumentDelivery(tmp_path, sent.append, sink)
    contract, grant = _authorization(object_id)

    with pytest.raises(ReceiptPersistenceError):
        delivery.send(
            object_id=object_id,
            expected_digest=digest,
            contract=contract,
            grant=grant,
        )
    second = delivery.send(
        object_id=object_id,
        expected_digest=digest,
        contract=contract,
        grant=grant,
    )

    assert len(sent) == 1
    assert second.status == "delivered"
    assert sink.append_calls == 2
    assert len(sink.payloads) == 1


def test_same_effect_is_once_only_under_concurrent_retries(tmp_path):
    object_id, digest = _vault(tmp_path)
    sent = []
    sender_lock = threading.Lock()

    def sender(document):
        with sender_lock:
            sent.append(document)

    sink = Sink()
    delivery = ManifestBoundDocumentDelivery(tmp_path, sender, sink)
    contract, grant = _authorization(object_id)
    results = []

    def invoke():
        results.append(
            delivery.send(
                object_id=object_id,
                expected_digest=digest,
                contract=contract,
                grant=grant,
            )
        )

    threads = [threading.Thread(target=invoke) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(sent) == 1
    assert len(sink.payloads) == 1
    assert len(results) == 8
    assert all(result is results[0] for result in results)


def test_same_effect_key_rejects_digest_drift_across_delivery_instances(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    object_id, first_digest = _vault(first_root)
    _, second_digest = _vault(second_root, content=b"changed formal SOP")
    sink = Sink()
    sent = []
    contract, grant = _authorization(object_id)
    first_delivery = ManifestBoundDocumentDelivery(first_root, sent.append, sink)
    second_delivery = ManifestBoundDocumentDelivery(second_root, sent.append, sink)

    first_delivery.send(
        object_id=object_id,
        expected_digest=first_digest,
        contract=contract,
        grant=grant,
    )
    with pytest.raises(EffectConflict):
        second_delivery.send(
            object_id=object_id,
            expected_digest=second_digest,
            contract=contract,
            grant=grant,
        )

    assert len(sent) == 1


def test_new_execution_contract_can_deliver_same_object_as_a_new_effect(tmp_path):
    object_id, digest = _vault(tmp_path)
    sink = Sink()
    sent = []
    delivery = ManifestBoundDocumentDelivery(tmp_path, sent.append, sink)
    first_contract, first_grant = _authorization(object_id)
    second_contract, second_grant = _authorization(object_id)

    first = delivery.send(
        object_id=object_id,
        expected_digest=digest,
        contract=first_contract,
        grant=first_grant,
    )
    second = delivery.send(
        object_id=object_id,
        expected_digest=digest,
        contract=second_contract,
        grant=second_grant,
    )

    assert first_contract.contract_id != second_contract.contract_id
    assert len(sent) == 2
    assert len(sink.payloads) == 2
    assert second is not first
