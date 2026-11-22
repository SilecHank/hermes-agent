"""Offline first-pass source selection from raw questions."""
import re
import sqlite3
from pathlib import Path
from contextlib import closing

IDENTIFIER = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:[-:][A-Za-z0-9]+)*(?![A-Za-z0-9])")
CLAUSE = re.compile(r"[\u3400-\u9fff]+")
STOP = {"有没有","哪份","有哪份","想找","有没有讲","文件","的适用","适用的","有没有论文","有没有研究","持续多久","想找研究","有没有讲适","哪份中国"}


def query_terms(question):
    exact = list(dict.fromkeys(x.group() for x in IDENTIFIER.finditer(question)
                              if len(x.group()) >= 3))
    chinese = {}
    for match in CLAUSE.finditer(question):
        clause = match.group()
        for size in (5, 4, 3):
            for offset in range(len(clause)-size+1):
                part = clause[offset:offset+size]
                if part not in STOP and not any(part.startswith(w) for w in ("有没有","想找","有哪份")):
                    chinese.setdefault(part, None)
    return exact, list(chinese)


def shortlist(catalog, question, limit=20):
    exact, chinese = query_terms(question)
    totals={}
    document_version = re.search(
        r"(?<![A-Za-z0-9])(?P<id>SOP-[A-Z]+-[0-9]+)[ _-]+(?P<version>[A-Z][0-9]+)(?![A-Za-z0-9])",
        question, re.I,
    )
    with closing(sqlite3.connect(Path(catalog).resolve().as_uri()+"?mode=ro",uri=True)) as db:
        if document_version:
            token = re.compile(
                r"(?<![A-Za-z0-9])" + re.escape(document_version["id"])
                + r"[ _-]+" + re.escape(document_version["version"]) + r"(?![A-Za-z0-9])",
                re.I,
            )
            return [
                sid for sid,path in db.execute("SELECT id,path FROM sources ORDER BY id")
                if token.search(Path(path).name)
            ][:limit]
        for term in exact+chinese[:38]:
            phrase='"'+term+'"'
            rows=db.execute("""
                SELECT s.id FROM search_fts JOIN chunks c ON c.id=search_fts.rowid
                JOIN sources s ON s.id=c.source_id WHERE search_fts MATCH ?
                AND s.path NOT LIKE '%/_已归档/%'
                AND coalesce(json_extract(s.source_context,'$.source_kind_hint'),'')!='analysis_result_candidate'
                ORDER BY bm25(search_fts) LIMIT 100
            """,(phrase,))
            weight=4 if term in exact else 1
            seen=set()
            for rank,(sid,) in enumerate(rows,1):
                if sid in seen:continue
                seen.add(sid)
                totals[sid]=totals.get(sid,0)+weight/(15+rank)
        for term in exact:
            for (sid,) in db.execute("SELECT id FROM sources WHERE instr(lower(path),lower(?))>0 AND path NOT LIKE '%/_已归档/%'",(term,)):
                totals[sid]=totals.get(sid,0)+0.25
    return sorted(totals,key=lambda sid:(-totals[sid],sid))[:limit]


if __name__=="__main__":
    cases=[
        ("做分子诊断检验程序的性能验证时，有没有讲适用范围的规范文件？",870),
        ("BRCA 胚系筛查在检测前的遗传咨询，有哪份中国专家文件可以参考？",1012),
        ("想找研究看减胎后母体血里的 cfDNA 持续多久，有没有论文？",1019),
    ]
    from time import monotonic
    for q,target in cases:
        t=monotonic()
        ids=shortlist("catalog.sqlite",q)
        print(target,ids.index(target)+1 if target in ids else None,
              "SEC",round(monotonic()-t,2),"EXACT",query_terms(q)[0],flush=True)
