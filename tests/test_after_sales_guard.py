from pathlib import Path

from gateway.after_sales_guard import CriticalAfterSalesValidator, prepare_after_sales_turn


KB = Path("/home/slim/IVD-KnowledgeHub")


def _config(enabled=True):
    return {
        "after_sales_guard": {
            "enabled": enabled,
            "platforms": ["weixin", "wecom", "qqbot"],
            "workflow_module": str(KB / "scripts/after_sales_workflow_gate.py"),
            "validator_module": str(KB / "scripts/after_sales_answer_validator.py"),
            "cards_dir": str(KB / "knowledge-base/workflows/facts"),
        }
    }


def _config_with_fast_response(enabled=True):
    config = _config(enabled=enabled)
    config["after_sales_guard"]["fast_response_module"] = str(KB / "scripts/hermes_fast_response_pipeline.py")
    return config


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
    assert supported["ok"] is True
    assert unsupported["ok"] is False
    assert "unsupported_numeric_claim:400ng" in unsupported["reasons"]


def test_prepare_after_sales_turn_does_not_inject_unsafe_context_route():
    turn = prepare_after_sales_turn(
        _config_with_fast_response(),
        platform="weixin",
        message="需要",
        history=[],
    )

    assert turn is None


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
