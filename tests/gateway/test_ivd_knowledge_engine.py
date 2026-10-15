import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from gateway.ivd_knowledge_engine import IVDKnowledgeEngine, PackageIntegrityError


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _create_registry(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE products (
            product_id TEXT PRIMARY KEY,
            product_line TEXT NOT NULL,
            product_variant TEXT NOT NULL
        );
        CREATE TABLE sources (
            source_id TEXT PRIMARY KEY,
            source_document_id TEXT NOT NULL
        );
        CREATE TABLE versions (
            version_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            source_version TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            source_record_digest TEXT NOT NULL
        );
        CREATE TABLE locators (
            locator_id TEXT PRIMARY KEY,
            version_id TEXT NOT NULL,
            source_locator TEXT NOT NULL
        );
        CREATE TABLE entities (
            entity_id TEXT PRIMARY KEY,
            knowledge_kind TEXT NOT NULL,
            product_id TEXT NOT NULL,
            workflow_stage TEXT NOT NULL,
            step_id TEXT NOT NULL,
            object_name TEXT NOT NULL,
            fact_key TEXT NOT NULL,
            conditions_json TEXT NOT NULL
        );
        CREATE TABLE assertions (
            assertion_id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL,
            locator_id TEXT NOT NULL,
            effective_status TEXT NOT NULL
        );
        CREATE TABLE parameters (assertion_id TEXT PRIMARY KEY, value TEXT NOT NULL, unit TEXT);
        CREATE TABLE process_facts (assertion_id TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE report_rules (assertion_id TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE files (assertion_id TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE contacts (assertion_id TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE evidence (assertion_id TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE aliases (alias TEXT NOT NULL, assertion_id TEXT NOT NULL);
        CREATE VIRTUAL TABLE aliases_fts USING fts5(alias, assertion_id UNINDEXED);
        CREATE VIEW entity_values AS
            SELECT assertion_id, value, unit FROM parameters
            UNION ALL SELECT assertion_id, value, NULL FROM process_facts
            UNION ALL SELECT assertion_id, value, NULL FROM report_rules
            UNION ALL SELECT assertion_id, value, NULL FROM files
            UNION ALL SELECT assertion_id, value, NULL FROM contacts
            UNION ALL SELECT assertion_id, value, NULL FROM evidence;
        """
    )
    connection.execute("INSERT INTO products VALUES ('nifty', 'NIFTY', '标准版')")
    connection.execute("INSERT INTO products VALUES ('cnv', 'CNV-seq', '')")
    connection.execute("INSERT INTO sources VALUES ('sop-nifty', 'SOP-JL-001')")
    connection.execute(
        "INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?)",
        ("v1", "sop-nifty", "A1", "knowledge-base/formal/nifty.md", "a" * 64, "b" * 64),
    )
    connection.execute("INSERT INTO locators VALUES ('l1', 'v1', 'L10-L12')")

    rows = (
        ("p1", "parameter", "plasma_input", "200", "uL", "无创提取需要多少血浆"),
        ("p2", "process_fact", "extraction_process", "先裂解，再进行磁珠纯化。", None, "无创提取流程"),
        ("p3", "file", "sop_file", "knowledge-base/formal/nifty.md", None, "发送无创提取SOP"),
        ("p4", "process_fact", "invalid_mixed_product", "CNV-seq 应执行另一流程。", None, "错误混用测试"),
    )
    for index, (entity, kind, fact_key, value, unit, alias) in enumerate(rows, start=1):
        assertion = f"a{index}"
        connection.execute(
            "INSERT INTO entities VALUES (?, ?, 'nifty', 'extraction', ?, ?, ?, '{}')",
            (entity, kind, f"step-{index}", "plasma", fact_key),
        )
        connection.execute(
            "INSERT INTO assertions VALUES (?, ?, 'l1', 'active')",
            (assertion, entity),
        )
        table = {"parameter": "parameters", "process_fact": "process_facts", "file": "files"}[kind]
        if kind == "parameter":
            connection.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", (assertion, value, unit))
        else:
            connection.execute(f"INSERT INTO {table} VALUES (?, ?)", (assertion, value))
        connection.execute("INSERT INTO aliases VALUES (?, ?)", (alias, assertion))
        connection.execute("INSERT INTO aliases_fts VALUES (?, ?)", (alias, assertion))
    connection.execute("INSERT INTO aliases VALUES ('plasma input amount', 'a1')")
    connection.execute("INSERT INTO aliases_fts VALUES ('plasma input amount', 'a1')")
    connection.commit()
    connection.close()


@pytest.fixture()
def package(tmp_path: Path) -> Path:
    root = tmp_path / "serving-package"
    _create_registry(root / "database/registry.sqlite")
    _write_json(
        root / "indexes/diagnostic-graph.json",
        {
            "schema_version": 1,
            "digest": "d" * 64,
            "service_graph": {
                "patterns": [
                    {
                        "pattern_id": "nifty-low-concentration",
                        "product_line": "NIFTY",
                        "product_variant": "",
                        "symptom_aliases": ["无创胎儿浓度偏低"],
                        "required_evidence": ["same_batch"],
                        "supporting_evidence": ["same_batch"],
                        "contradicting_evidence": ["single_sample"],
                        "first_direction": "优先排查批次性提取或建库偏差。",
                        "next_discriminator": {
                            "evidence_id": "same_batch",
                            "question": "同批样本是否同时偏低？",
                        },
                        "recommended_action": "先复核同批质控与提取记录。",
                        "stop_condition": ["confirmed_clinical_cause"],
                        "recovery_condition": ["repeat_passed"],
                        "formal_source_ids": [
                            {
                                "document": "SOP-JL-001",
                                "version": "A1",
                                "path": "knowledge-base/formal/nifty.md",
                                "locator": "L10-L12",
                                "source_sha256": "a" * 64,
                                "source_record_digest": "b" * 64,
                            }
                        ],
                    }
                ]
            },
        },
    )
    _write_json(
        root / "renders/render-policy.json",
        {
            "schema_version": 1,
            "render_mode": "strict",
            "scalar_template": "{value} {unit}.",
            "fallback_request": "当前正式知识包未收录该问题，请补充产品名称、版本或SOP编号。",
        },
    )
    _write_json(
        root / "metadata/source-manifest.json",
        {"schema_version": 1, "source_vault_digest": "3" * 64, "originals": []},
    )
    members = {
        relative: _digest(root / relative)
        for relative in (
            "database/registry.sqlite",
            "indexes/diagnostic-graph.json",
            "renders/render-policy.json",
            "metadata/source-manifest.json",
        )
    }
    package_digest = hashlib.sha256(
        _canonical_bytes(
            {"algorithm": "sha256-canonical-members-v1", "members": members}
        )
    ).hexdigest()
    _write_json(
        root / "package-manifest.json",
        {
            "schema_version": 1,
            "snapshot_digest": "1" * 64,
            "registry_digest": members["database/registry.sqlite"],
            "graph_digest": "d" * 64,
            "package_digest": package_digest,
            "source_vault_digest": "3" * 64,
            "member_digest_algorithm": "sha256-canonical-members-v1",
            "members": members,
        },
    )
    return root


def test_scalar_lookup_returns_value_and_unit_without_model(package: Path):
    result = IVDKnowledgeEngine(package).execute(question="无创提取需要多少血浆？")

    assert result.text == "200 uL."
    assert result.answer_shape == "scalar"
    assert result.model_calls == 0
    assert result.index_transactions == 0
    assert result.filesystem_scans == 0
    assert result.source.document == "SOP-JL-001"
    assert result.sources == (result.source,)


def test_unambiguous_near_exact_question_uses_one_compiled_index_lookup(package: Path):
    result = IVDKnowledgeEngine(package).execute(
        question="请问无创提取需要多少血浆呀",
        product_line="NIFTY",
        product_variant="标准版",
        workflow_stage="extraction",
        knowledge_type="parameter",
        answer_shape="scalar",
        allow_index_transaction=True,
    )

    assert result.text == "200 uL."
    assert result.model_calls == 0
    assert result.index_transactions == 1
    assert result.filesystem_scans == 0


def test_reordered_equivalent_parameter_question_uses_one_compiled_index_lookup(package: Path):
    result = IVDKnowledgeEngine(package).execute(
        question="无创血浆提取量是多少",
        product_line="NIFTY",
        product_variant="标准版",
        workflow_stage="extraction",
        knowledge_type="parameter",
        answer_shape="scalar",
        allow_index_transaction=True,
    )

    assert result.text == "200 uL."
    assert result.model_calls == 0
    assert result.index_transactions == 1
    assert result.filesystem_scans == 0


def test_engine_binds_observed_package_digest_to_expected_release(package: Path):
    manifest = json.loads((package / "package-manifest.json").read_text())
    digest = manifest["package_digest"]

    engine = IVDKnowledgeEngine(package, expected_package_digest=digest)
    try:
        assert engine.package_digest == digest
    finally:
        engine.close()

    with pytest.raises(PackageIntegrityError, match="expected package digest"):
        IVDKnowledgeEngine(package, expected_package_digest="f" * 64)


def test_engine_closes_candidate_database_when_release_initialization_fails(
    package: Path, monkeypatch
):
    import gateway.ivd_knowledge_engine as engine_module

    class BrokenDatabase:
        row_factory = None
        close_count = 0

        def execute(self, statement):
            if statement == "PRAGMA query_only=ON":
                return self
            raise sqlite3.DatabaseError("broken release registry")

        def close(self):
            self.close_count += 1

    database = BrokenDatabase()
    monkeypatch.setattr(engine_module.sqlite3, "connect", lambda *_args, **_kwargs: database)

    with pytest.raises(sqlite3.DatabaseError, match="broken release registry"):
        IVDKnowledgeEngine(package)

    assert database.close_count == 1


def test_process_and_file_exact_lookups_are_zero_search_transactions(package: Path):
    engine = IVDKnowledgeEngine(package)

    process = engine.execute(question="无创提取流程", product_line="NIFTY")
    file_result = engine.execute(question="发送无创提取SOP", product_line="NIFTY")

    assert process.text == "先裂解，再进行磁珠纯化。"
    assert process.answer_shape == "process"
    assert file_result.text == "knowledge-base/formal/nifty.md"
    assert file_result.answer_shape == "file"
    assert process.index_transactions == file_result.index_transactions == 0
    assert process.model_calls == file_result.model_calls == 0


def test_diagnostic_pattern_is_evaluated_deterministically(package: Path):
    result = IVDKnowledgeEngine(package).execute(
        question="无创胎儿浓度偏低",
        product_line="NIFTY",
        evidence={"same_batch": True},
    )

    assert result.answer_shape == "diagnostic"
    assert result.text == "优先排查批次性提取或建库偏差。\n先复核同批质控与提取记录。"
    assert result.model_calls == 0
    assert result.index_transactions == 0
    assert result.effect_count == 1
    assert result.sources[0].document == "SOP-JL-001"
    assert result.sources[0].version == "A1"


def test_unknown_question_returns_controlled_fallback_without_search(package: Path):
    result = IVDKnowledgeEngine(package).execute(
        question="未收录的新问题", product_line="NIFTY"
    )

    assert result.outcome == "fallback_request"
    assert result.text == "当前正式知识包未收录该问题，请补充产品名称、版本或SOP编号。"
    assert result.model_calls == 0
    assert result.index_transactions == 0
    assert result.filesystem_scans == 0
    assert result.sources == ()


def test_one_fts_transaction_may_return_a_validated_registry_hit(package: Path):
    result = IVDKnowledgeEngine(package).execute(
        question="plasma input",
        product_line="NIFTY",
        allow_index_transaction=True,
    )

    assert result.text == "200 uL."
    assert result.answer_shape == "scalar"
    assert result.model_calls == 0
    assert result.index_transactions == 1
    assert result.filesystem_scans == 0


def test_engine_applies_product_text_guard_to_registry_outputs(package: Path):
    with pytest.raises(PackageIntegrityError, match="product_text_mismatch"):
        IVDKnowledgeEngine(package).execute(
            question="错误混用测试", product_line="NIFTY"
        )


def test_ambiguous_or_wrong_product_does_not_cross_product_boundaries(package: Path):
    result = IVDKnowledgeEngine(package).execute(
        question="无创提取需要多少血浆？", product_line="CNV-seq"
    )

    assert result.outcome == "fallback_request"
    assert result.model_calls == 0
    assert result.index_transactions == 0


def test_product_variant_does_not_fall_back_to_another_variant(package: Path):
    result = IVDKnowledgeEngine(package).execute(
        question="无创提取需要多少血浆？",
        product_line="NIFTY",
        product_variant="PRO",
        workflow_stage="extraction",
        knowledge_type="parameter",
        answer_shape="scalar",
    )

    assert result.outcome == "fallback_request"
    assert result.model_calls == 0
    assert result.index_transactions == 0


def test_dispatch_bound_empty_variant_does_not_match_specific_variant(package: Path):
    result = IVDKnowledgeEngine(package).execute(
        question="无创提取需要多少血浆？",
        product_line="NIFTY",
        product_variant="",
        workflow_stage="extraction",
        knowledge_type="parameter",
        answer_shape="scalar",
    )

    assert result.outcome == "fallback_request"


def test_decision_axes_filter_lookup_and_validate_shape(package: Path):
    engine = IVDKnowledgeEngine(package)

    wrong_stage = engine.execute(
        question="无创提取需要多少血浆？",
        product_line="NIFTY",
        product_variant="标准版",
        workflow_stage="report",
        knowledge_type="parameter",
        answer_shape="scalar",
    )
    wrong_kind = engine.execute(
        question="无创提取需要多少血浆？",
        product_line="NIFTY",
        product_variant="标准版",
        workflow_stage="extraction",
        knowledge_type="process",
        answer_shape="process",
    )

    assert wrong_stage.outcome == "fallback_request"
    assert wrong_kind.outcome == "fallback_request"
    with pytest.raises(PackageIntegrityError, match="answer_shape_mismatch"):
        engine.execute(
            question="无创提取需要多少血浆？",
            product_line="NIFTY",
            product_variant="标准版",
            workflow_stage="extraction",
            knowledge_type="parameter",
            answer_shape="report",
        )


def test_package_member_tampering_fails_closed(package: Path):
    (package / "renders/render-policy.json").write_text("{}", encoding="utf-8")

    with pytest.raises(PackageIntegrityError, match="member digest mismatch"):
        IVDKnowledgeEngine(package)


def test_non_runtime_manifest_member_tampering_also_fails_closed(package: Path):
    (package / "metadata/source-manifest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(PackageIntegrityError, match="member digest mismatch"):
        IVDKnowledgeEngine(package)


def test_manifest_cannot_bind_a_member_outside_package(package: Path):
    outside = package.parent / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    manifest_path = package / "package-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["members"]["../outside.json"] = _digest(outside)
    manifest["package_digest"] = hashlib.sha256(
        _canonical_bytes(
            {
                "algorithm": "sha256-canonical-members-v1",
                "members": manifest["members"],
            }
        )
    ).hexdigest()
    _write_json(manifest_path, manifest)

    with pytest.raises(PackageIntegrityError, match="member path invalid"):
        IVDKnowledgeEngine(package)


def test_symlinked_member_ancestor_is_rejected(package: Path):
    metadata = package / "metadata"
    outside = package.parent / "outside-metadata"
    metadata.rename(outside)
    metadata.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PackageIntegrityError, match="package member invalid"):
        IVDKnowledgeEngine(package)


def test_presentation_is_checked_by_the_existing_final_validator(package: Path):
    engine = IVDKnowledgeEngine(package)
    seen = []
    def present(text,sources):
        seen.append(sources)
        return text + " 来源：" + sources[0].document
    try:
        result = engine.execute(question="无创提取需要多少血浆？",presentation_formatter=present)
        assert result.text.startswith("200 uL.")
        assert "SOP-JL-001" in result.text
        assert len(seen)==1
        assert result.model_calls==0
        with pytest.raises(PackageIntegrityError,match="internal_instruction_leak"):
            engine.execute(question="无创提取需要多少血浆？",
                           presentation_formatter=lambda text,sources: text + " fast_path")
    finally:
        engine.close()


@pytest.mark.parametrize('lookup', ['exact', 'semantic'])
def test_method_scoped_parameter_does_not_answer_unconfirmed_method(package, monkeypatch, lookup):
    from types import SimpleNamespace
    with IVDKnowledgeEngine(package) as engine:
        hit = engine._exact_registry('无创提取需要多少血浆', 'NIFTY', '标准版', '', 'parameter')
        hit.conditions_json = '{"method":"manual_extraction"}'
        monkeypatch.setattr(engine, '_exact_registry', lambda *args: hit if lookup == 'exact' else None)
        monkeypatch.setattr(engine, '_semantic_registry', lambda *args: hit)
        result = engine.execute(question='无创提取需要多少血浆', product_line='NIFTY',
            knowledge_type='parameter', answer_shape='scalar', allow_index_transaction=lookup=='semantic')
        assert result.outcome == 'clarification'
        assert '方法' in result.text
        assert '200' not in result.text
        assert result.model_calls == 0


@pytest.mark.parametrize('lookup', ['exact', 'semantic'])
def test_method_scoped_parameter_allows_explicit_matching_method(package, monkeypatch, lookup):
    with IVDKnowledgeEngine(package) as engine:
        hit = engine._exact_registry('无创提取需要多少血浆', 'NIFTY', '标准版', '', 'parameter')
        hit.conditions_json = '{"method":"manual_extraction"}'
        monkeypatch.setattr(engine, '_exact_registry', lambda *args: hit if lookup == 'exact' else None)
        monkeypatch.setattr(engine, '_semantic_registry', lambda *args: hit)
        result = engine.execute(question='无创手工提取需要多少血浆', product_line='NIFTY',
            knowledge_type='parameter', answer_shape='scalar', allow_index_transaction=lookup=='semantic')
        assert result.outcome == 'answer'
        assert '200' in result.text
        assert result.model_calls == 0


def test_method_scope_rejection_does_not_hide_non_serving_hit(package, monkeypatch):
    with IVDKnowledgeEngine(package) as engine:
        hit = engine._exact_registry('无创提取需要多少血浆', 'NIFTY', '标准版', '', 'parameter')
        hit.conditions_json = '{"method":"manual_extraction"}'
        hit.effective_status = 'pending_verify'
        monkeypatch.setattr(engine, '_exact_registry', lambda *args: hit)
        with pytest.raises(PackageIntegrityError, match='non_serving_knowledge'):
            engine.execute(question='无创提取需要多少血浆', product_line='NIFTY',knowledge_type='parameter',answer_shape='scalar')


@pytest.mark.parametrize('question,method', [
    ('NIFTY不是手工提取，需要多少血浆','manual_extraction'),
    ('NIFTY是否能手工提取，需要多少血浆','manual_extraction'),
    ('NIFTY手工提取可以吗，需要多少血浆','manual_extraction'),
    ('NIFTY不确定是不是自动化提取，需要多少血浆','automatic_extraction'),
    ('NIFTY手工还是自动提取，需要多少血浆','manual_extraction'),
    ('NIFTY不是手工或自动提取，需要多少血浆','automatic_extraction'),
    ('NIFTY手工方法不适用，需要多少血浆','manual_extraction'),
    ('无创提取需要多少血浆？手工？','manual_extraction'),
    ('无创提取需要多少血浆，我不确定，手工提取','manual_extraction'),
    ('我不确定。手工提取需要多少血浆','manual_extraction'),
    ('不确定是手工提取，需要多少血浆','manual_extraction'),
    ('未确认是手工提取，需要多少血浆','manual_extraction'),
    ('无创提取需要多少血浆，不能确定是手工提取','manual_extraction'),
    ('无创提取需要多少血浆不能确定是手工提取','manual_extraction'),
    ('还没确定是手工提取需要多少血浆','manual_extraction'),
    ('没有确认是手工提取需要多少血浆','manual_extraction'),
    ('并非已确认手工提取，需要多少血浆','manual_extraction'),
    ('NIFTY未采用手工提取，需要多少血浆', 'manual_extraction'),
    ('NIFTY不做手工提取，需要多少血浆', 'manual_extraction'),
    ('NIFTY无须手工提取，需要多少血浆', 'manual_extraction'),
    ('NIFTY手工提取尚待确认，需要多少血浆', 'manual_extraction'),
    ('无创手工提取需要多少血浆，方法待确认','manual_extraction'),
    ('无创手工提取需要多少血浆，方法待确定','manual_extraction'),
    ('已确认手工提取，需要多少血浆，方法待确认','manual_extraction'),
    ('不一定是手工提取，需要多少血浆','manual_extraction'),
    ('应该是手工提取，需要多少血浆','manual_extraction'),
    ('疑似手工提取，需要多少血浆','manual_extraction'),
    ('似乎是手工提取，需要多少血浆','manual_extraction'),

])
@pytest.mark.parametrize('lookup', ['exact','semantic'])
def test_uncertain_or_negated_method_is_not_parameter_confirmation(package,monkeypatch,question,method,lookup):
    with IVDKnowledgeEngine(package) as engine:
        hit=engine._exact_registry('无创提取需要多少血浆','NIFTY','标准版','','parameter')
        hit.conditions_json=json.dumps({'method':method})
        monkeypatch.setattr(engine,'_exact_registry',lambda *args: hit if lookup=='exact' else None)
        monkeypatch.setattr(engine,'_semantic_registry',lambda *args: hit)
        result=engine.execute(question=question,product_line='NIFTY',knowledge_type='parameter',answer_shape='scalar',allow_index_transaction=lookup=='semantic')
        assert result.outcome=='clarification'
        assert '200' not in result.text
        assert result.model_calls==0

@pytest.mark.parametrize('question,method', [
    ('NIFTY已确认手工提取，需要多少血浆','manual_extraction'),
    ('NIFTY不是自动，是手工提取，需要多少血浆','manual_extraction'),
    ('NIFTY不是自动而是手工提取，需要多少血浆','manual_extraction'),
    ('NIFTY从自动改成手工提取，需要多少血浆','manual_extraction'),
    ('NIFTY不是手工，是自动化提取，需要多少血浆','automatic_extraction'),
    ('之前不确定，现已确认手工提取，需要多少血浆','manual_extraction'),
    ('之前方法待确认，现在已确认手工提取，需要多少血浆','manual_extraction'),
    ('之前未采用手工提取，现在已确认手工提取，需要多少血浆', 'manual_extraction'),
    ('之前不做手工提取，现在已确认手工提取，需要多少血浆', 'manual_extraction'),
    ('之前无须手工提取，现在已确认手工提取，需要多少血浆', 'manual_extraction'),
    ('之前方法待确定，现在已确认手工提取，需要多少血浆','manual_extraction'),
    ('无创手工提取需要多少血浆或多少毫升','manual_extraction'),
    ('无创手工提取需要多少血浆，用uL或mL表示','manual_extraction'),
    ('已确认手工提取，不确定需要多少血浆','manual_extraction'),
    ('无创手工提取需要多少血浆，还是这个问题','manual_extraction'),
])
@pytest.mark.parametrize('lookup', ['exact','semantic'])
def test_explicit_method_correction_is_accepted(package,monkeypatch,question,method,lookup):
    with IVDKnowledgeEngine(package) as engine:
        hit=engine._exact_registry('无创提取需要多少血浆','NIFTY','标准版','','parameter')
        hit.conditions_json=json.dumps({'method':method})
        monkeypatch.setattr(engine,'_exact_registry',lambda *args:hit if lookup=='exact' else None)
        monkeypatch.setattr(engine,'_semantic_registry',lambda *args:hit)
        result=engine.execute(question=question,product_line='NIFTY',knowledge_type='parameter',answer_shape='scalar',allow_index_transaction=lookup=='semantic')
        assert result.outcome=='answer'
