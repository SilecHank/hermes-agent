import json
import os
from pathlib import Path

from gateway.ivd_maintenance_worker import (
    build_default_ivd_maintenance_steps,
    prune_ivd_worker_artifacts,
    run_ivd_maintenance_worker,
)
from gateway.maintenance_command_bus import MaintenanceCommandLedger


def test_default_worker_steps_are_deterministic_and_pending_safe():
    steps = build_default_ivd_maintenance_steps(Path("/kb"))
    joined = "\n".join(" ".join(step.argv) for step in steps)

    assert "hermes_candidate_promotion_queue.py" in joined
    assert "detect-kb-conflicts.py" in joined
    assert "review-inbox-maintenance.py" in joined
    assert "hermes_daily_maintenance_runner.py" in joined
    assert "--execute" in joined
    assert "git commit" not in joined
    assert "git push" not in joined


def test_default_worker_steps_mount_daily_runner_with_scope_date():
    steps = build_default_ivd_maintenance_steps(Path("/kb"), python_executable="py", run_date="2026-07-25")

    daily_steps = [step for step in steps if step.name == "daily_maintenance_runner"]

    assert len(daily_steps) == 1
    assert daily_steps[0].argv == (
        "py",
        "scripts/hermes_daily_maintenance_runner.py",
        "--date",
        "2026-07-25",
        "--repo-root",
        ".",
        "--execute",
    )


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


def test_worker_global_lock_blocks_overlapping_execution(tmp_path):
    kb_root = tmp_path / "kb"
    kb_root.mkdir()
    ledger = MaintenanceCommandLedger(tmp_path / "ledger.json")
    claim = ledger.claim("执行知识库维护", origin_platform="weixin", origin_chat_id="c1", scope="s1")
    lock_path = tmp_path / "ivd-worker.lock"
    lock_path.write_text("busy", encoding="utf-8")

    assert claim is not None
    artifact = run_ivd_maintenance_worker(
        ledger,
        claim.command_id,
        kb_root=kb_root,
        scope="s1",
        runner=lambda *_args, **_kwargs: None,
        worker_lock_path=lock_path,
        worker_lock_timeout_seconds=0.01,
    )

    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["error"] == "worker_already_running"
    assert "执行失败" in ledger.format_status_summary(claim.command_id)


def test_prune_ivd_worker_artifacts_removes_old_json_files(tmp_path):
    artifact_dir = tmp_path / "ivd-maintenance-results"
    artifact_dir.mkdir()
    old = artifact_dir / "old.json"
    recent = artifact_dir / "recent.json"
    old.write_text("{}", encoding="utf-8")
    recent.write_text("{}", encoding="utf-8")

    now = 1_800_000_000.0
    old_time = now - 10 * 24 * 3600
    recent_time = now - 60
    os.utime(old, (old_time, old_time))
    os.utime(recent, (recent_time, recent_time))

    removed = prune_ivd_worker_artifacts(artifact_dir, max_age_seconds=7 * 24 * 3600, now_epoch=now)

    assert removed == 1
    assert not old.exists()
    assert recent.exists()
