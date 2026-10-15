"""Narrow source-bound parameter regressions; local real package opt-in."""
import os
from pathlib import Path
import pytest
from gateway.ivd_dispatcher import IVDDispatcher
from gateway.ivd_knowledge_engine import IVDKnowledgeEngine

QUESTIONS = (
    "按SOP-JL-269 A5手工提取，加入裂解步骤的血浆样本是多少？",
    "携带者全血样本按SOP-JL-374 A2，纳磁S-48配MD5422美基试剂提取时，表5加入多少全血？",
)
@pytest.fixture
def real_package():
    value=os.environ.get("IVD_BOUNDARY_PACKAGE")
    if not value:pytest.skip("explicit local immutable package required")
    return Path(value)

@pytest.mark.parametrize("question,value,document",[(QUESTIONS[0],"200", "SOP-JL-269"),(QUESTIONS[1],"30","SOP-JL-374")])
def test_approved_source_bound_inputs(real_package,question,value,document):
    dispatcher=IVDDispatcher(real_package)
    with IVDKnowledgeEngine(real_package) as engine:
        dispatched=dispatcher.execute(engine,question=question)
        assert dispatched.envelope.intent == "parameter"
        result=dispatched.result
        assert result.outcome == "answer"
        assert result.answer_shape == "scalar"
        assert value+" uL" in result.text
        assert result.source.document == document
        assert result.index_transactions == result.model_calls == 0


def test_carrier_missing_conditions_asks_once(real_package):
    with IVDKnowledgeEngine(real_package) as engine:
        result=IVDDispatcher(real_package).execute(engine,question="携带者提取需要多少全血").result
        assert result.outcome == "clarification"
        assert result.text == "请确认采用的提取方法、平台与试剂。"
        assert result.model_calls == result.index_transactions == 0

@pytest.mark.parametrize("question",[
    QUESTIONS[0].replace("A5", "A0"),
    QUESTIONS[0].replace("血浆样本是多少", "血浆样本是多少ng"),
    QUESTIONS[0].replace("血浆样本", "血浆样本和蛋白酶K溶液"),
    QUESTIONS[0].replace("A5", "A5还是A0"),
    QUESTIONS[0].replace("手工", "不是手工"),
    QUESTIONS[0].replace("手工", "是否手工"),
    QUESTIONS[0].replace("手工", "手工或自动"),
    QUESTIONS[0].replace("按SOP", "携带者按SOP"),
    QUESTIONS[1].replace("纳磁S-48配MD5422美基试剂", "自动化"),
    QUESTIONS[1].replace("MD5422", "MD9999"),
    QUESTIONS[1].replace("S-48", "S-96"),
    QUESTIONS[1].replace("纳磁S-48", "不是纳磁S-48"),
    QUESTIONS[1].replace("MD5422", "是否MD5422"),
    QUESTIONS[1].replace("纳磁S-48", "纳磁S-48或S-96"),
    QUESTIONS[1].replace("A2", "A0"),
])
def test_source_method_platform_conflicts_never_answer_scalar(real_package,question):
    with IVDKnowledgeEngine(real_package) as engine:
        result=IVDDispatcher(real_package).execute(engine,question=question).result
        assert result is None or result.outcome != "answer" or result.answer_shape != "scalar"

@pytest.mark.parametrize("question",["SOP-JL-269 A5文件在哪里", "SOP-JL-374 A2文件路径是什么"])
def test_pure_sop_navigation_stays_navigation(real_package,question):
    with IVDKnowledgeEngine(real_package) as engine:
        dispatched=IVDDispatcher(real_package).execute(engine,question=question)
        assert dispatched.envelope.intent == "sop_lookup"
        assert dispatched.result.answer_shape != "scalar"


def test_direct_parameter_engine_cannot_be_preempted_by_navigation(real_package):
    with IVDKnowledgeEngine(real_package) as engine:
        for question in QUESTIONS:
            result=engine.execute(question=question,knowledge_type="parameter",answer_shape="scalar")
            assert result.outcome == "answer"
            assert result.source and result.source.version in ("A5","A2")
            assert result.index_transactions == 0

@pytest.mark.parametrize("question",["携带者自动化提取需要多少全血", "按SOP-JL-374 A2自动提取，加入多少全血？"])
def test_generic_automatic_is_not_platform_confirmation(real_package,question):
    with IVDKnowledgeEngine(real_package) as engine:
        result=IVDDispatcher(real_package).execute(engine,question=question).result
        assert result is None or result.outcome != "answer" or result.answer_shape != "scalar"


def test_source_helpers_do_not_turn_arbitrary_quantity_or_files_into_parameters():
    from gateway.ivd_parameter_scope import explicit_sop_input_request
    for question in ("一共有多少SOP文件", "SOP-JL-269 A5需要多少设备清单", "按SOP-JL-269 A5提取时间是多少", "按SOP-JL-269 A5手工提取浓度是多少", "按SOP-JL-269提取多少血浆", "按SOP-JL-269 A5及SOP-JL-374 A2加入多少全血"):
        assert not explicit_sop_input_request(question)


def test_equipment_list_is_not_a_parameter_task(real_package):
    with IVDKnowledgeEngine(real_package) as engine:
        result=IVDDispatcher(real_package).execute(engine,question="携带者纳磁S-48自动化提取需要哪些设备和试剂清单").result
        assert result is not None
        assert result.answer_shape != "scalar"
        assert "30 uL" not in result.text

@pytest.mark.parametrize("object_text,allowed",[
    ("血浆样本", True),
    ("不是血浆而是全血样本", False),
    ("不是全血而是血浆样本", True),
    ("血浆样本，改为全血样本", False),
    ("全血样本，改为血浆样本", True),
    ("不是血浆样本", False),
    ("不含血浆样本", False),
    ("是否改为血浆样本", False),
    ("不是改为血浆样本", False),
    ("是否血浆样本", False),
    ("血浆或全血样本", False),
    ("血浆和未知样本", False),
    ("血浆样本，改为未知样本", False),
])
@pytest.mark.parametrize("entry", ["dispatcher", "engine"])
def test_source_bound_object_assertion_controls_final_text(real_package, object_text, allowed, entry):
    question=QUESTIONS[0].replace("血浆样本",object_text)
    with IVDKnowledgeEngine(real_package) as engine:
        if entry == "dispatcher":
            result=IVDDispatcher(real_package).execute(engine,question=question).result
        else:
            result=engine.execute(question=question,knowledge_type="parameter",answer_shape="scalar")
        assert result is not None
        assert result.model_calls == result.index_transactions == 0
        if allowed:
            assert result.outcome == "answer"
            assert result.text == "手工提取时，裂解步骤的血浆样本加入量为200 uL"
            assert result.source.document == "SOP-JL-269"
        else:
            assert result.outcome == "clarification"
            assert "200" not in result.text
            assert not result.sources
