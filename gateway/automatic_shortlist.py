"""Offline first-pass source selection from raw questions."""
import re
import sqlite3
from pathlib import Path
from contextlib import closing

IDENTIFIER = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:[-:][A-Za-z0-9]+)*(?![A-Za-z0-9])")
CLAUSE = re.compile(r"[\u3400-\u9fff]+")
STOP = {"有没有","哪份","有哪份","想找","有没有讲","文件","的适用","适用的","有没有论文","有没有研究","持续多久","想找研究","有没有讲适","哪份中国"}

# Physical and operating specifications only live in full in the instrument and
# reagent manuals; an equipment list repeats a few columns of them next to the
# model name, which is exactly what the trigram index matches best.  Questions
# carrying these words therefore need the authority tier in the candidate pool.
SPEC_TERMS = (
    "尺寸","净重","重量","功率","功耗","电压","电流","保险丝","温湿度","温度",
    "湿度","气压","海拔","环境","空间","承重","安装","声压","噪音","通量",
    "维护","保养","清洁","清洗","校准",
)
AUTHORITY_PREFIXES = ("09_软件工具与说明书/", "10_试剂盒说明书/", "11_仪器平台说明书/")
AUTHORITY_PRIOR = 1.8
CATALOG_PRIOR = 0.6


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


def specification_terms(question):
    """Spec words the question actually carries, in canonical order."""
    return [term for term in SPEC_TERMS if term in question]


def authority_scores(db, probes, spec_terms):
    """Score manuals by how many spec words their own body carries.

    The index tokenizes with trigrams, so a two-character word such as 尺寸 can
    never match on its own; the manual is reached through the model identifier
    instead and then ranked by the spec vocabulary inside the chunk body.
    """
    prefixes = " OR ".join("s.path LIKE ?" for _ in AUTHORITY_PREFIXES)
    sql = (
        "SELECT s.id, c.text FROM search_fts JOIN chunks c ON c.id=search_fts.rowid"
        " JOIN sources s ON s.id=c.source_id"
        " WHERE search_fts MATCH ? AND s.path NOT LIKE '%/_已归档/%'"
        " AND (" + prefixes + ")"
        " AND coalesce(json_extract(s.source_context,'$.source_kind_hint'),'')"
        " != 'analysis_result_candidate'"
        " ORDER BY bm25(search_fts) LIMIT 60"
    )
    like = [prefix + "%" for prefix in AUTHORITY_PREFIXES]
    scores = {}
    for term, weight in probes:
        best = {}
        for sid, text in db.execute(sql, ('"' + term + '"', *like)):
            matched = sum(1 for word in spec_terms if word in text)
            if matched > best.get(sid, -1):
                best[sid] = matched
        for sid, matched in best.items():
            scores[sid] = scores.get(sid, 0.0) + weight * (1 + matched)
    return scores


def shortlist(catalog, question, limit=20):
    exact, chinese = query_terms(question)
    totals={}
    document_version = re.search(
        r"(?<![A-Za-z0-9])(?P<id>SOP-[A-Z]+-[0-9]+)[ _-]+(?P<version>[A-Z][0-9]+)(?![A-Za-z0-9])",
        question, re.I,
    )
    spec_terms = specification_terms(question)
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
            phrase='"' + term + '"'
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
        if spec_terms:
            probes = [(term,1.0) for term in exact[:3]] + [(term,0.5) for term in chinese[:5]]
            for sid, score in authority_scores(db, probes, spec_terms).items():
                totals[sid]=totals.get(sid,0)+score
        if not totals:
            return []
        placeholders=",".join("?" for _ in totals)
        rows=db.execute(
            "SELECT id,path,sha256 FROM sources WHERE id IN ("+placeholders+")",
            list(totals),
        ).fetchall()
    ranked=[]
    for sid,path,sha in rows:
        score=totals.get(sid,0.0)
        if spec_terms:
            if path.startswith(AUTHORITY_PREFIXES):
                score*=AUTHORITY_PRIOR
            elif path.startswith("07_"):
                score*=CATALOG_PRIOR
        ranked.append((-score,sid,sha))
    ranked.sort()
    chosen=[]
    seen_content=set()
    for _,sid,sha in ranked:
        key=sha or sid
        if key in seen_content:
            continue
        seen_content.add(key)
        chosen.append(sid)
        if len(chosen)>=limit:
            break
    return chosen


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
