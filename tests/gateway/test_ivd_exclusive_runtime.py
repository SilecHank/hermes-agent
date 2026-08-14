from types import MappingProxyType, SimpleNamespace

import pytest

from gateway.run import _prepare_gateway_ivd_boundary


@pytest.fixture(autouse=True)
def _isolated_engine_cache(monkeypatch):
    import gateway.ivd_runtime as runtime

    monkeypatch.setattr(
        runtime,
        "_IVD_ENGINE_CACHE",
        runtime._IVDKnowledgeEngineCache(),
    )


def _prepared_package_turn(tmp_path):
    package = tmp_path / "serving-package"
    package.mkdir()
    projection = MappingProxyType(
        {
            "serving_package_path": str(package),
            "retrieval_budget": 1,
            "context_budget": 8,
            "skill_allowlist": (),
        }
    )
    contract = SimpleNamespace(
        package_digest="a" * 64,
        serving_projection=projection,
    )
    return SimpleNamespace(execution_contract=contract)


def test_active_package_turn_never_invokes_legacy_router(tmp_path, monkeypatch):
    prepared = _prepared_package_turn(tmp_path)
    calls = []

    class Result:
        text = "200 uL."
        answer_shape = "scalar"
        outcome = "answer"
        model_calls = 0
        index_transactions = 0
        filesystem_scans = 0
        effect_count = 0
        sources = ()

    class Engine:
        def __init__(self, root, *, expected_package_digest):
            calls.append(("engine", root))
            assert expected_package_digest == "a" * 64

        def close(self):
            calls.append(("close",))

    class Dispatcher:
        def __init__(self, root):
            calls.append(("dispatcher", root))

        def execute(self, engine, *, question, context="", evidence=None):
            calls.append(("dispatch", question, context, evidence))
            return SimpleNamespace(
                envelope=SimpleNamespace(
                    clarifying_questions=(),
                    model_call_budget=0,
                    indexed_retrieval_budget=0,
                ),
                result=Result(),
            )

    monkeypatch.setattr(
        "gateway.ivd_runtime.prepare_enabled_ivd_turn",
        lambda *_args, **_kwargs: prepared,
    )
    monkeypatch.setattr(
        "gateway.after_sales_guard.prepare_after_sales_turn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy router invoked")
        ),
    )
    monkeypatch.setattr("gateway.ivd_runtime.IVDDispatcher", Dispatcher)
    monkeypatch.setattr("gateway.ivd_runtime.IVDKnowledgeEngine", Engine)

    selected, result = _prepare_gateway_ivd_boundary(
        {
            "after_sales_guard": {
                "enabled": True,
                "platforms": ["qqbot"],
                "engine_mode": "package",
            }
        },
        platform="qqbot",
        message="无创提取需要多少血浆？",
        history=[],
    )

    assert selected is prepared
    assert result.text == "200 uL."
    assert result.dispatch_count == 1
    assert result.final_validation_count == 1
    assert result.model_calls == 0
    assert result.index_transactions == 0
    assert [call[0] for call in calls] == ["dispatcher", "engine", "dispatch"]


def test_package_turn_caps_recent_context_to_compiled_budget(tmp_path, monkeypatch):
    prepared = _prepared_package_turn(tmp_path)
    observed = {}

    class Engine:
        def __init__(self, _root, *, expected_package_digest):
            assert expected_package_digest == "a" * 64

        def close(self):
            pass

    class Dispatcher:
        def __init__(self, _root):
            pass

        def execute(self, _engine, *, question, context="", evidence=None):
            observed.update(question=question, context=context, evidence=evidence)
            return SimpleNamespace(
                envelope=SimpleNamespace(
                    clarifying_questions=("请补充产品名称。",),
                    model_call_budget=0,
                    indexed_retrieval_budget=0,
                ),
                result=None,
            )

    monkeypatch.setattr("gateway.ivd_runtime.IVDDispatcher", Dispatcher)
    monkeypatch.setattr("gateway.ivd_runtime.IVDKnowledgeEngine", Engine)

    from gateway.ivd_runtime import execute_exclusive_ivd_turn

    result = execute_exclusive_ivd_turn(
        prepared,
        question="多少？",
        history=[
            {"role": "user", "content": "NIFTY无创提取流程"},
            {"role": "assistant", "content": "不应计入"},
            {"role": "user", "content": "需要多少血浆以及后续说明"},
        ],
    )

    assert result.outcome == "clarification"
    assert len(observed["context"]) <= 8
    assert "不应计入" not in observed["context"]


def test_compatibility_mode_keeps_legacy_router_reachable(tmp_path, monkeypatch):
    prepared = _prepared_package_turn(tmp_path)
    legacy_turn = object()
    calls = []
    monkeypatch.setattr(
        "gateway.ivd_runtime.prepare_enabled_ivd_turn",
        lambda *_args, **_kwargs: prepared,
    )
    monkeypatch.setattr(
        "gateway.after_sales_guard.prepare_after_sales_turn",
        lambda *_args, **_kwargs: calls.append("legacy") or legacy_turn,
    )

    selected, result = _prepare_gateway_ivd_boundary(
        {
            "after_sales_guard": {
                "enabled": True,
                "platforms": ["qqbot"],
                "engine_mode": "compatibility",
            }
        },
        platform="qqbot",
        message="问题",
        history=[],
    )

    assert selected is prepared
    assert result is legacy_turn
    assert calls == ["legacy"]


def test_package_mode_requires_loaded_package_contract(monkeypatch):
    monkeypatch.setattr(
        "gateway.ivd_runtime.prepare_enabled_ivd_turn",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="package contract"):
        _prepare_gateway_ivd_boundary(
            {
                "after_sales_guard": {
                    "enabled": True,
                    "platforms": ["qqbot"],
                    "engine_mode": "package",
                }
            },
            platform="qqbot",
            message="问题",
            history=[],
        )


@pytest.mark.parametrize(
    ("model_calls", "index_transactions", "expected"),
    ((1, 0, "model-call budget"), (0, 1, "index-transaction budget")),
)
def test_package_turn_fails_closed_when_engine_exceeds_envelope_budget(
    tmp_path, monkeypatch, model_calls, index_transactions, expected
):
    prepared = _prepared_package_turn(tmp_path)
    result = SimpleNamespace(
        text="不应发送",
        answer_shape="scalar",
        outcome="answer",
        model_calls=model_calls,
        index_transactions=index_transactions,
        filesystem_scans=0,
        effect_count=0,
        sources=(),
    )
    envelope = SimpleNamespace(
        clarifying_questions=(),
        model_call_budget=0,
        indexed_retrieval_budget=0,
    )

    class Engine:
        def __init__(self, _root, *, expected_package_digest):
            assert expected_package_digest == "a" * 64
            pass

        def close(self):
            pass

    class Dispatcher:
        def __init__(self, _root):
            pass

        def execute(self, _engine, **_kwargs):
            return SimpleNamespace(envelope=envelope, result=result)

    monkeypatch.setattr("gateway.ivd_runtime.IVDDispatcher", Dispatcher)
    monkeypatch.setattr("gateway.ivd_runtime.IVDKnowledgeEngine", Engine)

    from gateway.ivd_runtime import execute_exclusive_ivd_turn

    with pytest.raises(RuntimeError, match=expected):
        execute_exclusive_ivd_turn(prepared, question="问题")
