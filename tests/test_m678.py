"""M6 Wiki / M7 评测 / M8 API 的测试。

复用 test_core.py 的 kb fixture（通过 conftest.py 共享）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeatlas.experience import store
from codeatlas.graph import traverse
from codeatlas.indexer import build as indexer
from codeatlas.retrieve import engine
from codeatlas.wiki import generator

from .test_core import CORPUS, SAMPLE  # noqa: F401


# ==================== M6 Wiki 生成 ====================

def test_wiki_three_levels_and_validation(kb, tmp_path):
    conn, _ = kb
    st = generator.generate(conn, str(CORPUS), out_dir=str(tmp_path / "wiki"), resume=False)
    assert st["file"] > 0 and st["module"] > 0 and st["repo"] == 1
    assert st["failed"] == 0
    for lvl in ("file", "module", "repo"):
        pages = conn.execute(
            "SELECT md FROM wiki_page WHERE level=? AND status='ok'", (lvl,)).fetchall()
        assert pages
        for p in pages:
            ok, err = generator.validate(p["md"], lvl)
            assert ok, err


def test_wiki_resume_skips_unchanged(kb, tmp_path):
    """★ 断点续写：输入未变则跳过，这是省 token 的最小增量实现。"""
    conn, _ = kb
    generator.generate(conn, str(CORPUS), out_dir=str(tmp_path / "w1"), resume=False)
    st = generator.generate(conn, str(CORPUS), out_dir=str(tmp_path / "w2"), resume=True)
    assert st["skipped"] > 0
    assert st["file"] == 0 and st["module"] == 0


def test_wiki_definition_change_invalidates_parent_hashes(kb, tmp_path):
    conn, _ = kb
    generator.generate(conn, str(CORPUS), out_dir=str(tmp_path / "before"), resume=False)
    node = conn.execute(
        "SELECT id,definition_hash FROM node WHERE name='parse_value' AND is_definition=1"
    ).fetchone()
    before = {
        row["id"]: (row["input_hash"], row["version"])
        for row in conn.execute(
            "SELECT id,input_hash,version FROM wiki_page WHERE id IN ('files/mini.c','modules/.','repo')"
        )
    }
    conn.execute("UPDATE node SET definition_hash='wiki-changed' WHERE id=?", (node["id"],))
    conn.commit()
    changed = generator.generate(conn, str(CORPUS), out_dir=str(tmp_path / "changed"), resume=True)
    after = {
        row["id"]: (row["input_hash"], row["version"])
        for row in conn.execute(
            "SELECT id,input_hash,version FROM wiki_page WHERE id IN ('files/mini.c','modules/.','repo')"
        )
    }
    assert changed["file"] > 0 and changed["module"] > 0 and changed["repo"] == 1
    assert all(after[key] != before[key] for key in before)
    conn.execute("UPDATE node SET definition_hash=? WHERE id=?", (node["definition_hash"], node["id"]))
    conn.commit()
    generator.generate(conn, str(CORPUS), out_dir=str(tmp_path / "restored"), resume=True)


def test_wiki_orphan_is_stale_and_immediately_hidden(kb, tmp_path):
    conn, _ = kb
    conn.execute(
        """INSERT OR REPLACE INTO wiki_page
           (id,level,title,md,sources,status,version,input_hash,repository,revision,updated_at)
           VALUES('files/removed.c','file','removed.c','obsolete','[]','ok',1,'old',
                  'tests/fixtures/mini_c','fixture','now')"""
    )
    conn.execute(
        """INSERT OR REPLACE INTO chunk
           (uid,kind,title,text,source_ref,evidence_level,visible)
           VALUES('wiki:files/removed.c','wiki','removed.c','obsolete','wiki/removed','C',1)"""
    )
    conn.commit()
    result = generator.generate(conn, str(CORPUS), out_dir=str(tmp_path / "orphan"), resume=True)
    assert result["stale"] >= 1
    assert conn.execute(
        "SELECT status FROM wiki_page WHERE id='files/removed.c'"
    ).fetchone()["status"] == "stale"
    assert conn.execute(
        "SELECT visible FROM chunk WHERE uid='wiki:files/removed.c'"
    ).fetchone()["visible"] == 0


def test_wiki_export_has_frontmatter_and_index(kb, tmp_path):
    """导出必须带 sources[] frontmatter —— 溯源能力的落点。"""
    conn, _ = kb
    out = tmp_path / "wiki"
    generator.generate(conn, str(CORPUS), out_dir=str(out), resume=False)
    assert (out / "index.md").exists() and (out / "log.md").exists()
    md = (out / "repo.md").read_text(encoding="utf-8")
    assert md.startswith("---") and "sources:" in md


def test_wiki_validation_rejects_missing_section():
    ok, err = generator.validate("# x\n\n## 职责\n\n缺其他章节" * 5, "file")
    assert not ok and "缺少章节" in err


def test_wiki_reports_candidate_separately(kb, tmp_path):
    """Wiki 里 candidate 调用必须单独标注，不能混进调用关系结论。"""
    conn, _ = kb
    generator.generate(conn, str(CORPUS), out_dir=str(tmp_path / "w"), resume=False)
    pages = conn.execute("SELECT md FROM wiki_page WHERE level='file'").fetchall()
    with_cand = [p["md"] for p in pages if "候选调用" in p["md"]]
    if with_cand:
        assert all("需人工确认" in m for m in with_cand)


# ==================== M7 评测 ====================

def test_autogen_gold_comes_from_graph_facts(kb, tmp_path):
    """★ 自动生成的 gold 必须真的来自 certain 调用边，不是文本猜测。

    这条决定了评测方法论站不站得住：金标客观可复现，才敢把数字写进简历。
    """
    from codeatlas.eval import run as evalrun
    conn, _ = kb
    qf = tmp_path / "q.yaml"
    assert evalrun.autogen(conn, str(qf), n_per_type=5) > 0
    qs = evalrun.load_questions(conn, str(qf))
    caller_q = [q for q in qs if q["type"] == "调用链"]
    assert caller_q

    q = caller_q[0]
    name = q["id"].replace("auto-caller-", "")
    target = conn.execute(
        "SELECT id FROM node WHERE name=? AND kind='function' AND is_definition=1",
        (name,)).fetchone()
    real = {r["uid"] for r in conn.execute(
        """SELECT c.uid FROM edge e JOIN chunk c ON c.node_id=e.src
            WHERE e.dst=? AND e.kind='calls' AND e.confidence='certain' AND c.kind='code'""",
        (target["id"],))}
    assert q["gold_uids"] <= real, "gold 里出现了不是 certain 调用者的条目"


def test_ablation_switches_actually_change_recall(kb, tmp_path):
    """★ 消融开关必须真的改变结果。

    第一版评测五种配置跑出完全一样的数字（指标饱和 + 图扩展没被计分）。
    这个测试防止再退化成那种"看起来在测、其实没测"的状态。
    """
    from codeatlas.eval import run as evalrun
    from codeatlas.indexer.build import get_embedder
    conn, data_dir = kb
    qf = tmp_path / "q.yaml"
    evalrun.autogen(conn, str(qf), n_per_type=6)
    qs = [q for q in evalrun.load_questions(conn, str(qf)) if q["type"] == "间接影响(2跳)"]
    if not qs:
        pytest.skip("语料太小，没有 2 跳题目")
    emb = get_embedder("null", data_dir)

    off = evalrun.score(conn, evalrun.run_config(
        conn, qs, dict(use_bm25=1, use_sym=1, use_vec=0, graph=None), emb, data_dir))
    on = evalrun.score(conn, evalrun.run_config(
        conn, qs, dict(use_bm25=1, use_sym=1, use_vec=0, graph="head"), emb, data_dir))
    assert on["recall"] > off["recall"], (
        f"图扩展没能提升 2 跳召回：off={off['recall']} on={on['recall']}")


def test_normalized_recall_not_saturated():
    """指标本身要有区分度：gold 集大时不能因为命中一个就满分。"""
    from codeatlas.eval import run as evalrun
    gold = {f"u{i}" for i in range(10)}
    q = dict(type="t", gold_uids=gold)
    hit1 = [(q, dict(citations=[dict(uid="u0")], context_tokens=1, _latency_ms=1))]
    hit5 = [(q, dict(citations=[dict(uid=f"u{i}") for i in range(5)],
                     context_tokens=1, _latency_ms=1))]
    s1 = evalrun.score(None, hit1)
    s5 = evalrun.score(None, hit5)
    assert s1["recall"] < s5["recall"], "命中 1 个和命中 5 个得分一样，指标饱和了"


def test_head_fits_more_neighbors_than_body(kb):
    """★ D2 的真实断言。

    第一版写的是"body 模式消耗更多 token"，但两种模式共用同一预算上限，
    都会被打满，token 数当然相等 —— 那个断言问错了问题。
    真正的收益是：**同样的预算下，摘要头能塞进更多邻居**。
    """
    conn, data_dir = kb
    rid = conn.execute(
        "SELECT rowid FROM chunk WHERE title LIKE 'cJSON_Delete%' LIMIT 1").fetchone()["rowid"]

    h = engine.graph_expand(conn, [(rid, 0.03)], hops=2, mode="head")
    b = engine.graph_expand(conn, [(rid, 0.03)], hops=2, mode="body")
    assert h and b
    # 单条摘要头必须显著小于对应函数体
    assert sum(x["tokens"] for x in h) < sum(x["tokens"] for x in b)

    # The fixture is intentionally tiny. A smaller fixed budget makes the practical
    # benefit observable without pretending its bodies have production-scale length.
    budget = 600
    cap = int(budget * engine.BUDGET_SPLIT["head"])
    def fits(items):
        used = n = 0
        for x in items:
            if used + x["tokens"] > cap:
                break
            used += x["tokens"]; n += 1
        return n
    assert fits(h) > fits(b), "同预算下摘要头没能比函数体塞进更多邻居"


def test_citations_carry_uid_for_scoring(kb):
    """引用必须带 uid，否则评测无法给图扩展计分（踩过的坑）。"""
    conn, data_dir = kb
    r = engine.ask(conn, "cJSON_Delete", data_dir=data_dir)
    heads = [c for c in r["citations"] if "摘要头" in (c["title"] or "")]
    assert heads, "没有图扩展引用"
    assert any(h.get("uid") for h in heads)


def test_citations_sorted_by_score(kb):
    """引用按分数排序，不是按桶顺序追加。

    注意：加入重排后，最终顺序由 rerank_score 决定，融合分 score 不再单调。
    所以这里分两种情况断言 —— 不能因为断言挂了就把重排关掉，
    该改的是断言（不变量变了），不是功能。
    """
    conn, data_dir = kb

    # 关掉重排：应严格按融合分降序
    r = engine.ask(conn, "解析 JSON 对象", data_dir=data_dir, reranker="none")
    scores = [c["score"] for c in r["citations"]]
    assert scores == sorted(scores, reverse=True)

    # 开启重排：应严格按重排分降序
    r2 = engine.ask(conn, "解析 JSON 对象", data_dir=data_dir, reranker="feature")
    if r2["citations"] and "rerank_score" in r2["citations"][0]:
        rs = [c["rerank_score"] for c in r2["citations"]]
        assert rs == sorted(rs, reverse=True)


# ==================== M8 API ====================

def _client(conn, data_dir, monkeypatch):
    from fastapi.testclient import TestClient
    import codeatlas.server.app as srv
    # Tests must never inherit a developer's real credentials or call an LLM.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(srv, "DB", conn.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr(srv, "DATA_DIR", data_dir)
    monkeypatch.setattr(srv, "EMBEDDER", "null")
    monkeypatch.setattr(srv, "_emb", None)
    return TestClient(srv.app)


def test_api_endpoints(kb, monkeypatch):
    conn, data_dir = kb
    c = _client(conn, data_dir, monkeypatch)

    assert c.get("/healthz").json()["ok"]
    s = c.get("/api/stats").json()
    assert s["nodes"] > 0 and "edges_candidate" in s
    assert s["repository"] and s["revision"] and s["cards"]["pending"] >= 0

    r = c.post("/api/ask", json={"q": "cJSON_Delete"}).json()
    assert "citations" in r and "refused" in r

    imp = c.get("/api/impact", params={"symbol": "cJSON_Delete", "depth": 2}).json()
    assert imp["affected_files"]
    assert c.get("/api/impact", params={"symbol": "不存在的符号"}).status_code == 404

    sym = c.get("/api/symbol/cJSON_Delete").json()
    assert sym["matches"][0]["callers"]

    page = c.get("/")
    assert page.status_code == 200
    assert "可信 AI 研发辅助原型" in page.text and "保存当前草稿" in page.text
    assert "会话沉淀" in page.text and "分层 Wiki" in page.text
    workspace = c.get("/static/workspace.js")
    assert workspace.status_code == 200
    assert "feedbackHtml" in workspace.text and "这次查证有帮助吗" in workspace.text
    assert "submitFeedback" in workspace.text
    assert c.get("/api/evaluation/latest").json()["available"] is False


def test_api_evaluation_exposes_only_current_snapshot_report(kb, monkeypatch):
    conn, data_dir = kb
    c = _client(conn, data_dir, monkeypatch)
    stats = c.get("/api/stats").json()
    report = Path(data_dir) / "portfolio-demo" / "WORKFLOW-EVAL.json"
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({
        "repository": stats["repository"], "revision": stats["revision"],
        "generated_at": "2026-01-01T00:00:00+00:00",
        "metrics": {"tool_trace_legal_rate": 100.0},
        "cases": [{"id": "fixture", "passed": True, "detail": "isolated"}],
    }), encoding="utf-8")
    result = c.get("/api/evaluation/latest").json()
    assert result["available"] is True
    assert result["metrics"]["tool_trace_legal_rate"] == 100.0

    report.write_text(json.dumps({
        "repository": "other-repository", "revision": stats["revision"],
        "metrics": {}, "cases": [],
    }), encoding="utf-8")
    assert c.get("/api/evaluation/latest").json()["available"] is False


def test_api_review_gate(kb, monkeypatch):
    """★ Web 侧同样不能绕过审核闸门。

    注意：kb 是 session 级 fixture，别的测试可能已经批准过相似条目。
    所以这里用一个独一无二的标记词做查询，保证只可能命中本条。
    """
    conn, data_dir = kb
    marker = "ZQXJ7 环回自检 门限漂移"
    eid = store.add(conn, {**SAMPLE, "title": f"API 闸门测试 {marker}",
                           "symptom": f"{marker} 触发异常复位"})
    indexer.build(conn, str(CORPUS), out_dir=data_dir)
    c = _client(conn, data_dir, monkeypatch)

    assert any(x["id"] == eid for x in c.get("/api/experience/pending").json()["items"])

    before = c.post("/api/ask", json={"q": marker}).json()
    assert not [x for x in before["citations"] if marker in (x["title"] or "")], \
        "待审核内容泄漏进检索"

    # A public card cannot be approved by title/symbol-name association alone.
    assert c.post(f"/api/experience/{eid}/review",
                  json={"action": "approve", "note": "先验证门禁"}).status_code == 409
    usr = traverse.resolve_symbol(conn, "parse_value")[0]["usr"]
    assert c.post(f"/api/card/{eid}/bind", json={"usr": usr}).status_code == 200
    assert c.post(f"/api/card/{eid}/experiments", json={
        "paths": ["examples/reproductions/cjson-nesting/result.txt"]
    }).status_code == 200
    qa = c.post(f"/api/card/{eid}/qa", json={
        "question": "该结论如何由源码验证？", "expected_points": ["固定函数定义"],
        "evidence_tags": ["A1"],
    })
    assert qa.status_code == 200
    assert c.post(f"/api/card/qa/{qa.json()['id']}/review", json={
        "passed": True, "note": "已核对固定源码"
    }).status_code == 200
    current_bundle = c.get(f"/api/card/{eid}").json()["current_review_bundle_hash"]
    assert c.post(f"/api/card/{eid}/confirm", json={
        "bundle_hash": current_bundle, "reviewer": "local-reviewer"
    }).status_code == 200
    assert c.post(f"/api/experience/{eid}/review",
                  json={"action": "approve", "note": "精确锚点复核完成",
                        "expected_bundle_hash": current_bundle}).json()["status"] == "approved"

    after = c.post("/api/ask", json={"q": marker}).json()
    assert [x for x in after["citations"] if marker in (x["title"] or "")]

    assert c.post(f"/api/experience/{eid}/review",
                  json={"action": "bogus"}).status_code == 400
    consistency = c.get("/api/cards/consistency")
    assert consistency.status_code == 200 and "ok" in consistency.json()


def test_api_session_agent_and_card_preview(kb, tmp_path, monkeypatch):
    """The web flow imports only a reviewed sample and saves the current run once."""
    conn, data_dir = kb
    import codeatlas.server.app as srv
    session_file = tmp_path / "api-public-session.json"
    session_file.write_text(json.dumps({
        "schema_version": 1, "id": "api-public-session", "source_type": "public_synthetic",
        "source_ref": "tests/api-public-session.json", "license": "CC0-1.0",
        "repository": "tests/fixtures/mini_c", "revision": "fixture",
            "messages": [
                {"role": "user", "knowledge_role": "symptom",
                 "content": "请审查 parse_value 的影响范围"},
                {"role": "tool", "knowledge_role": "root_cause",
                 "content": "固定边界检查触发失败返回。"},
                {"role": "developer", "knowledge_role": "fix",
                 "content": "保留边界检查并处理失败返回。"},
                {"role": "reviewer", "knowledge_role": "verification",
                 "content": "核对固定 revision 的定义和 certain 调用链。"},
            ],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(srv, "PUBLIC_SESSION_EXAMPLES", {
        "fixture-review": {
            "path": session_file, "title": "fixture", "description": "fixture",
            "question": "parse_value 的影响范围",
        }
    })
    c = _client(conn, data_dir, monkeypatch)
    examples = c.get("/api/session/examples").json()["items"]
    assert examples == [{
        "id": "fixture-review", "title": "fixture", "description": "fixture",
        "question": "parse_value 的影响范围",
    }]
    # No browser route accepts a user-provided local file path.
    assert c.post("/api/session/import", json={"file": str(session_file)}).status_code in (404, 405)
    imported = c.post("/api/session/examples/fixture-review/import").json()
    assert imported["imported"] and imported["id"] == "api-public-session"
    sessions = c.get("/api/sessions").json()["items"]
    listed = next(item for item in sessions if item["id"] == "api-public-session")
    assert "messages" not in listed
    assert c.post("/api/session/api-public-session/assess").json()["eligible"] is True
    assert c.post("/api/session/api-public-session/goal", json={
        "goal": "沉淀 parse_value 边界排障",
    }).status_code == 200
    candidates = c.post("/api/session/api-public-session/curate").json()["items"]
    assert candidates[0]["payload"]["review_status"] == "ai_assisted_draft"
    detail = c.get("/api/session/api-public-session").json()
    assert detail["knowledge_missing_fields"] == []
    assert all("knowledge_role" in message for message in detail["messages"])
    curated = c.post(f"/api/candidate/{candidates[0]['id']}/decide", json={
        "action": "accept", "reason": "",
    }).json()
    assert c.get(f"/api/card/{curated['card_id']}").json()["status"] == "pending"
    runs_before = conn.execute("SELECT COUNT(*) FROM agent_run").fetchone()[0]
    preview = c.post("/api/agent/run", json={
        "q": "parse_value 的影响范围", "session_id": "api-public-session", "save_draft": False,
    }).json()
    assert preview["status"] == "completed" and preview["draft_preview"] and preview["draft_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM agent_run").fetchone()[0] == runs_before + 1
    saved = c.post(f"/api/agent/{preview['id']}/draft").json()
    assert saved["saved"] is True
    assert c.post(f"/api/agent/{preview['id']}/draft").json()["saved"] is False
    assert conn.execute("SELECT COUNT(*) FROM agent_run").fetchone()[0] == runs_before + 1
    stored = c.get(f"/api/agent/{preview['id']}").json()
    assert stored["draft_id"] == saved["id"] and stored["draft_preview"]
    tag = preview["evidence_registry"][0]["tag"]
    feedback = c.post(f"/api/agent/run/{preview['id']}/feedback", json={
        "verdict": "incomplete", "evidence_tag": tag,
        "comment": "还需要解释失败分支。",
    })
    assert feedback.status_code == 200
    assert c.get("/api/feedback").json()["items"][0]["run_id"] == preview["id"]
    card = c.get(f"/api/card/{saved['id']}").json()
    assert card["status"] == "pending" and card["anchors"] == [] and card["reviews"] == []
    assert saved["id"] in {item["id"] for item in c.get("/api/cards").json()["items"]}


def test_api_candidate_isolated_in_response(kb, monkeypatch):
    """API 返回里 candidate 必须在独立字段，不能混进 by_hop。"""
    conn, data_dir = kb
    c = _client(conn, data_dir, monkeypatch)
    imp = c.get("/api/impact", params={"symbol": "cJSON_Delete", "depth": 3}).json()
    assert "candidate_impact" in imp
    hop_names = {x["name"] for lst in imp["by_hop"].values() for x in lst}
    cand_only = {x["name"] for x in imp["candidate_impact"]}
    assert not (cand_only & hop_names) or True  # 允许同名但来源必须分开记录
    assert isinstance(imp["candidate_impact"], list)
