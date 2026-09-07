"""核心不变量测试。

这里每一个测试都对应架构文档里的一条硬约束。
测试跑在 corpus/cJSON 上，秒级完成，CI 友好。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "mini_c"

sys.path.insert(0, str(ROOT / "src"))

from codeatlas import db as dbm                      # noqa: E402
from codeatlas.experience import store               # noqa: E402
from codeatlas.graph import traverse                 # noqa: E402
from codeatlas.indexer import build as indexer       # noqa: E402
from codeatlas.parser import ast_walker, compile_db  # noqa: E402
from codeatlas.retrieve import engine                # noqa: E402
from codeatlas.summary import head                   # noqa: E402


def test_main_node_identity_is_scoped_per_compile_target_path():
    """Independent executable entry points must not overwrite each other."""
    from codeatlas.parser.ast_walker import CK, node_id_for

    def cursor(path: str, spelling: str, usr: str):
        return SimpleNamespace(
            get_usr=lambda: usr,
            location=SimpleNamespace(file=SimpleNamespace(name=path), line=1),
            kind=CK.FUNCTION_DECL,
            spelling=spelling,
        )

    assert node_id_for(cursor("/tmp/a.c", "main", "c:@F@main")) != node_id_for(
        cursor("/tmp/b.c", "main", "c:@F@main")
    )
    assert node_id_for(cursor("/tmp/a.c", "api", "c:@F@api")) == node_id_for(
        cursor("/tmp/b.c", "api", "c:@F@api")
    )




# ==================== D1 事实与推断分离 ====================

def test_parse_produces_nodes_and_edges(kb):
    conn, _ = kb
    assert conn.execute("SELECT COUNT(*) c FROM node WHERE kind='function'").fetchone()["c"] >= 15
    assert conn.execute("SELECT COUNT(*) c FROM edge WHERE kind='calls'").fetchone()["c"] >= 20


def test_confidence_values_are_constrained(kb):
    """置信度只能是 certain / candidate，CHECK 约束必须生效。"""
    conn, _ = kb
    bad = conn.execute(
        "SELECT COUNT(*) c FROM edge WHERE confidence NOT IN ('certain','candidate')"
    ).fetchone()["c"]
    assert bad == 0
    with pytest.raises(Exception):
        conn.execute("INSERT INTO edge(src,dst,kind,confidence) VALUES('a','b','calls','maybe')")
        conn.commit()
    conn.rollback()


def test_candidate_edges_exist_and_are_labeled(kb):
    """candidate 必须真的被识别出来，且带 reason —— 否则 D1 只是空话。"""
    conn, _ = kb
    rows = conn.execute(
        "SELECT reason, COUNT(*) c FROM edge WHERE confidence='candidate' GROUP BY reason"
    ).fetchall()
    assert rows, "一条 candidate 都没有，说明分类逻辑没生效"
    for r in rows:
        assert r["reason"] in ("fn_pointer", "address_taken", "unresolved")


def test_compiler_resolved_reference_edges_are_certain(kb):
    conn, _ = kb
    counts = {
        row["kind"]: row["c"]
        for row in conn.execute(
            """SELECT kind,COUNT(*) c FROM edge
                WHERE kind IN ('type_use','field_access','global_ref')
                GROUP BY kind"""
        )
    }
    assert all(counts.get(kind, 0) > 0 for kind in ("type_use", "field_access", "global_ref"))
    bad = conn.execute(
        """SELECT COUNT(*) c FROM edge
            WHERE kind IN ('type_use','field_access','global_ref')
              AND (confidence<>'certain' OR reason<>'ast')"""
    ).fetchone()["c"]
    assert bad == 0


def test_traversal_never_includes_candidate(kb):
    """★ 核心不变量：图遍历绝不能走 candidate 边。"""
    conn, _ = kb
    # 构造一条纯 candidate 边，看它会不会被遍历到
    conn.execute("INSERT OR IGNORE INTO node(id,kind,name) VALUES('fake_src','function','FS')")
    conn.execute("INSERT OR IGNORE INTO node(id,kind,name) VALUES('fake_dst','function','FD')")
    conn.execute("INSERT OR IGNORE INTO edge(src,dst,kind,confidence,reason) "
                 "VALUES('fake_src','fake_dst','calls','candidate','fn_pointer')")
    conn.commit()

    hops = traverse.neighbors(conn, "fake_src", direction="out", kind="calls", max_hop=3)
    assert all(n != "fake_dst" for n, _ in hops), "candidate 边被走进了调用链！"

    imp = traverse.impact(conn, "fake_dst", max_hop=3)
    reached = {x["id"] for lst in imp["by_hop"].values() for x in lst}
    assert "fake_src" not in reached, "candidate 边混进了影响分析结论！"
    # 但它必须出现在单独的 candidate 栏里
    assert any(c["name"] == "FS" for c in imp["candidate_impact"])


def test_impact_is_multi_hop(kb):
    """影响分析要能翻出 1 跳之外的调用者，否则和 grep 没区别。"""
    conn, _ = kb
    node = traverse.resolve_symbol(conn, "cJSON_Delete")[0]
    r = traverse.impact(conn, node["id"], max_hop=3)
    assert 1 in r["by_hop"] and len(r["by_hop"][1]) >= 6
    assert max(r["by_hop"]) >= 2, "没有 2 跳以上结果"
    assert len(r["affected_files"]) >= 2


def test_usr_dedup_no_duplicate_functions(kb):
    """同一个函数不能因为被多个 TU 看到就出现多个定义节点。"""
    conn, _ = kb
    dup = conn.execute(
        """SELECT name, COUNT(*) c FROM node
            WHERE kind='function' AND is_definition=1 AND is_static=0
            GROUP BY usr HAVING c > 1"""
    ).fetchall()
    assert not dup, f"USR 去重失效: {[d['name'] for d in dup]}"


# ==================== D2 摘要头定长约束 ====================

def test_every_function_has_summary_head(kb):
    conn, _ = kb
    fns = conn.execute(
        "SELECT COUNT(*) c FROM node WHERE kind='function' AND is_definition=1").fetchone()["c"]
    heads = conn.execute("SELECT COUNT(*) c FROM summary_head").fetchone()["c"]
    assert heads == fns


def test_summary_head_token_budget(kb):
    """★ 硬上限 200 token，一个都不能超。"""
    conn, _ = kb
    mx = conn.execute("SELECT MAX(token_len) m FROM summary_head").fetchone()["m"]
    assert mx <= head.MAX_TOKENS, f"摘要头超预算: {mx} > {head.MAX_TOKENS}"


def test_graph_expand_returns_heads_not_bodies(kb):
    """★ 图扩展只能返回摘要头。返回函数体会让上下文爆掉。"""
    conn, _ = kb
    rid = conn.execute(
        "SELECT rowid FROM chunk WHERE title LIKE 'cJSON_Delete%' LIMIT 1").fetchone()["rowid"]
    heads = engine.graph_expand(conn, [rid], hops=2)
    assert heads
    for h in heads:
        assert h["tokens"] <= head.MAX_TOKENS

    # 真正要验证的不变量：摘要头必须显著小于对应函数体，
    # 否则"扩得更远还塞得下"这个说法就不成立（这也是消融表那一行的依据）
    total_head = sum(head.est_tokens(h["text"]) for h in heads)
    total_body = 0
    for h in heads:
        row = conn.execute("SELECT text FROM chunk WHERE node_id=? AND kind='code'",
                           (h["node_id"],)).fetchone()
        if row:
            total_body += head.est_tokens(row["text"])
    assert total_body > 0
    assert total_head < total_body, (
        f"摘要头压缩比不足：head={total_head} body={total_body}")


# ==================== D4 人工审核硬闸门 ====================

SAMPLE = {
    "title": "解析深层嵌套 JSON 栈溢出",
    "symptom": "输入嵌套超过 1000 层时进程崩溃，backtrace 显示 parse_value 递归",
    "hypotheses": ["输入过大", "递归无深度限制"],
    "dead_ends": ["加大线程栈到 8MB —— 只是把崩溃点推后，没解决"],
    "root_cause": "parse_value 递归下降解析器没有深度上限",
    "fix_steps": "在 parse_buffer 里加 depth 计数，超过 CJSON_NESTING_LIMIT 直接返回失败",
    "verification": "构造 2000 层嵌套输入，应返回解析失败而非崩溃",
    "related_symbols": ["parse_value", "cJSON_ParseWithOpts"],
    "confidence": "high",
    "evidence_tags": ["A1"],
    "evidence_turns": [1, 2],
}


def test_pending_experience_not_retrievable(kb):
    """★★ 全项目最重要的一个测试：审核前，检索命中必须为 0。"""
    conn, data_dir = kb
    eid = store.add(conn, SAMPLE, source_type="session", source_ref="t.jsonl#L1")

    row = conn.execute("SELECT status FROM experience WHERE id=?", (eid,)).fetchone()
    assert row["status"] == "pending", "新条目必须是 pending"

    indexer.build(conn, str(CORPUS), out_dir=data_dir)

    # 用条目里最独特的词去搜，应该一条都搜不到
    q = "嵌套 栈溢出 递归 深度限制"
    r = engine.ask(conn, q, data_dir=data_dir)
    hit = [c for c in r["citations"] if c["level"] == "B"]
    assert hit == [], f"待审核内容泄漏进检索: {hit}"

    # FTS 索引里根本不应该有这一行
    n = conn.execute(
        "SELECT COUNT(*) c FROM chunk WHERE uid=? AND visible=1", (f"exp:{eid}",)
    ).fetchone()["c"]
    assert n == 0


def test_public_example_requires_review_before_retrieval(kb):
    """仓库附带的公开样例也不能绕过 pending → approved 审核闸门。"""
    conn, data_dir = kb
    sample = ROOT / "examples" / "experience" / "public-cjson-review-example.jsonl"
    stats = store.import_jsonl(conn, str(sample), llm_client=None)
    assert stats == {"imported": 1, "skipped": 0, "failed": 0}
    row = conn.execute(
        "SELECT status, source_type FROM experience WHERE source_type='public-manual-example'"
    ).fetchone()
    assert row is not None and row["status"] == "pending"
    indexer.build(conn, str(CORPUS), out_dir=data_dir)
    result = engine.ask(conn, "深层嵌套输入审查", data_dir=data_dir)
    assert not [c for c in result["citations"] if c["level"] == "B"]


def test_approved_experience_becomes_retrievable(kb):
    """★★ 审核通过后立即生效，且证据级别为 B。"""
    conn, data_dir = kb
    eid = store.add(conn, SAMPLE, source_type="session", source_ref="t.jsonl#L1")
    indexer.build(conn, str(CORPUS), out_dir=data_dir)

    anchor = traverse.resolve_symbol(conn, "parse_value")[0]
    store.bind(conn, eid, anchor["usr"])
    qa = store.qa_add(conn, eid, "根因如何由源码验证？", ["递归入口与限制"], ["A1"])
    store.qa_review(conn, qa["id"], True, "固定源码核对通过")
    store.attach_experiments(conn, eid, ["examples/reproductions/cjson-nesting/result.txt"])
    store.qa_review(conn, qa["id"], True, "复现实验与固定源码核对通过")
    store.confirm_review(conn, eid, store.review_bundle(conn, eid), "local-reviewer")
    store.review(conn, eid, "approve", note="确认有效")

    r = engine.ask(conn, "嵌套 栈溢出 递归 深度限制", data_dir=data_dir)
    hit = [c for c in r["citations"][:10] if c["level"] == "B"]
    assert hit, "审核通过后仍然检索不到"
    assert any("嵌套" in (c["title"] or "") for c in hit)


def test_only_strong_current_bm25_experience_gets_a_first_page_slot():
    conn = dbm.connect(":memory:")
    conn.execute(
        """INSERT INTO chunk(uid,kind,title,text,evidence_level,visible)
           VALUES('exp:reviewed','experience','reviewed card','unique fact','B',1)"""
    )
    rowid = conn.execute(
        "SELECT rowid FROM chunk WHERE uid='exp:reviewed'"
    ).fetchone()["rowid"]
    citations = [
        {"uid": f"code:{index}", "level": "A", "tag": f"A{index}",
         "provenance": {}}
        for index in range(1, 13)
    ] + [{
        "uid": "exp:reviewed", "level": "B", "tag": "B1",
        "provenance": {"card_status": "approved", "stale_status": "approved"},
    }]
    promoted = engine._reserve_strong_reviewed_experience(
        conn, citations, [rowid], top_k=10,
    )
    assert promoted[9]["uid"] == "exp:reviewed"
    assert all(item["uid"] != "exp:reviewed" for item in
               engine._reserve_strong_reviewed_experience(
                   conn, citations, [1001, 1002, 1003, rowid], top_k=10,
               )[:10])
    stale = [dict(item) for item in citations]
    stale[-1] = {**stale[-1], "provenance": {
        "card_status": "stale", "stale_status": "stale",
    }}
    assert all(item["uid"] != "exp:reviewed" for item in
               engine._reserve_strong_reviewed_experience(
                   conn, stale, [rowid], top_k=10,
               )[:10])


def test_reviewed_experience_has_scoped_lexical_recall_lane():
    conn = dbm.connect(":memory:")
    conn.execute(
        """INSERT INTO chunk(uid,kind,title,text,evidence_level,visible)
           VALUES('exp:reviewed','experience','嵌套边界排障',
                  'parse limit verification', 'B',1),
                 ('exp:hidden','experience','嵌套边界草稿',
                  'parse limit verification', 'B',0)"""
    )
    conn.execute(
        "INSERT INTO chunk_fts(rowid,title,text) "
        "SELECT rowid,title,text FROM chunk WHERE visible=1"
    )
    conn.commit()
    reviewed = conn.execute(
        "SELECT rowid FROM chunk WHERE uid='exp:reviewed'"
    ).fetchone()["rowid"]
    assert engine.recall_reviewed_experience(
        conn, "parse limit 如何 verification"
    ) == [reviewed]
    # One generic overlap is not enough to turn a card into answer evidence.
    assert engine.recall_reviewed_experience(conn, "parse unrelated question") == []


def test_cannot_create_approved_directly(kb):
    """不能绕过审核直接插 approved。"""
    conn, _ = kb
    with pytest.raises(ValueError):
        store.add(conn, SAMPLE, status="approved")


def test_experience_schema_validation(kb):
    conn, _ = kb
    with pytest.raises(ValueError):
        store.add(conn, {"symptom": "缺 title"})
    with pytest.raises(ValueError):
        store.add(conn, {"title": "x", "symptom": "y", "dead_ends": "应该是数组"})


# ==================== D5 证据分级与拒答 ====================

def test_refuses_when_no_ab_evidence(kb):
    """★ A/B 级证据为 0 时必须拒答，不能瞎编。"""
    conn, data_dir = kb
    r = engine.ask(conn, "如何配置 Kubernetes Ingress 的 TLS 证书轮换", data_dir=data_dir)
    assert r["refused"] is True
    assert r["ab_evidence"] == 0
    assert "证据不足" in r["answer"]


def test_citations_are_graded(kb):
    conn, data_dir = kb
    r = engine.ask(conn, "cJSON_Delete", data_dir=data_dir)
    assert r["citations"]
    assert all(c["level"] in ("A", "B", "C", "D") for c in r["citations"])
    assert all(c["tag"][0] == c["level"] for c in r["citations"])
    code_citation = next(c for c in r["citations"] if c["level"] == "A")
    provenance = code_citation["provenance"]
    assert provenance["repository"] == "tests/fixtures/mini_c"
    assert provenance["revision"] == "fixture"
    assert provenance["path"]
    assert provenance["line_start"] > 0
    assert provenance["symbol"]


# ==================== 约束 #4 无 LLM / 无向量降级 ====================

def test_full_pipeline_without_llm_and_vector(kb):
    """★ 没有 API key、没有 embedding 模型时，全流程必须能跑通。"""
    conn, data_dir = kb
    r = engine.ask(conn, "cJSON_Parse 的调用链", embedder=None, use_vector=False,
                   data_dir=data_dir, llm_client=None)
    assert r["recall"]["vector"] == 0
    assert r["recall"]["fused"] > 0
    assert r["refused"] is False
    assert r["evidence_pack"], "无 LLM 模式必须返回证据包"


def test_bm25_query_sanitization(kb):
    """用户输入里的特殊字符不能把 FTS5 打崩。"""
    conn, data_dir = kb
    for q in ['cJSON "unterminated', "a - b * c:", "NULL) AND 1=1--", "", "???"]:
        r = engine.ask(conn, q, data_dir=data_dir)
        assert "refused" in r


# ==================== 幂等性 ====================

def test_rebuild_is_idempotent(kb):
    conn, data_dir = kb
    a = conn.execute("SELECT COUNT(*) c FROM chunk").fetchone()["c"]
    indexer.build(conn, str(CORPUS), out_dir=data_dir)
    b = conn.execute("SELECT COUNT(*) c FROM chunk").fetchone()["c"]
    assert a == b


def test_summary_build_skips_unchanged(kb):
    conn, _ = kb
    s = head.build(conn, str(CORPUS))
    assert s["skipped"] > 0, "第二次跑应该大量跳过（幂等）"


def test_rrf_fusion_math():
    """RRF 只依赖排名，不依赖分数量纲。"""
    out = dict(engine.rrf([[1, 2, 3], [3, 2, 1]]))
    assert out[2] == pytest.approx(2 / 62)
    assert out[1] == pytest.approx(1 / 61 + 1 / 63)
    assert out[1] == pytest.approx(out[3])          # 对称输入 → 对称结果

    # 关键性质：在任一路里排第一，比在两路里都排中游更有优势
    only_top = dict(engine.rrf([[9], [7, 8, 9]]))
    assert max(only_top, key=only_top.get) == 9      # 9 同时占了 a 的第1和 b 的第3

    # 权重能生效（symbol 模式给符号召回加权就靠这个）
    w = dict(engine.rrf([[1], [2]], weights=[2.0, 1.0]))
    assert w[1] > w[2]


def test_route_detects_symbols():
    assert engine.route("nr_pusch_decode 是什么") == "symbol"
    assert engine.route("cJSON.c 里有什么") == "symbol"
    assert engine.route("这个模块整体流程是怎样的") == "nl"
