from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from gateway.ivd_runtime import (
    COMPLEX_DIAGNOSIS_POLICY,
    DIRECT_POLICY,
    EVIDENCE_SUPPLEMENT_POLICY,
    INDEX_FALLBACK_POLICY,
    IVDRetrievalPolicy,
    begin_ivd_answer_turn,
    build_ivd_retrieval_context,
    consume_ivd_search,
    end_ivd_answer_turn,
    get_ivd_retrieval_snapshot,
    record_ivd_search_result,
    resolve_enabled_ivd_retrieval,
    resolve_ivd_retrieval_policy,
    prepare_enabled_ivd_turn,
    enqueue_ivd_receipt,
    preload_enabled_ivd_contracts,
)
from gateway.ivd_execution_contract import IVDRuntimeConfigurationError


def test_verified_routed_sources_use_direct_profile(tmp_path):
    source = tmp_path / "IVD-KnowledgeHub" / "knowledge-base" / "reference" / "formal.md"
    source.parent.mkdir(parents=True)
    source.write_text("formal", encoding="utf-8")
    turn = SimpleNamespace(
        fast_path=True,
        source_paths=(str(source),),
        route_id="sop_parameter_short_answer",
        requires_source_validation=True,
    )

    policy = resolve_ivd_retrieval_policy("建库投入量是多少", turn)

    assert policy.profile == "direct"
    assert policy.max_searches == 0


def test_missing_routed_source_does_not_use_direct_profile():
    turn = SimpleNamespace(
        fast_path=True,
        source_paths=("/missing/IVD-KnowledgeHub/knowledge-base/reference/formal.md",),
        route_id="sop_parameter_short_answer",
        requires_source_validation=True,
    )

    policy = resolve_ivd_retrieval_policy("建库投入量是多少", turn)

    assert policy.profile == "index_fallback"


def test_cross_product_conflict_overrides_direct_route():
    turn = SimpleNamespace(
        fast_path=True,
        source_paths=("/kb/formal.md",),
        route_id="sop_parameter_short_answer",
        requires_source_validation=True,
    )

    policy = resolve_ivd_retrieval_policy("NIFTY 和 CNV 同批异常且版本冲突", turn)

    assert policy.profile == "complex_diagnosis"
    assert policy.max_searches == 3


def test_literature_intent_uses_evidence_profile_without_direct_source():
    policy = resolve_ivd_retrieval_policy("这个机制有什么文献依据", None)

    assert policy.profile == "evidence_supplement"
    assert policy.max_searches == 2


def test_unmatched_question_uses_two_index_fallback_searches():
    policy = resolve_ivd_retrieval_policy("这个问题怎么处理", None)

    assert policy.profile == "index_fallback"
    assert policy.max_searches == 2


def test_direct_context_requires_reads_without_searching(tmp_path):
    source = tmp_path / "IVD-KnowledgeHub" / "knowledge-base" / "reference" / "formal.md"
    source.parent.mkdir(parents=True)
    source.write_text("formal", encoding="utf-8")
    turn = SimpleNamespace(
        fast_path=True,
        source_paths=(str(source),),
        route_id="sop_parameter_short_answer",
        requires_source_validation=True,
    )
    policy = resolve_ivd_retrieval_policy("建库投入量是多少", turn)

    context = build_ivd_retrieval_context(policy)

    assert "read_file" in context
    assert "Do not call file search" in context
    assert "Do not disclose" in context


def test_fallback_context_requires_one_batched_alias_search():
    policy = resolve_ivd_retrieval_policy("这个问题怎么处理", None)

    context = build_ivd_retrieval_context(policy)

    assert "one batched" in context
    assert "aliases" in context
    assert "Do not disclose" in context


def test_enabled_after_sales_platform_resolves_retrieval_policy(tmp_path):
    config = {
        "after_sales_guard": {
            "enabled": True,
            "platforms": ["weixin", "wecom"],
        }
    }
    source = tmp_path / "IVD-KnowledgeHub" / "knowledge-base" / "reference" / "formal.md"
    source.parent.mkdir(parents=True)
    source.write_text("formal", encoding="utf-8")
    turn = SimpleNamespace(
        fast_path=True,
        source_paths=(str(source),),
        route_id="sop_parameter_short_answer",
        requires_source_validation=True,
    )

    policy = resolve_enabled_ivd_retrieval(
        config,
        platform="weixin",
        message="建库投入量是多少",
        turn=turn,
    )

    assert policy is not None
    assert policy.profile == "direct"


def test_disabled_after_sales_platform_has_no_retrieval_policy():
    config = {
        "after_sales_guard": {
            "enabled": True,
            "platforms": "weixin,wecom",
        }
    }

    assert resolve_enabled_ivd_retrieval(
        config,
        platform="telegram",
        message="这个问题怎么处理",
        turn=None,
    ) is None


def test_ivd_search_budget_is_bounded_and_resets():
    token = begin_ivd_answer_turn(max_searches=2, mode="answer")
    try:
        assert consume_ivd_search() == (True, 1, 2)
        assert consume_ivd_search() == (True, 2, 2)
        assert consume_ivd_search() == (False, 3, 2)
    finally:
        end_ivd_answer_turn(token)

    assert consume_ivd_search() == (True, 0, 0)


def test_maintenance_mode_does_not_apply_answer_budget():
    token = begin_ivd_answer_turn(max_searches=1, mode="maintenance")
    try:
        assert consume_ivd_search() == (True, 0, 0)
        assert consume_ivd_search() == (True, 0, 0)
    finally:
        end_ivd_answer_turn(token)


def test_direct_profile_blocks_file_search():
    token = begin_ivd_answer_turn(policy=DIRECT_POLICY, mode="answer")
    try:
        assert consume_ivd_search(pattern="NIFTY", path="/kb", target="content") == (
            False,
            1,
            0,
        )
        assert get_ivd_retrieval_snapshot()["stop_reason"] == "direct"
    finally:
        end_ivd_answer_turn(token)


def test_profile_allowances_are_two_two_and_three():
    for policy, expected in (
        (INDEX_FALLBACK_POLICY, 2),
        (EVIDENCE_SUPPLEMENT_POLICY, 2),
        (COMPLEX_DIAGNOSIS_POLICY, 3),
    ):
        token = begin_ivd_answer_turn(policy=policy, mode="answer")
        try:
            for number in range(1, expected + 1):
                assert consume_ivd_search(
                    pattern=f"query-{number}", path="/kb", target="content"
                ) == (True, number, expected)
            assert consume_ivd_search(
                pattern="one-more", path="/kb", target="content"
            ) == (False, expected + 1, expected)
        finally:
            end_ivd_answer_turn(token)


def test_duplicate_signature_stops_repeated_search_without_consuming_stage():
    token = begin_ivd_answer_turn(policy=EVIDENCE_SUPPLEMENT_POLICY, mode="answer")
    try:
        assert consume_ivd_search(pattern=" NIFTY ", path="/kb/./", target="content")[0]
        assert not consume_ivd_search(
            pattern="nifty", path="/kb", target="content"
        )[0]
        snapshot = get_ivd_retrieval_snapshot()
        assert snapshot["searches"] == 1
        assert snapshot["signature_count"] == 1
        assert snapshot["stop_reason"] == "duplicate"
    finally:
        end_ivd_answer_turn(token)


def test_formal_source_gain_stops_ordinary_fallback():
    token = begin_ivd_answer_turn(policy=INDEX_FALLBACK_POLICY, mode="answer")
    try:
        assert consume_ivd_search(pattern="NIFTY", path="/kb", target="content")[0]
        record_ivd_search_result(
            pattern="NIFTY",
            path="/kb",
            target="content",
            result_paths=(
                "/kb/IVD-KnowledgeHub/knowledge-base/reference/nifty.md",
            ),
        )
        snapshot = get_ivd_retrieval_snapshot()
        assert snapshot["formal_source_count"] == 1
        assert snapshot["stop_reason"] == "formal_source_found"
    finally:
        end_ivd_answer_turn(token)


def test_evidence_profile_blocks_synonym_retry_in_same_scope_after_gain():
    token = begin_ivd_answer_turn(policy=EVIDENCE_SUPPLEMENT_POLICY, mode="answer")
    try:
        assert consume_ivd_search(
            pattern="胎儿浓度", path="/kb/knowledge-base", target="content"
        )[0]
        record_ivd_search_result(
            pattern="胎儿浓度",
            path="/kb/knowledge-base",
            target="content",
            result_paths=(
                "/kb/IVD-KnowledgeHub/knowledge-base/reference/nifty.md",
            ),
        )

        assert not consume_ivd_search(
            pattern="fetal fraction", path="/kb/knowledge-base", target="content"
        )[0]
        assert get_ivd_retrieval_snapshot()["stop_reason"] == "duplicate_intent"
    finally:
        end_ivd_answer_turn(token)


def test_evidence_profile_allows_one_rewrite_after_zero_results():
    token = begin_ivd_answer_turn(policy=EVIDENCE_SUPPLEMENT_POLICY, mode="answer")
    try:
        assert consume_ivd_search(
            pattern="胎儿浓度", path="/kb/knowledge-base", target="content"
        )[0]
        record_ivd_search_result(
            pattern="胎儿浓度",
            path="/kb/knowledge-base",
            target="content",
            result_paths=(),
        )

        assert consume_ivd_search(
            pattern="fetal fraction", path="/kb/knowledge-base", target="content"
        )[0]
    finally:
        end_ivd_answer_turn(token)


def test_two_no_gain_searches_stop_complex_profile_early():
    token = begin_ivd_answer_turn(policy=COMPLEX_DIAGNOSIS_POLICY, mode="answer")
    try:
        for pattern in ("first", "second"):
            assert consume_ivd_search(pattern=pattern, path="/kb", target="content")[0]
            record_ivd_search_result(
                pattern=pattern,
                path="/kb",
                target="content",
                result_paths=(),
            )
        assert not consume_ivd_search(
            pattern="third", path="/kb", target="content"
        )[0]
        assert get_ivd_retrieval_snapshot()["stop_reason"] == "no_gain"
    finally:
        end_ivd_answer_turn(token)


def test_non_formal_results_do_not_count_as_evidence_gain():
    token = begin_ivd_answer_turn(policy=EVIDENCE_SUPPLEMENT_POLICY, mode="answer")
    try:
        for pattern, result_path in (
            (
                "first",
                "/kb/IVD-KnowledgeHub/knowledge-base/_extracted/truncated.md",
            ),
            (
                "second",
                "/kb/IVD-KnowledgeHub/knowledge-base/matrices/candidate.tsv",
            ),
        ):
            assert consume_ivd_search(pattern=pattern, path="/kb", target="content")[0]
            record_ivd_search_result(
                pattern=pattern,
                path="/kb",
                target="content",
                result_paths=(result_path,),
            )
        snapshot = get_ivd_retrieval_snapshot()
        assert snapshot["formal_source_count"] == 0
        assert snapshot["stop_reason"] == "no_gain"
    finally:
        end_ivd_answer_turn(token)


def test_result_outside_ivd_knowledge_base_is_not_formal_gain():
    token = begin_ivd_answer_turn(policy=INDEX_FALLBACK_POLICY, mode="answer")
    try:
        assert consume_ivd_search(pattern="config", path="/tmp", target="content")[0]
        record_ivd_search_result(
            pattern="config",
            path="/tmp",
            target="content",
            result_paths=("/tmp/project/config.py",),
        )
        snapshot = get_ivd_retrieval_snapshot()
        assert snapshot["formal_source_count"] == 0
        assert snapshot["stop_reason"] == ""
    finally:
        end_ivd_answer_turn(token)


def test_hard_limit_caps_unsafe_custom_policy_at_four_searches():
    unsafe = IVDRetrievalPolicy(
        "unsafe",
        ("one", "two", "three", "four", "five"),
        max_searches=5,
        hard_limit=4,
    )
    token = begin_ivd_answer_turn(policy=unsafe, mode="answer")
    try:
        for number in range(1, 5):
            assert consume_ivd_search(
                pattern=f"query-{number}", path="/kb", target="content"
            )[0]
        assert not consume_ivd_search(
            pattern="query-5", path="/kb", target="content"
        )[0]
        assert get_ivd_retrieval_snapshot()["stop_reason"] == "hard_limit"
    finally:
        end_ivd_answer_turn(token)


def test_snapshot_is_inactive_outside_answer_turn():
    snapshot = get_ivd_retrieval_snapshot()

    assert snapshot["active"] is False
    assert snapshot["profile"] == "inactive"
    assert snapshot["signature_count"] == 0


def test_trusted_ivd_path_prepares_one_contract_from_serving_manifest(tmp_path):
    import hashlib
    import json

    manifest = tmp_path / "release.json"
    serving = {
        "serving_package_path": str(tmp_path / "serving-package"),
        "serving_agent_path": str(tmp_path / "serving-agent"),
        "source_vault_path": str(tmp_path / "source-vault"),
        "dispatch_policy_path": str(tmp_path / "serving-package/dispatch.json"),
        "render_policy_path": str(tmp_path / "serving-package/render.json"),
        "context_budget": 8,
        "retrieval_budget": 2,
        "skill_allowlist": [],
        "receipt_destination": str(tmp_path / "observability/receipts.jsonl"),
    }
    projection_digest = hashlib.sha256(
        json.dumps(serving, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest.write_text(
        json.dumps({
            "shared_identity": {
                "package_digest": "b" * 64,
                "execution_contract_schema_version": "1",
                "turn_receipt_schema_version": "1",
            },
            "projections": {"serving": serving},
            "projection_digests": {"serving": projection_digest},
        }),
        encoding="utf-8",
    )
    config = {
        "after_sales_guard": {
            "enabled": True,
            "platforms": ["qqbot"],
            "serving_projection_path": str(manifest),
        }
    }

    prepared = prepare_enabled_ivd_turn(config, platform="qqbot")

    assert prepared is not None
    assert prepared.execution_contract.package_digest == "b" * 64
    assert prepared.execution_contract.serving_projection_digest == projection_digest
    assert prepare_enabled_ivd_turn(config, platform="cli") is None


def test_trusted_ivd_path_fails_closed_without_serving_projection(tmp_path):
    manifest = tmp_path / "release.json"
    manifest.write_text(
        '{"shared_identity":{"package_digest":"' + "b" * 64
        + '"},"projections":{"control":{}}}',
        encoding="utf-8",
    )
    config = {
        "after_sales_guard": {
            "enabled": True,
            "platforms": ["qqbot"],
            "serving_projection_path": str(manifest),
        }
    }

    with pytest.raises(IVDRuntimeConfigurationError):
        prepare_enabled_ivd_turn(config, platform="qqbot")


@pytest.mark.parametrize("guard", [{}, {"serving_projection_path": ""}])
def test_managed_ivd_platform_requires_serving_projection_configuration(guard):
    config = {
        "after_sales_guard": {
            "enabled": True,
            "platforms": ["qqbot"],
            **guard,
        }
    }

    with pytest.raises(IVDRuntimeConfigurationError):
        prepare_enabled_ivd_turn(config, platform="qqbot")

    assert prepare_enabled_ivd_turn(config, platform="cli") is None


def test_preload_freezes_platform_mapping_and_reuses_one_prepared_turn(tmp_path):
    import hashlib
    import json

    manifest = tmp_path / "release.json"
    serving = {
        "serving_package_path": str(tmp_path / "serving-package"),
        "serving_agent_path": str(tmp_path / "serving-agent"),
        "source_vault_path": str(tmp_path / "source-vault"),
        "dispatch_policy_path": str(tmp_path / "serving-package/dispatch.json"),
        "render_policy_path": str(tmp_path / "serving-package/render.json"),
        "context_budget": 8,
        "retrieval_budget": 2,
        "skill_allowlist": [],
        "receipt_destination": str(tmp_path / "observability/receipts.jsonl"),
    }
    digest = hashlib.sha256(
        json.dumps(serving, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest.write_text(json.dumps({
        "shared_identity": {
            "package_digest": "d" * 64,
            "execution_contract_schema_version": "1",
            "turn_receipt_schema_version": "1",
        },
        "projections": {"serving": serving},
        "projection_digests": {"serving": digest},
    }), encoding="utf-8")

    prepared = preload_enabled_ivd_contracts({
        "after_sales_guard": {
            "enabled": True,
            "platforms": ["qqbot", "telegram"],
            "serving_projection_path": str(manifest),
        }
    })

    assert prepared["qqbot"] is prepared["telegram"]
    with pytest.raises(TypeError):
        prepared["cli"] = prepared["qqbot"]


@pytest.mark.parametrize(
    "config",
    [{}, {"after_sales_guard": {"enabled": False, "platforms": ["qqbot"]}}],
)
def test_preload_skips_disabled_or_unmanaged_guard(monkeypatch, config):
    monkeypatch.setattr(
        "gateway.ivd_runtime.load_serving_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("loaded")),
    )

    assert not preload_enabled_ivd_contracts(config)


def test_oversized_receipt_is_dropped_before_writer_queue():
    submitted = enqueue_ivd_receipt(
        "/tmp/unused-receipt", {"turn_id": "x", "extra": "z" * 5000}
    )

    assert submitted is False


def test_receipt_worker_failure_is_one_attempt_without_retry(monkeypatch):
    import gateway.ivd_runtime as runtime

    attempts = []
    sink = type(
        "Sink",
        (),
        {"append": lambda self, payload: attempts.append(payload) and False},
    )()
    monkeypatch.setattr(runtime, "_RECEIPT_WORKER_STARTED", False)
    monkeypatch.setattr(runtime, "_RECEIPT_QUEUE", runtime.queue.Queue(maxsize=1))

    assert enqueue_ivd_receipt(sink, {"turn_id": "one"})
    runtime._RECEIPT_QUEUE.join()

    assert len(attempts) == 1


def test_append_receipt_does_not_retry_partial_os_write(monkeypatch):
    import gateway.ivd_runtime as runtime

    calls = []
    sink = type(
        "Sink",
        (),
        {"append": lambda self, payload: calls.append(payload) or False},
    )()
    payload = b'{"turn_id":"one"}\n'

    assert runtime._append_ivd_receipt(sink, payload) is False
    assert calls == [payload]


def test_receipt_sink_uses_one_os_write_on_short_write(monkeypatch, tmp_path):
    import gateway.ivd_execution_contract as contracts

    destination = tmp_path / "observability/turn-receipts.jsonl"
    calls = []
    real_write = contracts.os.write

    def short_write(fd, payload):
        calls.append((fd, bytes(payload)))
        return real_write(fd, payload[:3])

    monkeypatch.setattr(
        contracts.os,
        "write",
        short_write,
    )
    sink = contracts.AppendOnlyReceiptSink.open(
        destination,
        release_root=tmp_path,
    )
    payload = b'{"turn_id":"one"}\n'

    try:
        assert sink.append(payload) is False
        assert len(calls) == 1
        assert calls[0][1] == payload
    finally:
        sink.close()

    assert destination.read_bytes() == b""


def test_two_receipt_sinks_append_complete_records_without_interleaving(tmp_path):
    import gateway.ivd_execution_contract as contracts

    destination = tmp_path / "observability/turn-receipts.jsonl"
    first = contracts.AppendOnlyReceiptSink.open(destination, release_root=tmp_path)
    second = contracts.AppendOnlyReceiptSink.open(destination, release_root=tmp_path)
    payloads = [f'{{"turn_id":"{index}"}}\n'.encode() for index in range(100)]

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda item: (first if item[0] % 2 else second).append(item[1]),
                    enumerate(payloads),
                )
            )
        assert all(results)
    finally:
        first.close()
        second.close()

    assert sorted(destination.read_bytes().splitlines(keepends=True)) == sorted(payloads)


def test_receipt_sink_fails_closed_without_fcntl(monkeypatch, tmp_path):
    import gateway.ivd_execution_contract as contracts

    monkeypatch.setattr(contracts, "fcntl", None)

    with pytest.raises(IVDRuntimeConfigurationError):
        contracts.AppendOnlyReceiptSink.open(
            tmp_path / "observability/turn-receipts.jsonl",
            release_root=tmp_path,
        )


def test_receipt_sink_survives_destination_symlink_swap(monkeypatch, tmp_path):
    import gateway.ivd_execution_contract as contracts

    destination = tmp_path / "observability/turn-receipts.jsonl"
    outside = tmp_path / "outside.jsonl"
    sink = contracts.AppendOnlyReceiptSink.open(
        destination,
        release_root=tmp_path,
    )
    destination.rename(destination.with_suffix(".pinned"))
    outside.write_bytes(b"")
    destination.symlink_to(outside)
    payload = b'{"turn_id":"one"}\n'

    try:
        assert sink.append(payload) is True
    finally:
        sink.close()

    assert destination.with_suffix(".pinned").read_bytes() == payload
    assert outside.read_bytes() == b""
