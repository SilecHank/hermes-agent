import json
from dataclasses import FrozenInstanceError

import pytest

from gateway.ivd_execution_contract import (
    IVDRuntimeConfigurationError,
    load_serving_projection,
    prepare_ivd_turn,
)


PACKAGE_DIGEST = "a" * 64


def _write_release(tmp_path, *, digest=PACKAGE_DIGEST, serving=None, **projections):
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "shared_identity": {"package_digest": digest},
                "projections": {
                    "serving": serving
                    if serving is not None
                    else {"records": [{"record_id": "record-1"}]},
                    **projections,
                },
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_loader_returns_only_immutable_serving_projection_bound_to_package(tmp_path):
    manifest = _write_release(
        tmp_path,
        build={"secret": "build-only"},
        control={"secret": "control-only"},
        observability={"secret": "observability-only"},
    )

    projection = load_serving_projection(manifest)
    prepared = prepare_ivd_turn(projection)

    assert projection.package_digest == PACKAGE_DIGEST
    assert prepared.contract.package_digest == PACKAGE_DIGEST
    assert prepared.contract_count == 1
    assert prepared.contract.trusted_legacy_answer_enabled is True
    assert prepared.contract.serving_projection == {
        "records": ({"record_id": "record-1"},)
    }
    assert "secret" not in repr(prepared)
    assert not hasattr(prepared, "user_text")
    with pytest.raises(FrozenInstanceError):
        prepared.contract = None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"shared_identity": {}, "projections": {"serving": {}}},
        {
            "shared_identity": {"package_digest": "not-a-digest"},
            "projections": {"serving": {}},
        },
        {
            "shared_identity": {"package_digest": PACKAGE_DIGEST},
            "projections": {"build": {}},
        },
    ],
)
def test_loader_fails_closed_for_missing_or_malformed_serving_projection(
    tmp_path, payload
):
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IVDRuntimeConfigurationError):
        load_serving_projection(manifest)
