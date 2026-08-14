import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import pytest

import gateway.ivd_execution_contract as execution_contracts
from gateway.ivd_execution_contract import (
    CompatibilityExecutionContract,
    IVDRuntimeConfigurationError,
    load_serving_projection,
    prepare_ivd_turn,
)


PACKAGE_DIGEST = "a" * 64


def _canonical_digest(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _set_manifest_digest(payload):
    unsigned = dict(payload)
    unsigned.pop("manifest_digest", None)
    payload["manifest_digest"] = _canonical_digest(unsigned)
    return payload


def _serving_projection(tmp_path):
    root = str(tmp_path)
    return {
        "serving_package_path": f"{root}/serving-package",
        "serving_agent_path": f"{root}/serving-agent",
        "source_vault_path": f"{root}/source-vault",
        "dispatch_policy_path": f"{root}/serving-package/dispatch.json",
        "render_policy_path": f"{root}/serving-package/render.json",
        "context_budget": 8,
        "retrieval_budget": 2,
        "skill_allowlist": [],
        "receipt_destination": f"{root}/observability/receipts.jsonl",
    }


def _write_release(tmp_path, *, package_digest=PACKAGE_DIGEST, serving=None):
    projection = serving or _serving_projection(tmp_path)
    manifest = tmp_path / "release-manifest.json"
    payload = {
        "schema_version": 1,
        "release_id": "f" * 64,
        "shared_identity": {
            "package_digest": package_digest,
            "execution_contract_schema_version": "1",
            "turn_receipt_schema_version": "1",
        },
        "projections": {
            "serving": projection,
            "build": {"ignored": "build-only"},
            "control": {"ignored": "control-only"},
            "observability": {"ignored": "observability-only"},
        },
        "projection_digests": {},
        "authority_owners": [{"field": "x", "owner": "y", "source": "z"}],
    }
    payload["projection_digests"] = {
        name: _canonical_digest(value)
        for name, value in payload["projections"].items()
    }
    manifest.write_text(
        json.dumps(_set_manifest_digest(payload)),
        encoding="utf-8",
    )
    return manifest


def test_loader_accepts_release_schema_and_keeps_only_serving_identity(tmp_path):
    projection = load_serving_projection(_write_release(tmp_path))
    prepared = prepare_ivd_turn(projection)

    assert projection.package_digest == PACKAGE_DIGEST
    assert projection.serving_projection_digest == _canonical_digest(
        _serving_projection(tmp_path)
    )
    assert prepared.execution_contract.package_digest == PACKAGE_DIGEST
    assert prepared.execution_contract.serving_projection_digest == (
        projection.serving_projection_digest
    )
    assert prepared.execution_contract_count == 1
    assert prepared.trusted_legacy_answer_enabled is True
    assert "ignored" not in repr(prepared)
    assert not hasattr(prepared, "user_text")
    with pytest.raises(FrozenInstanceError):
        prepared.execution_contract = None


def test_only_prepared_execution_contract_can_issue_file_delivery_grant(tmp_path):
    prepared = prepare_ivd_turn(load_serving_projection(_write_release(tmp_path)))
    contract = prepared.execution_contract

    grant = contract.issue_file_delivery_grant(("b" * 64,))

    assert grant.contract_id == contract.contract_id
    assert grant.package_digest == contract.package_digest
    assert grant.allowed_object_ids == ("b" * 64,)
    forged = CompatibilityExecutionContract(
        contract_id=contract.contract_id,
        package_digest=contract.package_digest,
        serving_projection_digest=contract.serving_projection_digest,
        receipt_destination=contract.receipt_destination,
        serving_projection=contract.serving_projection,
    )
    with pytest.raises(IVDRuntimeConfigurationError, match="trusted"):
        forged.issue_file_delivery_grant(("b" * 64,))


def test_loader_rejects_serving_tamper_even_with_updated_projection_digest(tmp_path):
    manifest = _write_release(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["projections"]["serving"]["context_budget"] = 9
    payload["projection_digests"]["serving"] = _canonical_digest(
        payload["projections"]["serving"]
    )
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IVDRuntimeConfigurationError, match="manifest digest"):
        load_serving_projection(manifest)


def test_loader_rejects_shared_identity_tamper(tmp_path):
    manifest = _write_release(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["shared_identity"]["turn_receipt_schema_version"] = "2"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IVDRuntimeConfigurationError, match="manifest digest"):
        load_serving_projection(manifest)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("release_id"),
        lambda payload: payload.__setitem__("unexpected", True),
    ],
)
def test_loader_rejects_noncanonical_top_level_field_set(tmp_path, mutation):
    manifest = _write_release(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    mutation(payload)
    _set_manifest_digest(payload)
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IVDRuntimeConfigurationError, match="fields"):
        load_serving_projection(manifest)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload["projection_digests"].pop("serving"),
        lambda payload: payload["projection_digests"].__setitem__("serving", "0" * 64),
        lambda payload: payload["projections"]["serving"].__setitem__(
            "package_digest", PACKAGE_DIGEST
        ),
        lambda payload: payload["projections"]["serving"].__setitem__("unknown", True),
        lambda payload: payload["projections"]["serving"].__setitem__(
            "skill_allowlist", ["forbidden"]
        ),
        lambda payload: payload["projections"]["serving"].__setitem__(
            "receipt_destination", "relative/receipts.jsonl"
        ),
        lambda payload: payload["projections"]["serving"].__setitem__(
            "context_budget", 0
        ),
    ],
)
def test_loader_rejects_projection_outside_release_schema(tmp_path, mutation):
    manifest = _write_release(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    mutation(payload)
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IVDRuntimeConfigurationError):
        load_serving_projection(manifest)


def test_loader_rejects_receipt_destination_outside_release_observability(tmp_path):
    serving = _serving_projection(tmp_path)
    serving["receipt_destination"] = str(tmp_path / "elsewhere/receipts.jsonl")

    with pytest.raises(IVDRuntimeConfigurationError):
        load_serving_projection(_write_release(tmp_path, serving=serving))


def test_loader_rejects_policy_dotdot_escape_after_normalization(tmp_path):
    serving = _serving_projection(tmp_path)
    serving["dispatch_policy_path"] = str(
        tmp_path / "serving-package/../outside/dispatch.json"
    )

    with pytest.raises(IVDRuntimeConfigurationError):
        load_serving_projection(_write_release(tmp_path, serving=serving))


def test_loader_freezes_normalized_policy_paths(tmp_path):
    serving = _serving_projection(tmp_path)
    serving["dispatch_policy_path"] = str(
        tmp_path / "serving-package/policies/../dispatch.json"
    )

    projection = load_serving_projection(_write_release(tmp_path, serving=serving))

    assert projection.serving_projection["dispatch_policy_path"] == str(
        (tmp_path / "serving-package/dispatch.json").resolve()
    )


def test_loader_rejects_directory_style_receipt_destination(tmp_path):
    serving = _serving_projection(tmp_path)
    serving["receipt_destination"] = str(tmp_path / "observability/receipts")

    with pytest.raises(IVDRuntimeConfigurationError):
        load_serving_projection(_write_release(tmp_path, serving=serving))


def test_loader_rejects_final_receipt_symlink(tmp_path):
    observability = tmp_path / "observability"
    observability.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    (observability / "receipts.jsonl").symlink_to(outside)

    with pytest.raises(IVDRuntimeConfigurationError):
        load_serving_projection(_write_release(tmp_path))


def test_projection_exposes_controlled_receipt_sink_with_explicit_close(tmp_path):
    projection = load_serving_projection(_write_release(tmp_path))

    assert not isinstance(projection.receipt_destination, str)
    assert projection.receipt_destination.closed is False
    projection.receipt_destination.close()
    projection.receipt_destination.close()

    assert projection.receipt_destination.closed is True


def test_loader_reuses_cached_projection_without_reopening_same_file(tmp_path):
    manifest = _write_release(tmp_path)
    first = load_serving_projection(manifest)

    with patch("pathlib.Path.open", side_effect=AssertionError("reopened")):
        second = load_serving_projection(manifest)

    assert second is first


def test_loader_reopens_sink_closed_by_previous_gateway_owner(tmp_path):
    manifest = _write_release(tmp_path)
    first = load_serving_projection(manifest)
    first.receipt_destination.close()

    second = load_serving_projection(manifest)

    assert second is not first
    assert second.receipt_destination.closed is False
    second.receipt_destination.close()


def test_concurrent_cache_miss_reads_projection_once(tmp_path):
    manifest = _write_release(tmp_path)
    real_read_text = Path.read_text
    reads = 0
    reads_lock = threading.Lock()

    def counted_read(path, *args, **kwargs):
        nonlocal reads
        with reads_lock:
            reads += 1
        time.sleep(0.05)
        return real_read_text(path, *args, **kwargs)

    with patch("pathlib.Path.read_text", counted_read):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: load_serving_projection(manifest), range(8)))

    assert reads == 1
    assert all(item is results[0] for item in results)


def test_loader_replaces_stale_cache_entry_for_same_path(tmp_path):
    manifest = _write_release(tmp_path)
    first = load_serving_projection(manifest)
    replacement_digest = "d" * 64
    _write_release(tmp_path, package_digest=replacement_digest)

    second = load_serving_projection(manifest)
    keys = [key for key in execution_contracts._CACHE if key[0] == str(manifest.resolve())]

    assert second is not first
    assert second.package_digest == replacement_digest
    assert len(keys) == 1


def test_cache_reload_does_not_close_previously_returned_sink(tmp_path):
    manifest = _write_release(tmp_path)
    first = prepare_ivd_turn(load_serving_projection(manifest))
    _write_release(tmp_path, package_digest="d" * 64)

    second = prepare_ivd_turn(load_serving_projection(manifest))

    assert second is not first
    assert first.execution_contract.receipt_destination.closed is False
    first.close()
    second.close()


def test_projection_cache_is_global_lru_bounded_and_keeps_recent_entry(tmp_path):
    execution_contracts._CACHE.clear()
    manifests = []
    for index in range(execution_contracts._CACHE_LIMIT + 2):
        root = tmp_path / str(index)
        root.mkdir()
        manifest = _write_release(root, package_digest=f"{index:064x}")
        manifests.append(manifest)
        load_serving_projection(manifest)
    recent = load_serving_projection(manifests[-2])

    assert len(execution_contracts._CACHE) <= execution_contracts._CACHE_LIMIT
    assert any(
        key[0] == str(manifests[-2].resolve())
        for key in execution_contracts._CACHE
    )
    assert recent.package_digest == f"{execution_contracts._CACHE_LIMIT:064x}"


def test_cache_eviction_does_not_close_previously_returned_sink(tmp_path):
    execution_contracts._CACHE.clear()
    first_root = tmp_path / "first"
    first_root.mkdir()
    first = prepare_ivd_turn(load_serving_projection(_write_release(first_root)))
    for index in range(execution_contracts._CACHE_LIMIT + 1):
        root = tmp_path / f"evict-{index}"
        root.mkdir()
        load_serving_projection(
            _write_release(root, package_digest=f"{index + 1:064x}")
        )

    assert first.execution_contract.receipt_destination.closed is False
    first.close()
