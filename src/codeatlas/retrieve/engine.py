"""检索引擎 —— 项目最核心的在线路径。

流程（对应架构文档 §6.1）：
  1. 查询路由：含 C 标识符/文件名 → symbol 模式，符号精确召回加权
  2. 三路召回：符号精确 / BM25(FTS5) / 向量
  3. RRF 融合：只用排名，无量纲，免调参
  4. 图多跳扩展：沿 certain 边扩 2 跳，★ 只取摘要头，不取函数体
  5. 证据分级拼装：A/B/C 按预算分配
  6. 生成：强制引用；A/B 级证据为 0 时拒答
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from ..graph import traverse
from ..summary import head as head_mod

log = logging.getLogger(__name__)

RE_C_IDENT = re.compile(r"\b[a-z_][a-z0-9_]{3,}(?:_[a-z0-9]+)+\b", re.I)
RE_FILE = re.compile(r"\b[\w/.-]+\.(?:c|h)\b", re.I)
RRF_K = 60


# ------------------------------------------------------------------ 路由

def route(q: str) -> str:
    if RE_FILE.search(q) or RE_C_IDENT.search(q):
        return "symbol"
    return "nl"


def _fts_query(q: str) -> str:
    """把自然语言安全地转成 FTS5 MATCH 表达式。

    直接把用户输入丢给 MATCH 会因为 - " * : 等字符抛语法错误。
    """
    toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]{2,}", q)
    toks = [t for t in toks if len(t) >= 2][:12]
    if not toks:
        return ""
    return " OR ".join(f'"{t}"' for t in toks)


# ------------------------------------------------------------------ 三路召回

def recall_symbol(conn: sqlite3.Connection, q: str, limit: int = 30) -> list[int]:
    """符号精确召回：C 代码里同名符号必须精确命中，这是向量的弱项。"""
    names = set(RE_C_IDENT.findall(q)) | set(re.findall(r"\b[A-Za-z_]\w{2,}\b", q))
    files = set(RE_FILE.findall(q))
    if not names and not files:
        return []

    out: list[int] = []
    for n in sorted(names)[:8]:
        rows = conn.execute(
            """SELECT c.rowid FROM node nd JOIN chunk c ON c.node_id = nd.id
                WHERE nd.name = ? AND c.visible = 1
                ORDER BY nd.is_definition DESC, c.rowid LIMIT ?""", (n, limit)).fetchall()
        if not rows:   # 退化为前缀匹配
            rows = conn.execute(
                """SELECT c.rowid FROM node nd JOIN chunk c ON c.node_id = nd.id
                    WHERE nd.name LIKE ? AND c.visible = 1 ORDER BY c.rowid LIMIT ?""",
                (n + "%", limit)).fetchall()
        out.extend(r["rowid"] for r in rows)
    for f in sorted(files):
        rows = conn.execute(
            """SELECT c.rowid FROM node nd JOIN chunk c ON c.node_id = nd.id
                WHERE nd.path LIKE ? AND c.visible = 1 ORDER BY c.rowid LIMIT ?""",
            ("%" + f, limit)).fetchall()
        out.extend(r["rowid"] for r in rows)

    seen, uniq = set(), []
    for r in out:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq[:limit]


def recall_bm25(conn: sqlite3.Connection, q: str, limit: int = 30) -> list[int]:
    m = _fts_query(q)
    if not m:
        return []
    try:
        rows = conn.execute(
            """SELECT rowid FROM chunk_fts WHERE chunk_fts MATCH ?
                ORDER BY bm25(chunk_fts), rowid LIMIT ?""", (m, limit)).fetchall()
    except sqlite3.OperationalError as e:
        log.warning("FTS 查询失败: %s", e)
        return []
    return [r["rowid"] for r in rows]


def recall_vector(conn: sqlite3.Connection, q: str, embedder, *,
                  data_dir: str = "data", limit: int = 30) -> list[int]:
    """向量召回。无模型 / 无索引时返回 []，系统自动退化为 BM25+图。"""
    vec_f = Path(data_dir) / "vectors.npy"
    ids_f = Path(data_dir) / "vec_ids.json"
    if embedder is None or not vec_f.exists() or not ids_f.exists():
        return []
    try:
        import numpy as np
        mat = np.load(vec_f)
        ids = json.loads(ids_f.read_text())
        qv = embedder.encode([q])
        if qv is None:
            return []
        sims = mat @ qv[0]
        top = np.lexsort((np.asarray(ids), -sims))[:limit]
        return [ids[i] for i in top if i < len(ids)]
    except Exception as e:
        log.warning("向量召回失败: %s", e)
        return []


# ------------------------------------------------------------------ 融合

def rrf(rank_lists: list[list[int]], weights: list[float] | None = None,
        k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion。

    为什么不是加权分数相加：BM25 分数和 cosine 相似度量纲不同、分布不同，
    线性加权需要调参且对异常分数敏感。RRF 只用排名，无量纲、免调参、鲁棒。
    """
    scores: dict[int, float] = defaultdict(float)
    ws = weights or [1.0] * len(rank_lists)
    for w, lst in zip(ws, rank_lists):
        for rank, doc in enumerate(lst, start=1):
            scores[doc] += w / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


# ------------------------------------------------------------------ 图扩展

def graph_expand(conn: sqlite3.Connection, seeds: list, *,
                 hops: int = 2, decay: float = 0.5, limit: int = 24,
                 mode: str = "head") -> list[dict]:
    """从种子 chunk 的节点出发扩展多跳邻居。

    mode="head" 只取定长摘要头（生产路径）；
    mode="body" 取完整函数体 —— 仅用于消融实验的对照组，
    用来量化"改用摘要头后上下文降了多少"。
    """
    out: dict[str, dict] = {}
    # seeds 可以是 [rowid] 或 [(rowid, 融合分数)]。带分数时，扩展节点的分数
    # ★ 继承种子的 RRF 分数再按跳数衰减 —— 这样摘要头和普通 chunk 处在同一量纲，
    #   才能混在一起按分数排序。否则 decay^hop(≈0.5) 和 RRF(≈0.03) 没法比。
    for item in seeds:
        rid, seed_score = item if isinstance(item, (tuple, list)) else (item, 1.0)
        seed = conn.execute("SELECT node_id FROM chunk WHERE rowid=?", (rid,)).fetchone()
        if not seed or not seed["node_id"]:
            continue
        for direction in ("out", "in"):
            for nid_, hop in traverse.neighbors(conn, seed["node_id"], direction=direction,
                                                kind="calls", max_hop=hops):
                h = head_mod.get(conn, nid_)
                if h is None:
                    continue
                score = seed_score * (decay ** hop)
                if nid_ in out and out[nid_]["score"] >= score:
                    continue
                if mode == "body":
                    # ★ 变量名不能叫 row：外层循环的种子行也叫 row，
                    #   覆盖后下一轮 row["node_id"] 直接 IndexError。
                    body = conn.execute(
                        "SELECT text FROM chunk WHERE node_id=? AND kind='code'",
                        (nid_,)).fetchone()
                    from ..summary.head import est_tokens
                    text = body["text"] if body else head_mod.render(h, h["name"])
                    tokens = est_tokens(text)
                else:
                    text = head_mod.render(h, h["name"])
                    tokens = h["token_len"] or 0
                out[nid_] = dict(node_id=nid_, hop=hop, score=score,
                                 name=h["name"], path=h["path"],
                                 text=text, tokens=tokens)
    return sorted(out.values(), key=lambda d: (-d["score"], d["node_id"]))[:limit]


# ------------------------------------------------------------------ 证据拼装

BUDGET_SPLIT = {"code": 0.40, "head": 0.25, "experience": 0.20, "wiki": 0.15}


def assemble(conn: sqlite3.Connection, fused: list[tuple[int, float]],
             heads: list[dict], budget_tokens: int = 8000) -> dict:
    """按证据等级和预算拼装上下文。★ visible=0 的内容根本不会出现在这里。"""
    from ..summary.head import est_tokens

    buckets: dict[str, list[dict]] = {"code": [], "experience": [], "wiki": [], "head": []}
    used = {k: 0 for k in buckets}
    citations: list[dict] = []
    counter = defaultdict(int)

    for rid, score in fused:
        row = conn.execute(
            "SELECT * FROM chunk WHERE rowid=? AND visible=1", (rid,)).fetchone()
        if row is None:
            continue
        bucket = row["kind"] if row["kind"] in buckets else "code"
        cap = int(budget_tokens * BUDGET_SPLIT[bucket])
        txt = row["text"][:2400]
        t = est_tokens(txt)
        if used[bucket] + t > cap:
            continue
        used[bucket] += t
        lvl = row["evidence_level"]
        counter[lvl] += 1
        tag = f"{lvl}{counter[lvl]}"
        citations.append(dict(tag=tag, level=lvl, title=row["title"],
                              uid=row["uid"], node_id=row["node_id"],
                              source_ref=row["source_ref"], score=round(score, 5)))
        buckets[bucket].append(dict(tag=tag, uid=row["uid"], title=row["title"], text=txt,
                                    source_ref=row["source_ref"]))

    # 注意：body 模式下单条就可能上千 token，这里用同一份预算，
    # 保证两种模式在"相同预算"下比较，差异体现在能塞进多少条邻居。
    cap_head = int(budget_tokens * BUDGET_SPLIT["head"])
    for h in heads:
        t = h["tokens"] or est_tokens(h["text"])
        if used["head"] + t > cap_head:
            break
        used["head"] += t
        counter["A"] += 1
        tag = f"A{counter['A']}"
        # ★ 摘要头引用也必须带 node_id，否则评测无法把图扩展的贡献计分，
        #   消融表会出现"开不开图扩展指标完全一样"的假象。
        code_uid = conn.execute(
            "SELECT uid FROM chunk WHERE node_id=? AND kind='code'",
            (h["node_id"],)).fetchone()
        citations.append(dict(tag=tag, level="A", title=f"{h['name']} 摘要头(hop{h['hop']})",
                              uid=code_uid["uid"] if code_uid else None,
                              node_id=h["node_id"],
                              source_ref=h["path"], score=round(h["score"], 4)))
        buckets["head"].append(dict(tag=tag, title=h["name"], text=h["text"],
                                    source_ref=h["path"]))

    # ★ 统一按分数排序后再编号。之前是按桶顺序追加，
    #   图扩展的摘要头永远排在所有代码块之后，评测里直接被 top-10 截掉，
    #   导致"开不开图扩展指标几乎一样"的假象。
    citations.sort(
        key=lambda c: (-c["score"], str(c.get("uid") or c.get("title") or ""))
    )
    counter2 = defaultdict(int)
    remap: dict[str, str] = {}
    for c in citations:
        counter2[c["level"]] += 1
        new = f"{c['level']}{counter2[c['level']]}"
        remap[c["tag"]] = new
        c["tag"] = new
    for items in buckets.values():
        for it in items:
            it["tag"] = remap.get(it["tag"], it["tag"])

    ab = sum(1 for c in citations if c["level"] in ("A", "B"))
    return dict(buckets=buckets, citations=citations, used_tokens=used,
                total_tokens=sum(used.values()), ab_evidence=ab)


def _resync_tags(ctx: dict, reranked: list[dict]) -> None:
    """重排后同步上下文正文里的引用编号。

    ★ 这是个容易漏的坑：citations 重排了，但 buckets 里每条证据前面的 [A1]/[B2]
      还是旧编号。模型看到的编号和最终展示给用户的引用列表就对不上了，
      表现为"答案引用了 A1，但列表里 A1 是另一篇" —— 属于静默错误。
    """
    by_uid = {c.get("uid"): c["tag"] for c in reranked if c.get("uid")}
    by_title = {c.get("title"): c["tag"] for c in reranked}
    for items in ctx["buckets"].values():
        for it in items:
            new = by_uid.get(it.get("uid")) or by_title.get(it.get("title"))
            if new:
                it["tag"] = new


def render_context(ctx: dict) -> str:
    parts = []
    labels = {"code": "源码证据(A级)", "head": "调用上下文摘要头(A级)",
              "experience": "已审核经验(B级)", "wiki": "Wiki 背景(C级)"}
    for k, items in ctx["buckets"].items():
        if not items:
            continue
        parts.append(f"===== {labels[k]} =====")
        for it in items:
            parts.append(f"[{it['tag']}] {it['title']}  ({it['source_ref']})\n{it['text']}")
    return "\n\n".join(parts)


# ------------------------------------------------------------------ 主入口

SYSTEM_PROMPT = """你是一个 C 代码库助手。只能依据给定证据回答。

硬性要求：
1. 每个结论句末尾必须标注证据编号，如 [A1]、[B2]。
2. 证据没有覆盖的内容，直接说"证据中未涉及"，不要推测。
3. A 级=编译器确认的代码事实，B 级=人工审核过的经验，C 级=生成的 Wiki（仅作背景）。
4. 回答简洁，先给结论，再给依据。"""


def ask(conn: sqlite3.Connection, q: str, *, embedder=None, budget_tokens: int = 8000,
        data_dir: str = "data", hops: int = 2, use_graph: bool = True,
        use_vector: bool = True, use_bm25: bool = True, use_symbol: bool = True,
        expand_mode: str = "head", reranker="feature", explain: bool = False,
        llm_client=None) -> dict:
    mode = route(q)

    r_sym = recall_symbol(conn, q) if use_symbol else []
    r_bm = recall_bm25(conn, q) if use_bm25 else []
    r_vec = recall_vector(conn, q, embedder, data_dir=data_dir) if use_vector else []

    weights = [1.5 if mode == "symbol" else 1.0, 1.0, 1.0]
    fused = rrf([r_sym, r_bm, r_vec], weights=weights)

    heads = (graph_expand(conn, fused[:8], hops=hops, mode=expand_mode)
             if use_graph else [])
    ctx = assemble(conn, fused, heads, budget_tokens=budget_tokens)

    # ★ 重排：召回 → 融合 → **重排** → 生成。
    #   融合只看各路排名，看不到内容；重排才用得上"是不是定义""是不是精确同名"
    #   "扇入多少"这些结构性信号。原来缺这一环，症状是 Recall 不低但 MRR 偏低。
    from .rerank import get_reranker, apply as rerank_apply
    rr = get_reranker(reranker) if isinstance(reranker, str) else reranker
    reranked, detail = rerank_apply(conn, q, ctx["citations"], rr, explain=explain)
    ctx["citations"] = reranked
    # 上下文正文里的 [tag] 必须跟着新顺序改，否则生成侧引用编号会错位
    _resync_tags(ctx, reranked)

    result = dict(query=q, mode=mode, citations=ctx["citations"],
                  reranker=(rr.name if rr else "none"),
                  recall=dict(symbol=len(r_sym), bm25=len(r_bm), vector=len(r_vec),
                              fused=len(fused), graph_heads=len(heads)),
                  context_tokens=ctx["total_tokens"], ab_evidence=ctx["ab_evidence"])
    if explain:
        result["rerank_detail"] = [d.explain() for d in detail[:10]]

    # ★ D5：A/B 级证据为 0 → 拒答，只给线索
    if ctx["ab_evidence"] == 0:
        result["answer"] = (
            "证据不足，不给出结论。\n"
            "知识库中没有找到与该问题直接相关的代码事实或已审核经验。\n"
            "建议：换用具体的函数名/文件名检索，或先补充相关经验条目并完成审核。")
        result["refused"] = True
        return result

    result["refused"] = False
    result["context"] = render_context(ctx)

    if llm_client is None:
        # 无 LLM 模式：返回证据包本身，全流程依然可用（架构约束 #4）
        result["answer"] = None
        result["evidence_pack"] = result["context"]
    else:
        result["answer"] = llm_client.answer(SYSTEM_PROMPT, q, result["context"])
    return result
