import hashlib
import json
import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.ivd_execution_contract import IVDRuntimeConfigurationError
import gateway.run as gateway_run
from gateway.config import Platform
from gateway.config import GatewayConfig
from gateway.ivd_runtime import preload_enabled_ivd_contracts
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tests.gateway.restart_test_helpers import make_restart_runner
from tests.gateway.ivd_manifest_test_helpers import release_manifest


def _runner(runtime_config=None):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._ivd_prepared_contracts = preload_enabled_ivd_contracts(
        runtime_config or {}
    )
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
    path = tmp_path / "release.json"
    path.write_text(
        json.dumps(release_manifest(serving, package_digest="a" * 64)),
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
    runner._ivd_prepared_contracts = {}

    prepared, turn = runner._prepare_ivd_lifecycle(
        {"after_sales_guard": {"enabled": True, "platforms": ["qqbot"]}},
        platform="cli",
        message="hello",
        history=[],
    )

    assert prepared is None
    assert turn is None


def test_runner_lifecycle_uses_only_explicitly_preloaded_contract(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    prepared = object()
    runner._ivd_prepared_contracts = {"qqbot": prepared}
    calls = []
    monkeypatch.setattr(
        "gateway.after_sales_guard.prepare_after_sales_turn",
        lambda *args, **kwargs: calls.append(kwargs["prepared_ivd_turn"]),
    )
    monkeypatch.setattr(
        "gateway.ivd_runtime.load_serving_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("loaded")),
    )

    selected, _turn = runner._prepare_ivd_lifecycle(
        {"after_sales_guard": {"enabled": True, "platforms": ["qqbot"]}},
        platform="qqbot",
        message="问题",
        history=[],
    )

    assert selected is prepared
    assert calls == [prepared]


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

    adapter_starts = []
    monkeypatch.setattr(
        GatewayRunner,
        "_create_adapter",
        lambda *_args, **_kwargs: adapter_starts.append(1),
    )

    with pytest.raises(IVDRuntimeConfigurationError):
        GatewayRunner(GatewayConfig())

    assert adapter_starts == []


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

    result = await _runner(config)._run_agent(
        "问题", "", [], _source(), "session", session_key="qqbot:123",
        run_generation=41,
    )

    assert result["final_response"] == "最终答案"
    assert len(validations) == 1
    assert len(receipts) == 1
    assert receipts[0]["validation_status"] == "pass"
    assert receipts[0]["event_id"] == "session:41"


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

    result = await _runner(config)._run_agent(
        "候选结论可以回复吗", "", [], _source(), "session",
        session_key="qqbot:123", event_message_id="message-9",
    )

    assert result["api_calls"] == 0
    assert result["preflight_blocked"] is True
    assert "待验证或非正式来源" in result["final_response"]
    assert len(receipts) == 1
    assert receipts[0]["validation_status"] == "preflight_blocked"
    assert receipts[0]["event_id"] == "message-9"


@pytest.mark.asyncio
async def test_preflight_blocked_receipt_uses_run_unique_event_fallback(
    monkeypatch, tmp_path
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
    )
    monkeypatch.setattr(
        "gateway.after_sales_guard.prepare_after_sales_turn",
        lambda *args, **kwargs: turn,
    )
    monkeypatch.setattr(
        "gateway.ivd_runtime.enqueue_ivd_receipt",
        lambda destination, receipt: receipts.append(receipt) or True,
    )

    result = await _runner(config)._run_agent(
        "问题", "", [], _source(), "session", session_key="qqbot:123",
        run_generation=42,
    )

    assert result["preflight_blocked"] is True
    assert len(receipts) == 1
    assert receipts[0]["event_id"] == "session:42"


@pytest.mark.asyncio
async def test_valid_init_reads_once_and_two_real_turns_reuse_contract_without_io(
    monkeypatch, tmp_path
):
    projection_path = _write_projection(tmp_path)
    config = {
        "after_sales_guard": {
            "enabled": True,
            "platforms": ["qqbot"],
            "serving_projection_path": str(projection_path),
        }
    }
    _install_runtime(monkeypatch, config, object)
    real_read_text = gateway_run.Path.read_text
    reads = []

    def counted_read(path, *args, **kwargs):
        if path == projection_path:
            reads.append(path)
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(gateway_run.Path, "read_text", counted_read)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    assert len(reads) == 1

    identities = []
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
    )

    def prepare_turn(*_args, prepared_ivd_turn=None, **_kwargs):
        identities.append(prepared_ivd_turn)
        return turn

    monkeypatch.setattr(
        "gateway.after_sales_guard.prepare_after_sales_turn", prepare_turn
    )
    real_stat = gateway_run.Path.stat

    def reject_projection_stat(path, *args, **kwargs):
        if path == projection_path:
            raise AssertionError("projection stat")
        return real_stat(path, *args, **kwargs)

    def reject_projection_read(path, *args, **kwargs):
        if path == projection_path:
            raise AssertionError("projection read")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr("pathlib.Path.stat", reject_projection_stat)
    monkeypatch.setattr("pathlib.Path.read_text", reject_projection_read)

    for event_id in ("message-1", "message-2"):
        result = await runner._run_agent(
            "问题", "", [], _source(), "session",
            session_key="qqbot:123", event_message_id=event_id,
        )
        assert result["preflight_blocked"] is True

    assert len(identities) == 2
    assert identities[0] is identities[1]


@pytest.mark.parametrize(
    "runtime_config",
    [{}, {"after_sales_guard": {"enabled": False, "platforms": ["qqbot"]}}],
)
def test_runner_init_skips_projection_when_ivd_disabled(
    monkeypatch, tmp_path, runtime_config
):
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: runtime_config)
    monkeypatch.setattr(
        "gateway.ivd_runtime.load_serving_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("loaded")),
    )

    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))

    assert not runner._ivd_prepared_contracts


@pytest.mark.asyncio
async def test_gateway_stop_closes_unique_preloaded_contract_once():
    closes = []
    prepared = SimpleNamespace(close=lambda: closes.append(1))
    runner, _adapter = make_restart_runner()
    runner._ivd_prepared_contracts = {
        "qqbot": prepared,
        "telegram": prepared,
    }

    with (
        patch("gateway.status.remove_pid_file"),
        patch("gateway.status.write_runtime_status"),
        patch("agent.auxiliary_client.shutdown_cached_clients"),
    ):
        await runner.stop()
        await runner.stop()

    assert closes == [1]
    assert not runner._ivd_prepared_contracts
