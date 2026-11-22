"""Read-only lookup of explicitly registered document sections."""
import hashlib
import re
import sqlite3
from contextlib import closing
from pathlib import Path


_LOOKUP = re.compile(r"哪.{0,4}(?:页|章节)|第几页|位置|哪里")
_EXCLUDED = re.compile(r"不查|不要|不是|对比|比较|区别|发给我|发送|原文件|附件|下载")
_YEAR = re.compile(r"(?<![0-9])20[0-9]{2}(?![0-9])")


def locate_document_section(question, catalog, vectors, originals, entries):
    if not _LOOKUP.search(question) or _EXCLUDED.search(question):
        return None
    query = question.casefold()
    years = set(_YEAR.findall(query))
    candidates = [
        entry for entry in entries
        if entry["topic"].casefold() in query
        and any(alias.casefold() in query for alias in entry["aliases"])
        and years <= {entry["year"]}
    ]
    if len(candidates) != 1:
        return None
    entry = candidates[0]
    root = Path(originals).resolve()
    with closing(sqlite3.connect(Path(catalog).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("ATTACH DATABASE ? AS v", (Path(vectors).resolve().as_uri() + "?mode=ro",))
        matches = db.execute("""
            WITH selected AS MATERIALIZED (
                SELECT c.id,s.path,s.sha256,c.location FROM sources s
                JOIN chunks c ON c.source_id=s.id
                WHERE s.path=? AND c.kind='pdf_page_text'
                  AND instr(c.text,?)>0 AND instr(c.text,?)>0
            )
            SELECT c.path,c.sha256,c.location FROM selected c
            CROSS JOIN v.processed p ON p.chunk_id=c.id
            WHERE p.status='indexed'
        """, (entry["path"],entry["heading"],entry["body_anchor"])).fetchall()
    if len(matches) != 1:
        return None
    relative,digest,location = matches[0]
    original = (root / relative).resolve()
    if (not original.is_relative_to(root) or not original.is_file()
            or hashlib.sha256(original.read_bytes()).hexdigest() != digest):
        return None
    return {
        "text": f"{entry['topic']}在 {entry['label']}{location}「{entry['heading']}」。",
        "path": relative,
        "location": location,
        "sha256": digest,
    }


def locate_cnas_scope(question, catalog, vectors, originals):
    entry = {
        "path": "13_政策标准与指南/CNAS-GL039_2019_分子诊断检验程序性能验证指南.pdf",
        "label": "CNAS-GL039:2019《分子诊断检验程序性能验证指南》",
        "topic": "适用范围",
        "year": "2019",
        "aliases": ("CNAS-GL039", "CNAS 分子诊断性能验证指南", "CNAS 分子诊断检验程序性能验证指南"),
        "heading": "1 范围",
        "body_anchor": "本指南适用于",
    }
    return locate_document_section(question, catalog, vectors, originals, [entry])
