from types import SimpleNamespace

from agent.conversation_loop import enforce_ivd_serving_tool_boundary, run_conversation
from agent.tool_guardrails import filter_tools_for_execution_mode


def _tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def test_public_ivd_turn_has_no_broad_tools():
    definitions = [_tool("search_files"), _tool("terminal"), _tool("skill"), _tool("read_file")]

    filtered = filter_tools_for_execution_mode(definitions, mode="ivd_serving")

    assert filtered == []


def test_conversation_loop_defense_clears_stale_tool_name_cache():
    agent = SimpleNamespace(
        tools=[_tool("search_files"), _tool("terminal")],
        valid_tool_names={"search_files", "terminal"},
    )

    enforce_ivd_serving_tool_boundary(agent, mode="ivd_serving")

    assert agent.tools == []
    assert agent.valid_tool_names == set()


def test_non_ivd_modes_preserve_tools():
    definitions = [_tool("terminal")]

    assert filter_tools_for_execution_mode(definitions, mode="standard") == definitions


def test_conversation_entry_enforces_ivd_boundary_before_turn_setup(monkeypatch):
    agent = SimpleNamespace(
        execution_mode="ivd_serving",
        tools=[_tool("search_files"), _tool("terminal")],
        valid_tool_names={"search_files", "terminal"},
    )
    observed = []

    def stop_after_observation(*_args, **_kwargs):
        observed.append((agent.tools, agent.valid_tool_names))
        raise RuntimeError("stop after boundary")

    monkeypatch.setattr("agent.conversation_loop.build_turn_context", stop_after_observation)

    try:
        run_conversation(agent, "问题")
    except RuntimeError as error:
        assert str(error) == "stop after boundary"
    else:
        raise AssertionError("conversation continued past test boundary")

    assert observed == [([], set())]
