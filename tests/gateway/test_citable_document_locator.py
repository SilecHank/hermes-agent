import hashlib
import sqlite3

from gateway.citable_document_locator import locate_document_section


def test_section_lookup_requires_exact_source_and_request(tmp_path):
    path = tmp_path / "guide.pdf"
    path.write_bytes(b"fixture")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    catalog = tmp_path / "catalog.sqlite"
    vectors = tmp_path / "vector.sqlite"
    with sqlite3.connect(catalog) as db:
        db.executescript("CREATE TABLE sources(id,path,sha256); CREATE TABLE chunks(id,source_id,kind,text,location);")
        db.execute("INSERT INTO sources VALUES(1,?,?)", (path.name, digest))
        db.execute("INSERT INTO chunks VALUES(1,1,'pdf_page_text','1 Scope applies to fixtures','PDF第3页')")
    with sqlite3.connect(vectors) as db:
        db.executescript("CREATE TABLE processed(chunk_id,status); INSERT INTO processed VALUES(1,'indexed');")
    entry = dict(path=path.name,label="Guide",topic="范围",year="2019",
                 aliases=["GUIDE"],heading="1 Scope",body_anchor="applies to fixtures")
    args = (catalog, vectors, tmp_path, [entry])
    query = "GUIDE 2019 范围在哪一页？"
    assert locate_document_section(query, *args)["location"] == "PDF第3页"
    assert locate_document_section(query.replace("2019", "2020"), *args) is None
    assert locate_document_section("GUIDE 范围在哪一页？请发送原文件", *args) is None
    assert locate_document_section(query, *args[:3], [entry, entry]) is None
    path.write_bytes(b"changed")
    assert locate_document_section(query, *args) is None
