from gateway.run import (
    _format_exec_approval_fallback,
    _normalize_gateway_approval_reply,
    _normalize_slash_confirm_reply,
)


def test_exec_approval_fallback_uses_plain_chinese_instructions():
    text = _format_exec_approval_fallback(
        "rm -rf /tmp/example",
        "needs confirmation",
        "/",
    )

    assert "需要你确认后才能执行" in text
    assert "回复 `同意`" in text
    assert "回复 `本轮同意`" in text
    assert "回复 `以后都同意`" in text
    assert "回复 `取消`" in text
    assert "Dangerous command requires approval" not in text


def test_gateway_plain_text_approval_accepts_chinese_words():
    assert _normalize_gateway_approval_reply("同意") == ("approve", "")
    assert _normalize_gateway_approval_reply("本轮同意") == ("approve", "session")
    assert _normalize_gateway_approval_reply("以后都同意") == ("approve", "always")
    assert _normalize_gateway_approval_reply("取消") == ("deny", "")


def test_slash_confirm_accepts_chinese_words():
    assert _normalize_slash_confirm_reply(cmd_reply="", norm_reply="同意") == "once"
    assert _normalize_slash_confirm_reply(cmd_reply="", norm_reply="以后都同意") == "always"
    assert _normalize_slash_confirm_reply(cmd_reply="", norm_reply="取消") == "cancel"
