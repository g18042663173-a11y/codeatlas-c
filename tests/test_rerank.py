"""重排测试。

【这组测试守的是什么】
重排最危险的地方在于它**看起来总是对的**：加个重排，指标动一动，
很难判断是真改善还是运气。所以这里断言的不是"分数变高了"，
而是几条**机制层面的不变量** —— 它们不会随权重微调而失效。
"""
from __future__ import annotations

import pytest

from codeatlas.retrieve import engine, rerank


# ==================== 意图路由（核心不变量）====================

@pytest.mark.parametrize("q", [
    "谁调用了 cJSON_Delete",
    "修改 cJSON_New_Item 会影响什么",
    "parse_value 出问题会间接波及哪些上层函数",
    "cJSON_Parse 的调用链路",
    "who calls parse_string",
    "impact of changing cJSON_free",
])
def test_structural_queries_skip_rerank(q):
    """★ 结构性查询必须跳过内容重排。

    这条是实测逼出来的：对所有查询一律重排时，
    变差的题**全部**是这一类 —— 内容特征会把种子函数和近邻顶上去，
    把真正的 2 跳答案挤出前十。
    """
    assert rerank.should_rerank(q) is False


@pytest.mark.parametrize("q", [
    "cJSON_Delete 这个函数做什么",
    "内存是怎么分配和释放的",
    "parse_hex4 怎么解析十六进制",
])
def test_content_queries_do_rerank(q):
    """内容型查询才走重排。"""
    assert rerank.should_rerank(q) is True


def test_structural_query_order_unchanged(kb):
    """结构性查询下，重排前后引用顺序必须完全一致。"""
    conn, _ = kb
    q = "修改 cJSON_New_Item 会影响什么"
    a = engine.ask(conn, q, data_dir="data", reranker="none")
    b = engine.ask(conn, q, data_dir="data", reranker="feature")
    assert [c["uid"] for c in a["citations"]] == [c["uid"] for c in b["citations"]]


# ==================== 前缀匹配必须是负权重 ====================

def test_prefix_match_is_penalized():
    """★ 前缀匹配权重必须为负。

    问 cJSON_Delete 时命中 cJSON_DeleteItemFromObject，说明这是**另一个**符号。
    第一版给了正权重，直接把同名混淆问题引了回来，Recall 掉了 5.4 个点。
    """
    assert rerank.WEIGHTS["prefix_symbol"] < 0
    assert rerank.WEIGHTS["exact_symbol"] > 0
    assert rerank.WEIGHTS["exact_symbol"] > abs(rerank.WEIGHTS["prefix_symbol"])


def test_fusion_weight_dominates():
    """融合分权重必须显著大于任一内容特征。

    内容特征是**打破平局**用的，不是用来推翻召回排序的。
    """
    content = {k: v for k, v in rerank.WEIGHTS.items() if k != "fusion"}
    assert rerank.WEIGHTS["fusion"] > 2 * max(abs(v) for v in content.values())


# ==================== 可解释性 ====================

def test_rerank_is_explainable(kb):
    """★ 每条结果必须能给出特征分解 —— 这是选特征线性打分而非黑盒模型的理由。"""
    conn, _ = kb
    r = engine.ask(conn, "cJSON_Delete 这个函数做什么", data_dir="data",
                   reranker="feature", explain=True)
    assert r["rerank_detail"]
    first = r["rerank_detail"][0]
    assert "fusion=" in first
    assert any(k in first for k in ("exact_symbol", "is_definition", "evidence"))


def test_exact_symbol_beats_unrelated(kb):
    """点名的符号必须排在无关结果之前。"""
    conn, _ = kb
    r = engine.ask(conn, "cJSON_Delete 这个函数做什么", data_dir="data",
                   reranker="feature")
    top = rerank._symbol_of(r["citations"][0]["title"])
    assert top == "cJSON_Delete"


# ==================== 编号同步（静默错误防线）====================

def test_tags_resync_after_rerank(kb):
    """★ 重排后引用编号必须重新排，且与上下文正文一致。

    漏掉这步是静默错误：模型看到的 [A1] 和展示给用户的引用列表第一条对不上，
    表现为"答案引用了 A1，但列表里 A1 是另一篇"。
    """
    conn, _ = kb
    r = engine.ask(conn, "cJSON_Delete 这个函数做什么", data_dir="data",
                   reranker="feature")
    cits = r["citations"]
    # 同级别编号必须从 1 连续递增
    seen = {}
    for c in cits:
        lvl = c["level"]
        seen[lvl] = seen.get(lvl, 0) + 1
        assert c["tag"] == f"{lvl}{seen[lvl]}", f"编号不连续: {c['tag']}"

    # 上下文正文里的 tag 必须都能在引用列表里找到
    ctx_tags = set()
    for line in (r.get("context") or "").splitlines():
        if line.startswith("["):
            ctx_tags.add(line[1:line.index("]")])
    assert ctx_tags <= {c["tag"] for c in cits}, "上下文里出现了引用列表没有的编号"


# ==================== 降级 ====================

def test_reranker_none_is_noop(kb):
    conn, _ = kb
    a = engine.ask(conn, "parse_value 的实现", data_dir="data", reranker="none")
    assert a["reranker"] == "none"
    assert all("rerank_score" not in c for c in a["citations"])


def test_cross_encoder_falls_back(monkeypatch):
    """cross-encoder 模型不可用时必须降级为特征重排，不能崩。"""
    rr = rerank.get_reranker("BAAI/does-not-exist-xyz")
    assert isinstance(rr, rerank.FeatureReranker)


def test_rerank_handles_empty():
    assert rerank.apply(None, "q", [], rerank.FeatureReranker()) == ([], [])
