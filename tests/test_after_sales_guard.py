import os
from pathlib import Path
from unittest.mock import patch

from gateway.after_sales_guard import (
    CriticalAfterSalesValidator,
    build_preflight_block_result,
    prepare_after_sales_turn,
)


def _knowledgehub_root():
    configured = os.environ.get("IVD_KB_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / "IVD-KnowledgeHub"


KB = _knowledgehub_root()


def test_knowledgehub_root_honors_cross_platform_override():
    with patch.dict("os.environ", {"IVD_KB_ROOT": "/tmp/ivd-kb"}):
        assert _knowledgehub_root() == Path("/tmp/ivd-kb").resolve()


def _config(enabled=True):
    return {
        "after_sales_guard": {
            "enabled": enabled,
            "platforms": ["weixin", "wecom", "qqbot", "telegram"],
            "workflow_module": str(KB / "scripts/after_sales_workflow_gate.py"),
            "validator_module": str(KB / "scripts/after_sales_answer_validator.py"),
            "cards_dir": str(KB / "knowledge-base/workflows/facts"),
        }
    }


def _config_with_fast_response(enabled=True):
    config = _config(enabled=enabled)
    config["after_sales_guard"]["fast_response_module"] = str(KB / "scripts/hermes_fast_response_pipeline.py")
    return config


def _write_experience_modules(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    fast = scripts / "hermes_fast_response_pipeline.py"
    fast.write_text(
        "def build_fast_response_plan(message, question_type='general'):\n"
        "    return {'runtime_preflight': {'eligible': False}}\n",
        encoding="utf-8",
    )
    (scripts / "after_sales_platform_policy.py").write_text(
        "def render_answer_experience_context(*, platform):\n"
        "    platform = platform.strip().lower()\n"
        "    if platform not in ('weixin', 'wecom', 'qqbot', 'telegram'):\n"
        "        raise ValueError(platform)\n"
        "    return '[统一体验]\\n先直接回答结论。\\n按顺序给出下一步动作。'\n",
        encoding="utf-8",
    )
    return fast


def test_all_ivd_platforms_receive_identical_shared_answer_experience(tmp_path):
    fast = _write_experience_modules(tmp_path)
    config = _config(enabled=True)
    config["after_sales_guard"]["fast_response_module"] = str(fast)
    config["after_sales_guard"]["validator_module"] = str(tmp_path / "missing-validator.py")
    config["after_sales_guard"]["cards_dir"] = str(tmp_path / "missing-cards")

    contexts = {}
    for platform in ("weixin", "wecom", "qqbot", "telegram"):
        turn = prepare_after_sales_turn(
            config,
            platform=platform,
            message="这个异常应该先查什么？",
            history=[],
        )
        assert turn is not None
        contexts[platform] = turn.context
        assert turn.facts == {}
        assert turn.has_validator is False

    assert len(set(contexts.values())) == 1
    assert "先直接回答结论" in contexts["qqbot"]


def _write_boundary_fast_module(tmp_path, source_path, *, source_status="source_candidates_found"):
    scripts = tmp_path / "scripts-boundary"
    scripts.mkdir()
    fast = scripts / "hermes_fast_response_pipeline.py"
    fast.write_text(
        "def build_fast_response_plan(message, question_type='general'):\n"
        "    return {\n"
        "      'runtime_preflight': {'eligible': True, 'route_version': 'boundary-v1'},\n"
        "      'answer_template': {'style': 'short_first'},\n"
        "      'preflight_gate': {'decision': 'allow', 'pipeline_action': 'continue_final_answer', 'issues': []},\n"
        "      'fast_path': {'route_id': 'sop_parameter_short_answer'},\n"
        "      'initial_files': [],\n"
        "      'answer_contract': {\n"
        "        'deliverable': 'difference_list', 'comparison_dimensions': ['process'],\n"
        "        'excluded_topics': ['reaction_conditions', 'performance_claims'],\n"
        "        'must_preserve': ['version_scope', 'source_conflict', 'uncertainty'],\n"
        "        'detail_level': 'brief'},\n"
        f"      'source_location': {{'status': {source_status!r}, 'input_sufficient': True,\n"
        f"        'candidates': [{{'resolved_path': {str(source_path)!r}, 'authority': 'locator_only'}}]}}\n"
        "    }\n",
        encoding="utf-8",
    )
    (scripts / "after_sales_platform_policy.py").write_text(
        "def render_answer_experience_context(*, platform):\n"
        "    return '[统一体验]\\n按当前任务边界直接回答。'\n",
        encoding="utf-8",
    )
    return fast


def test_four_platforms_receive_same_boundary_and_material_location(tmp_path):
    source = tmp_path / "PMseq RNA V5 SOP.pdf"
    source.write_text("fixture", encoding="utf-8")
    fast = _write_boundary_fast_module(tmp_path, source)
    config = _config(enabled=True)
    config["after_sales_guard"]["fast_response_module"] = str(fast)

    contexts = []
    for platform in ("weixin", "wecom", "qqbot", "telegram"):
        turn = prepare_after_sales_turn(
            config,
            platform=platform,
            message="PMseq RNA V5跟V4有什么不同，只列流程差异",
            history=[],
        )
        assert turn is not None
        assert turn.answer_contract["deliverable"] == "difference_list"
        assert turn.source_location["status"] == "source_candidates_found"
        assert str(source.resolve()) in turn.source_paths
        assert "只输出差异清单" in turn.context
        assert "反应条件" in turn.context
        contexts.append(turn.context)

    assert len(set(contexts)) == 1


def test_sufficient_identity_lookup_failure_does_not_repeat_known_fields(tmp_path):
    source = tmp_path / "PMseq RNA V5 SOP.pdf"
    source.write_text("fixture", encoding="utf-8")
    fast = _write_boundary_fast_module(tmp_path, source)
    config = _config(enabled=True)
    config["after_sales_guard"]["fast_response_module"] = str(fast)

    turn = prepare_after_sales_turn(
        config,
        platform="weixin",
        message="PMseq RNA V5 建库投入量是多少",
        history=[],
    )
    assert turn is not None
    fallback = turn.validate("建库投入量为100ng。", messages=[])["fallback"]

    assert "请补充产品版本" not in fallback
    assert "请补充SOP编号" not in fallback
    assert "材料库没有" not in fallback
    assert "未完成已定位正式资料的读取" in fallback


def test_ivd_platform_matching_is_normalized_before_policy_loading(tmp_path):
    fast = _write_experience_modules(tmp_path)
    config = _config(enabled=True)
    config["after_sales_guard"]["fast_response_module"] = str(fast)
    config["after_sales_guard"]["workflow_module"] = str(tmp_path / "missing.py")
    config["after_sales_guard"]["validator_module"] = str(tmp_path / "missing-validator.py")
    config["after_sales_guard"]["cards_dir"] = str(tmp_path / "missing-cards")

    turn = prepare_after_sales_turn(
        config,
        platform=" Telegram ",
        message="报告出不来",
        history=[],
    )

    assert turn is not None
    assert turn.context.startswith("[统一体验]")


def test_matched_turn_composes_experience_with_facts_and_validator(tmp_path):
    fast = _write_experience_modules(tmp_path)
    config = _config(enabled=True)
    config["after_sales_guard"]["fast_response_module"] = str(fast)

    turn = prepare_after_sales_turn(
        config,
        platform="qqbot",
        message="NIFTY手工实验文库浓度低怎么排查？",
        history=[],
    )

    assert turn is not None
    assert "[统一体验]" in turn.context
    assert "当前测量节点：文库浓度质控" in turn.context
    assert turn.has_validator is True


def test_missing_experience_module_keeps_existing_fail_open_behavior(tmp_path):
    fast = tmp_path / "hermes_fast_response_pipeline.py"
    fast.write_text(
        "def build_fast_response_plan(message, question_type='general'):\n"
        "    return {'runtime_preflight': {'eligible': False}}\n",
        encoding="utf-8",
    )
    config = _config(enabled=True)
    config["after_sales_guard"]["fast_response_module"] = str(fast)
    config["after_sales_guard"]["workflow_module"] = str(tmp_path / "missing.py")

    turn = prepare_after_sales_turn(
        config,
        platform="qqbot",
        message="这个异常应该先查什么？",
        history=[],
    )

    assert turn is None


def test_blocked_preflight_is_structured_and_stops_before_model(tmp_path):
    module = tmp_path / "scripts" / "fake_fast_pipeline.py"
    module.parent.mkdir()
    module.write_text(
        "def build_fast_response_plan(message, question_type='general'):\n"
        "    return {\n"
        "      'runtime_preflight': {'eligible': True, 'route_version': 'test-v1'},\n"
        "      'answer_template': {'style': 'short_first'},\n"
        "      'preflight_gate': {\n"
        "        'decision': 'block',\n"
        "        'pipeline_action': 'stop_before_final_answer',\n"
        "        'issues': ['pending_candidate_source_used'],\n"
        "      },\n"
        "      'fast_path': {'route_id': 'blocked-test'},\n"
        "      'initial_files': ['knowledge-base/candidates/pending.md'],\n"
        "    }\n",
        encoding="utf-8",
    )
    config = _config(enabled=True)
    config["after_sales_guard"]["fast_response_module"] = str(module)

    turn = prepare_after_sales_turn(
        config,
        platform="qqbot",
        message="这个候选结论可以直接回复吗？",
        history=[],
    )

    assert turn is not None
    assert turn.preflight_decision == "block"
    assert turn.preflight_action == "stop_before_answer_generation"
    assert turn.blocks_answer_generation is True
    assert turn.preflight_issues == ("pending_candidate_source_used",)

    result = build_preflight_block_result(turn, "这个候选结论可以直接回复吗？")
    assert result["api_calls"] == 0
    assert result["agent_persisted"] is False
    assert result["preflight_blocked"] is True
    assert "待验证或非正式来源" in result["final_response"]
    assert "pipeline" not in result["final_response"].casefold()


def test_prepare_after_sales_turn_injects_verified_context():
    turn = prepare_after_sales_turn(
        _config(),
        platform="qqbot",
        message="NIFTY手工实验文库浓度低怎么排查？",
        history=[],
    )

    assert turn is not None
    assert "当前测量节点：文库浓度质控" in turn.context
    assert turn.facts["current_stage"] == "library_qc"


def test_prepare_after_sales_turn_does_not_label_a_fast_path_miss_as_fast():
    turn = prepare_after_sales_turn(
        _config_with_fast_response(),
        platform="qqbot",
        message="NIFTY手工实验文库浓度低怎么排查？",
        history=[],
    )

    assert turn is not None
    assert "快速回答管线" not in turn.context
    assert "stop_before_final_answer" not in turn.context


def test_prepare_after_sales_turn_uses_fast_preflight_without_fact_card_match():
    turn = prepare_after_sales_turn(
        _config_with_fast_response(),
        platform="weixin",
        message="WES V5 建库投入量是多少",
        history=[],
    )

    assert turn is not None
    assert "快速回答管线" in turn.context
    assert "wes-v5-sop-index.md" in turn.context
    assert turn.has_validator is True
    assert all(Path(path).is_absolute() for path in turn.source_paths)


def test_fast_turn_injects_explicit_product_variant_identity():
    turn = prepare_after_sales_turn(
        _config_with_fast_response(),
        platform="weixin",
        message="地贫508为什么不能检测三联体？",
        history=[],
    )

    assert turn is not None
    assert turn.product_scope == "地贫"
    assert turn.product_variant == "508"
    assert "已识别产品：地贫" in turn.context
    assert "产品变体：508" in turn.context
    assert "thalassemia-508-mutations.md" in turn.context


def test_fast_parameter_turn_requires_reading_routed_source_and_rejects_unknown_number():
    turn = prepare_after_sales_turn(
        _config_with_fast_response(),
        platform="weixin",
        message="WES V5 建库投入量是多少",
        history=[],
    )
    source_path = turn.source_paths[0]
    missing_source = turn.validate("建库投入量为100ng。", messages=[])
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "source-read",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "' + source_path + '"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "source-read",
            "content": "当前正式SOP规定建库投入量为100ng。",
        },
    ]

    supported = turn.validate("建库投入量为100ng。", messages=messages)
    unsupported = turn.validate("建库投入量为400ng。", messages=messages)

    assert missing_source["ok"] is False
    assert "formal_source_not_read" in missing_source["reasons"]
    assert "请先读取" not in missing_source["fallback"]
    assert "未能完成正式来源核实" in missing_source["fallback"]
    assert "请补充产品版本" not in missing_source["fallback"]
    assert "SOP编号" not in missing_source["fallback"]
    assert supported["ok"] is True
    assert unsupported["ok"] is False
    assert "unsupported_numeric_claim:400ng" in unsupported["reasons"]
    assert "请重新读取" not in unsupported["fallback"]
    assert "与已核实的正式来源不一致" in unsupported["fallback"]


def test_unsafe_fast_route_injects_only_shared_answer_experience():
    turn = prepare_after_sales_turn(
        _config_with_fast_response(),
        platform="weixin",
        message="需要",
        history=[],
    )

    assert turn is not None
    assert "IVD售后统一回答规范" in turn.context
    assert "快速回答管线" not in turn.context
    assert "已识别产品" not in turn.context
    assert turn.facts == {}
    assert turn.has_validator is False


def test_prepare_after_sales_turn_uses_recent_user_context():
    turn = prepare_after_sales_turn(
        _config(),
        platform="wecom",
        message="同批连续走低，48例和96例都发生。",
        history=[
            {
                "role": "user",
                "content": "NIFTY手工实验文库浓度低怎么排查？",
            }
        ],
    )

    assert turn is not None
    assert turn.facts["product"] == "NIFTY"


def test_prepare_after_sales_turn_ignores_unrelated_message():
    turn = prepare_after_sales_turn(
        _config(),
        platform="weixin",
        message="iLink怎么使用？",
        history=[],
    )

    assert turn is None


def test_nifty_ht_report_turn_uses_other_autosome_branch_on_all_platforms():
    for platform in ("weixin", "wecom", "qqbot"):
        turn = prepare_after_sales_turn(
            _config(),
            platform=platform,
            message="无创样本结果提示HT15，应该怎么处理",
            history=[],
        )

        assert turn is not None
        assert turn.facts["decision_branch"] == "other_autosome"
        assert "不匹配阳性报告" in turn.context


def test_nifty_mca_turn_uses_one_sample_qc_branch_on_all_platforms():
    history = [{"role": "assistant", "content": "首次MCA必须直接重抽血。"}]

    for platform in ("weixin", "wecom", "qqbot"):
        turn = prepare_after_sales_turn(
            _config(),
            platform=platform,
            message="SOP里无创样本首次检测提示MCA，应该重抽血还是重建库",
            history=history,
        )

        assert turn is not None
        assert turn.facts["current_stage"] == "sample_qc"
        assert turn.facts["decision_branch"] == "mca"
        assert "SOP未规定" in turn.context


def test_prepare_after_sales_turn_is_disabled_outside_configured_platforms():
    turn = prepare_after_sales_turn(
        _config(),
        platform="cli",
        message="NIFTY文库浓度低",
        history=[],
    )

    assert turn is None


def test_prepare_after_sales_turn_is_disabled_by_switch():
    turn = prepare_after_sales_turn(
        _config(enabled=False),
        platform="qqbot",
        message="NIFTY文库浓度低",
        history=[],
    )

    assert turn is None


def test_after_sales_turn_validates_before_persistence():
    turn = prepare_after_sales_turn(
        _config(),
        platform="qqbot",
        message="NIFTY文库浓度低，48例和96例都出现。",
        history=[],
    )

    invalid = turn.validate("建议先检查DNB浓度。")
    allowed = turn.validate(
        "48例和96例均出现文库浓度走低；SOP合格线为≥2 ng/µL。"
    )

    assert invalid["ok"] is False
    assert "future_stage:dnb_preparation" in invalid["reasons"]
    assert invalid["fallback"]
    assert allowed["ok"] is True
    assert turn.has_validator is True


def test_facts_matched_turn_uses_critical_validator_policy():
    turn = prepare_after_sales_turn(
        _config(),
        platform="qqbot",
        message="NIFTY文库浓度低，48例和96例都出现。",
        history=[],
    )

    validator = CriticalAfterSalesValidator(turn=turn, messages_provider=list)

    assert validator.fail_closed is True
    assert "未经校验" in validator.error_fallback
    assert validator("48例和96例均出现文库浓度走低。")


def test_prepare_after_sales_turn_infers_unit_for_user_supplied_range():
    turn = prepare_after_sales_turn(
        _config(),
        platform="qqbot",
        message=(
            "无创富集流程，手工实验，客户反馈近期出库浓度偏低，"
            "均值3-5,一般有什么排查方向？"
        ),
        history=[],
    )

    assert turn is not None
    result = turn.validate("客户反馈文库浓度均值为3-5 ng/µL。")
    assert result["ok"] is True, result["reasons"]


def test_verified_context_names_the_authoritative_source():
    turn = prepare_after_sales_turn(
        _config(),
        platform="qqbot",
        message="NIFTY手工实验出库浓度低怎么排查？",
        history=[],
    )

    assert turn is not None
    assert "SOP-JL-269" in turn.context
    assert "A5" in turn.context
    assert "联合实验室 NIFTY 项目手工提取富集建库标准作业指导书.md" in turn.context
    assert "不得改用同编号旧版本" in turn.context


def test_trusts_numbers_read_from_the_authoritative_sop():
    turn = prepare_after_sales_turn(
        _config(),
        platform="qqbot",
        message="NIFTY手工实验文库浓度低怎么排查？",
        history=[],
    )
    source_path = turn.facts["authoritative_sources"][0]["resolved_path"]
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-authoritative",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "' + source_path + '"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-authoritative",
            "content": "富集操作使用75%乙醇，磁珠平衡30min，使用96孔PCR板。",
        },
    ]

    result = turn.validate(
        "核对75%乙醇、30min平衡记录和96孔PCR板操作。",
        messages=messages,
    )

    assert result["ok"] is True, result["reasons"]


def test_does_not_trust_numbers_read_from_an_unlisted_file():
    turn = prepare_after_sales_turn(
        _config(),
        platform="qqbot",
        message="NIFTY手工实验文库浓度低怎么排查？",
        history=[],
    )
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-untrusted",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "/tmp/old-sop.md"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-untrusted",
            "content": "使用75%乙醇。",
        },
    ]

    result = turn.validate("使用75%乙醇。", messages=messages)

    assert result["ok"] is False
    assert "unsupported_numeric_claim:75%" in result["reasons"]


def test_real_qq_followup_advances_and_accepts_authoritative_sop_evidence():
    history = [
        {
            "role": "user",
            "content": (
                "无创富集流程，手工实验，客户反馈近期出库浓度偏低，"
                "均值3-5,一般有什么排查方向？"
            ),
        },
        {"role": "assistant", "content": "请补充质控品、异常分布和变化点。"},
    ]
    message = (
        "同批阴阳性质控品浓度正常，30+，异常是整批次，近期试剂批次没有更换。"
        "样本量有时48例有时96例，48例时是客户操作，96例时是驻点实验员操作。"
        "同样都是浓度偏低"
    )
    turn = prepare_after_sales_turn(
        _config(),
        platform="qqbot",
        message=message,
        history=history,
    )
    source_path = turn.facts["authoritative_sources"][0]["resolved_path"]
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-a5",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "' + source_path + '"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-a5",
            "content": "使用75%乙醇，磁珠平衡30min，操作容器为96孔PCR板。",
        },
    ]
    answer = (
        "现有证据更支持末端修复前的共同环节，但样本量与操作人员存在混杂，"
        "暂不能单独排除任一因素。请核对75%乙醇、30min平衡记录和96孔PCR板操作。"
    )

    result = turn.validate(answer, messages=messages)

    assert turn.facts["diagnostic_mode"] == "evidence_available_for_bounded_ranking"
    assert result["ok"] is True, result["reasons"]
