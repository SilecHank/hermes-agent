import sqlite3

from gateway.ivd_verified_facts import VerifiedFactService
from hermes_state import SessionDB


def _record(**overrides):
    record = {
        "fact_id": "fact-carrier-dna-input",
        "product_scope": "carrier-screening",
        "product_variant": "v1",
        "question_type": "scalar_lookup",
        "fact_key": "dna_starting_input",
        "value": "240",
        "unit": "ng",
        "conditions": ["blood"],
        "answer_template": "{value} {unit}。",
        "evidence": [
            {
                "evidence_id": "ev-a",
                "source_path": "knowledge-base/reference/carrier-facts.md",
                "source_revision": "rev-a",
            }
        ],
        "source_revision": "rev-a",
        "status": "active",
    }
    record.update(overrides)
    return record


def test_activation_and_exact_lookup(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    service = VerifiedFactService(db)

    assert service.activate(_record()) is True
    match = service.lookup(
        product_scope="carrier-screening",
        product_variant="v1",
        fact_key="dna_starting_input",
        conditions=["blood"],
        source_revisions={"knowledge-base/reference/carrier-facts.md": "rev-a"},
    )

    assert match is not None
    assert match["rendered_answer"] == "240 ng。"
    assert match["status"] == "active"


def test_cross_product_conditions_and_stale_revision_miss(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    service = VerifiedFactService(db)
    assert service.activate(_record()) is True

    common = {
        "product_variant": "v1",
        "fact_key": "dna_starting_input",
        "source_revisions": {"knowledge-base/reference/carrier-facts.md": "rev-a"},
    }
    assert service.lookup(product_scope="other-product", conditions=["blood"], **common) is None
    assert service.lookup(product_scope="carrier-screening", conditions=["saliva"], **common) is None
    assert service.lookup(
        product_scope="carrier-screening",
        conditions=["blood"],
        **{**common, "source_revisions": {"knowledge-base/reference/carrier-facts.md": "rev-b"}},
    ) is None


def test_revoke_and_changed_path_revalidation(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    service = VerifiedFactService(db)
    assert service.activate(_record()) is True

    assert service.mark_for_revalidation(["knowledge-base/reference/carrier-facts.md"]) == 1
    assert service.lookup(
        product_scope="carrier-screening",
        product_variant="v1",
        fact_key="dna_starting_input",
        conditions=["blood"],
        source_revisions={"knowledge-base/reference/carrier-facts.md": "rev-a"},
    ) is None
    assert service.activate(_record()) is True
    assert service.revoke("fact-carrier-dna-input", "superseded") is True


def test_derived_table_rebuilds_after_deletion(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    assert VerifiedFactService(db).activate(_record()) is True
    db.close()

    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE ivd_verified_facts")
    conn.commit()
    conn.close()

    reopened = SessionDB(db_path=db_path)
    columns = {
        row[1] for row in reopened._conn.execute("PRAGMA table_info(ivd_verified_facts)").fetchall()
    }
    assert {"fact_id", "product_scope", "evidence_json", "status"}.issubset(columns)


def test_only_allowlisted_template_is_accepted(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    service = VerifiedFactService(db)

    assert service.activate(_record(answer_template="{value} {unit}，建议立即放行。")) is False
