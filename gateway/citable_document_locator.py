"""Read-only lookup of explicitly registered document sections."""
import hashlib
import re
import sqlite3
from contextlib import closing
from pathlib import Path


_LOOKUP = re.compile(r"哪.{0,4}(?:页|章节)|第几页|位置|哪里")
_EXCLUDED = re.compile(r"不查|不要|不是|对比|比较|区别|发给我|发送|原文件|附件|下载")
_YEAR = re.compile(r"(?<![0-9])20[0-9]{2}(?![0-9])")


def locate_document_section(question, catalog, vectors, originals, entries, *, clauses_only=False):
    clause = _locate_reviewed_brca_clause(question, originals, entries)
    if clause:
        return clause
    if clauses_only or not _LOOKUP.search(question) or _EXCLUDED.search(question):
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


def _phases(text):
    rules = {
        "before": r"检测前|检测之前|检测以前",
        "after": r"检测后|检测之后|检测以后|做完检测|检测结束后",
    }
    return {phase for phase, pattern in rules.items() if re.search(pattern, text)}


_TOPICS = {
    "2": r"(?:筛查|人群).*?(?:工作)?流程|工作流程",
    "3": r"遗传咨询|(?:检测前|检测之前|检测以前)咨询",
    "7": r"(?:样本|数据).*?管理|管理.*?(?:样本|数据)",
}
_REQUIRED_PHASES = {"3": {"before"}, "7": {"after"}}
_LOCATION_REQUEST = r"哪(?:一|个)?(?:条|页|小节|段|章节)|何处|位置|在哪里|在哪[？?\s]*$"
_OTHER_REQUEST = r"下载|发(?:我|给|送)|附件|总结|对比|比较|分别|检测前后"


def _locate_reviewed_brca_clause(question, originals, entries):
    if not re.search(r"BRCA", question, re.I) or _EXCLUDED.search(question):
        return None
    if set(re.findall(r"(?<!\d)20\d{2}(?!\d)", question)) - {"2024"}:
        return None
    if not re.search(_LOCATION_REQUEST, question) or re.search(_OTHER_REQUEST, question):
        return None
    phases = _phases(question)
    topics = [number for number, pattern in _TOPICS.items() if re.search(pattern, question)]
    if len(phases) > 1 or len(topics) != 1:
        return None
    number = topics[0]
    if _REQUIRED_PHASES.get(number, phases) != phases:
        return None
    relative = "13_政策标准与指南/肿瘤检测/【共识】基于中国人群的BRCA胚系突变筛查专家共识（2024年版）.pdf"
    registered = [entry for entry in entries if entry["path"] == relative and entry["year"] == "2024"]
    if len(registered) != 1:
        return None
    root = Path(originals).resolve()
    source = (root / relative).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        return None
    expected_digest = "118d2a5a527c7ab7c87204dcb2cc088c89334aab5bc886838e5dc2ffd63201dc"
    with source.open("rb") as file:
        if hashlib.file_digest(file, "sha256").hexdigest() != expected_digest:
            return None
    locations = {"2": "PDF 第5页左栏", "3": "PDF 第5页右栏",
                 "7": "PDF 第11页左栏末尾至右栏顶部"}
    location = locations[number]
    label = registered[0]["label"]
    return {
        "text": f"见{label}「推荐意见 {number}」（{location}）。",
        "path": relative,
        "location": location,
        "sha256": expected_digest,
    }
