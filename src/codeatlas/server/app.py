"""M8 FastAPI 服务。本地优先、单用户，只绑 127.0.0.1。"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .. import db as dbm
from ..experience import store as exp_store
from ..graph import traverse
from ..indexer.build import get_embedder
from ..llm.client import get_client
from ..retrieve import engine

DB = os.environ.get("CODEATLAS_DB", "data/kb.db")
DATA_DIR = os.environ.get("CODEATLAS_DATA", "data")
EMBEDDER = os.environ.get("CODEATLAS_EMBEDDER", "tfidf")

app = FastAPI(title="CodeAtlas", version="0.1.0")
_emb = None


def conn():
    return dbm.connect(DB)


def embedder():
    global _emb
    if _emb is None:
        _emb = get_embedder(EMBEDDER, DATA_DIR)
    return _emb


class AskReq(BaseModel):
    q: str
    budget: int = 8000
    hops: int = 2
    use_graph: bool = True
    use_vector: bool = True


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/ask")
def api_ask(req: AskReq):
    c = conn()
    r = engine.ask(c, req.q, embedder=embedder() if req.use_vector else None,
                   budget_tokens=req.budget, hops=req.hops, data_dir=DATA_DIR,
                   use_graph=req.use_graph, use_vector=req.use_vector,
                   llm_client=get_client())
    r.pop("context", None)
    return r


@app.get("/api/symbol/{name}")
def api_symbol(name: str):
    c = conn()
    rows = traverse.resolve_symbol(c, name)
    if not rows:
        raise HTTPException(404, f"未找到符号 {name}")
    out = []
    for row in rows[:5]:
        callees = traverse.direct_edges(c, row["id"], direction="out")
        callers = traverse.direct_edges(c, row["id"], direction="in")
        cand = traverse.direct_edges(c, row["id"], direction="out", confidence="candidate")
        exps = c.execute(
            """SELECT ex.id, ex.title, ex.root_cause FROM experience ex
                 JOIN experience_link l ON l.exp_id = ex.id
                WHERE l.node_id=? AND ex.status='approved'""", (row["id"],)).fetchall()
        out.append(dict(
            **{k: row[k] for k in row.keys()},
            callees=[dict(name=x["name"], path=x["path"]) for x in callees],
            callers=[dict(name=x["name"], path=x["path"]) for x in callers],
            candidate_callees=[dict(name=x["name"], reason=x["reason"]) for x in cand],
            experiences=[dict(id=e["id"], title=e["title"]) for e in exps]))
    return {"matches": out}


@app.get("/api/impact")
def api_impact(symbol: str, depth: int = Query(3, ge=1, le=6)):
    c = conn()
    rows = traverse.resolve_symbol(c, symbol)
    if not rows:
        raise HTTPException(404, f"未找到符号 {symbol}")
    r = traverse.impact(c, rows[0]["id"], max_hop=depth)
    r["symbol"] = dict(rows[0]) if rows[0] else None
    r["by_hop"] = {str(k): v for k, v in r["by_hop"].items()}
    return r


@app.get("/api/graph/callchain")
def api_callchain(symbol: str, depth: int = 2, direction: str = "out"):
    c = conn()
    rows = traverse.resolve_symbol(c, symbol)
    if not rows:
        raise HTTPException(404, f"未找到符号 {symbol}")
    root = rows[0]
    hops = traverse.neighbors(c, root["id"], direction=direction, max_hop=depth)
    nodes = [dict(id=root["id"], name=root["name"], hop=0, path=root["path"])]
    for nid_, hop in hops:
        n = c.execute("SELECT id,name,path FROM node WHERE id=?", (nid_,)).fetchone()
        if n:
            nodes.append(dict(id=n["id"], name=n["name"], hop=hop, path=n["path"]))
    ids = {n["id"] for n in nodes}
    edges = [dict(src=e["src"], dst=e["dst"], confidence=e["confidence"])
             for e in c.execute(
                 "SELECT src,dst,confidence FROM edge WHERE kind='calls'")
             if e["src"] in ids and e["dst"] in ids]
    return {"nodes": nodes, "edges": edges}


@app.get("/api/wiki/tree")
def api_wiki_tree():
    c = conn()
    return {"pages": [dict(id=r["id"], level=r["level"], title=r["title"],
                           status=r["status"])
                      for r in c.execute(
                          "SELECT id,level,title,status FROM wiki_page ORDER BY level,id")]}


@app.get("/api/wiki/{page_id:path}")
def api_wiki_page(page_id: str):
    r = conn().execute("SELECT * FROM wiki_page WHERE id=?", (page_id,)).fetchone()
    if not r:
        raise HTTPException(404, "页面不存在")
    return dict(r)


@app.get("/api/experience/pending")
def api_pending():
    return {"items": [dict(r) for r in exp_store.pending(conn())]}


class ReviewReq(BaseModel):
    action: str
    note: str = ""


@app.post("/api/experience/{exp_id}/review")
def api_review(exp_id: str, req: ReviewReq):
    try:
        return exp_store.review(conn(), exp_id, req.action, req.note)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/stats")
def api_stats():
    c = conn()
    q = lambda s: c.execute(s).fetchone()[0]
    return {
        "repo": dbm.get_meta(c, "repo"),
        "nodes": q("SELECT COUNT(*) FROM node"),
        "functions": q("SELECT COUNT(*) FROM node WHERE kind='function' AND is_definition=1"),
        "edges": q("SELECT COUNT(*) FROM edge"),
        "edges_certain": q("SELECT COUNT(*) FROM edge WHERE confidence='certain'"),
        "edges_candidate": q("SELECT COUNT(*) FROM edge WHERE confidence='candidate'"),
        "summary_heads": q("SELECT COUNT(*) FROM summary_head"),
        "head_avg_token": round(q("SELECT COALESCE(AVG(token_len),0) FROM summary_head"), 1),
        "chunks_visible": q("SELECT COUNT(*) FROM chunk WHERE visible=1"),
        "exp_pending": q("SELECT COUNT(*) FROM experience WHERE status='pending'"),
        "exp_approved": q("SELECT COUNT(*) FROM experience WHERE status='approved'"),
        "wiki_pages": q("SELECT COUNT(*) FROM wiki_page WHERE status='ok'"),
        "llm": get_client() is not None,
    }


@app.get("/", response_class=HTMLResponse)
def index():
    f = Path(__file__).parent / "static" / "index.html"
    return f.read_text(encoding="utf-8")
