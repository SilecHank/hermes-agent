from agent.ivd_skill_governance import (
    IVD_SKILL_CATALOG,
    begin_ivd_skill_turn,
    end_ivd_skill_turn,
    evaluate_ivd_skill_load,
    get_ivd_skill_snapshot,
    resolve_ivd_skill_catalog,
)
import json

from tools.skills_tool import _skill_view_with_bump


def _config(mode="active"):
    return {
        "after_sales_guard": {
            "enabled": True,
            "platforms": "weixin,wecom,qqbot",
            "skill_governance_mode": mode,
        }
    }


def test_active_ivd_catalog_is_small_and_platform_scoped():
    assert resolve_ivd_skill_catalog("weixin", _config()) == IVD_SKILL_CATALOG
    assert resolve_ivd_skill_catalog("wecom", _config()) == IVD_SKILL_CATALOG
    assert resolve_ivd_skill_catalog("qqbot", _config()) == IVD_SKILL_CATALOG
    assert resolve_ivd_skill_catalog("telegram", _config()) is None


def test_shadow_mode_observes_without_filtering_catalog():
    assert resolve_ivd_skill_catalog("weixin", _config("shadow")) is None


def test_business_turn_allows_one_business_skill_and_blocks_overlap():
    token = begin_ivd_skill_turn(
        question="WES V5 建库投入量是多少",
        governance_mode="active",
    )
    try:
        first = evaluate_ivd_skill_load("ngs-workflow-router", body_chars=800)
        second = evaluate_ivd_skill_load("ivd-knowledge-delivery", body_chars=1200)
        ops = evaluate_ivd_skill_load("ivd-system-operations", body_chars=1600)
        snapshot = get_ivd_skill_snapshot()
    finally:
        end_ivd_skill_turn(token)

    assert first.allowed is True
    assert second.allowed is False
    assert second.reason == "business_skill_limit"
    assert ops.allowed is False
    assert ops.reason == "task_domain_mismatch"
    assert snapshot["skill_load_count"] == 1
    assert snapshot["skill_body_chars"] == 800
    assert snapshot["skill_blocked_loads"] == 2
    assert snapshot["skill_max_concurrent"] == 1


def test_ivd_operations_uses_single_canonical_operations_skill():
    token = begin_ivd_skill_turn(
        question="排查 Hermes Gateway 响应延迟",
        governance_mode="active",
    )
    try:
        canonical = evaluate_ivd_skill_load("ivd-system-operations", body_chars=900)
        duplicate = evaluate_ivd_skill_load(
            "hermes-latency-troubleshooting", body_chars=900
        )
    finally:
        end_ivd_skill_turn(token)

    assert canonical.allowed is True
    assert duplicate.allowed is False
    assert duplicate.reason == "canonical_skill_required"


def test_eval_skill_is_not_available_to_ordinary_after_sales_answer():
    token = begin_ivd_skill_turn(
        question="CNV-seq 反应体积是多少",
        governance_mode="active",
    )
    try:
        decision = evaluate_ivd_skill_load("ngs-eval-maintainer", body_chars=500)
    finally:
        end_ivd_skill_turn(token)

    assert decision.allowed is False
    assert decision.reason == "task_domain_mismatch"


def test_shadow_mode_records_would_block_but_keeps_behavior():
    token = begin_ivd_skill_turn(
        question="PMseq 孵育多长时间",
        governance_mode="shadow",
    )
    try:
        decision = evaluate_ivd_skill_load("ivd-system-operations", body_chars=700)
        snapshot = get_ivd_skill_snapshot()
    finally:
        end_ivd_skill_turn(token)

    assert decision.allowed is True
    assert decision.reason == "shadow_would_block:task_domain_mismatch"
    assert snapshot["skill_load_count"] == 1
    assert snapshot["skill_shadow_would_block"] == 1


def _write_skill(home, name):
    path = home / "skills" / "research" / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Use when testing {name}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def test_real_skill_view_blocks_wrong_domain(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_skill(tmp_path, "ivd-system-operations")
    token = begin_ivd_skill_turn(
        question="NIFTY 建库投入量是多少",
        governance_mode="active",
    )
    try:
        payload = json.loads(
            _skill_view_with_bump({"name": "ivd-system-operations"})
        )
    finally:
        end_ivd_skill_turn(token)

    assert payload["success"] is False
    assert payload["reason"] == "task_domain_mismatch"


def test_real_skill_view_records_accepted_body_once(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_skill(tmp_path, "ngs-workflow-router")
    token = begin_ivd_skill_turn(
        question="PMseq 孵育多长时间",
        governance_mode="active",
    )
    try:
        first = json.loads(_skill_view_with_bump({"name": "ngs-workflow-router"}))
        second = json.loads(_skill_view_with_bump({"name": "ngs-workflow-router"}))
        snapshot = get_ivd_skill_snapshot()
    finally:
        end_ivd_skill_turn(token)

    assert first["success"] is True
    assert second["success"] is False
    assert second["reason"] == "duplicate_skill_load"
    assert snapshot["skill_load_count"] == 1
    assert snapshot["skill_body_chars"] > 0
