from pathlib import Path

from tests.gateway import conftest as gateway_conftest


def test_gateway_guard_cache_dir_honors_release_override(monkeypatch, tmp_path):
    cache_dir = tmp_path / "gateway-guard"
    monkeypatch.setenv("HERMES_GATEWAY_GUARD_CACHE_DIR", str(cache_dir))

    assert gateway_conftest._gateway_guard_cache_dir() == cache_dir


def test_gateway_guard_cache_dir_defaults_to_worktree(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_GATEWAY_GUARD_CACHE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    assert gateway_conftest._gateway_guard_cache_dir() == Path.cwd() / ".pytest-cache"
