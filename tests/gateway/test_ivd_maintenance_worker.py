import json
from pathlib import Path

from gateway.ivd_maintenance_worker import (
    build_default_ivd_maintenance_steps,
    run_ivd_maintenance_worker,
)
from gateway.maintenance_command_bus import MaintenanceCommandLedger


def test_default_worker_steps_are_deterministic_and_pending_safe():
    steps = build_default_ivd_maintenance_steps(Path("/kb"))
    joined = "\n".join(" ".join(step.argv) for step in steps)

    assert "hermes_candidate_promotion_queue.py" in joined
    assert "detect-kb-conflicts.py" in joined
    assert "review-inbox-maintenance.py" in joined
    assert "git commit" not in joined
    assert "git push" not in joined


def test_worker_marks_completed_and_writes_artifact(tmp_path):
    kb_root = tmp_path / "kb"
    kb_root.mkdir()
    ledger = MaintenanceCommandLedger(tmp_path / "ledger.json")
    claim = ledger.claim("执行知识库维护", origin_platform="weixin", origin_chat_id="c1", scope="s1")
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv)
        class Result:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return Result()

    assert claim is not None
    artifact = run_ivd_maintenance_worker(
        ledger,
        claim.command_id,
        kb_root=kb_root,
        scope="s1",
        runner=fake_runner,
    )

    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert len(payload["steps"]) >= 3
    assert calls
    assert "已完成" in ledger.format_status_summary(claim.command_id)


def test_worker_marks_failed_when_any_step_fails(tmp_path):
    kb_root = tmp_path / "kb"
    kb_root.mkdir()
    ledger = MaintenanceCommandLedger(tmp_path / "ledger.json")
    claim = ledger.claim("执行知识库维护", origin_platform="weixin", origin_chat_id="c1", scope="s1")

    def fake_runner(argv, **kwargs):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "boom"
        return Result()

    assert claim is not None
    artifact = run_ivd_maintenance_worker(
        ledger,
        claim.command_id,
        kb_root=kb_root,
        scope="s1",
        runner=fake_runner,
    )

    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert "执行失败" in ledger.format_status_summary(claim.command_id)
