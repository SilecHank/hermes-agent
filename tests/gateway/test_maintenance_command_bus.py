from gateway.maintenance_command_bus import (
    REQUIRED_IVD_PLATFORMS,
    MaintenanceCommandLedger,
    classify_maintenance_command,
)


def test_classifies_chinese_ivd_maintenance_sync_command():
    assert REQUIRED_IVD_PLATFORMS == {"weixin", "wecom", "qqbot", "telegram"}
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


def test_recent_status_without_id_lists_latest_commands(tmp_path):
    ledger = MaintenanceCommandLedger(tmp_path / "maintenance-ledger.json")
    first = ledger.claim("执行知识库维护", origin_platform="weixin", origin_chat_id="c1", scope="a")
    second = ledger.claim("执行知识库维护", origin_platform="wecom", origin_chat_id="c2", scope="b")

    assert first is not None
    assert second is not None
    text = ledger.format_recent_summary(limit=5)

    assert "最近维护命令" in text
    assert first.command_id in text
    assert second.command_id in text


def test_recover_stale_running_commands_and_prune_old_records(tmp_path):
    ledger = MaintenanceCommandLedger(tmp_path / "maintenance-ledger.json")
    claim = ledger.claim("执行知识库维护", origin_platform="weixin", origin_chat_id="c1", scope="old")

    assert claim is not None
    ledger.mark_running(claim.command_id)
    state = ledger._read_state()
    state["commands"][claim.command_id]["updated_at"] = "2026-07-20T00:00:00Z"
    ledger._write_state(state)

    recovered = ledger.recover_stale_running(max_age_seconds=60, now_epoch=1_800_000_000)
    assert recovered == 1
    assert "执行失败" in ledger.format_status_summary(claim.command_id)

    state = ledger._read_state()
    state["commands"][claim.command_id]["updated_at"] = "2026-07-20T00:00:00Z"
    ledger._write_state(state)
    pruned = ledger.prune(max_age_seconds=60, now_epoch=1_800_000_000)
    assert pruned == 1
    assert claim.command_id not in ledger._read_state()["commands"]
