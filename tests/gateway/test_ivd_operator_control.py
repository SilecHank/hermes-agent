import json
import subprocess
import time
from pathlib import Path

from gateway.ivd_operator_control import format_ivd_operator_status, read_ivd_operator_status


def test_status_combines_release_platform_and_cron_in_plain_chinese():
    report = {
        "status": "ready",
        "active_host": "wsl-primary",
        "active_generation": 10,
        "current_release": "a" * 64,
        "knowledge_release_digest": "b" * 64,
        "platform_health": {"status": "healthy", "platforms": {"weixin": "connected", "wecom": "connected", "qqbot": "connected"}},
        "cron": {"status": "healthy", "jobs": 3},
    }

    text = format_ivd_operator_status(report)

    assert "生产主机：wsl-primary（generation 10）" in text
    assert "三平台：正常" in text
    assert "Knowledge Release：" in text
    assert "Cron：正常（3 项）" in text
    assert "结论：无需操作" in text
    assert "Reply with" not in text


def test_status_reader_uses_fixed_entrypoint_and_adds_verified_marker(tmp_path):
    kb = tmp_path / "kb"
    script = kb / "scripts" / "hermes_oob_entrypoint.py"
    script.parent.mkdir(parents=True)
    script.write_text("# probe\n", encoding="utf-8")
    state = tmp_path / "state"
    current = state / "current"
    current.mkdir(parents=True)
    (current / ".verified.json").write_text(json.dumps({"knowledge_release_digest": "d" * 64}), encoding="utf-8")
    cron = tmp_path / "live" / "cron"
    cron.mkdir(parents=True)
    (cron / "jobs.json").write_text(json.dumps({"jobs": [{}, {}]}), encoding="utf-8")
    (cron / "ticker_last_success").write_text(str(time.time()), encoding="utf-8")
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        class Result:
            returncode = 0
            stdout = json.dumps({
                "status": "ready", "active_host": "wsl-primary", "active_generation": 10,
                "current_release": "a" * 64, "platform_health": {"status": "healthy", "platforms": {}},
            })
            stderr = ""
        return Result()

    result = read_ivd_operator_status(
        kb_root=kb, state_root=state, live_root=tmp_path / "live", runner=runner
    )

    assert calls[0][0][-2:] == ["status", "--json"]
    assert result["knowledge_release_digest"] == "d" * 64
    assert result["cron"]["status"] == "healthy"
    assert result["cron"]["jobs"] == 2


def test_status_timeout_returns_plain_chinese_without_repair(tmp_path):
    kb = tmp_path / "kb"
    script = kb / "scripts" / "hermes_oob_entrypoint.py"
    script.parent.mkdir(parents=True)
    script.write_text("# probe\n", encoding="utf-8")

    def runner(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["fixed"], 20)

    report = read_ivd_operator_status(kb_root=kb, runner=runner)
    text = format_ivd_operator_status(report)

    assert report["status"] == "blocked"
    assert "未执行任何修改" in text
    assert "Working" not in text
