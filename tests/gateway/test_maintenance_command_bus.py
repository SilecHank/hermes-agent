from gateway.maintenance_command_bus import (
    REQUIRED_IVD_PLATFORMS,
    MaintenanceCommandLedger,
    classify_maintenance_command,
)


def test_classifies_chinese_ivd_maintenance_sync_command():
    assert classify_maintenance_command("请识别调用这次的全部更新") == "ivd_maintenance_sync"
    assert classify_maintenance_command("/ivd sync") == "ivd_maintenance_sync"


def test_claim_executes_once_across_platforms_for_same_scope(tmp_path):
    ledger = MaintenanceCommandLedger(tmp_path / "maintenance-ledger.json")

    first = ledger.claim(
        "请识别调用这次的全部更新",
        origin_platform="weixin",
        origin_chat_id="chat-a",
        origin_user_id="u1",
        scope="kb-update-20260725",
    )
    second = ledger.claim(
        "/ivd sync",
        origin_platform="qqbot",
        origin_chat_id="chat-b",
        origin_user_id="u1",
        scope="kb-update-20260725",
    )

    assert first is not None
    assert second is not None
    assert first.should_execute is True
    assert second.should_execute is False
    assert second.command_id == first.command_id
    assert set(first.notify_platforms) == REQUIRED_IVD_PLATFORMS - {"weixin"}


def test_status_summary_is_short_chinese(tmp_path):
    ledger = MaintenanceCommandLedger(tmp_path / "maintenance-ledger.json")
    claim = ledger.claim(
        "执行知识库维护",
        origin_platform="wecom",
        origin_chat_id="chat-a",
        scope="manual",
    )

    assert claim is not None
    ledger.mark_running(claim.command_id)
    text = ledger.format_status_summary(claim.command_id)

    assert "维护命令" in text
    assert "正在执行" in text
    assert claim.command_id in text
