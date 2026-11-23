"""Candidate-pool behaviour of the offline source shortlist.

The scenarios mirror what the production catalog actually contains: hundreds of
equipment-list rows that repeat an instrument model next to a few spec columns,
content-identical duplicate copies of those lists, and the one manual that
holds the full specification table.
"""
import sqlite3

from gateway.automatic_shortlist import shortlist

SCHEMA = """
CREATE TABLE sources(
    id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, sha256 TEXT NOT NULL,
    format TEXT NOT NULL, status TEXT NOT NULL, source_context TEXT NOT NULL
);
CREATE TABLE chunks(
    id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, location TEXT NOT NULL,
    kind TEXT NOT NULL, text TEXT NOT NULL, details TEXT NOT NULL, warnings TEXT NOT NULL
);
CREATE VIRTUAL TABLE search_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='trigram'
);
"""

MANUAL = (
    "技术规格 项目 描述 尺寸 1086 mm ( 长 ) × 756 mm ( 宽 ) × 710 mm ( 高 ) "
    "净重 200 kg 电源 100 - 240 V 额定功率 1200 VA 保险丝规格 F10AL250V "
    "MGISEQ-2000 基因测序仪"
)
LIST_ROW = (
    "A31: MA0168 B31: MGISEQ-2000 基因测序仪 尺寸 1086×756×710 净重 200kg "
    "功率 1200VA 保险丝 F10AL250V"
)
SOP = "联合实验室 NIFTY 建库实验流程标准作业指导书 MGISEQ-2000 上机流程与操作步骤"


def build_catalog(tmp_path, sources):
    path = tmp_path / "catalog.sqlite"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    for chunk_id, (source_id, source_path, sha256, text) in enumerate(sources, 1):
        db.execute(
            "INSERT INTO sources VALUES (?,?,?,?,?,?)",
            (source_id, source_path, sha256, "pdf", "ok",
             '{"source_kind_hint":"unclassified"}'),
        )
        db.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?)",
            (chunk_id, source_id, "PDF第58页", "page", text, "{}", "[]"),
        )
        db.execute("INSERT INTO search_fts(rowid, text) VALUES (?,?)", (chunk_id, text))
    db.commit()
    db.close()
    return path


def source_shas(catalog, ids):
    db = sqlite3.connect(catalog)
    try:
        marks = ",".join("?" for _ in ids)
        return dict(db.execute(f"SELECT id, sha256 FROM sources WHERE id IN ({marks})", ids))
    finally:
        db.close()


def test_specification_question_prefers_manual_over_equipment_list(tmp_path):
    catalog = build_catalog(tmp_path, [
        (1, "11_仪器平台说明书/测序平台与一体机/MGISEQ-2000 产品说明书.pdf", "a" * 64, MANUAL),
        (2, "07_设备试剂耗材清单/_拆分清单/病原/PMseq-MGISEQ-2000手工-设备.xlsx", "b" * 64, LIST_ROW),
        (3, "07_设备试剂耗材清单/_拆分清单/病原/PTseq-MGISEQ-2000手工-设备.xlsx", "c" * 64, LIST_ROW),
    ])

    ids = shortlist(catalog, "MGISEQ-2000 尺寸", limit=3)

    assert ids[0] == 1, "the manual holding the spec table must outrank equipment-list rows"


def test_operational_question_keeps_sop_authority(tmp_path):
    catalog = build_catalog(tmp_path, [
        (1, "11_仪器平台说明书/测序平台与一体机/MGISEQ-2000 产品说明书.pdf", "a" * 64, MANUAL),
        (2, "07_设备试剂耗材清单/_拆分清单/病原/PMseq-MGISEQ-2000手工-设备.xlsx", "b" * 64, LIST_ROW),
        (3, "01_标准作业指导书_SOP/NIFTY/联合实验室NIFTY项目建库实验标准作业指导书.docx", "d" * 64, SOP),
    ])

    ids = shortlist(catalog, "MGISEQ-2000 上机流程与操作步骤", limit=3)

    assert ids[0] == 3, "a workflow question belongs to the SOP, not to a specification prior"


def test_content_identical_copies_never_eat_two_slots(tmp_path):
    catalog = build_catalog(tmp_path, [
        (1, "11_仪器平台说明书/测序平台与一体机/MGISEQ-2000 产品说明书.pdf", "a" * 64, MANUAL),
        (2, "07_设备试剂耗材清单/_拆分清单/病原/PMseq-MGISEQ-2000手工-设备.xlsx", "b" * 64, LIST_ROW),
        (3, "07_设备试剂耗材清单/_拆分清单/病原 2/PMseq-MGISEQ-2000手工-设备.xlsx", "b" * 64, LIST_ROW),
        (4, "07_设备试剂耗材清单/_拆分清单/病原/PTseq-MGISEQ-2000手工-设备.xlsx", "c" * 64, LIST_ROW),
    ])

    ids = shortlist(catalog, "MGISEQ-2000 尺寸", limit=3)
    shas = source_shas(catalog, ids)

    assert len(ids) == 3
    assert len(set(shas.values())) == 3, "byte-identical copies must collapse to one candidate"


def test_question_without_spec_words_is_not_rerouted(tmp_path):
    catalog = build_catalog(tmp_path, [
        (1, "11_仪器平台说明书/测序平台与一体机/MGISEQ-2000 产品说明书.pdf", "a" * 64, MANUAL),
        (2, "01_标准作业指导书_SOP/NIFTY/联合实验室NIFTY项目建库实验标准作业指导书.docx", "d" * 64, SOP),
    ])

    ids = shortlist(catalog, "NIFTY 建库实验流程", limit=2)

    assert ids[0] == 2


def test_empty_catalog_returns_no_candidates(tmp_path):
    catalog = build_catalog(tmp_path, [
        (1, "01_标准作业指导书_SOP/NIFTY/联合实验室NIFTY项目建库实验标准作业指导书.docx", "d" * 64, SOP),
    ])

    assert shortlist(catalog, "完全不相干的问题表述", limit=3) == []
