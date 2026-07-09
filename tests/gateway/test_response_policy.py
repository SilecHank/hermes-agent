from gateway.config import Platform
from gateway.run import _sanitize_gateway_final_response


def test_response_policy_disabled_leaves_normal_answer_unchanged(monkeypatch):
    monkeypatch.setattr("gateway.response_policy.read_raw_config", lambda: {})
    answer = "结论：可以直接处理。\n来源：SOP-JL-001。"

    assert _sanitize_gateway_final_response(Platform.WEIXIN, answer) == answer


def test_weixin_response_policy_truncates_long_answer(monkeypatch):
    monkeypatch.setattr(
        "gateway.response_policy.read_raw_config",
        lambda: {
            "response_policies": {
                "weixin": {
                    "enabled": True,
                    "max_chars": 40,
                    "remove_closing_invitations": True,
                }
            }
        },
    )
    answer = "结论：" + ("这是很长的说明" * 20)

    result = _sanitize_gateway_final_response(Platform.WEIXIN, answer)

    assert len(result) <= 40
    assert result.endswith("…")


def test_response_policy_removes_common_closing_invitation(monkeypatch):
    monkeypatch.setattr(
        "gateway.response_policy.read_raw_config",
        lambda: {
            "response_policies": {
                "qqbot": {
                    "enabled": True,
                    "max_chars": 600,
                    "remove_closing_invitations": True,
                }
            }
        },
    )
    answer = "结论：进入医院本地库。\n如果你需要，我可以继续帮你整理成客户话术。"

    result = _sanitize_gateway_final_response(Platform.QQBOT, answer)

    assert result == "结论：进入医院本地库。"
