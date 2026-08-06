from gateway.artifact_delivery_guard import (
    ArtifactDeliveryValidator,
    CompositeFinalResponseValidator,
)


def _tool_messages(name="terminal", result="exit code: 1"):
    return [
        {
            "role": "assistant",
            "tool_calls": [{"id": "call-1", "function": {"name": name, "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": result},
    ]


def test_xlsx_refusal_without_attempt_is_retried_on_every_sales_platform():
    for platform in ("weixin", "wecom", "qqbot", "telegram"):
        validator = ArtifactDeliveryValidator(
            user_message="请整理成 xlsx 文件发给我",
            platform=platform,
            messages_provider=lambda: [],
            available_tool_names={"terminal", "write_file"},
        )

        result = validator("当前环境缺失，无法生成 Excel 文件。")

        assert result["ok"] is False
        assert result["reasons"] == ["artifact_capability_refusal_without_attempt:xlsx"]
        assert "生成" in result["fallback"]
        assert "发送" in result["fallback"]


def test_xlsx_refusal_is_allowed_after_a_real_generation_attempt_failed():
    validator = ArtifactDeliveryValidator(
        user_message="生成 xlsx 给我",
        platform="qqbot",
        messages_provider=lambda: _tool_messages(),
        available_tool_names={"terminal", "write_file"},
    )

    result = validator("文件生成失败：运行环境缺少 openpyxl。")

    assert result["ok"] is True


def test_plain_question_does_not_trigger_artifact_contract():
    validator = ArtifactDeliveryValidator(
        user_message="PMseq V5 和 V4 有什么差异",
        platform="qqbot",
        messages_provider=lambda: [],
        available_tool_names={"terminal", "write_file"},
    )

    assert validator("当前环境缺失，无法生成表格。")["ok"] is True


def test_validator_does_not_force_generation_when_no_generation_tool_exists():
    validator = ArtifactDeliveryValidator(
        user_message="生成 xlsx 给我",
        platform="qqbot",
        messages_provider=lambda: [],
        available_tool_names={"web_search"},
    )

    assert validator("当前环境缺失，无法生成 Excel 文件。")["ok"] is True


def test_composite_preserves_existing_after_sales_validation():
    existing = lambda _answer: {
        "ok": False,
        "reasons": ("unsupported_numeric_claim:100ng",),
        "fallback": "数值未核实。",
    }
    artifact = ArtifactDeliveryValidator(
        user_message="生成 xlsx 给我",
        platform="qqbot",
        messages_provider=lambda: [],
        available_tool_names={"terminal"},
    )
    validator = CompositeFinalResponseValidator((existing, artifact))

    result = validator("100ng，已整理完成。")

    assert result["ok"] is False
    assert result["reasons"] == ("unsupported_numeric_claim:100ng",)
    assert result["fallback"] == "数值未核实。"
