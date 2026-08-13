import hashlib
import json


def canonical_digest(value):
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def release_manifest(serving, *, package_digest):
    projections = {
        "serving": serving,
        "build": {"ignored": "build-only"},
        "control": {"ignored": "control-only"},
        "observability": {"ignored": "observability-only"},
    }
    manifest = {
        "schema_version": 1,
        "release_id": "f" * 64,
        "shared_identity": {
            "package_digest": package_digest,
            "execution_contract_schema_version": "1",
            "turn_receipt_schema_version": "1",
        },
        "projections": projections,
        "projection_digests": {
            name: canonical_digest(projection)
            for name, projection in projections.items()
        },
        "authority_owners": [{"field": "x", "owner": "y", "source": "z"}],
    }
    manifest["manifest_digest"] = canonical_digest(manifest)
    return manifest
