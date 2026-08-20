from __future__ import annotations

import inspect

from gateway import run


def test_agent_inner_uses_shared_runtime_path_resolver_for_all_branches():
    source = inspect.getsource(run.GatewayRunner._run_agent_inner)

    assert "_resolve_runtime_event_path(" in source
    assert "default_runtime_event_path" not in source


def test_runtime_path_resolver_preserves_configured_path(monkeypatch, tmp_path):
    configured = tmp_path / "configured.jsonl"
    monkeypatch.setattr(
        "gateway.after_sales_telemetry.default_runtime_event_path",
        lambda: tmp_path / "default.jsonl",
    )

    assert run._resolve_runtime_event_path({"runtime_events_path": str(configured)}) == str(configured)
    assert run._resolve_runtime_event_path({}) == str(tmp_path / "default.jsonl")
