from threading import Event, Thread
from unittest.mock import patch

from gateway.open_evidence import build_candidate_context


def test_open_evidence_skips_concurrent_request_and_releases_slot():
    started, release = Event(), Event()
    config = {
        "enabled": True,
        "root": "/tmp/test",
        "originals": "/tmp/test",
        "allowed_source_sha256": ["0" * 64],
    }

    def slow(*args, **kwargs):
        started.set()
        release.wait(5)
        return []

    with patch("gateway.open_evidence.retrieve", side_effect=slow) as retrieve:
        worker = Thread(target=build_candidate_context, args=("first", config))
        worker.start()
        try:
            assert started.wait(5)
            assert build_candidate_context("second", config) == ""
            assert retrieve.call_count == 1
        finally:
            release.set()
            worker.join(5)
        assert not worker.is_alive()
        assert build_candidate_context("third", config) == ""
        assert retrieve.call_count == 2


def test_open_evidence_disabled_or_without_approval_does_not_search():
    config = {"enabled": False, "root": "/tmp/test", "originals": "/tmp/test"}
    with patch("gateway.open_evidence.retrieve") as retrieve:
        assert build_candidate_context("question", config) == ""
        assert build_candidate_context("question", dict(config, enabled=True)) == ""
        retrieve.assert_not_called()
