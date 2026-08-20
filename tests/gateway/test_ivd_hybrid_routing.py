from types import MappingProxyType, SimpleNamespace

from gateway.ivd_hybrid_router import decide_hybrid_route
from gateway.run import _prepare_gateway_ivd_boundary


def _envelope(**overrides):
    values = {
        "intent": "parameter",
        "product_line": "carrier_screening",
        "product_variant": "default",
        "answer_shape": "scalar_lookup",
        "ambiguities": (),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _result(**overrides):
    values = {
        "outcome": "answer",
        "answer_shape": "scalar_lookup",
        "text": "200 uL。",
        "sources": ("SOP-001",),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _prepared():
    package = SimpleNamespace()
    package.execution_contract = SimpleNamespace(
        package_digest="a" * 64,
        serving_projection=MappingProxyType({"serving_package_path": "/tmp/package"}),
    )
    return package


def test_exact_scalar_parameter_uses_structured_package():
    decision = decide_hybrid_route(
        "无创提取需要多少血浆？",
        envelope=_envelope(product_line="nifty"),
        result=_result(),
    )

    assert decision.mode == "package_scalar"


def test_mechanism_question_returns_to_expert_mode_even_if_package_has_scalar():
    decision = decide_hybrid_route(
        "为什么无创提取需要这个血浆量？",
        envelope=_envelope(product_line="nifty"),
        result=_result(),
    )

    assert decision.mode == "expert"
    assert decision.reason == "expert_intent"


def test_mixed_parameter_and_reason_question_returns_to_expert_mode():
    decision = decide_hybrid_route(
        "携带者筛查DNA起始投入量是多少，为什么？",
        envelope=_envelope(),
        result=_result(text="240 ng。"),
    )

    assert decision.mode == "expert"


def test_ambiguous_or_non_scalar_package_result_returns_to_expert_mode():
    decision = decide_hybrid_route(
        "这个项目是多少？",
        envelope=_envelope(
            intent="troubleshooting",
            answer_shape="diagnostic",
            product_line=None,
            ambiguities=("product_line",),
        ),
        result=_result(answer_shape="diagnostic", sources=("SOP-001", "SOP-002")),
    )

    assert decision.mode == "expert"
    assert decision.reason in {"ambiguous", "non_scalar"}


def test_hybrid_boundary_returns_expert_turn_for_reasoning_question(monkeypatch):
    prepared = _prepared()
    legacy_turn = object()
    package_result = _result(
        text="240 ng。",
        intent="parameter",
        product_line="carrier_screening",
        product_variant="default",
        ambiguities=(),
    )
    monkeypatch.setattr(
        "gateway.ivd_runtime.prepare_enabled_ivd_turn",
        lambda *_args, **_kwargs: prepared,
    )
    monkeypatch.setattr(
        "gateway.ivd_runtime.execute_exclusive_ivd_turn",
        lambda *_args, **_kwargs: package_result,
    )
    monkeypatch.setattr(
        "gateway.after_sales_guard.prepare_after_sales_turn",
        lambda *_args, **_kwargs: legacy_turn,
    )

    selected, result = _prepare_gateway_ivd_boundary(
        {
            "after_sales_guard": {
                "enabled": True,
                "platforms": ["qqbot"],
                "engine_mode": "hybrid",
            }
        },
        platform="qqbot",
        message="携带者筛查DNA起始投入量是多少，为什么？",
        history=[],
    )

    assert selected is prepared
    assert result is legacy_turn


def test_hybrid_boundary_returns_package_for_exact_scalar(monkeypatch):
    prepared = _prepared()
    package_result = _result(
        text="200 uL。",
        intent="parameter",
        product_line="nifty",
        product_variant="default",
        ambiguities=(),
    )
    monkeypatch.setattr(
        "gateway.ivd_runtime.prepare_enabled_ivd_turn",
        lambda *_args, **_kwargs: prepared,
    )
    monkeypatch.setattr(
        "gateway.ivd_runtime.execute_exclusive_ivd_turn",
        lambda *_args, **_kwargs: package_result,
    )
    monkeypatch.setattr(
        "gateway.after_sales_guard.prepare_after_sales_turn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("expert router should not run for exact scalar")
        ),
    )

    selected, result = _prepare_gateway_ivd_boundary(
        {
            "after_sales_guard": {
                "enabled": True,
                "platforms": ["qqbot"],
                "engine_mode": "hybrid",
            }
        },
        platform="qqbot",
        message="无创提取需要多少血浆？",
        history=[],
    )

    assert selected is prepared
    assert result is package_result


def test_hybrid_probe_failure_falls_back_to_expert_turn(monkeypatch):
    from gateway.run import GatewayRunner

    prepared = _prepared()
    legacy_turn = object()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.__dict__["_ivd_prepared_contracts"] = {"qqbot": prepared}
    monkeypatch.setattr(
        "gateway.ivd_runtime.execute_exclusive_ivd_turn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("probe failed")),
    )
    monkeypatch.setattr(
        "gateway.after_sales_guard.prepare_after_sales_turn",
        lambda *_args, **_kwargs: legacy_turn,
    )

    _selected, result = runner._prepare_ivd_lifecycle(
        {
            "after_sales_guard": {
                "enabled": True,
                "platforms": ["qqbot"],
                "engine_mode": "hybrid",
            }
        },
        platform="qqbot",
        message="为什么这个流程失败？",
        history=[],
    )

    assert result is legacy_turn
