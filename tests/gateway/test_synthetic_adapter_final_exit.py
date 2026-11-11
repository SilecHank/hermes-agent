"""Synthetic-only adapter-to-visible-exit acceptance for the IVD candidate.

The OpenAI-compatible SDK and Hermes conversation loop are real. A local
httpx MockTransport is the only model endpoint; no provider key or network is
used. KB source text and user turns are generated inside each test.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path

import httpx
import pytest

from agent.final_response_validation import evaluate_final_response
from gateway.after_sales_guard import AfterSalesTurn, CriticalAfterSalesValidator, _load_module
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.session import SessionSource, build_session_key
from run_agent import AIAgent


KB = Path(os.environ.get("IVD_KB_ROOT") or "/nonexistent-ivd-kb")
pytestmark = pytest.mark.skipif(
    not os.environ.get("IVD_KB_ROOT"),
    reason="requires an explicitly bound private KB; covered by private acceptance",
)


def _modules():
    pipeline = KB / "scripts/hermes_fast_response_pipeline.py"
    _load_module(str(pipeline), pipeline.stat().st_mtime_ns)
    return (
        importlib.import_module("scripts.hermes_sop_scope_lock"),
        importlib.import_module("scripts.hermes_expert_evidence_projection"),
        importlib.import_module("scripts.hermes_expert_route_contract"),
    )


def _synthetic_package(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "serving-package"
    config = root / "knowledge-base/config"
    docs = root / "knowledge-base/01_标准作业指导书_SOP/SyntheticKit"
    config.mkdir(parents=True)
    docs.mkdir(parents=True)
    # Copy the unchanged validator so its source root is the same synthetic
    # package whose evidence and reviewed lock are exercised by the test.
    import shutil
    scripts = root / "scripts"
    scripts.mkdir()
    shutil.copyfile(KB / "scripts/hermes_expert_answer_contract.py", scripts / "hermes_expert_answer_contract.py")
    actions = root / "knowledge-base/workflows/decision-actions.json"
    actions.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(KB / "knowledge-base/workflows/decision-actions.json", actions)
    for name in ("ivd-expert-capability-registry.json", "ivd-diagnostic-graph.json"):
        (config / name).write_bytes((KB / "knowledge-base/config" / name).read_bytes())

    paths = {
        "a5": "knowledge-base/01_标准作业指导书_SOP/SyntheticKit/SOP-SYN-100_A5.md",
        "a0": "knowledge-base/01_标准作业指导书_SOP/SyntheticKit/SOP-SYN-100_A0.md",
        "variant": "knowledge-base/01_标准作业指导书_SOP/SyntheticKit/SOP-SYN-100_variant_A5.md",
    }
    bodies = {
        "a5": (
            "---\nstatus: active\n---\n# SyntheticKit SOP\n"
            "文件编号：SOP-SYN-100 版本号：A5\n"
            "适用范围：SyntheticKit 合成手工提取流程。\n"
            "## 合成观察\n合成磁珠可能吸附目标分子。\n"
        ),
        "a0": (
            "---\nstatus: active\n---\n# SyntheticKit SOP\n"
            "文件编号：SOP-SYN-100 版本号：A0\n"
            "适用范围：SyntheticKit 合成手工提取流程。\n"
            "## 合成参数\n合成起始液体体积为3合成单位。\n"
        ),
        "variant": (
            "---\nstatus: active\n---\n# SyntheticKit experiment\n"
            "文件编号：SOP-SYN-100 版本号：A5\n"
            "适用范围：SyntheticKit 合成手工提取流程。\n"
            "## 合成参数\n合成起始液体体积为9合成单位。\n"
        ),
    }
    for key, relative in paths.items():
        (root / relative).write_text(bodies[key], encoding="utf-8")

    revisions = {"a0": "A0", "a5": "A5"}
    members = [
        {"source_path": paths[key], "sha256": hashlib.sha256(bodies[key].encode()).hexdigest(),
         "revision": revisions[key]}
        for key in ("a0", "a5")
    ]
    lock = {
        "schema_version": 1,
        "purpose": "restrict_candidates_not_authorize_claims",
        "groups": [{
            "group_id": "synthetic-sop-lock", "document_id": "SOP-SYN-100",
            "product": "SyntheticKit", "scope": "synthetic_manual_extraction",
            "reviewed": True, "members": members,
        }],
    }
    (config / "reviewed-sop-scope-locks.json").write_text(
        json.dumps(lock), encoding="utf-8"
    )
    return root, paths


def _projection(root: Path, relative: str, question: str):
    _, projection, _ = _modules()
    card = {
        "fact_key": "synthetic-card", "source_path": relative,
        "effective_status": "active", "authority_level": "formal_authority",
        "source_version": "A5", "product_scope": "SyntheticKit",
        "answer_text": "合成磁珠可能吸附目标分子。",
    }
    return projection.project_expert_evidence(
        root=root, formal_cards=[card], question=question
    )


def _turn(root: Path, projection: dict, *, task_id: str, question: str,
          question_type: str = "why_or_mechanism"):
    _, _, route_module = _modules()
    bundle = projection.get("bundle") or projection.get("reference_bundle") or {}
    decision = {
        "action": "expert_fallback", "evidence_tier": projection["tier"],
        "product_line": "SyntheticKit", "version": "A5",
        "question_type": question_type,
    }
    registry = json.loads((root / "knowledge-base/config/ivd-expert-capability-registry.json").read_text())
    graph = json.loads((root / "knowledge-base/config/ivd-diagnostic-graph.json").read_text())
    contract = route_module.build_expert_route_contract(
        decision, evidence_bundle=bundle or None, registry=registry, graph=graph
    )
    task = {
        "task_id": task_id, "subject": "SyntheticKit", "context": {"product_version": "A5"},
        "question": question, "intent": "mechanism",
    }
    turn = AfterSalesTurn(
        context="", facts={}, validator=None, allowed_numeric_claims=(),
        task_contract=task, expert_route_contract=contract,
        expert_evidence_bundle=bundle,
        expert_validator_path=str(root / "scripts/hermes_expert_answer_contract.py"),
    )
    return turn


def _draft(task_id: str, evidence_id: str, quote: str, *, text: str, kind: str = "hypothesis",
           start_line: int = 1) -> str:
    return json.dumps({
        "task_id": task_id,
        "claims": [{
            "kind": kind, "scope": "general", "text": text,
            "verification": "核对合成实验记录。",
            "boundary": "这是合成演示内容，不构成产品或个案结论。",
            "evidence_ids": [evidence_id],
            "support": [{
                "evidence_id": evidence_id, "quote": quote, "start_line": start_line,
                "relation": "只用于说明合成机制假设。",
            }],
        }],
    }, ensure_ascii=False)


def _sse(content: str) -> bytes:
    frames = [
        {"id": "chatcmpl-synthetic", "object": "chat.completion.chunk", "created": 1,
         "model": "synthetic-model", "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"id": "chatcmpl-synthetic", "object": "chat.completion.chunk", "created": 1,
         "model": "synthetic-model", "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]},
        {"id": "chatcmpl-synthetic", "object": "chat.completion.chunk", "created": 1,
         "model": "synthetic-model", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    return ("".join("data: " + json.dumps(frame, separators=(",", ":")) + "\n\n" for frame in frames)
            + "data: [DONE]\n\n").encode()


def _agent(outputs: list[str], captured: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "http" and request.url.host == "mock.invalid"
        payload = json.loads(request.content)
        captured.append(payload)
        assert payload["model"] == "synthetic-model"
        assert outputs, "unexpected extra request or fallback provider call"
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=_sse(outputs.pop(0)),
        )

    agent = AIAgent(
        base_url="http://mock.invalid/v1", api_key="synthetic-test-key",
        provider="openai", model="synthetic-model", max_iterations=1,
        enabled_toolsets=[], quiet_mode=True, skip_context_files=True,
        skip_memory=True, load_soul_identity=False, fallback_model=None,
    )
    agent._build_keepalive_http_client = lambda base_url="", *, verify=True: httpx.Client(
        transport=httpx.MockTransport(handler)
    )
    return agent


def test_synthetic_adapter_through_finalizer_return_is_lock_bound(tmp_path, monkeypatch):
    root, paths = _synthetic_package(tmp_path)
    lock_module, _, _ = _modules()
    assert lock_module.reviewed_source_decision(root, paths["a5"], product="SyntheticKit")["status"] == "allowed"
    assert lock_module.reviewed_source_decision(root, paths["a0"], product="SyntheticKit")["status"] == "stale"
    assert lock_module.reviewed_source_decision(root, paths["variant"], product="SyntheticKit")["status"] == "unreviewed_member"
    assert lock_module.reviewed_source_decision(root, paths["a5"], product="")["status"] == "product_required"
    assert lock_module.reviewed_source_decision(root, paths["a5"], product="OtherSyntheticKit")["status"] == "product_mismatch"

    # SOP documents that have no reviewed identity/scope projection remain
    # unknown and cannot be promoted into expert serving evidence.
    unknown_path = "knowledge-base/01_标准作业指导书_SOP/SyntheticKit/unreviewed.md"
    (root / unknown_path).write_text(
        "---\nstatus: active\n---\n# 未登记的合成流程\n合成演示文字。\n", encoding="utf-8"
    )
    _, projection_module, _ = _modules()
    assert projection_module._source_record(
        root, evidence_id="unknown-sop", tier="formal_authority",
        source_ref=unknown_path, product_line="SyntheticKit", version="A5",
    ) is None

    tampered_root, tampered_paths = _synthetic_package(tmp_path / "tampered-package")
    tampered_path = tampered_root / tampered_paths["a5"]
    tampered_path.write_text(
        tampered_path.read_text(encoding="utf-8").replace("吸附目标分子", "描述已更改"),
        encoding="utf-8",
    )
    assert lock_module.reviewed_source_decision(
        tampered_root, tampered_paths["a5"], product="SyntheticKit"
    )["status"] == "invalid"

    active_question = "SYNTHETIC_EXPLICIT_METHOD_QUERY"
    active_projection = _projection(root, paths["a5"], active_question)
    assert active_projection["tier"] == "formal_authority"
    record = active_projection["bundle"]["evidence"][0]
    quote = record["excerpts"][0]["text"]
    valid_draft = _draft(
        "synthetic-confirmed-task", record["evidence_id"], quote,
        text="合成磁珠可能吸附目标分子。", start_line=record["excerpts"][0]["start_line"],
    )

    stale_projection = _projection(root, paths["a0"], "SYNTHETIC_STALE_A0_QUERY")
    variant_projection = _projection(root, paths["variant"], "SYNTHETIC_UNREGISTERED_A5_QUERY")
    tampered_projection = _projection(
        tampered_root, tampered_paths["a5"], "SYNTHETIC_HASH_TAMPER_QUERY"
    )
    assert stale_projection["tier"] == "no_evidence"
    assert variant_projection["tier"] == "no_evidence"
    assert tampered_projection["tier"] == "no_evidence"
    ambiguous_draft = _draft(
        "synthetic-ambiguous-task", "unadmitted:synthetic", "合成参数待确认。",
        text="合成起始液体体积为999合成单位。", kind="fact",
    )
    stale_draft = _draft(
        "synthetic-stale-task", "formal_card:synthetic-card",
        "合成起始液体体积为3合成单位。",
        text="合成起始液体体积为999合成单位。", kind="fact",
    )
    variant_draft = _draft(
        "synthetic-variant-task", "formal_card:synthetic-card",
        "合成起始液体体积为9合成单位。",
        text="合成起始液体体积为999合成单位。", kind="fact",
    )
    tampered_draft = _draft(
        "synthetic-tampered-task", "formal_card:synthetic-card",
        "合成磁珠可能吸附目标分子。",
        text="合成起始液体体积为999合成单位。", kind="fact",
    )

    outputs = [
        ambiguous_draft,  # ambiguity: final contract must replace the guessed number
        valid_draft,     # same conversation, after synthetic product/method clarification
        stale_draft,     # stale A0 source must not reach visible output
        variant_draft,   # unregistered A5 variant must not reach visible output
        tampered_draft,  # changed bytes behind a registered path must not reach visible output
    ]
    requests: list[dict] = []
    agent = _agent(outputs, requests)

    ambiguous_question = "SYNTHETIC_AMBIGUOUS_METHOD_QUERY"
    ambiguous_projection = _projection(root, paths["variant"], ambiguous_question)
    ambiguous_turn = _turn(
        root, ambiguous_projection, task_id="synthetic-ambiguous-task",
        question=ambiguous_question, question_type="parameter",
    )
    agent._final_response_validator = CriticalAfterSalesValidator(
        ambiguous_turn, messages_provider=lambda: [],
    )
    first = agent.run_conversation(
        ambiguous_question, system_message="SYNTHETIC_TEST_SYSTEM_ONLY",
        task_id="synthetic-ambiguous-task",
    )
    first_decision = evaluate_final_response(
        CriticalAfterSalesValidator(ambiguous_turn, messages_provider=lambda: []),
        first["final_response"], attempts=1,
    )
    assert "999" not in first_decision.response
    assert first_decision.action in {"accept", "fallback"}

    confirmed_question = "SYNTHETIC_PRODUCT_METHOD_CONFIRMED"
    confirmed_turn = _turn(
        root, active_projection, task_id="synthetic-confirmed-task",
        question=confirmed_question,
    )
    agent._final_response_validator = CriticalAfterSalesValidator(
        confirmed_turn, messages_provider=lambda: [],
    )
    second = agent.run_conversation(
        confirmed_question, system_message="SYNTHETIC_TEST_SYSTEM_ONLY",
        conversation_history=first.get("messages", []),
        task_id="synthetic-confirmed-task",
    )
    assert "合成磁珠可能吸附目标分子" in second["final_response"]
    assert "999" not in second["final_response"]

    for name, projection, source_root in (
        ("stale", stale_projection, root),
        ("variant", variant_projection, root),
        ("tampered", tampered_projection, tampered_root),
    ):
        question = f"SYNTHETIC_{name.upper()}_A5_QUERY"
        turn = _turn(source_root, projection, task_id=f"synthetic-{name}-task", question=question)
        agent._final_response_validator = CriticalAfterSalesValidator(turn, messages_provider=lambda: [])
        result = agent.run_conversation(
            question, system_message="SYNTHETIC_TEST_SYSTEM_ONLY",
            task_id=f"synthetic-{name}-task",
        )
        assert "999" not in result["final_response"]
        assert "来源" in result["final_response"] or "正式" in result["final_response"]

    assert len(requests) == 5
    assert not outputs
    assert all(
        message.get("role") != "user"
        or str(message.get("content") or "").startswith("SYNTHETIC_")
        for payload in requests for message in payload.get("messages", [])
    )
    second_roles = [message["role"] for message in requests[1]["messages"]]
    assert second_roles.count("user") == 2 and second_roles.count("assistant") == 1
    assert all(left != right for left, right in zip(second_roles, second_roles[1:]))
    prior_assistant = next(
        message for message in reversed(requests[1]["messages"])
        if message["role"] == "assistant"
    )
    assert first["final_response"] in prior_assistant["content"]
    assert "999" not in prior_assistant["content"]


@pytest.mark.asyncio
async def test_synthetic_ambiguous_final_text_reaches_gateway_send(tmp_path):
    root, paths = _synthetic_package(tmp_path)
    question = "SYNTHETIC_AMBIGUOUS_METHOD_QUERY"
    projection = _projection(root, paths["variant"], question)
    turn = _turn(root, projection, task_id="synthetic-gateway-task", question=question,
                 question_type="parameter")
    bad_draft = _draft(
        "synthetic-gateway-task", "unadmitted:synthetic", "合成参数待确认。",
        text="合成起始液体体积为999合成单位。", kind="fact",
    )
    requests: list[dict] = []
    agent = _agent([bad_draft], requests)
    agent._final_response_validator = CriticalAfterSalesValidator(
        turn, messages_provider=lambda: [],
    )
    agent_response: dict[str, str] = {}

    async def message_handler(_event):
        result = agent.run_conversation(
            question,
            system_message="SYNTHETIC_GATEWAY_SYSTEM_ONLY",
            task_id="synthetic-gateway-task",
        )
        agent_response["text"] = result["final_response"]
        return result["final_response"]

    class CapturingTelegramAdapter(BasePlatformAdapter):
        def __init__(self):
            super().__init__(
                PlatformConfig(enabled=True, token="synthetic-token", typing_indicator=False),
                Platform.TELEGRAM,
            )
            self.sent: list[dict] = []

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            return True

        async def disconnect(self) -> None:
            return None

        async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
            self.sent.append({"chat_id": chat_id, "content": content, "reply_to": reply_to,
                              "metadata": metadata})
            return SendResult(success=True, message_id="SYNTHETIC_MESSAGE_ID")

        async def send_typing(self, chat_id: str, metadata=None) -> None:
            return None

        async def stop_typing(self, chat_id: str, metadata=None) -> None:
            return None

        async def get_chat_info(self, chat_id: str):
            return {"id": chat_id}

    adapter = CapturingTelegramAdapter()
    adapter.set_message_handler(message_handler)
    event = MessageEvent(
        text=question,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="SYNTHETIC_CHAT",
            chat_type="private",
        ),
        message_id="SYNTHETIC_EVENT_ID",
    )

    await adapter.handle_message(event)
    await adapter._session_tasks[build_session_key(event.source)]

    assert len(requests) == 1
    assert len(adapter.sent) == 1
    delivered = adapter.sent[0]["content"]
    assert delivered == agent_response["text"]
    assert delivered == (
        "本轮尚未取得可用于分析的来源正文，不能据此声称资料库没有相关内容。"
        "结论以当前执行版本的受控文件为准，未核实到的部分我按当前执行版本继续核对，不作操作依据。"
    )
    assert "999" not in delivered
    assert adapter.sent[0]["chat_id"] == "SYNTHETIC_CHAT"
