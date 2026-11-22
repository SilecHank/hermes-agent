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


def test_full_catalog_requires_explicit_scope_and_keeps_original_verification():
    config = {
        "enabled": True,
        "root": "/tmp/test",
        "originals": "/tmp/originals",
        "allowed_source_sha256": ["0" * 64],
        "scope": "full_catalog",
    }
    candidates = [{"path": "document.pdf"}]
    with patch("gateway.open_evidence.retrieve", return_value=candidates) as retrieve, patch(
        "gateway.open_evidence.verify_candidates", return_value=[]
    ) as verify:
        assert build_candidate_context("question", config) == ""
        retrieve.assert_called_once_with("question", "/tmp/test", limit=3, allowed_hashes=None)
        verify.assert_called_once_with(candidates, "/tmp/originals")
        assert build_candidate_context("question", dict(config, scope="unknown")) == ""
        assert retrieve.call_count == 1


def test_expert_turn_records_catalog_context_without_counting_it_as_used():
    from gateway.after_sales_guard import AfterSalesTurn, _attach_verified_fact

    turn = AfterSalesTurn(
        context="existing expert context", facts={}, validator=None,
        allowed_numeric_claims=(), route_id="expert_mode_auto_fallback",
    )
    guard = {"open_document_evidence": {"enabled": True}}
    with patch("gateway.after_sales_guard._attach_verified_fact_base", return_value=turn), patch(
        "gateway.open_evidence.build_candidate_context", side_effect=["catalog excerpt", ""]
    ):
        attached = _attach_verified_fact(turn, guard=guard, question="question", session_db=None)
        unavailable = _attach_verified_fact(turn, guard=guard, question="question", session_db=None)

    assert attached.catalog_context_status == "attached"
    assert "catalog excerpt" in attached.context
    assert unavailable.catalog_context_status == "unavailable"
    assert "继续给出能支持的相关内容或专业分析" in unavailable.context
    assert "catalog excerpt" not in unavailable.context
    assert not attached.source_paths
