from gateway.after_sales_guard import AfterSalesTurn, build_source_validation_fallback


def test_scalar_claim_failure_names_missing_step_context_instead_of_generic_static_text():
    turn = AfterSalesTurn(
        context="",
        facts={},
        validator=None,
        allowed_numeric_claims=(),
        product_scope="NIFTY",
        product_variant="V5",
        source_location={"status": "source_candidates_found"},
    )

    response = build_source_validation_fallback(turn, "scalar_claim_not_unique")

    assert "NIFTY V5" in response
    assert "实验步骤" in response
    assert "当前问题需要唯一数值，但草稿中未形成唯一的已核实数值" not in response


def test_legacy_static_scalar_fallback_is_not_present_in_guard_source():
    source = __import__("gateway.after_sales_guard", fromlist=["__file__"]).__file__
    text = open(source, encoding="utf-8").read()
    assert "当前问题需要唯一数值，但草稿中未形成唯一的已核实数值" not in text
