import hashlib
import json
import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.ivd_execution_contract import IVDRuntimeConfigurationError
import gateway.run as gateway_run
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _runner():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._service_tier = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None)
    runner.session_store = SimpleNamespace()
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED")
    return runner


def _source(platform=Platform.QQBOT):
    return SessionSource(
        platform=platform, chat_id="123", chat_type="dm", user_id="user-1"
    )


def _install_runtime(monkeypatch, config, agent_cls):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: config)
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "model")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"provider": "test", "api_key": "fake"},
    )
    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda *args: {"core"})


def _write_projection(tmp_path):
    serving = {
        "serving_package_path": str(tmp_path / "serving-package"),
        "serving_agent_path": str(tmp_path / "serving-agent"),
        "source_vault_path": str(tmp_path / "source-vault"),
        "dispatch_policy_path": str(tmp_path / "serving-package/dispatch.json"),
        "render_policy_path": str(tmp_path / "serving-package/render.json"),
        "context_budget": 8,
        "retrieval_budget": 2,
        "skill_allowlist": [],
        "receipt_destination": str(tmp_path / "observability/receipt.jsonl"),
    }
    digest = hashlib.sha256(
        json.dumps(serving, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path = tmp_path / "release.json"
    path.write_text(
        json.dumps(
            {
                "shared_identity": {
                    "package_digest": "a" * 64,
                    "execution_contract_schema_version": "1",
                    "turn_receipt_schema_version": "1",
                },
                "projections": {"serving": serving},
                "projection_digests": {"serving": digest},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_runner_lifecycle_uses_validation_state_when_telemetry_fails(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    validator = SimpleNamespace(validation_status="pass")
    agent = SimpleNamespace(_final_response_validator=validator)
    turn = SimpleNamespace(has_validator=True)
    contract = SimpleNamespace(
        contract_id="ivd-contract-1",
        package_digest="a" * 64,
        serving_projection_digest="b" * 64,
        receipt_destination="/tmp/receipt",
    )
    prepared = SimpleNamespace(execution_contract=contract)
    receipts = []
    monkeypatch.setattr(
        "gateway.ivd_runtime.enqueue_ivd_receipt",
        lambda destination, receipt: receipts.append((destination, receipt)) or True,
    )

    answer = runner._finalize_ivd_lifecycle(
        final_response="最终答案",
        prepared=prepared,
        turn=turn,
        agent=agent,
        platform="qqbot",
        session_key="session",
        event_id="event",
        telemetry_action=lambda _status: (_ for _ in ()).throw(OSError("telemetry")),
    )

    assert answer == "最终答案"
    assert len(receipts) == 1
    assert receipts[0][1]["validation_status"] == "pass"


def test_runner_boundary_bypasses_non_managed_platform():
    runner = GatewayRunner.__new__(GatewayRunner)

    prepared, turn = runner._prepare_ivd_lifecycle(
        {"after_sales_guard": {"enabled": True, "platforms": ["qqbot"]}},
        platform="cli",
        message="hello",
        history=[],
    )

    assert prepared is None
    assert turn is None


def test_runner_managed_missing_projection_blocks_before_after_sales_and_agent(
    monkeypatch,
):
    runner = GatewayRunner.__new__(GatewayRunner)
    calls = []
    monkeypatch.setattr(
        "gateway.after_sales_guard.prepare_after_sales_turn",
        lambda *args, **kwargs: calls.append("after_sales"),
    )

    with pytest.raises(IVDRuntimeConfigurationError):
        runner._prepare_ivd_lifecycle(
            {"after_sales_guard": {"enabled": True, "platforms": ["qqbot"]}},
            platform="qqbot",
            message="问题",
            history=[],
        )

    assert calls == []


def test_runner_valid_turn_validator_and_receipt_each_run_once(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    validations = []
    receipts = []

    class Validator:
        validation_status = "not_applicable"

        def __call__(self, answer):
            validations.append(answer)
            self.validation_status = "pass"
            return {"ok": True}

    validator = Validator()
    validator("最终答案")
    agent = SimpleNamespace(_final_response_validator=validator)
    turn = SimpleNamespace(has_validator=True)
    prepared = SimpleNamespace(
        execution_contract=SimpleNamespace(
            contract_id="ivd-contract-1",
            package_digest="a" * 64,
            serving_projection_digest="b" * 64,
            receipt_destination="/tmp/receipt",
        )
    )
    monkeypatch.setattr(
        "gateway.ivd_runtime.enqueue_ivd_receipt",
        lambda destination, receipt: receipts.append(receipt) or True,
    )

    result = runner._finalize_ivd_lifecycle(
        final_response="最终答案",
        prepared=prepared,
        turn=turn,
        agent=agent,
        platform="qqbot",
        session_key="session",
        event_id="event",
    )

    assert result == "最终答案"
    assert validations == ["最终答案"]
    assert len(receipts) == 1
    assert receipts[0]["validation_status"] == "pass"


@pytest.mark.asyncio
async def test_real_run_sync_blocks_missing_projection_before_agent(monkeypatch):
    class Agent:
        def __init__(self, **kwargs):
            raise AssertionError("agent constructed")

    config = {"after_sales_guard": {"enabled": True, "platforms": ["qqbot"]}}
    _install_runtime(monkeypatch, config, Agent)

    with pytest.raises(IVDRuntimeConfigurationError):
        await _runner()._run_agent(
            "问题", "", [], _source(), "session", session_key="qqbot:123"
        )


@pytest.mark.asyncio
async def test_real_run_sync_telemetry_failure_keeps_pass_receipt_and_answer(
    monkeypatch, tmp_path
):
    validations = []
    receipts = []

    class Agent:
        def __init__(self, **kwargs):
            self.tools = []
            self._session_messages = []

        def run_conversation(self, message, **kwargs):
            result = self._final_response_validator("最终答案")
            validations.append(result)
            return {"final_response": "最终答案", "messages": [], "api_calls": 1}

    config = {
        "after_sales_guard": {
            "enabled": True,
            "platforms": ["qqbot"],
            "serving_projection_path": str(_write_projection(tmp_path)),
        }
    }
    _install_runtime(monkeypatch, config, Agent)
    turn = SimpleNamespace(
        context="",
        facts={"workflow_id": "test", "current_stage": "stage"},
        blocks_answer_generation=False,
        has_validator=True,
        product_scope="",
        product_variant="",
        route_id="test",
        route_version="1",
        fast_path=False,
        source_paths=(),
        preflight_decision="",
        preflight_action="",
        preflight_issues=(),
        validate=lambda answer, messages=None: {"ok": True, "reasons": [], "fallback": ""},
    )
    monkeypatch.setattr(
        "gateway.after_sales_guard.prepare_after_sales_turn",
        lambda *args, **kwargs: turn,
    )
    monkeypatch.setattr(
        "gateway.after_sales_telemetry.append_runtime_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("telemetry")),
    )
    monkeypatch.setattr(
        "gateway.ivd_runtime.enqueue_ivd_receipt",
        lambda destination, receipt: receipts.append(receipt) or True,
    )

    result = await _runner()._run_agent(
        "问题", "", [], _source(), "session", session_key="qqbot:123"
    )

    assert result["final_response"] == "最终答案"
    assert len(validations) == 1
    assert len(receipts) == 1
    assert receipts[0]["validation_status"] == "pass"


@pytest.mark.parametrize("enqueue_result", [True, False])
@pytest.mark.asyncio
async def test_real_run_sync_records_preflight_blocked_receipt_before_early_return(
    monkeypatch, tmp_path, enqueue_result
):
    receipts = []

    class Agent:
        def __init__(self, **kwargs):
            raise AssertionError("agent constructed")

    config = {
        "after_sales_guard": {
            "enabled": True,
            "platforms": ["qqbot"],
            "serving_projection_path": str(_write_projection(tmp_path)),
        }
    }
    _install_runtime(monkeypatch, config, Agent)
    turn = SimpleNamespace(
        context="",
        facts={},
        blocks_answer_generation=True,
        has_validator=True,
        product_scope="",
        product_variant="",
        route_id="blocked",
        route_version="1",
        fast_path=False,
        source_paths=(),
        preflight_decision="block",
        preflight_action="stop_before_answer_generation",
        preflight_issues=("pending_source",),
        validate=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("validator called")
        ),
    )
    monkeypatch.setattr(
        "gateway.after_sales_guard.prepare_after_sales_turn",
        lambda *args, **kwargs: turn,
    )
    monkeypatch.setattr(
        "gateway.ivd_runtime.enqueue_ivd_receipt",
        lambda destination, receipt: receipts.append(receipt) or enqueue_result,
    )

    result = await _runner()._run_agent(
        "候选结论可以回复吗", "", [], _source(), "session",
        session_key="qqbot:123", event_message_id="message-9",
    )

    assert result["api_calls"] == 0
    assert result["preflight_blocked"] is True
    assert "待验证或非正式来源" in result["final_response"]
    assert len(receipts) == 1
    assert receipts[0]["validation_status"] == "preflight_blocked"
    assert receipts[0]["event_id"] == "message-9"
