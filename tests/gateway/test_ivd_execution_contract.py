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
    manifest.write_text(
        json.dumps(
            {
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
                "projection_digests": {
                    "serving": _canonical_digest(projection),
                    "build": "b" * 64,
                    "control": "c" * 64,
                    "observability": "d" * 64,
                },
                "authority_owners": [{"field": "x", "owner": "y", "source": "z"}],
                "manifest_digest": "e" * 64,
            }
        ),
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


def test_loader_reuses_cached_projection_without_reopening_same_file(tmp_path):
    manifest = _write_release(tmp_path)
    first = load_serving_projection(manifest)

    with patch("pathlib.Path.open", side_effect=AssertionError("reopened")):
        second = load_serving_projection(manifest)

    assert second is first


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
