from gateway.ivd_evidence import build_answer_sidecar


def test_numeric_claim_without_adopted_evidence_fails_closed():
    result = build_answer_sidecar(
        answer="240 ng。",
        answer_shape="scalar_lookup",
        validated_sources=[],
        product_scope="carrier-screening",
    )

    assert result["status"] == "needs_source"
    assert result["evidence_ids"] == []


def test_unselected_retrieval_candidate_is_not_adopted():
    sources = [
        {
            "evidence_id": "ev-a",
            "status": "validated",
            "authority": "formal_sop",
            "source_path": "knowledge-base/reference/product-a.md",
            "source_revision": "rev-a",
        },
        {
            "evidence_id": "ev-b",
            "status": "validated",
            "authority": "formal_sop",
            "source_path": "knowledge-base/reference/product-b.md",
            "source_revision": "rev-b",
        },
    ]

    result = build_answer_sidecar(
        answer="240 ng。",
        answer_shape="scalar_lookup",
        validated_sources=sources,
        adopted_evidence_ids=["ev-a"],
        product_scope="carrier-screening",
    )

    assert result["status"] == "validated"
    assert result["evidence_ids"] == ["ev-a"]
    assert result["sources"] == [
        {
            "evidence_id": "ev-a",
            "authority": "formal_sop",
            "source_path": "knowledge-base/reference/product-a.md",
            "source_revision": "rev-a",
        }
    ]


def test_candidate_and_extracted_sources_never_enter_sidecar():
    result = build_answer_sidecar(
        answer="240 ng。",
        answer_shape="scalar_lookup",
        validated_sources=[
            {
                "evidence_id": "ev-bad",
                "status": "validated",
                "authority": "formal_sop",
                "source_path": "knowledge-base/_extracted/source.md",
                "source_revision": "rev-bad",
            }
        ],
        adopted_evidence_ids=["ev-bad"],
        product_scope="carrier-screening",
    )

    assert result["status"] == "needs_source"
    assert result["evidence_ids"] == []
