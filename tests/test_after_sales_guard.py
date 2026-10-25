import os
import hashlib
import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
import subprocess

import pytest

from gateway.after_sales_guard import (
    activate_validated_fact,
    CriticalAfterSalesValidator,
    build_direct_fact_result,
    build_preflight_block_result,
    prepare_after_sales_turn,
)
from gateway.ivd_verified_facts import VerifiedFactService
from hermes_state import SessionDB
from gateway.after_sales_guard import _load_module


# Local audit pins the approved KB candidate. Governed releases instead use
# their existing sibling .staged.json identity, so future releases need no pin edit.
LOCAL_KB_COMMIT = "95eddfeeba92d882e69ffd746620569745e4dbc6"


def _local_git(root, *args, input=None):
    # Process-local protocol policy works on Git 2.43, including lazy fetches.
    # Do not inherit repository/config injection through GIT_* environment vars.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_ALLOW_PROTOCOL="file", GIT_TERMINAL_PROMPT="0",
               GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    return subprocess.check_output(
        ["git", "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
         "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
        input=input, text=True, stderr=subprocess.PIPE, env=env, timeout=5,
    ).strip()


def _verify_local_git_tree(root):
    def checked(*args, input=None):
        try:
            return _local_git(root, *args, input=input)
        except subprocess.CalledProcessError as exc:
            raise ValueError("local-only Git repository or candidate object missing: "
                             + (exc.stderr or "")) from exc
    # Linked staged worktrees share their common object directory legitimately.
    common = Path(checked("rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = root / common
    objects = common.resolve(strict=True) / "objects"
    try:
        partial = _local_git(root, "config", "--get-regexp",
                             r"^(extensions\.partialclone|remote\..*\.promisor)$")
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1:
            raise
        partial = ""
    if partial or list((objects / "pack").glob("*.promisor")):
        raise ValueError("local-only Git refuses partial/promisor repository")
    if (objects / "info/alternates").exists():
        raise ValueError("local-only Git refuses alternate object stores")
    commit = checked("rev-parse", "--verify", "HEAD^{commit}")
    tree = checked("rev-parse", "--verify", "HEAD^{tree}")
    # Check only this candidate commit and its full tree, never history/fsck.
    expected = {commit: "commit", tree: "tree"}
    for row in checked("ls-tree", "-r", "-t", "-z", "--full-tree", "HEAD").split("\0"):
        if row:
            _, kind, oid = row.split("\t", 1)[0].split()
            if kind == "commit":
                raise ValueError("local-only Git refuses submodule inputs")
            expected[oid] = kind
    checked = checked("cat-file", "--batch-check=%(objectname) %(objecttype)",
                         input="\n".join(expected) + "\n").splitlines()
    if checked != [oid + " " + kind for oid, kind in expected.items()]:
        raise ValueError("local-only Git missing or invalid candidate object")


def _knowledgehub_root(*, agent_root=None):
    import subprocess

    configured = os.environ.get("IVD_KB_ROOT")
    if not configured:
        raise RuntimeError("KB binding: IVD_KB_ROOT must explicitly name the candidate")
    try:
        root = Path(configured).expanduser().resolve(strict=True)
        agent = (agent_root or Path(__file__).resolve().parents[1]).resolve()
        marker = agent.parent / ".staged.json"
        expected = LOCAL_KB_COMMIT
        if marker.is_file():
            staged = json.loads(marker.read_text())
            expected = staged["knowledgehub_commit"]
            if root != (agent.parent / "knowledgehub").resolve(strict=True):
                raise ValueError("root does not belong to the staged candidate")
        _verify_local_git_tree(root)
        def git(*args):
            return _local_git(root, *args)
        if Path(git("rev-parse", "--show-toplevel")).resolve() != root:
            raise ValueError("not a candidate repository root")
        if git("rev-parse", "HEAD") != expected:
            raise ValueError("candidate commit mismatch")
        for name in ("scripts/after_sales_workflow_gate.py",
                     "scripts/after_sales_answer_validator.py",
                     "scripts/hermes_fast_response_pipeline.py",
                     "knowledge-base/workflows/facts"):
            if not (root / name).exists():
                raise ValueError("required candidate input missing: " + name)
        if git("status", "--porcelain", "--untracked-files=no"):
            raise ValueError("candidate tracked inputs are modified")
        return root
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise RuntimeError("KB binding: " + str(exc)) from exc


KB = _knowledgehub_root()
SCALAR_QUESTION = "NIFTY手工提取血浆体积是多少"
THRESHOLD_QUESTION = "NIFTY手工文库质控浓度阈值是多少"


def _parameter_card():
    path = KB / "scripts/hermes_fast_response_pipeline.py"
    module = _load_module(str(path), path.stat().st_mtime_ns)
    return module.build_fast_response_plan(SCALAR_QUESTION)["fast_path"]["answer_card"]


def _presentation():
    path = KB / "scripts/hermes_channel_presentation.py"
    return _load_module(str(path), path.stat().st_mtime_ns)


NIFTY_SOURCE_REL = _parameter_card()["source_path"]
SOURCE_REVISION = hashlib.sha256((KB / NIFTY_SOURCE_REL).read_bytes()).hexdigest()


def _parameter_conditions():
    return sorted(f"{key}={value}" for key, value in _parameter_card()["conditions"].items())


def _validated_claim(**changes):
    card = _parameter_card()
    return dict({"fact_key": card["fact_key"], "value": card["value"], "unit": card["unit"],
                 "conditions": _parameter_conditions(), "evidence_id": "fixture-validated",
                 "source_path": card["source_path"], "source_revision": SOURCE_REVISION,
                 "locator": card["source_locator"], "source_document_id": card["source_document_id"],
                 "source_version": card["source_version"]}, **changes)




def test_knowledgehub_root_honors_cross_platform_override():
    # H01: explicit, immutable fixture binding replaces a HOME-only helper test.
    assert _knowledgehub_root() == Path(os.environ["IVD_KB_ROOT"]).resolve()


def _config(enabled=True):
    return {
        "after_sales_guard": {
            "enabled": enabled,
            "platforms": ["weixin", "wecom", "qqbot", "telegram"],
            "workflow_module": str(KB / "scripts/after_sales_workflow_gate.py"),
            "validator_module": str(KB / "scripts/after_sales_answer_validator.py"),
            "cards_dir": str(KB / "knowledge-base/workflows/facts"),
        }
    }


def _config_with_fast_response(enabled=True):
    config = _config(enabled=enabled)
    config["after_sales_guard"]["fast_response_module"] = str(KB / "scripts/hermes_fast_response_pipeline.py")
    return config


def _verified_fact_fixture(tmp_path, *, mode="active", manifest_revision=None,
                           value="200", fact_key=None, record_changes=None):
    card = _parameter_card()
    db = SessionDB(db_path=tmp_path / "state.db")
    record = {
        "fact_id": "fixture-nifty-plasma-volume", "product_scope": card["product_scope"],
        "product_variant": card["product_variant"], "question_type": "scalar_lookup",
        "fact_key": fact_key or card["fact_key"], "value": value, "unit": card["unit"],
        "conditions": _parameter_conditions(), "answer_template": "{value} {unit}。",
        "evidence": [{"evidence_id": "ev-a", "source_path": NIFTY_SOURCE_REL,
                      "source_revision": SOURCE_REVISION, "locator": card["source_locator"],
                      "source_document_id": card["source_document_id"], "source_version": card["source_version"]}],
        "source_revision": SOURCE_REVISION, "status": "active",
    }
    record.update(record_changes or {})
    assert VerifiedFactService(db).activate(record)
    manifest = tmp_path / "knowledge-release.json"
    manifest.write_text(json.dumps({"status": "ready", "source_revisions": {
        NIFTY_SOURCE_REL: manifest_revision if manifest_revision is not None else SOURCE_REVISION}}))
    config = _config_with_fast_response()
    config["after_sales_guard"].update({
        "answer_shape_enabled": True, "evidence_sidecar_enabled": True,
        "verified_fact_reuse_mode": mode, "knowledge_release_manifest": str(manifest)})
    return db, config



def _write_experience_modules(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    fast = scripts / "hermes_fast_response_pipeline.py"
    fast.write_text(
        "def build_fast_response_plan(message, question_type='general'):\n"
        "    return {'runtime_preflight': {'eligible': False}}\n",
        encoding="utf-8",
    )
    (scripts / "after_sales_platform_policy.py").write_text(
        "def render_answer_experience_context(*, platform):\n"
        "    platform = platform.strip().lower()\n"
        "    if platform not in ('weixin', 'wecom', 'qqbot', 'telegram'):\n"
        "        raise ValueError(platform)\n"
        "    return '[统一体验]\\n先直接回答结论。\\n按顺序给出下一步动作。'\n",
        encoding="utf-8",
    )
    # Real fixed-release presentation module; the fast plan remains a test fixture.
    (scripts / "hermes_channel_presentation.py").write_bytes(
        (KB / "scripts/hermes_channel_presentation.py").read_bytes())
    return fast


def test_all_ivd_platforms_receive_identical_shared_answer_experience(tmp_path, monkeypatch):
    fast = _write_experience_modules(tmp_path)
    _unit_planner_loader(monkeypatch, fast)
    config = _config(enabled=True)
    config["after_sales_guard"]["fast_response_module"] = str(fast)
    config["after_sales_guard"]["validator_module"] = str(tmp_path / "missing-validator.py")
    config["after_sales_guard"]["cards_dir"] = str(tmp_path / "missing-cards")

    contexts = {}
    for platform in ("weixin", "wecom", "qqbot", "telegram"):
        turn = prepare_after_sales_turn(
            config,
            platform=platform,
            message="这个异常应该先查什么？",
            history=[],
        )
        assert turn is not None
        contexts[platform] = {k: v for k, v in turn.presentation_contract.items() if k != "platform"}
        assert turn.presentation_contract["platform"] == platform
        assert turn.context == _presentation().render_presentation_instructions(turn.presentation_contract)
        assert turn.facts == {}
        assert turn.has_validator is False

    assert len({json.dumps(c, sort_keys=True) for c in contexts.values()}) == 1
    assert contexts["qqbot"]["lead_with_answer"] is True
    assert contexts["qqbot"]["internal_status_visible"] is False


def _unit_planner_loader(monkeypatch, fixture_path):
    """Isolate only this synthetic planner; every real path uses trusted loader."""
    import importlib.util
    from gateway import after_sales_guard as guard
    real_loader = guard._load_module
    # Prove the runtime would reject this arbitrary temporary root. This unit
    # seam does not grant it production authority or change original assertions.
    with pytest.raises(ImportError):
        real_loader(str(fixture_path), fixture_path.stat().st_mtime_ns)

    def load(path, mtime):
        if Path(path).resolve() != fixture_path.resolve():
            return real_loader(path, mtime)
        spec = importlib.util.spec_from_file_location("synthetic_plan_unit_only", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    monkeypatch.setattr(guard, "_load_module", load)


def _write_boundary_fast_module(tmp_path, source_path, *, source_status="source_candidates_found"):
    scripts = tmp_path / "scripts-boundary"
    scripts.mkdir()
    fast = scripts / "hermes_fast_response_pipeline.py"
    fast.write_text(
        "def build_fast_response_plan(message, question_type='general'):\n"
        "    return {\n"
        "      'runtime_preflight': {'eligible': True, 'route_version': 'boundary-v1'},\n"
        "      'answer_template': {'style': 'short_first'},\n"
        "      'preflight_gate': {'decision': 'allow', 'pipeline_action': 'continue_final_answer', 'issues': []},\n"
        "      'fast_path': {'route_id': 'sop_parameter_short_answer'},\n"
        "      'initial_files': [],\n"
        "      'answer_contract': {\n"
        "        'deliverable': 'difference_list', 'comparison_dimensions': ['process'],\n"
        "        'excluded_topics': ['reaction_conditions', 'performance_claims'],\n"
        "        'must_preserve': ['version_scope', 'source_conflict', 'uncertainty'],\n"
        "        'detail_level': 'brief'},\n"
        f"      'source_location': {{'status': {source_status!r}, 'input_sufficient': True,\n"
        f"        'candidates': [{{'resolved_path': {str(source_path)!r}, 'authority': 'locator_only'}}]}}\n"
        "    }\n",
        encoding="utf-8",
    )
    (scripts / "after_sales_platform_policy.py").write_text(
        "def render_answer_experience_context(*, platform):\n"
        "    return '[统一体验]\\n按当前任务边界直接回答。'\n",
        encoding="utf-8",
    )
    # Real fixed-release presentation module; the fast plan remains a test fixture.
    (scripts / "hermes_channel_presentation.py").write_bytes(
        (KB / "scripts/hermes_channel_presentation.py").read_bytes())
    return fast


def test_four_platforms_receive_same_boundary_and_material_location(tmp_path, monkeypatch):
    source = tmp_path / "PMseq RNA V5 SOP.pdf"
    source.write_text("fixture", encoding="utf-8")
    fast = _write_boundary_fast_module(tmp_path, source)
    _unit_planner_loader(monkeypatch, fast)
    config = _config(enabled=True)
    config["after_sales_guard"]["fast_response_module"] = str(fast)

    contexts = []
    for platform in ("weixin", "wecom", "qqbot", "telegram"):
        turn = prepare_after_sales_turn(
            config,
            platform=platform,
            message="PMseq RNA V5跟V4有什么不同，只列流程差异",
            history=[],
        )
        assert turn is not None
        assert turn.answer_contract["deliverable"] == "difference_list"
        assert turn.source_location["status"] == "source_candidates_found"
        assert str(source.resolve()) in turn.source_paths
        assert "只输出差异清单" in turn.context
        assert "反应条件" in turn.context
        assert "正文第一行直接进入第1项差异" in turn.context
        assert "禁止前置“结论”“主要差异”“来源”" in turn.context
        assert "不得输出材料库文件名、路径或内部 reference 标识" in turn.context
        assert turn.presentation_contract["platform"] == platform
        assert _presentation().render_presentation_instructions(turn.presentation_contract) in turn.context
        contexts.append((turn.answer_contract, turn.source_location, turn.source_paths))

    assert all(c == contexts[0] for c in contexts)


def test_sufficient_identity_lookup_failure_does_not_repeat_known_fields(tmp_path, monkeypatch):
    source = tmp_path / "PMseq RNA V5 SOP.pdf"
    source.write_text("fixture", encoding="utf-8")
    fast = _write_boundary_fast_module(tmp_path, source)
    _unit_planner_loader(monkeypatch, fast)
    config = _config(enabled=True)
    config["after_sales_guard"]["fast_response_module"] = str(fast)

    turn = prepare_after_sales_turn(
        config,
        platform="weixin",
        message="PMseq RNA V5 建库投入量是多少",
        history=[],
    )
    assert turn is not None
    fallback = turn.validate("建库投入量为100ng。", messages=[])["fallback"]

    assert "请补充产品版本" not in fallback
    assert "请补充SOP编号" not in fallback
    assert "材料库没有" not in fallback
    assert "未完成已定位正式资料的读取" in fallback


def test_ivd_platform_matching_is_normalized_before_policy_loading(tmp_path, monkeypatch):
    fast = _write_experience_modules(tmp_path)
    _unit_planner_loader(monkeypatch, fast)
    config = _config(enabled=True)
    config["after_sales_guard"]["fast_response_module"] = str(fast)
    config["after_sales_guard"]["workflow_module"] = str(tmp_path / "missing.py")
    config["after_sales_guard"]["validator_module"] = str(tmp_path / "missing-validator.py")
    config["after_sales_guard"]["cards_dir"] = str(tmp_path / "missing-cards")

    turn = prepare_after_sales_turn(
        config,
        platform=" Telegram ",
        message="报告出不来",
        history=[],
    )

    assert turn is not None
    assert turn.presentation_contract["platform"] == "telegram"
    assert turn.context == _presentation().render_presentation_instructions(turn.presentation_contract)


def test_matched_turn_composes_experience_with_facts_and_validator(tmp_path, monkeypatch):
    fast = _write_experience_modules(tmp_path)
    _unit_planner_loader(monkeypatch, fast)
    config = _config(enabled=True)
    config["after_sales_guard"]["fast_response_module"] = str(fast)

    turn = prepare_after_sales_turn(
        config,
        platform="qqbot",
        message="NIFTY手工实验文库浓度低怎么排查？",
        history=[],
    )

    assert turn is not None
    assert _presentation().render_presentation_instructions(turn.presentation_contract) in turn.context
    assert "当前测量节点：文库浓度质控" in turn.context
    assert turn.has_validator is True


def test_missing_experience_module_keeps_existing_fail_open_behavior(tmp_path, monkeypatch):
    fast = tmp_path / "hermes_fast_response_pipeline.py"
    fast.write_text(
        "def build_fast_response_plan(message, question_type='general'):\n"
        "    return {'runtime_preflight': {'eligible': False}}\n",
        encoding="utf-8",
    )
    _unit_planner_loader(monkeypatch, fast)
    config = _config(enabled=True)
    config["after_sales_guard"]["fast_response_module"] = str(fast)
    config["after_sales_guard"]["workflow_module"] = str(tmp_path / "missing.py")

    turn = prepare_after_sales_turn(
        config,
        platform="qqbot",
        message="这个异常应该先查什么？",
        history=[],
    )

    assert turn is None


def test_blocked_preflight_is_structured_and_stops_before_model(tmp_path):
    module = tmp_path / "scripts" / "fake_fast_pipeline.py"
    module.parent.mkdir()
    module.write_text(
        "def build_fast_response_plan(message, question_type='general'):\n"
        "    return {\n"
        "      'runtime_preflight': {'eligible': True, 'route_version': 'test-v1'},\n"
        "      'answer_template': {'style': 'short_first'},\n"
        "      'preflight_gate': {\n"
        "        'decision': 'block',\n"
        "        'pipeline_action': 'stop_before_final_answer',\n"
        "        'issues': ['pending_candidate_source_used'],\n"
        "      },\n"
        "      'fast_path': {'route_id': 'blocked-test'},\n"
        "      'initial_files': ['knowledge-base/candidates/pending.md'],\n"
        "    }\n",
        encoding="utf-8",
    )
    config = _config(enabled=True)
    config["after_sales_guard"]["fast_response_module"] = str(module)

    turn = prepare_after_sales_turn(
        config,
        platform="qqbot",
        message="这个候选结论可以直接回复吗？",
        history=[],
    )

    assert turn is not None
    assert turn.preflight_decision == "block"
    assert turn.preflight_action == "stop_before_answer_generation"
    assert turn.blocks_answer_generation is True
    assert turn.preflight_issues == ("pending_candidate_source_used",)

    result = build_preflight_block_result(turn, "这个候选结论可以直接回复吗？")
    assert result["api_calls"] == 0
    assert result["agent_persisted"] is False
    assert result["preflight_blocked"] is True
    assert "待验证或非正式来源" in result["final_response"]
    assert "pipeline" not in result["final_response"].casefold()


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
        message=SCALAR_QUESTION,
        history=[],
    )

    assert turn is not None
    assert "快速回答管线" in turn.context
    assert str((KB / NIFTY_SOURCE_REL).resolve()) in turn.context
    assert not turn.facts
    assert turn.has_validator is True
    assert all(Path(path).is_absolute() for path in turn.source_paths)


def test_fast_turn_injects_explicit_product_variant_identity():
    turn = prepare_after_sales_turn(
        _config_with_fast_response(),
        platform="weixin",
        message="地贫508为什么不能检测三联体？",
        history=[],
    )

    assert turn is not None
    assert turn.product_scope == "地贫"
    assert turn.product_variant == "508"
    assert "已识别产品：地贫" in turn.context
    assert "产品变体：508" in turn.context
    assert "thalassemia-508-mutations.md" in turn.context


def test_fast_parameter_turn_requires_reading_routed_source_and_rejects_unknown_number():
    turn = prepare_after_sales_turn(
        _config_with_fast_response(),
        platform="weixin",
        message=SCALAR_QUESTION,
        history=[],
    )
    source_path = turn.source_paths[0]
    missing_source = turn.validate("提取血浆体积为200μL。", messages=[])
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
            "content": "当前受控参数索引规定提取血浆体积为200μL。",
        },
    ]

    supported = turn.validate("提取血浆体积为200μL。", messages=messages)
    unsupported = turn.validate("提取血浆体积为400μL。", messages=messages)

    assert missing_source["ok"] is False
    assert "formal_source_not_read" in missing_source["reasons"]
    assert "请先读取" not in missing_source["fallback"]
    assert "未能完成正式来源核实" in missing_source["fallback"]
    assert "请补充产品版本" not in missing_source["fallback"]
    assert "SOP编号" not in missing_source["fallback"]
    assert supported["ok"] is True
    assert unsupported["ok"] is False
    assert unsupported["reasons"] == ["scalar_claim_mismatch"]
    assert "请重新读取" not in unsupported["fallback"]
    assert unsupported["fallback"] == "当前草稿数值与该产品、该参数字段的正式值不一致。"


def test_fast_plan_carries_canonical_answer_shape_into_turn():
    turn = prepare_after_sales_turn(
        _config_with_fast_response(),
        platform="weixin",
        message=SCALAR_QUESTION,
        history=[],
    )

    assert turn is not None
    assert turn.answer_shape == "scalar_lookup"


def test_scalar_turn_uses_canonical_fact_key_from_knowledgehub():
    turn = prepare_after_sales_turn(
        _config_with_fast_response(),
        platform="weixin",
        message=SCALAR_QUESTION,
        history=[],
    )

    assert turn is not None
    assert turn.answer_shape == "scalar_lookup"
    assert turn.source_paths[0] == str((KB / NIFTY_SOURCE_REL).resolve())
    assert turn.resolved_answer.card_id == _parameter_card()["fact_key"]
    assert turn.resolved_answer.text == _parameter_card()["answer_text"]
    # Check adapter identity last: preceding source/card/output checks still execute.
    assert turn.fact_key == _parameter_card()["fact_key"]


def test_validated_scalar_answer_returns_canonical_value_and_unit_only():
    turn = prepare_after_sales_turn(
        _config_with_fast_response(),
        platform="weixin",
        message=SCALAR_QUESTION,
        history=[],
    )
    source_path = turn.source_paths[0]
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "source-read",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": source_path}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "source-read",
            "content": "向浅孔板中加入200μL血浆样本。",
        },
    ]

    result = turn.validate(
        "无创提取需要200 μL血浆。建议同时注意样本质量并按SOP操作。",
        messages=messages,
    )

    assert result["ok"] is True
    assert result["normalized_response"] == "200 μL。"


def test_scalar_answer_with_multiple_supported_values_is_not_truncated():
    turn = prepare_after_sales_turn(
        _config_with_fast_response(),
        platform="weixin",
        message=SCALAR_QUESTION,
        history=[],
    )
    source_path = turn.source_paths[0]
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "source-read",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": source_path}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "source-read",
            "content": "标准流程使用200μL血浆；另一个条件使用600μL。",
        },
    ]

    result = turn.validate("标准流程200μL，另一个条件600μL。", messages=messages)

    assert result["ok"] is False
    assert "scalar_claim_not_unique" in result["reasons"]
    assert "normalized_response" not in result


def test_scalar_answer_rejects_a_different_number_from_the_same_sop():
    turn = prepare_after_sales_turn(
        _config_with_fast_response(),
        platform="weixin",
        message=SCALAR_QUESTION,
        history=[],
    )
    source_path = turn.source_paths[0]
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "source-read",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": source_path}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "source-read",
            "content": "提取加样使用200μL血浆；血浆分装时可见600μL。",
        },
    ]

    result = turn.validate("600 μL。", messages=messages)

    assert result["ok"] is False
    assert "scalar_claim_mismatch" in result["reasons"]


def test_fact_card_scalar_answer_uses_the_same_canonical_boundary():
    turn = prepare_after_sales_turn(
        _config_with_fast_response(),
        platform="weixin",
        message=THRESHOLD_QUESTION,
        history=[
            {"role": "user", "content": "NIFTY文库质控异常"},
            {"role": "assistant", "content": "请提供文库浓度。"},
        ],
    )

    assert turn is not None
    assert turn.answer_shape == "scalar_lookup"
    assert turn.facts

    result = turn.validate("文库浓度应≥2 ng/μL，建议再检查质控记录。")

    assert result["ok"] is True
    assert result["normalized_response"] == "≥2 ng/μL。"


def test_fact_card_scalar_rejects_value_outside_current_route_contract():
    turn = prepare_after_sales_turn(
        _config_with_fast_response(),
        platform="weixin",
        message=THRESHOLD_QUESTION,
        history=[
            {"role": "user", "content": "NIFTY文库质控异常"},
            {"role": "assistant", "content": "请提供文库浓度。"},
        ],
    )
    turn = replace(turn, expected_scalar_claims=(">=2 ng/μL",))

    result = turn.validate("文库浓度是4 ng/μL。")

    assert result["ok"] is False
    assert "scalar_claim_mismatch" in result["reasons"]


def test_shadow_fact_hit_does_not_bypass_model(tmp_path):
    db, config = _verified_fact_fixture(tmp_path, mode="shadow")

    turn = prepare_after_sales_turn(
        config,
        platform="weixin",
        message=SCALAR_QUESTION,
        history=[],
        session_db=db,
    )

    assert turn is not None
    assert turn.verified_fact_hit is True
    assert turn.direct_response == _parameter_card()["answer_text"]
    assert turn.resolved_answer.source_kind == "formal_answer_card"
    assert "ev-a" not in turn.direct_evidence_ids


def test_active_fact_hit_returns_direct_answer_without_model(tmp_path):
    db, config = _verified_fact_fixture(tmp_path, mode="active")

    turn = prepare_after_sales_turn(
        config,
        platform="weixin",
        message=SCALAR_QUESTION,
        history=[],
        session_db=db,
    )
    result = build_direct_fact_result(turn, SCALAR_QUESTION)

    assert turn is not None
    assert turn.answer_shape == "scalar_lookup"
    assert turn.verified_fact_hit is True
    assert turn.direct_response == _parameter_card()["answer_text"] + "。"
    assert result["api_calls"] == 0
    assert result["final_response"] == turn.present_text(_parameter_card()["answer_text"] + "。")


def test_active_fact_with_old_scalar_value_does_not_bypass_current_route(tmp_path):
    db, config = _verified_fact_fixture(tmp_path, mode="active", value="240", fact_key="volume",
                                         record_changes={"conditions": []})

    turn = prepare_after_sales_turn(
        config,
        platform="weixin",
        message=SCALAR_QUESTION,
        history=[],
        session_db=db,
    )

    assert turn is not None
    assert turn.verified_fact_hit is False
    assert turn.direct_response == _parameter_card()["answer_text"]
    assert turn.resolved_answer.source_kind == "formal_answer_card"
    assert "240" not in build_direct_fact_result(turn, SCALAR_QUESTION)["final_response"]


def test_ascii_threshold_operator_is_preserved_in_canonical_response():
    turn = prepare_after_sales_turn(
        _config_with_fast_response(),
        platform="weixin",
        message=THRESHOLD_QUESTION,
        history=[
            {"role": "user", "content": "NIFTY文库质控异常"},
            {"role": "assistant", "content": "请提供文库浓度。"},
        ],
    )

    result = turn.validate("文库浓度应>=2 ng/μL。")

    assert result["ok"] is True
    assert result["normalized_response"] == "≥2 ng/μL。"


def test_stale_source_revision_disables_direct_fact_reuse(tmp_path):
    db, config = _verified_fact_fixture(
        tmp_path,
        mode="active",
        manifest_revision="rev-b",
        value="777",
    )

    turn = prepare_after_sales_turn(
        config,
        platform="weixin",
        message=SCALAR_QUESTION,
        history=[],
        session_db=db,
    )

    assert turn is not None
    assert turn.verified_fact_hit is False
    assert turn.direct_response == _parameter_card()["answer_text"]
    assert turn.resolved_answer.source_kind == "formal_answer_card"


def test_validated_adopted_claim_activates_rebuildable_fact(tmp_path):
    db, config = _verified_fact_fixture(tmp_path, mode="off")
    turn = prepare_after_sales_turn(
        config,
        platform="weixin",
        message=SCALAR_QUESTION,
        history=[],
        session_db=db,
    )
    validation = {"ok": True, "adopted_claims": [_validated_claim(evidence_id="ev-new")]}

    assert activate_validated_fact(
        db,
        turn,
        question=SCALAR_QUESTION,
        validation=validation,
    ) is True
    match = VerifiedFactService(db).lookup(
        product_scope="NIFTY",
        product_variant="",
        fact_key=_parameter_card()["fact_key"],
        conditions=_parameter_conditions(),
        source_revisions={NIFTY_SOURCE_REL: SOURCE_REVISION},
    )
    assert match["rendered_answer"] == _parameter_card()["answer_text"] + "。"


def test_fact_activation_rejects_value_outside_current_scalar_contract(tmp_path):
    db, config = _verified_fact_fixture(tmp_path, mode="off")
    turn = prepare_after_sales_turn(
        config,
        platform="weixin",
        message=SCALAR_QUESTION,
        history=[],
        session_db=db,
    )

    assert activate_validated_fact(
        db,
        turn,
        question=SCALAR_QUESTION,
        validation={"ok": True, "adopted_claims": [_validated_claim(value="100", evidence_id="ev-old")]},
    ) is False


def test_read_formal_source_builds_one_runtime_adopted_claim(tmp_path):
    db, config = _verified_fact_fixture(tmp_path, mode="off")
    turn = prepare_after_sales_turn(
        config,
        platform="weixin",
        message=SCALAR_QUESTION,
        history=[],
        session_db=db,
    )
    source_path = turn.source_paths[0]
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "source-read",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": source_path}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "source-read",
            "content": "当前正式来源规定血浆提取体积为200μL。",
        },
    ]

    result = turn.validate("200 μL。", messages=messages)

    assert result["ok"] is True
    assert len(result["adopted_claims"]) == 1
    assert result["adopted_claims"][0]["value"] == "200"
    assert result["adopted_claims"][0]["source_revision"] == SOURCE_REVISION
    assert result["adopted_claims"][0]["tool_call_id"] == "source-read"


@pytest.mark.parametrize("validation_case", ["not_validated", "zero_claims", "two_claims"])
def test_unvalidated_or_multi_claim_answer_does_not_activate_fact(tmp_path, validation_case):
    db, config = _verified_fact_fixture(tmp_path, mode="off")
    turn = prepare_after_sales_turn(
        config, platform="weixin", message=SCALAR_QUESTION, history=[], session_db=db,
    )
    claim = _validated_claim(evidence_id="ev-multi")
    validation = {
        "not_validated": {"ok": False, "adopted_claims": []},
        "zero_claims": {"ok": True, "adopted_claims": []},
        "two_claims": {"ok": True, "adopted_claims": [claim, dict(claim)]},
    }[validation_case]
    assert activate_validated_fact(db, turn, question=SCALAR_QUESTION, validation=validation) is False
    match = VerifiedFactService(db).lookup(
        product_scope="NIFTY", product_variant="", fact_key=_parameter_card()["fact_key"], conditions=_parameter_conditions(),
        source_revisions={NIFTY_SOURCE_REL: SOURCE_REVISION},
    )
    assert match["rendered_answer"] == _parameter_card()["answer_text"] + "。"



def test_unsafe_fast_route_injects_only_shared_answer_experience():
    turn = prepare_after_sales_turn(
        _config_with_fast_response(),
        platform="weixin",
        message="需要",
        history=[],
    )

    assert turn is not None
    assert turn.context == _presentation().render_presentation_instructions(turn.presentation_contract)
    assert turn.presentation_contract["content_obligations"] == (
        "scope", "objects_and_units", "evidence", "limitations", "next_action_when_needed")
    assert "快速回答管线" not in turn.context
    assert "已识别产品" not in turn.context
    assert turn.facts == {}
    assert turn.has_validator is False


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

    assert turn is None  # No actor-bound live pending task authorized this short reply.


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
    assert validator.validation_status == "pass"


def test_critical_validator_records_the_existing_final_validation_once():
    calls = []

    class Turn:
        def validate(self, answer, *, messages=None):
            calls.append((answer, messages))
            return {"ok": True, "reasons": [], "fallback": ""}

    validator = CriticalAfterSalesValidator(
        turn=Turn(), messages_provider=lambda: [{"role": "assistant"}]
    )

    result = validator("答案")

    assert result["ok"] is True
    assert validator.validation_status == "pass"
    assert len(calls) == 1


def test_after_sales_turn_delegates_compatibility_contract_fields():
    contract = object()
    prepared = SimpleNamespace(
        execution_contract=contract,
        trusted_legacy_answer_enabled=True,
        execution_contract_count=1,
    )
    turn = prepare_after_sales_turn(
        _config(),
        platform="qqbot",
        message="NIFTY文库浓度低，48例和96例都出现。",
        history=[],
        prepared_ivd_turn=prepared,
    )

    assert turn.execution_contract is contract
    assert turn.trusted_legacy_answer_enabled is True
    assert turn.execution_contract_count == 1


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
    import time
    from gateway.after_sales_guard import resolve_after_sales_task
    from scripts.after_sales_workflow_gate import match_case_facts
    guard = _config()['after_sales_guard']
    task = resolve_after_sales_task(guard, history[0]['content'], actor_id='fixture-actor')
    facts = match_case_facts(guard['cards_dir'], history[0]['content'], [], task_contract=task)['facts']
    pending = {'task': task, 'question': task['question'], 'actor_id': 'fixture-actor',
               'status': 'awaiting_reply', 'expires_at': time.time()+300,
               'missing_fields': facts['missing_discriminators'], 'workflow_binding': facts['workflow_binding']}
    turn = prepare_after_sales_turn(
        _config(),
        actor_id='fixture-actor', pending_task=pending,
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


# Source identity is part of this regression's test fixture, never HOME lookup.


@pytest.mark.parametrize('bad_root', ['missing', 'nonexistent', 'not_a_repo', 'wrong_commit'])
def test_explicit_kb_binding_rejects_wrong_or_missing_source(monkeypatch, tmp_path, bad_root):
    if bad_root == 'missing':
        monkeypatch.delenv('IVD_KB_ROOT', raising=False)
    else:
        root = {'nonexistent': tmp_path / 'absent', 'not_a_repo': tmp_path,
                'wrong_commit': Path(__file__).resolve().parents[1]}[bad_root]
        monkeypatch.setenv('IVD_KB_ROOT', str(root))
    with pytest.raises(RuntimeError, match='KB binding'):
        _knowledgehub_root()


@pytest.mark.parametrize("fault", ["none", "commit", "root", "missing_file", "dirty_file", "bad_marker"])
def test_kb_binding_checks_staged_pair_and_tracked_inputs(tmp_path, monkeypatch, fault):
    # Synthetic Git fixture tests binding logic only, not business source truth.
    release = tmp_path / "release"
    root = release / "knowledgehub"
    agent = release / "hermes-agent"
    root.mkdir(parents=True)
    agent.mkdir()
    for name in ("scripts/after_sales_workflow_gate.py", "scripts/after_sales_answer_validator.py",
                 "scripts/hermes_fast_response_pipeline.py", "knowledge-base/workflows/facts/fixture.json"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture")
    def git(*args):
        return _local_git(root, *args)
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "identity fixture")
    marker = release / ".staged.json"
    marker.write_text(json.dumps({"knowledgehub_commit": git("rev-parse", "HEAD")}))
    monkeypatch.setenv("IVD_KB_ROOT", str(root))
    if fault == "commit":
        marker.write_text(json.dumps({"knowledgehub_commit": "0" * 40}))
    elif fault == "root":
        monkeypatch.setenv("IVD_KB_ROOT", str(KB))
    elif fault == "missing_file":
        (root / "scripts/after_sales_answer_validator.py").unlink()
    elif fault == "dirty_file":
        (root / "scripts/after_sales_answer_validator.py").write_text("changed fixture")
    elif fault == "bad_marker":
        marker.write_text("not JSON")
    if fault == "none":
        assert _knowledgehub_root(agent_root=agent) == root
    else:
        with pytest.raises(RuntimeError, match="KB binding"):
            _knowledgehub_root(agent_root=agent)


@pytest.mark.parametrize('fault', ['key', 'value', 'unit', 'revision', 'locator', 'conditions_empty',
                                  'method', 'object', 'step', 'source', 'document', 'version'])
def test_cache_rejects_any_outside_parameter_contract_and_keeps_formal_card(tmp_path, fault):
    card = _parameter_card()
    evidence = dict(_validated_claim())
    evidence.pop('conditions')
    changes = {}
    if fault == 'key': changes['fact_key'] = 'volume'
    elif fault == 'value': changes['value'] = '240'
    elif fault == 'unit': changes['unit'] = 'mL'
    elif fault == 'revision':
        changes['source_revision'] = 'old-revision'; evidence['source_revision'] = 'old-revision'
    elif fault == 'conditions_empty': changes['conditions'] = []
    elif fault in {'method', 'object', 'step'}:
        key = {'method': 'method', 'object': 'sample_object', 'step': 'step_label'}[fault]
        changes['conditions'] = sorted(x if not x.startswith(key+'=') else key+'=wrong' for x in _parameter_conditions())
    else:
        key = {'locator': 'locator', 'source': 'source_path', 'document': 'source_document_id', 'version': 'source_version'}[fault]
        evidence[key] = 'wrong-'+fault
    changes['evidence'] = [evidence]
    db, config = _verified_fact_fixture(tmp_path, record_changes=changes)
    turn = prepare_after_sales_turn(config, platform='weixin', message=SCALAR_QUESTION, history=[], session_db=db)
    assert turn.verified_fact_hit is False
    assert turn.direct_response == card['answer_text']
    assert turn.resolved_answer.source_kind == 'formal_answer_card'


@pytest.mark.parametrize('fault', ['key', 'value', 'unit', 'revision', 'locator', 'conditions_empty',
                                  'method', 'source', 'document', 'version'])
def test_activation_independently_rejects_manual_ok_outside_contract(tmp_path, fault):
    claim = _validated_claim()
    if fault == 'conditions_empty': claim['conditions'] = []
    elif fault == 'method': claim['conditions'] = ['method=wrong']
    else:
        key = {'key':'fact_key', 'value':'value', 'unit':'unit', 'revision':'source_revision', 'locator':'locator',
               'source':'source_path', 'document':'source_document_id', 'version':'source_version'}[fault]
        claim[key] = '100' if fault == 'value' else 'mL' if fault == 'unit' else 'wrong-'+fault
    db, config = _verified_fact_fixture(tmp_path, mode='off')
    turn = prepare_after_sales_turn(config, platform='weixin', message=SCALAR_QUESTION, history=[], session_db=db)
    before = db._conn.execute('SELECT * FROM ivd_verified_facts').fetchall()
    assert activate_validated_fact(db, turn, question=SCALAR_QUESTION,
                                   validation={'ok':True,'adopted_claims':[claim]}) is False
    assert db._conn.execute('SELECT * FROM ivd_verified_facts').fetchall() == before


def _plan_fixture(tmp_path, plan):
    p = tmp_path / 'fixture-kb/scripts/hermes_fast_response_pipeline.py'
    p.parent.mkdir(parents=True)
    source = p.parent.parent / NIFTY_SOURCE_REL
    source.parent.mkdir(parents=True)
    source.write_bytes((KB / NIFTY_SOURCE_REL).read_bytes())
    (p.parent / 'hermes_channel_presentation.py').write_bytes((KB / 'scripts/hermes_channel_presentation.py').read_bytes())
    p.write_text('def build_fast_response_plan(*args, **kwargs):\n    return '+repr(plan)+'\n')
    return p


@pytest.mark.parametrize('fault', ['valid', 'expert_block', 'preflight_block', 'not_adopted', 'identity_unconfirmed',
                                  'wrong_scope', 'wrong_variant', 'inactive', 'not_formal', 'missing_unit',
                                  'missing_locator', 'empty_conditions', 'not_parameter'])
def test_non_adopted_or_incomplete_card_cannot_promote_cache_or_activation(tmp_path, fault, monkeypatch):
    path = KB / 'scripts/hermes_fast_response_pipeline.py'
    plan = copy.deepcopy(_load_module(str(path), path.stat().st_mtime_ns).build_fast_response_plan(SCALAR_QUESTION))
    if fault == 'expert_block': plan['expert_route_contract'] = {'status':'blocked','formal_answer_allowed':False,'bounded_expert_answer_allowed':False}
    elif fault == 'preflight_block': plan['preflight_gate'] = {'decision':'block','pipeline_action':'stop_before_final_answer'}
    elif fault == 'not_adopted': plan['fast_path']['hit'] = False
    elif fault == 'missing_unit': plan['fast_path']['answer_card'].pop('unit')
    elif fault == 'missing_locator': plan['fast_path']['answer_card'].pop('source_locator')
    elif fault == 'empty_conditions': plan['fast_path']['answer_card']['conditions'] = {}
    elif fault == 'not_parameter': plan['fast_path']['answer_card']['knowledge_kind'] = 'contact'
    elif fault == 'identity_unconfirmed': plan['product_identity']['product_scope_confirmed'] = False
    elif fault == 'wrong_scope': plan['fast_path']['answer_card']['product_scope'] = 'other'
    elif fault == 'wrong_variant': plan['fast_path']['answer_card']['product_variant'] = 'other'
    elif fault == 'inactive': plan['fast_path']['answer_card']['effective_status'] = 'pending_verify'
    elif fault == 'not_formal': plan['fast_path']['answer_card']['authority_level'] = 'candidate'
    db, config = _verified_fact_fixture(tmp_path)
    fixture_path = _plan_fixture(tmp_path, plan)
    _unit_planner_loader(monkeypatch, fixture_path)
    config['after_sales_guard']['fast_response_module'] = str(fixture_path)
    turn = prepare_after_sales_turn(config, platform='weixin', message=SCALAR_QUESTION, history=[], session_db=db)
    if fault == 'valid':
        assert turn.verified_fact_hit is True
        assert activate_validated_fact(db, turn, question=SCALAR_QUESTION,
                                       validation={'ok':True,'adopted_claims':[_validated_claim()]}) is True
    else:
        assert turn.verified_fact_hit is False
        assert activate_validated_fact(db, turn, question=SCALAR_QUESTION,
                                       validation={'ok':True,'adopted_claims':[_validated_claim()]}) is False


def test_parameter_contract_is_shared_across_four_platforms(tmp_path):
    db, config = _verified_fact_fixture(tmp_path, mode='off')
    contracts = []
    for platform in ('weixin','wecom','qqbot','telegram'):
        turn = prepare_after_sales_turn(config, platform=platform, message=SCALAR_QUESTION, history=[], session_db=db)
        assert turn.fact_key == _parameter_card()['fact_key']
        assert turn.expected_scalar_claims == ('200 uL',)
        contracts.append(turn.parameter_contract)
    assert all(c == contracts[0] for c in contracts)
    assert contracts[0]['source_revision'] == SOURCE_REVISION
    assert contracts[0]['conditions'] == _parameter_conditions()


def test_validate_to_activation_preserves_real_card_locator_and_conditions(tmp_path):
    db, config = _verified_fact_fixture(tmp_path, mode='off')
    turn = prepare_after_sales_turn(config, platform='weixin', message=SCALAR_QUESTION, history=[], session_db=db)
    messages = [{'role':'assistant','tool_calls':[{'id':'real-contract-fixture','function':{
        'name':'read_file','arguments':json.dumps({'path':turn.source_paths[0]})}}]},
        {'role':'tool','tool_call_id':'real-contract-fixture','content':'fixture:本字段200μL，另一字段600μL。'}]
    validation = turn.validate('200 μL。', messages=messages)
    assert validation['ok'] is True
    assert validation['adopted_claims'][0]['locator'] == _parameter_card()['source_locator']
    assert validation['adopted_claims'][0]['conditions'] == _parameter_conditions()
    assert activate_validated_fact(db, turn, question=SCALAR_QUESTION, validation=validation) is True


@pytest.mark.parametrize('missing', ['manifest', 'validator', 'method'])
def test_missing_contract_prerequisite_never_enables_reuse_or_activation(tmp_path, missing):
    db, config = _verified_fact_fixture(tmp_path)
    question = SCALAR_QUESTION
    if missing == 'manifest': config['after_sales_guard'].pop('knowledge_release_manifest')
    elif missing == 'validator': config['after_sales_guard']['validator_module'] = str(tmp_path / 'absent.py')
    elif missing == 'method': question = '无创提取需要多少血浆'
    turn = prepare_after_sales_turn(config, platform='weixin', message=question, history=[], session_db=db)
    assert turn.verified_fact_hit is False
    assert activate_validated_fact(db, turn, question=question,
                                   validation={'ok':True,'adopted_claims':[_validated_claim()]}) is False
    if missing == 'validator':
        assert turn.validate('200 μL。')['reasons'] == ['formal_validator_unavailable']


def test_selected_scalar_requires_its_source_but_legacy_dynamic_formal_reads_still_work():
    from gateway.after_sales_guard import _trusted_tool_numeric_evidence_details
    turn = prepare_after_sales_turn(_config_with_fast_response(), platform='weixin', message=SCALAR_QUESTION, history=[])
    other = str(KB / 'knowledge-base/reference/nifty-troubleshooting-tree.md')
    messages = [{'role':'assistant','tool_calls':[{'id':'other-source','function':{
        'name':'read_file','arguments':json.dumps({'path':other})}}]},
        {'role':'tool','tool_call_id':'other-source','content':'fixture:200μL'}]
    assert turn.validate('200 μL。', messages=messages)['reasons'] == ['formal_source_not_read']
    claims, read, _ = _trusted_tool_numeric_evidence_details(messages, [], turn.validator)
    assert read is True
    assert claims == ('200μL',)


@pytest.mark.parametrize("fault", ["key_only", "missing_variant"])
def test_incomplete_turn_contract_cannot_activate_even_with_manual_ok(tmp_path, fault):
    db, config = _verified_fact_fixture(tmp_path, mode='off')
    turn = prepare_after_sales_turn(config, platform='weixin', message=SCALAR_QUESTION, history=[], session_db=db)
    # Component boundary: a truncated internal contract is not a new authority.
    contract = {'fact_key': _parameter_card()['fact_key']} if fault == 'key_only' else dict(turn.parameter_contract)
    if fault == 'missing_variant': contract.pop('product_variant')
    incomplete = replace(turn, parameter_contract=contract)
    assert activate_validated_fact(db, incomplete, question=SCALAR_QUESTION,
                                   validation={'ok':True,'adopted_claims':[_validated_claim()]}) is False


@pytest.mark.parametrize('mode,adopted', [('off',False),('shadow',False),('active',True)])
def test_delivery_reports_cache_adoption_separately_from_formal_card(tmp_path, mode, adopted):
    db, config = _verified_fact_fixture(tmp_path, mode=mode)
    turn = prepare_after_sales_turn(config, platform='weixin', message=SCALAR_QUESTION, history=[], session_db=db)
    result = build_direct_fact_result(turn, SCALAR_QUESTION)
    assert result['verified_fact_reused'] is adopted
    assert turn.resolved_answer.source_kind == ('verified_fact' if adopted else 'formal_answer_card')


@pytest.mark.parametrize("fault", ["none", "missing_blob", "promisor_config", "promisor_pack", "wrong_root", "network_protocol", "linked_worktree", "missing_tree", "partial_config", "alternates", "remote_helper"])
def test_local_only_git_checks_real_objects_and_protocol(tmp_path, fault):
    root = tmp_path / "source"
    root.mkdir()
    def setup_git(*args):
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE,
            timeout=5,
        ).strip()
    setup_git("init", "-q")
    (root / "input").write_text("local fixture")
    setup_git("add", "input")
    setup_git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
              "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    if fault == "none":
        clone = tmp_path / "independent"
        _local_git(tmp_path, "clone", "--no-local", "--no-hardlinks", root.as_uri(), str(clone))
        _verify_local_git_tree(clone)
        assert _local_git(clone, "rev-parse", "HEAD") == setup_git("rev-parse", "HEAD")
        assert not (root / ".git/worktrees").exists()
    elif fault in {"network_protocol", "remote_helper"}:
        url = "https://127.0.0.1:1/forbidden" if fault == "network_protocol" else "ext::false"
        with pytest.raises(subprocess.CalledProcessError) as caught:
            _local_git(root, "ls-remote", url)
        assert "not allowed" in caught.value.stderr
    elif fault == "linked_worktree":
        linked = tmp_path / "staged-worktree"
        setup_git("worktree", "add", "--detach", str(linked), "HEAD")
        _verify_local_git_tree(linked)
        assert _local_git(linked, "rev-parse", "HEAD") == setup_git("rev-parse", "HEAD")
    else:
        if fault == "missing_blob":
            oid = setup_git("rev-parse", "HEAD:input")
            (root / ".git/objects" / oid[:2] / oid[2:]).unlink()
        elif fault == "missing_tree":
            oid = setup_git("rev-parse", "HEAD^{tree}")
            (root / ".git/objects" / oid[:2] / oid[2:]).unlink()
        elif fault == "partial_config":
            setup_git("config", "extensions.partialClone", "origin")
        elif fault == "alternates":
            (root / ".git/objects/info/alternates").write_text(str(tmp_path / "outside"))
        elif fault == "promisor_config":
            setup_git("config", "remote.origin.promisor", "true")
        elif fault == "promisor_pack":
            (root / ".git/objects/pack/fixture.promisor").write_text("")
        else:
            root = tmp_path / "not-repository"
            root.mkdir()
        with pytest.raises((ValueError, subprocess.SubprocessError), match="local-only|missing|repository"):
            _verify_local_git_tree(root)


def test_serving_artifact_startup_imports_are_closed(tmp_path):
    """The existing formal Agent gate must exercise the shipped startup artifact."""
    from tests.test_ivd_serving_agent_cold_import import (
        test_serving_artifact_cold_start_import_is_closed as check_artifact,
    )

    check_artifact(tmp_path, 'gateway.run')
