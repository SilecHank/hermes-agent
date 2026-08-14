from __future__ import annotations

import threading
from types import MappingProxyType, SimpleNamespace

import pytest

import gateway.ivd_runtime as runtime


def _prepared(tmp_path, digest: str):
    package = tmp_path / digest[:8]
    package.mkdir(exist_ok=True)
    projection = MappingProxyType({"serving_package_path": str(package)})
    contract = SimpleNamespace(
        package_digest=digest,
        serving_projection=projection,
    )
    return SimpleNamespace(execution_contract=contract)


class _Result:
    text = "200 uL."
    answer_shape = "scalar"
    outcome = "answer"
    model_calls = 0
    index_transactions = 0
    filesystem_scans = 0
    effect_count = 0
    sources = ()


class _Dispatcher:
    def __init__(self, _root):
        pass

    def execute(self, engine, *, question, evidence=None):
        engine.answer(question, evidence)
        return SimpleNamespace(
            envelope=SimpleNamespace(
                clarifying_questions=(),
                model_call_budget=0,
                indexed_retrieval_budget=0,
            ),
            result=_Result(),
        )


def _install_fakes(monkeypatch, engine_type):
    monkeypatch.setattr(runtime, "IVDKnowledgeEngine", engine_type)
    monkeypatch.setattr(runtime, "IVDDispatcher", _Dispatcher)
    monkeypatch.setattr(
        runtime,
        "_IVD_ENGINE_CACHE",
        runtime._IVDKnowledgeEngineCache(),
    )


def test_same_package_digest_reuses_one_engine_until_release_swap(
    tmp_path, monkeypatch
):
    created = []

    class Engine:
        def __init__(self, root, *, expected_package_digest):
            self.root = root
            self.digest = expected_package_digest
            self.questions = []
            self.close_count = 0
            created.append(self)

        def answer(self, question, _evidence):
            assert self.close_count == 0
            self.questions.append(question)

        def close(self):
            self.close_count += 1

    _install_fakes(monkeypatch, Engine)
    first = _prepared(tmp_path, "a" * 64)
    second = _prepared(tmp_path, "b" * 64)

    runtime.execute_exclusive_ivd_turn(first, question="one")
    runtime.execute_exclusive_ivd_turn(first, question="two")

    assert len(created) == 1
    assert created[0].questions == ["one", "two"]
    assert created[0].close_count == 0

    runtime.execute_exclusive_ivd_turn(second, question="three")

    assert len(created) == 2
    assert created[0].close_count == 1
    assert created[1].questions == ["three"]
    assert created[1].close_count == 0


def test_release_swap_waits_for_inflight_old_engine(tmp_path, monkeypatch):
    created = []
    entered = threading.Event()
    release = threading.Event()

    class Engine:
        def __init__(self, _root, *, expected_package_digest):
            self.digest = expected_package_digest
            self.close_count = 0
            created.append(self)

        def answer(self, question, _evidence):
            assert self.close_count == 0
            if question == "slow":
                entered.set()
                release.wait(timeout=2)

        def close(self):
            self.close_count += 1

    _install_fakes(monkeypatch, Engine)
    old = _prepared(tmp_path, "c" * 64)
    new = _prepared(tmp_path, "d" * 64)
    worker = threading.Thread(
        target=runtime.execute_exclusive_ivd_turn,
        kwargs={"prepared": old, "question": "slow"},
    )

    worker.start()
    assert entered.wait(timeout=1)
    runtime.execute_exclusive_ivd_turn(new, question="new")

    assert len(created) == 2
    assert created[0].close_count == 0
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert created[0].close_count == 1
    assert created[1].close_count == 0


def test_failed_release_construction_keeps_prior_engine_active(tmp_path, monkeypatch):
    created = []

    class Engine:
        def __init__(self, _root, *, expected_package_digest):
            if expected_package_digest == "f" * 64:
                raise RuntimeError("invalid release")
            self.questions = []
            self.close_count = 0
            created.append(self)

        def answer(self, question, _evidence):
            assert self.close_count == 0
            self.questions.append(question)

        def close(self):
            self.close_count += 1

    _install_fakes(monkeypatch, Engine)
    old = _prepared(tmp_path, "e" * 64)
    invalid = _prepared(tmp_path, "f" * 64)

    runtime.execute_exclusive_ivd_turn(old, question="before")
    with pytest.raises(RuntimeError, match="invalid release"):
        runtime.execute_exclusive_ivd_turn(invalid, question="blocked")
    runtime.execute_exclusive_ivd_turn(old, question="after")

    assert len(created) == 1
    assert created[0].questions == ["before", "after"]
    assert created[0].close_count == 0
