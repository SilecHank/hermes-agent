from types import SimpleNamespace

from gateway.ivd_final_validator import validate_final_response
from gateway.ivd_renderer import IVDRenderer


POLICY = {
    "schema_version": 1,
    "render_mode": "strict",
    "scalar_template": "{value} {unit}.",
    "fallback_request": "当前正式知识包未收录该问题，请补充产品名称、版本或SOP编号。",
}


def _hit(**overrides):
    values = {
        "knowledge_kind": "parameter",
        "product_line": "NIFTY",
        "product_variant": "",
        "value": "200",
        "unit": "uL",
        "effective_status": "active",
        "source_document_id": "SOP-JL-001",
        "source_version": "A1",
        "source_locator": "L10-L12",
        "source_path": "knowledge-base/formal/nifty.md",
        "source_sha256": "a" * 64,
        "source_record_digest": "b" * 64,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_strict_scalar_renderer_does_not_expand_beyond_requested_value():
    rendered = IVDRenderer(POLICY).render_registry_hit(_hit())

    assert rendered.text == "200 uL."
    assert rendered.answer_shape == "scalar"
    assert "建议" not in rendered.text
    assert "原理" not in rendered.text


def test_diagnostic_renderer_asks_only_the_compiled_discriminator():
    rendered = IVDRenderer(POLICY).render_diagnostic(
        {
            "outcome": "needs_discriminator",
            "questions": ["同批样本是否同时偏低？"],
            "lookup_count": 1,
            "effect_count": 0,
        }
    )

    assert rendered.text == "同批样本是否同时偏低？"
    assert rendered.answer_shape == "diagnostic"


def test_final_validator_rejects_product_mixing():
    decision = validate_final_response(
        text="200 uL.",
        contract={"product_line": "CNV-seq", "answer_shape": "scalar"},
        effect_receipt={"hit": _hit(), "model_calls": 0, "index_transactions": 0},
    )

    assert not decision.allowed
    assert decision.reasons == ("product_scope_mismatch",)


def test_final_validator_rejects_candidate_or_pending_knowledge():
    for status in ("candidate", "candidate_from_case", "pending_verify"):
        decision = validate_final_response(
            text="200 uL.",
            contract={"product_line": "NIFTY", "answer_shape": "scalar"},
            effect_receipt={"hit": _hit(effective_status=status), "model_calls": 0},
        )

        assert not decision.allowed
        assert "non_serving_knowledge" in decision.reasons


def test_final_validator_rejects_exact_parameter_without_complete_source():
    decision = validate_final_response(
        text="200 uL.",
        contract={"product_line": "NIFTY", "answer_shape": "scalar"},
        effect_receipt={
            "hit": _hit(source_locator=None, source_sha256=None),
            "model_calls": 0,
        },
    )

    assert not decision.allowed
    assert decision.reasons == ("formal_source_incomplete",)


def test_final_validator_rejects_model_or_filesystem_effects_on_exact_answer():
    decision = validate_final_response(
        text="200 uL.",
        contract={"product_line": "NIFTY", "answer_shape": "scalar"},
        effect_receipt={
            "hit": _hit(),
            "model_calls": 1,
            "filesystem_scans": 1,
            "index_transactions": 0,
        },
    )

    assert not decision.allowed
    assert decision.reasons == ("unexpected_model_call", "unexpected_filesystem_scan")


def test_final_validator_allows_only_the_contract_index_budget():
    allowed = validate_final_response(
        text="200 uL.",
        contract={
            "product_line": "NIFTY",
            "answer_shape": "scalar",
            "max_index_transactions": 1,
        },
        effect_receipt={"hit": _hit(), "model_calls": 0, "index_transactions": 1},
    )
    blocked = validate_final_response(
        text="200 uL.",
        contract={
            "product_line": "NIFTY",
            "answer_shape": "scalar",
            "max_index_transactions": 0,
        },
        effect_receipt={"hit": _hit(), "model_calls": 0, "index_transactions": 1},
    )

    assert allowed.allowed
    assert blocked.reasons == ("unexpected_index_transaction",)


def test_final_validator_rejects_diagnostic_product_or_source_mismatch():
    decision = validate_final_response(
        text="优先排查批次性偏差。",
        contract={"product_line": "NIFTY", "answer_shape": "diagnostic"},
        effect_receipt={
            "diagnostic_pattern": {
                "product_line": "CNV-seq",
                "formal_source_ids": [{"document": "SOP-X", "version": ""}],
            },
            "model_calls": 0,
            "index_transactions": 0,
        },
    )

    assert not decision.allowed
    assert decision.reasons == ("product_scope_mismatch", "formal_source_incomplete")


def test_final_validator_rejects_another_product_name_in_rendered_text():
    decision = validate_final_response(
        text="CNV-seq 的投入量为 200 uL。",
        contract={
            "product_line": "NIFTY",
            "known_product_lines": ("NIFTY", "CNV-seq"),
            "answer_shape": "scalar",
        },
        effect_receipt={"hit": _hit(), "model_calls": 0, "index_transactions": 0},
    )

    assert not decision.allowed
    assert decision.reasons == ("product_text_mismatch",)


def test_final_validator_rejects_non_serving_diagnostic_pattern():
    source = {
        "document": "SOP-JL-001",
        "version": "A1",
        "path": "knowledge-base/formal/nifty.md",
        "locator": "L10-L12",
        "source_sha256": "a" * 64,
        "source_record_digest": "b" * 64,
    }
    decision = validate_final_response(
        text="优先排查批次性偏差。",
        contract={"product_line": "NIFTY", "answer_shape": "diagnostic"},
        effect_receipt={
            "diagnostic_pattern": {
                "product_line": "NIFTY",
                "effective_status": "pending_verify",
                "formal_source_ids": [source],
            },
            "model_calls": 0,
        },
    )

    assert not decision.allowed
    assert decision.reasons == ("non_serving_knowledge",)
