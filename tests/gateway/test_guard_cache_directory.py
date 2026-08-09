"""Tests for the gateway adapter guard cache directory."""

from types import SimpleNamespace

from tests.gateway import conftest as gateway_conftest


GUARD_CACHE_ENV = "HERMES_GATEWAY_GUARD_CACHE_DIR"
FINGERPRINT = "focused-test"


def _configure_clean_guard(monkeypatch) -> None:
    monkeypatch.setattr(
        gateway_conftest,
        "_fingerprint_gateway_tests",
        lambda: FINGERPRINT,
    )
    monkeypatch.setattr(
        gateway_conftest,
        "_run_adapter_antipattern_scan",
        lambda: [],
    )
    gateway_conftest.pytest_configure(SimpleNamespace())


def test_guard_cache_directory_can_be_overridden(monkeypatch, tmp_path) -> None:
    release_dir = tmp_path / "release"
    cache_dir = tmp_path / "guard-cache"
    release_dir.mkdir()
    monkeypatch.chdir(release_dir)
    monkeypatch.setenv(GUARD_CACHE_ENV, str(cache_dir))

    _configure_clean_guard(monkeypatch)

    assert (cache_dir / f"gw-adapter-guard-{FINGERPRINT}").read_text() == "clean"
    assert not (release_dir / ".pytest-cache").exists()


def test_guard_cache_directory_defaults_to_cwd(monkeypatch, tmp_path) -> None:
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    monkeypatch.chdir(release_dir)
    monkeypatch.delenv(GUARD_CACHE_ENV, raising=False)

    _configure_clean_guard(monkeypatch)

    cache_file = release_dir / ".pytest-cache" / f"gw-adapter-guard-{FINGERPRINT}"
    assert cache_file.read_text() == "clean"
