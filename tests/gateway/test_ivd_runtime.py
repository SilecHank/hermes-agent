from types import SimpleNamespace

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
)


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
