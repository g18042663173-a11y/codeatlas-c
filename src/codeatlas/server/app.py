"""M8 FastAPI 服务。本地优先、单用户，只绑 127.0.0.1。"""
from __future__ import annotations

import os
import json
from contextvars import ContextVar
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import db as dbm
from .. import agent as agent_runner
from .. import conversation
from ..experience import store as exp_store
from ..graph import structure, traverse
from ..indexer.build import get_embedder
from ..llm.client import capabilities as llm_capabilities
from ..llm.client import get_client
from ..retrieve import engine
from ..wiki import generator as wiki_generator

DB = os.environ.get("CODEATLAS_DB", "data/kb.db")
DATA_DIR = os.environ.get("CODEATLAS_DATA", "data")
EMBEDDER = os.environ.get("CODEATLAS_EMBEDDER", "tfidf")

app = FastAPI(title="CodeAtlas", version="0.1.0")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
_emb = None
_query_context = ContextVar("http_knowledge_set", default=None)
_read_request = ContextVar("http_read_only", default=False)


@app.on_event("startup")
def prepare_database():
    # Schema upgrades happen once at startup, not on retrieval or review reads.
    connection = dbm.connect(DB)
    connection.close()


@app.middleware("http")
async def pin_query_snapshot(request, call_next):
    _read_request.set(request.method == "GET")
    path = request.url.path
    read = (path in ("/api/ask", "/api/stats", "/api/impact", "/api/source/read",
                     "/api/evaluation/latest", "/api/capabilities", "/api/structure")
            or path.startswith(("/api/wiki/", "/api/symbol/", "/api/graph/", "/api/structure/")))
    if not read:
        return await call_next(request)
    control = dbm.read_only(DB)
    try:
        if dbm.get_meta(control, "snapshot_enabled") != "1":
            return await call_next(request)
        from ..snapshots import pin, SnapshotError
        try:
            with pin(control) as context:
                context.control = control
                token = _query_context.set(context)
                try:
                    response = await call_next(request)
                    response.headers["X-CodeAtlas-Knowledge-Set"] = context.knowledge_set_id
                    return response
                finally:
                    _query_context.reset(token)
        except SnapshotError as exc:
            return JSONResponse(status_code=503, content={"status": "not_ready", "detail": str(exc)})
    finally:
        control.close()
PROJECT_ROOT = Path(__file__).resolve().parents[3]
# The browser can import only these reviewed, public synthetic assets.  The CLI
# retains its explicit file-path import for maintainers, but the presentation UI
# never accepts an arbitrary path from a form field.
PUBLIC_SESSION_EXAMPLES = {
    "cjson-nesting-review": {
        "path": PROJECT_ROOT / "examples/conversations/cjson-nesting-review.json",
        "title": "深层嵌套 JSON 的公开审查",
        "description": "定位递归入口、深度限制与失败返回；演示一条经验如何经过人工确认后变得可用。",
        "question": "审查 parse_value 与 cJSON_ParseWithOpts 对深层嵌套 JSON 的失败处理和影响范围",
    },
    "cjson-insufficient-review": {
        "path": PROJECT_ROOT / "examples/conversations/cjson-insufficient-review.json",
        "title": "证据不足的知识卡拒绝",
        "description": "缺少函数、输入与固定版本时，不生成可批准知识卡。",
        "question": "解析偶尔出错，但没有函数名、输入样例或固定版本",
    },
    "cjson-unrelated-request": {
        "path": PROJECT_ROOT / "examples/conversations/cjson-unrelated-request.json",
        "title": "无关问题的拒答",
        "description": "与固定 cJSON 快照无关的问题，只返回证据不足。",
        "question": "如何轮换 Kubernetes Ingress TLS 证书",
    },
    "lwip-netif-review": {
        "path": PROJECT_ROOT / "examples/conversations/lwip-netif-review.json",
        "title": "netif_add 初始化路径审查",
        "description": "审查公开 lwIP 接口初始化、状态关系与 certain 影响范围。",
        "question": "审查 netif_add 的初始化路径和接口状态影响范围",
    },
    "lwip-insufficient-review": {
        "path": PROJECT_ROOT / "examples/conversations/lwip-insufficient-review.json",
        "title": "证据不足的网络问题",
        "description": "缺少抓包、配置、输入和函数锚点时，不允许提炼知识卡。",
        "question": "网络偶尔断开，但没有可复现输入或函数锚点",
    },
    "lwip-unrelated-request": {
        "path": PROJECT_ROOT / "examples/conversations/lwip-unrelated-request.json",
        "title": "超出 lwIP 范围的拒答",
        "description": "数据库问题不属于 lwIP 代码证据范围，应直接拒答。",
        "question": "如何优化 PostgreSQL 慢查询",
    },
}


def conn():
    context = _query_context.get()
    if context is not None:
        return context.conn
    return dbm.read_only(DB) if _read_request.get() else dbm.open_existing(DB)


def embedder():
    context = _query_context.get()
    if context is not None:
        return get_embedder("local", str(context.directory))
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


class AgentReq(BaseModel):
    q: str
    session_id: str | None = None
    save_draft: bool = False
    mode: str = "auto"
    use_llm: bool | None = None
    read_mode: str = "progressive"


class BindReq(BaseModel):
    usr: str
    reviewer: str = "local-reviewer"
    relation: str = "explains"


class GoalReq(BaseModel):
    goal: str


class CandidateReq(BaseModel):
    action: str
    payload: dict | None = None
    reason: str = ""


class WikiSectionReq(BaseModel):
    page_id: str
    section: str


class CodeReadReq(BaseModel):
    usr: str | None = None
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None


class QaReq(BaseModel):
    question: str
    expected_points: list[str]
    evidence_tags: list[str]


class QaReviewReq(BaseModel):
    passed: bool
    note: str
    reviewer: str = "local-reviewer"
    reviewer_type: str = "human"
    evaluation_scope: str = "formal"
    model: str | None = None
    agent_id: str | None = None
    prompt_hash: str | None = None
    input_hash: str | None = None


class SupersedeReq(BaseModel):
    prior_id: str


class AttachExperimentsReq(BaseModel):
    paths: list[str]


class DependenciesReq(BaseModel):
    entity_names: list[str] = Field(default_factory=list)
    macro_names: list[str] = Field(default_factory=list)
    call_depth: int = 0


class FeedbackReq(BaseModel):
    verdict: str
    evidence_tag: str | None = None
    comment: str = ""


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
                WHERE l.node_id=? AND l.confidence='certain' AND ex.status='approved'
                  AND COALESCE(ex.artifact_scope,'formal')=COALESCE(
                    (SELECT value FROM meta WHERE key='evaluation_scope'),'formal')
                  AND EXISTS (SELECT 1 FROM experience_anchor a
                               WHERE a.exp_id=ex.id AND a.active=1)""", (row["id"],)).fetchall()
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


@app.get("/api/structure")
def api_structure():
    return structure.repo_outline(conn())


@app.get("/api/structure/file")
def api_structure_file(path: str):
    try:
        return structure.file_card(conn(), path)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


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
                           status=r["status"], input_hash=r["input_hash"],
                           repository=r["repository"], revision=r["revision"],
                           updated_at=r["updated_at"])
                      for r in c.execute(
                          """SELECT id,level,title,status,input_hash,repository,revision,updated_at
                               FROM wiki_page ORDER BY level,id""")]}


@app.get("/api/wiki/status")
def api_wiki_status():
    c = conn()
    rows = c.execute(
        "SELECT level,status,COUNT(*) c FROM wiki_page GROUP BY level,status ORDER BY level,status"
    ).fetchall()
    return {
        "repository": dbm.get_meta(c, "repository_id", dbm.get_meta(c, "repo", "unversioned")),
        "revision": dbm.get_meta(c, "revision", "unversioned"),
        "counts": [dict(row) for row in rows],
        "last_updated": c.execute("SELECT MAX(updated_at) FROM wiki_page").fetchone()[0],
    }


@app.get("/api/wiki/outline")
def api_wiki_outline(module: str | None = None):
    return wiki_generator.outline(conn(), module)


@app.post("/api/wiki/section")
def api_wiki_section(req: WikiSectionReq):
    result = wiki_generator.section(conn(), req.page_id, req.section)
    if result.get("error"):
        raise HTTPException(404, result["error"])
    return result


@app.post("/api/source/read")
def api_source_read(req: CodeReadReq):
    try:
        return agent_runner._code_read(conn(), req.model_dump(exclude_none=True))
    except (agent_runner.AgentError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/wiki/{page_id:path}")
def api_wiki_page(page_id: str):
    r = conn().execute("SELECT * FROM wiki_page WHERE id=?", (page_id,)).fetchone()
    if not r:
        raise HTTPException(404, "页面不存在")
    return dict(r)


@app.get("/api/experience/pending")
def api_pending():
    c = conn()
    return {"items": [exp_store.get(c, row["id"]) for row in exp_store.pending(c)]}


@app.get("/api/cards")
def api_cards():
    """Read the local card audit trail; approval remains a separate explicit action."""
    c = conn()
    rows = c.execute(
        """SELECT id FROM experience
             WHERE status IN ('pending','approved','rejected','stale','superseded')
             ORDER BY updated_at DESC, id DESC LIMIT 100"""
    ).fetchall()
    return {"items": [_card_view(c, row["id"]) for row in rows]}


def _card_view(c, exp_id):
    """Add a derived reading view; never change the approved publication bundle."""
    from ..eval.knowledge_production import formal_card_answer_view
    card = exp_store.get(c, exp_id)
    if card.get("status") == "approved" and card.get("artifact_scope", "formal") == "formal":
        messages = [dict(row) for row in c.execute(
            "SELECT content FROM conversation_message WHERE session_id=? ORDER BY ordinal",
            (card.get("session_id"),))]
        card["answer_view"] = formal_card_answer_view(card, messages)
    return card


@app.get("/api/session/examples")
def api_session_examples():
    c = conn()
    repository = dbm.get_meta(c, "repository_id", dbm.get_meta(c, "repo", ""))
    revision = dbm.get_meta(c, "revision", "unversioned")

    items = []
    for example_id, item in PUBLIC_SESSION_EXAMPLES.items():
        try:
            session = conversation.load_public_session(item["path"])
        except conversation.ConversationError:
            continue
        if session["repository"] == repository and session["revision"] == revision:
            items.append({
                "id": example_id, "title": item["title"],
                "description": item["description"], "question": item["question"],
            })
    return {"items": items}


@app.post("/api/session/examples/{example_id}/import")
def api_session_example_import(example_id: str):
    item = PUBLIC_SESSION_EXAMPLES.get(example_id)
    if item is None:
        raise HTTPException(404, "未知公开示例")
    try:
        return {**conversation.import_public_session(conn(), item["path"]),
                "example_id": example_id, "question": item["question"]}
    except conversation.ConversationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/sessions")
def api_sessions():
    return {"items": conversation.list_sessions(conn())}


@app.get("/api/session/{session_id}")
def api_session(session_id: str):
    try:
        return conversation.get_session(conn(), session_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/session/{session_id}/assess")
def api_session_assess(session_id: str):
    try:
        return conversation.assess_session(conn(), session_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/session/{session_id}/goal")
def api_session_goal(session_id: str, req: GoalReq):
    try:
        return conversation.set_curation_goal(conn(), session_id, req.goal)
    except (KeyError, conversation.ConversationError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/session/{session_id}/curate")
def api_session_curate(session_id: str):
    try:
        return {"items": conversation.propose_candidates(conn(), session_id)}
    except (KeyError, conversation.ConversationError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/candidate/{candidate_id}/decide")
def api_candidate_decide(candidate_id: str, req: CandidateReq):
    try:
        return conversation.decide_candidate(
            conn(), candidate_id, req.action, payload=req.payload, reason=req.reason,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, conversation.ConversationError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/agent/run")
def api_agent_run(req: AgentReq):
    requested_mode = req.mode
    if req.use_llm is not None:  # v1 compatibility
        requested_mode = "model" if req.use_llm else "rule"
    try:
        result = agent_runner.run(
            conn(), req.q, data_dir=DATA_DIR, embedder=embedder(), session_id=req.session_id,
            llm_client=(get_client() if requested_mode != "rule" else None),
            save_draft=req.save_draft, mode=requested_mode, read_mode=req.read_mode,
        )
    except (agent_runner.AgentError, KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    result["evidence"].pop("context", None)
    result["evidence"].pop("evidence_pack", None)
    return result


@app.get("/api/agent/{run_id}")
def api_agent_get(run_id: str):
    try:
        return agent_runner.get_run(conn(), run_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/agent/run/{run_id}/feedback")
def api_agent_feedback(run_id: str, req: FeedbackReq):
    try:
        return agent_runner.add_feedback(
            conn(), run_id, req.verdict, evidence_tag=req.evidence_tag,
            comment=req.comment,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (agent_runner.AgentError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/feedback")
def api_feedback(limit: int = Query(100, ge=1, le=500)):
    return {"items": agent_runner.list_feedback(conn(), limit=limit)}


@app.post("/api/agent/{run_id}/draft")
def api_agent_save_draft(run_id: str):
    try:
        return agent_runner.save_draft(conn(), run_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (agent_runner.AgentError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/card/{exp_id}")
def api_card(exp_id: str):
    try:
        return _card_view(conn(), exp_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/card/{exp_id}/bind")
def api_card_bind(exp_id: str, req: BindReq):
    try:
        return exp_store.bind(conn(), exp_id, req.usr, bound_by=req.reviewer,
                              relation=req.relation)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/card/{exp_id}/supersede")
def api_card_supersede(exp_id: str, req: SupersedeReq):
    try:
        return exp_store.link_supersedes(conn(), exp_id, req.prior_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/card/{exp_id}/experiments")
def api_card_experiments(exp_id: str, req: AttachExperimentsReq):
    """Bind only repository-owned public reproduction assets to a review bundle."""
    try:
        return {"items": exp_store.attach_experiments(conn(), exp_id, req.paths)}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/card/{exp_id}/dependencies")
def api_card_dependencies(exp_id: str, req: DependenciesReq):
    """Replace a pending card's dependency declaration and invalidate old QA."""
    try:
        return exp_store.set_dependencies(
            conn(), exp_id, entity_names=req.entity_names,
            macro_names=req.macro_names, call_depth=req.call_depth,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/card/stale-check")
def api_card_stale_check():
    return exp_store.stale_check(conn())


@app.post("/api/card/{exp_id}/qa")
def api_card_qa(exp_id: str, req: QaReq):
    try:
        return exp_store.qa_add(conn(), exp_id, req.question, req.expected_points,
                                req.evidence_tags)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/card/qa/{qa_id}/review")
def api_card_qa_review(qa_id: int, req: QaReviewReq):
    try:
        return exp_store.qa_review(
            conn(), qa_id, req.passed, req.note, req.reviewer,
            reviewer_type=req.reviewer_type, evaluation_scope=req.evaluation_scope,
            model=req.model, agent_id=req.agent_id, prompt_hash=req.prompt_hash,
            input_hash=req.input_hash,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/cards/consistency")
def api_card_consistency():
    return exp_store.consistency(conn())


class ReviewReq(BaseModel):
    action: str
    note: str = ""
    expected_bundle_hash: str | None = None
    reviewer: str = "local-reviewer"
    reviewer_type: str = "human"
    evaluation_scope: str = "formal"
    model: str | None = None
    agent_id: str | None = None
    prompt_hash: str | None = None
    input_hash: str | None = None


class ConfirmReviewReq(BaseModel):
    bundle_hash: str
    reviewer: str = "local-reviewer"


@app.post("/api/card/{exp_id}/confirm")
def api_card_confirm(exp_id: str, req: ConfirmReviewReq):
    try:
        return exp_store.confirm_review(
            conn(), exp_id, req.bundle_hash, req.reviewer, reviewer_type="human"
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409 if "review_conflict" in str(exc) else 400, str(exc)) from exc


@app.post("/api/experience/{exp_id}/review")
def api_review(exp_id: str, req: ReviewReq):
    try:
        if req.action == "approve" and not req.expected_bundle_hash:
            raise HTTPException(409, "请重新读取当前审核材料及版本，再确认批准")
        return exp_store.review(
            conn(), exp_id, req.action, req.note, reviewer=req.reviewer,
            expected_bundle_hash=req.expected_bundle_hash,
            reviewer_type=req.reviewer_type, evaluation_scope=req.evaluation_scope,
            model=req.model, agent_id=req.agent_id, prompt_hash=req.prompt_hash,
            input_hash=req.input_hash,
        )
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409 if "review_conflict" in str(e) else 400, str(e))


@app.get("/api/stats")
def api_stats():
    c = conn()
    q = lambda s: c.execute(s).fetchone()[0]
    context = _query_context.get()
    governance = context.control if context else c
    g = lambda sql: governance.execute(sql).fetchone()[0]
    card_counts = {status: g(f"SELECT COUNT(*) FROM experience WHERE status='{status}'")
                   for status in ("draft", "pending", "approved", "rejected", "stale", "superseded")}
    return {
        "repo": dbm.get_meta(c, "repo"),
        "repository": dbm.get_meta(c, "repository_id", dbm.get_meta(c, "repo", "unversioned")),
        "revision": dbm.get_meta(c, "revision", "unversioned"),
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
        "cards": card_counts,
        "sessions": g("SELECT COUNT(*) FROM conversation_session"),
        "agent_runs": g("SELECT COUNT(*) FROM agent_run"),
        "wiki_pages": q("SELECT COUNT(*) FROM wiki_page WHERE status='ok'"),
        "llm": get_client() is not None,
        "model_capability": llm_capabilities(),
        "knowledge_set_id": dbm.get_meta(c, "knowledge_set_id"),
        "source_content_hash": dbm.get_meta(c, "source_content_hash"),
    }


@app.get("/api/snapshot/status")
def api_snapshot_status():
    from ..snapshots import status
    control = dbm.read_only(DB)
    try:
        return status(control)
    finally:
        control.close()


@app.get("/api/capabilities")
def api_capabilities():
    c = conn()
    return {
        "model": llm_capabilities(), "embedder": EMBEDDER,
        "repository": dbm.get_meta(c, "repository_id", dbm.get_meta(c, "repo", "unversioned")),
        "revision": dbm.get_meta(c, "revision", "unversioned"),
        "agent_modes": ["auto", "model", "rule"],
        "tools": sorted(agent_runner.ALLOWED_TOOLS),
        "max_tool_calls": agent_runner.MAX_TOOL_CALLS,
    }


@app.get("/api/evaluation/latest")
def api_evaluation_latest():
    """Expose only locally generated, snapshot-matched evaluation sidecars."""
    c = conn()
    repository = dbm.get_meta(c, "repository_id", dbm.get_meta(c, "repo", "unversioned"))
    revision = dbm.get_meta(c, "revision", "unversioned")
    from ..eval.acceptance_eval import implementation_hash
    current_set = dbm.get_meta(c, "knowledge_set_id")
    stale_reports = []

    def read_json(path: Path) -> dict | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    workflow_candidates = [
        read_json(PROJECT_ROOT / "docs" / "WORKFLOW-EVAL-CJSON.json"),
        read_json(PROJECT_ROOT / "docs" / "WORKFLOW-EVAL-LWIP.json"),
        read_json(Path(DATA_DIR) / "portfolio-demo" / "WORKFLOW-EVAL.json"),
    ]
    workflow_raw = next((raw for raw in workflow_candidates if raw
                         and raw.get("repository") == repository
                         and raw.get("revision") == revision), None)
    coverage_raw = read_json(PROJECT_ROOT / "docs" / "VALUE-PROOF-COVERAGE.json")
    coverage = None
    if (coverage_raw and coverage_raw.get("kind") == "value_proof_coverage_inventory"
            and isinstance(coverage_raw.get("corpora"), list)
            and any(
                item.get("repository") == repository and item.get("revision") == revision
                for item in coverage_raw["corpora"] if isinstance(item, dict)
            )):
        from ..eval import value_proof
        try:
            value_proof.validate_inventory_integrity(coverage_raw)
        except value_proof.ValueProofError:
            stale_reports.append("value-proof-coverage:hash_mismatch")
        else:
            coverage = {
                "status": coverage_raw.get("status"),
                "coverage_ready": coverage_raw.get("coverage_ready"),
                "inventory_hash": coverage_raw.get("inventory_hash"),
                "target_mechanisms_per_repository": coverage_raw.get(
                    "target_mechanisms_per_repository"),
                "corpora": [{key: item.get(key) for key in (
                    "id", "automatic_candidate_count", "target_count",
                    "selected_count", "status")}
                    for item in coverage_raw["corpora"] if isinstance(item, dict)],
                "boundary": coverage_raw.get("boundary"),
            }
    acceptance_raw = read_json(PROJECT_ROOT / "docs" / "ACCEPTANCE-CJSON-LWIP.json")
    if (acceptance_raw and current_set and (acceptance_raw.get("codeatlas") or {}).get("implementation_hash")
            != implementation_hash(PROJECT_ROOT)):
        stale_reports.append("acceptance:implementation_changed")
        acceptance_raw = None
    if acceptance_raw and current_set:
        matched = next((item.get("database") or {} for item in acceptance_raw.get("corpora", [])
                        if isinstance(item, dict) and (item.get("database") or {}).get("repository") == repository), {})
        if matched.get("knowledge_set_id") != current_set:
            stale_reports.append("acceptance:knowledge_set_changed")
            acceptance_raw = None
    if current_set and acceptance_raw is None:
        workflow_raw = None
    acceptance = None
    if (acceptance_raw and acceptance_raw.get("schema_version") == 1
            and isinstance(acceptance_raw.get("corpora"), list)
            and isinstance(acceptance_raw.get("claims"), list)
            and any(
                (item.get("database") or {}).get("repository") == repository
                and (item.get("database") or {}).get("revision") == revision
                for item in acceptance_raw["corpora"] if isinstance(item, dict)
            )):
        acceptance = {
            "mode": acceptance_raw.get("mode"), "passed": acceptance_raw.get("passed"),
            "foundation_passed": acceptance_raw.get("foundation_passed"),
            "task_complete": acceptance_raw.get("task_complete"),
            "legacy_task_complete": acceptance_raw.get(
                "legacy_task_complete", acceptance_raw.get("task_complete")),
            "current_release_complete": acceptance_raw.get("current_release_complete"),
            "experiments_complete": acceptance_raw.get("experiments_complete"),
            "reviews_complete": acceptance_raw.get("reviews_complete"),
            "claims_complete": acceptance_raw.get("claims_complete"),
            "judge_qualification": acceptance_raw.get("judge_qualification"),
            "portfolio_ready": acceptance_raw.get("portfolio_ready"),
            "engineer_pass": acceptance_raw.get("engineer_pass"),
            "human_release": acceptance_raw.get("human_release"),
            "effect_observed": acceptance_raw.get("effect_observed"),
            "public_repro": acceptance_raw.get("public_repro"),
            "proof": acceptance_raw.get("proof"),
            "publication": acceptance_raw.get("publication"),
            "corpora": acceptance_raw["corpora"], "claims": acceptance_raw["claims"],
            "model_abc": acceptance_raw.get("model_abc"),
            "generated_at": acceptance_raw.get("generated_at"),
        }
    workflow = None
    if (workflow_raw and workflow_raw.get("repository") == repository
            and workflow_raw.get("revision") == revision
            and isinstance(workflow_raw.get("metrics"), dict)
            and isinstance(workflow_raw.get("cases"), list)):
        workflow = {
            "metrics": workflow_raw["metrics"], "cases": workflow_raw["cases"],
            "generated_at": workflow_raw.get("generated_at"),
        }

    corpus = "CJSON" if repository.rstrip("/").endswith("/cJSON") else (
        "LWIP" if repository.rstrip("/").endswith("/lwip") else None
    )
    task_eval = None
    model_eval = None
    model_abc = None
    if corpus:
        task_raw = read_json(PROJECT_ROOT / "docs" / f"TASK-EVAL-{corpus}.json")
        manifest = (task_raw or {}).get("manifest") or {}
        if task_raw and current_set and task_raw.get("knowledge_set_id") != current_set:
            stale_reports.append("tasks:knowledge_set_changed")
            task_raw, manifest = None, {}
        if (manifest.get("repository") == repository and manifest.get("revision") == revision
                and isinstance((task_raw or {}).get("summary"), dict)):
            task_eval = {
                "name": manifest.get("name"), "summary": task_raw["summary"],
                "acceptance": task_raw.get("acceptance"),
                "generated_at": task_raw.get("generated_at"),
            }
        model_raw = read_json(PROJECT_ROOT / "docs" / f"MODEL-EVAL-{corpus}.json")
        from ..eval import rubric
        task_path = PROJECT_ROOT / "eval" / f"tasks_{corpus.lower()}.yaml"
        def model_matches(raw):
            if raw is None:
                return False
            matches = task_path.is_file() and raw.get("identity") == rubric.identity(c, task_path)
            if not matches:
                stale_reports.append("model:identity_changed_or_missing")
            return matches
        if (model_matches(model_raw) and model_raw.get("repository") == repository
                and model_raw.get("revision") == revision
                and isinstance(model_raw.get("metrics"), dict)):
            model_eval = {
                "model": model_raw.get("model"), "metrics": model_raw["metrics"],
                "acceptance": model_raw.get("acceptance"),
                "generated_at": model_raw.get("generated_at"),
            }
        abc_raw = read_json(PROJECT_ROOT / "docs" / f"MODEL-ABC-{corpus}.json")
        if (model_matches(abc_raw) and abc_raw.get("repository") == repository
                and abc_raw.get("revision") == revision
                and isinstance(abc_raw.get("variants"), dict)):
            model_abc = {
                "model": abc_raw.get("model"), "variants": abc_raw["variants"],
                "runs_per_task": abc_raw.get("runs_per_task"),
                "acceptance": abc_raw.get("acceptance"),
                "generated_at": abc_raw.get("generated_at"),
            }

    if not acceptance and not workflow and not task_eval and not model_eval and not model_abc and not coverage:
        return {"available": False, "stale_reports": stale_reports,
                "message": "尚未生成当前代码快照的受控评测报告，或旧报告已过期；请运行 demo.sh 后刷新。"}
    result = {
        "available": True, "repository": repository, "revision": revision,
        "workflow": workflow, "task_eval": task_eval, "model_eval": model_eval,
        "model_abc": model_abc, "acceptance": acceptance, "coverage": coverage,
        "stale_reports": stale_reports,
    }
    # Preserve the previous flat workflow shape for existing local clients.
    if workflow:
        result.update(workflow)
    return result


@app.get("/", response_class=HTMLResponse)
def index():
    f = Path(__file__).parent / "static" / "index.html"
    return f.read_text(encoding="utf-8")
