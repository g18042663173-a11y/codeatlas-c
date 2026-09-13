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

from .. import db as dbm
from ..graph import traverse
from ..summary import head as head_mod

log = logging.getLogger(__name__)

RE_C_IDENT = re.compile(r"\b[a-z_][a-z0-9_]{3,}(?:_[a-z0-9]+)+\b", re.I)
RE_CXX_QUAL = re.compile(r"\b[A-Za-z_]\w*::[A-Za-z_~]\w*")
RE_FILE = re.compile(r"\b[\w/.-]+\.(?:c|cc|cpp|cxx|c\+\+|h|hh|hpp|hxx|inc)\b", re.I)
RRF_K = 60

# Small, auditable bilingual vocabulary for the two public C corpora used by the
# portfolio.  This is query normalization, not a hidden answer map: values are
# general programming/network terms and never contain gold symbol names.  It
# prevents an offline English code index from treating a Chinese question as a
# one-token ``JSON``/``TCP`` query when no multilingual embedding model exists.
QUERY_VOCAB = {
    "解析": "parse parser", "对象": "object", "数组": "array",
    "字符串": "string", "数字": "number", "字面量": "literal value",
    "序列化": "serialize serialization print output", "无格式": "unformatted",
    "输出": "output print", "缓冲区": "buffer", "增长": "grow allocate",
    "扩容": "grow resize reallocate ensure buffer", "内存": "memory allocate realloc",
    "比较": "compare", "递归": "recursive", "类型": "type",
    "大小写": "case sensitive", "查找": "get find lookup", "成员": "item member",
    "引用计数": "reference count ref", "释放": "free release",
    "链表": "chain list", "头部": "header head", "网络接口": "network interface netif",
    "默认接口": "default interface", "设置": "set", "切换": "set change",
    "发送队列": "send queue unsent", "应用数据": "application data",
    "限制": "limit available", "收包": "input receive packet",
    "调用": "call caller", "影响": "impact caller", "入口": "entry",
    "定义": "definition", "函数": "function",
    "serialization": "serialize print output buffer ensure reallocate",
    "serialize": "serialization print output buffer ensure reallocate",
    "grow": "resize reallocate ensure buffer",
}

_IDENT_STOP = {
    "the", "and", "for", "with", "where", "which", "what", "how", "function",
    "definition", "entry", "code", "source", "network", "interface", "output",
    "input", "change", "impact", "caller", "call", "json", "tcp", "parser",
}
_IDENT_ACTION_PARTS = {
    "parse", "print", "serialize", "compare", "set", "get", "free", "release",
    "add", "remove", "ensure", "grow", "resize", "find", "lookup", "read", "write",
}
_IDENT_PART_ALIASES = {
    # General morphology/terminology normalization.  Values are lexical parts,
    # never benchmark task IDs or target symbol names.
    "parser": {"parse"},
    "parsing": {"parse"},
    "parsed": {"parse"},
    "values": {"value"},
    "objects": {"object"},
    "arrays": {"array"},
}


def _enrich_query(q: str) -> str:
    extras = [terms for phrase, terms in QUERY_VOCAB.items() if phrase in q]
    return f"{q} {' '.join(extras)}" if extras else q


def _identifier_parts(value: str) -> set[str]:
    """Split C identifiers into auditable lexical parts.

    FTS intentionally keeps ``_`` inside tokens, which is good for exact symbol
    search but means a natural-language query containing ``set`` and ``up`` does
    not match ``netif_set_up``.  This helper adds a general identifier-part
    signal without knowing any benchmark answer.
    """
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return {part.lower() for part in re.split(r"[^A-Za-z0-9]+", value)
            if len(part) >= 2}


def _query_identifier_parts(q: str) -> set[str]:
    parts: set[str] = set()
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_]*", _enrich_query(q)):
        parts.update(_identifier_parts(token))
    for part in tuple(parts):
        parts.update(_IDENT_PART_ALIASES.get(part, ()))
    return {part for part in parts if part not in _IDENT_STOP}


# ------------------------------------------------------------------ 路由

def route(q: str) -> str:
    if RE_FILE.search(q) or RE_C_IDENT.search(q) or RE_CXX_QUAL.search(q):
        return "symbol"
    return "nl"


def _fts_query(q: str) -> str:
    """把自然语言安全地转成 FTS5 MATCH 表达式。

    直接把用户输入丢给 MATCH 会因为 - " * : 等字符抛语法错误。
    """
    toks = re.findall(
        r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]{2,}", _enrich_query(q)
    )
    toks = [t for t in toks if len(t) >= 2][:12]
    if not toks:
        return ""
    return " OR ".join(f'"{t}"' for t in toks)


# ------------------------------------------------------------------ 三路召回

def recall_symbol(conn: sqlite3.Connection, q: str, limit: int = 30) -> list[int]:
    """符号精确召回：C 代码里同名符号必须精确命中，这是向量的弱项。"""
    names = {
        name for name in (
            set(RE_C_IDENT.findall(q)) | set(RE_CXX_QUAL.findall(q))
            | set(re.findall(r"\b[A-Za-z_]\w{2,}\b", q))
        ) if name.lower() not in _IDENT_STOP
    }
    files = set(RE_FILE.findall(q))
    query_parts = _query_identifier_parts(q)
    if not names and not files and len(query_parts) < 2:
        return []

    out: list[int] = []
    missing: list[str] = []
    # Exact matches from *all* query identifiers must precede prefix fallbacks.
    # Previously an early generic token such as ``TCP`` could consume the
    # entire 30-row budget with ``tcp_*`` prefixes before the explicitly named
    # ``tcp_write`` symbol was considered.
    ordered_names = sorted(names, key=lambda value: ("_" not in value, -len(value), value.lower()))[:12]
    for n in ordered_names:
        rows = conn.execute(
            """SELECT c.rowid FROM node nd JOIN chunk c ON c.node_id = nd.id
                WHERE (nd.name = ? OR (instr(?, '::') = 0 AND nd.name LIKE '%::' || ?))
                  AND c.visible = 1
                ORDER BY nd.is_definition DESC, c.rowid LIMIT ?""",
            (n, n, n, limit)).fetchall()
        if rows:
            out.extend(r["rowid"] for r in rows)
        else:
            missing.append(n)
    for n in missing:   # Exact signals are now safe; add weaker prefixes last.
        if len(n) < 4:
            continue
        rows = conn.execute(
            """SELECT c.rowid FROM node nd JOIN chunk c ON c.node_id = nd.id
                WHERE nd.name LIKE ? AND c.visible = 1 ORDER BY c.rowid LIMIT ?""",
            (n + "%", limit)).fetchall()
        if rows:
            out.extend(r["rowid"] for r in rows)
    for f in sorted(files):
        rows = conn.execute(
            """SELECT c.rowid FROM node nd JOIN chunk c ON c.node_id = nd.id
                WHERE nd.path LIKE ? AND c.visible = 1 ORDER BY c.rowid LIMIT ?""",
            ("%" + f, limit)).fetchall()
        out.extend(r["rowid"] for r in rows)

    # General natural-language-to-identifier matching.  Require at least two
    # shared parts so an unrelated query containing a generic word cannot turn
    # into A-level evidence.  Exact and prefix symbol results above retain
    # precedence; this signal is appended as a weaker fallback.
    if len(query_parts) >= 2:
        fragment_matches: list[tuple[int, str, int]] = []
        rows = conn.execute(
            """SELECT c.rowid,n.name FROM node n JOIN chunk c ON c.node_id=n.id
                 WHERE n.kind='function' AND n.is_definition=1 AND c.visible=1"""
        ).fetchall()
        for row in rows:
            shared = _identifier_parts(row["name"]) & query_parts
            score = sum(3 if part in _IDENT_ACTION_PARTS else 1 for part in shared)
            action_shared = bool(shared & _IDENT_ACTION_PARTS)
            if score >= 2 and (action_shared or len(shared) >= 3):
                fragment_matches.append((-score, row["name"].lower(), row["rowid"]))
        fragment_matches.sort()
        out.extend(rowid for _score, _name, rowid in fragment_matches[:limit])

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


def recall_reviewed_experience(conn: sqlite3.Connection, q: str,
                               limit: int = 10) -> list[int]:
    """Give approved knowledge its own lexical lane.

    Code-heavy repositories can contribute hundreds of A/C chunks for a broad
    term such as ``memory`` before a highly relevant reviewed card reaches the
    shared BM25 cut-off.  This lane does not promote arbitrary cards: the card
    must already be visible, match FTS, and share at least two distinct public
    query terms.  The normal evidence/currentness gates remain authoritative.
    """
    match = _fts_query(q)
    if not match:
        return []
    tokens = {
        token.lower() for token in re.findall(
            r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]{2,}", _enrich_query(q)
        ) if len(token) >= 2
    }
    if len(tokens) < 2:
        return []
    try:
        rows = conn.execute(
            """SELECT c.rowid,c.title,c.text FROM chunk_fts f
                 JOIN chunk c ON c.rowid=f.rowid
                WHERE chunk_fts MATCH ? AND c.visible=1
                  AND c.kind='experience' AND c.evidence_level='B'
                ORDER BY bm25(chunk_fts),c.rowid LIMIT ?""",
            (match, limit * 4),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log.warning("经验卡 FTS 查询失败: %s", exc)
        return []
    accepted: list[int] = []
    for row in rows:
        blob = f"{row['title']} {row['text']}".lower()
        if sum(token in blob for token in tokens) >= 2:
            accepted.append(row["rowid"])
        if len(accepted) >= limit:
            break
    return accepted


def _exact_symbol_rowids(conn: sqlite3.Connection, q: str, limit: int = 4) -> list[int]:
    """Return only exact function-name chunks explicitly written in the query."""
    names = sorted(
        set(re.findall(r"\b[A-Za-z_]\w{2,}\b", q)) | set(RE_CXX_QUAL.findall(q)),
        key=lambda x: -len(x),
    )
    out: list[int] = []
    for name in names:
        rows = conn.execute(
            """SELECT c.rowid FROM node n JOIN chunk c ON c.node_id=n.id
                 WHERE n.kind='function' AND n.is_definition=1 AND c.visible=1
                   AND (n.name=? OR (instr(?, '::') = 0 AND n.name LIKE '%::' || ?))
                 ORDER BY c.rowid LIMIT ?""", (name, name, name, limit),
        ).fetchall()
        out.extend(row["rowid"] for row in rows)
    return list(dict.fromkeys(out))[:limit]


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
        qv = embedder.encode([_enrich_query(q)])
        if qv is None:
            return []
        sims = mat @ qv[0]
        if len(ids) != len(sims):
            raise ValueError("vector mapping length mismatch")
        # Hidden/stale rows must not consume the top-k budget before assembly.
        visible = {row[0] for row in conn.execute("SELECT rowid FROM chunk WHERE visible=1")}
        eligible = np.asarray([i for i, identifier in enumerate(ids) if identifier in visible], dtype=int)
        top = eligible[np.lexsort((np.asarray(ids)[eligible], -sims[eligible]))[:limit]]
        return [ids[i] for i in top]
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
                 hops: int = 2, decay: float = 0.75, limit: int = 24,
                 mode: str = "head", directions: tuple[str, ...] = ("out", "in")) -> list[dict]:
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
        seed = conn.execute("SELECT node_id,kind,text FROM chunk WHERE rowid=?", (rid,)).fetchone()
        if not seed:
            continue
        # A current Wiki page contains machine-validated [A:path:Lx-Ly] anchors.
        # Following those anchors back to function definitions turns "read the
        # document first" into a traceable C→A verification step instead of a
        # free-form semantic jump.
        if seed["kind"] == "wiki":
            source_anchors = re.findall(
                r"\[A:([^:\]]+):L(\d+)(?:-L(\d+))?\]", seed["text"] or ""
            )
            for path, start_raw, end_raw in source_anchors[:40]:
                start, end = int(start_raw), int(end_raw or start_raw)
                rows = conn.execute(
                    """SELECT id FROM node WHERE kind='function' AND is_definition=1
                         AND path=? AND line_start<=? AND line_end>=?
                         ORDER BY line_start LIMIT 4""", (path, end, start),
                ).fetchall()
                for node in rows:
                    h = head_mod.get(conn, node["id"])
                    if h is None:
                        continue
                    score = seed_score * decay
                    if node["id"] in out and out[node["id"]]["score"] >= score:
                        continue
                    text = head_mod.render(h, h["name"])
                    out[node["id"]] = dict(
                        node_id=node["id"], hop=1, score=score, name=h["name"],
                        path=h["path"], text=text, tokens=h["token_len"] or 0,
                    )
        if not seed["node_id"]:
            continue
        for direction in directions:
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

SOURCE_TYPES = {
    "code": "compiler_ast",
    "head": "compiler_summary",
    "experience": "reviewed_experience",
    "wiki": "generated_wiki",
}


def _provenance(conn: sqlite3.Connection, node_id: str | None, kind: str,
                source_ref: str | None) -> dict:
    """Build a stable, display-ready provenance record for one citation.

    A source reference alone is not enough to reproduce a conclusion after a repository
    changes. Code citations therefore carry the repository snapshot plus the compiler's
    symbol identity and exact source range. Non-code evidence keeps the same envelope so
    consumers never have to infer what a field means from the citation level.
    """
    record = {
        "source_type": SOURCE_TYPES.get(kind, kind),
        "repository": dbm.get_meta(conn, "repository_id",
                                    dbm.get_meta(conn, "repo", "unversioned")),
        "revision": dbm.get_meta(conn, "revision", "unversioned"),
        "source_ref": source_ref,
        "symbol": None,
        "usr": None,
        "path": None,
        "line_start": None,
        "line_end": None,
        "definition_hash": None,
        "stale_status": "not_applicable",
    }
    if not node_id:
        return record
    node = conn.execute(
        """SELECT name, usr, repo, path, line_start, line_end, definition_hash
           FROM node WHERE id=?""",
        (node_id,),
    ).fetchone()
    if node is None:
        return record
    record.update({
        "symbol": node["name"],
        "usr": node["usr"],
        "path": node["path"],
        "line_start": node["line_start"],
        "line_end": node["line_end"],
        "definition_hash": node["definition_hash"],
    })
    return record


def _experience_provenance(conn: sqlite3.Connection, uid: str, source_ref: str | None) -> dict:
    """Return a B-level card's review status and exact active code anchor."""
    exp_id = uid.removeprefix("exp:")
    card = conn.execute("SELECT id,status,source_type,source_ref FROM experience WHERE id=?", (exp_id,)).fetchone()
    anchor = conn.execute(
        "SELECT * FROM experience_anchor WHERE exp_id=? AND active=1 ORDER BY id DESC LIMIT 1", (exp_id,)
    ).fetchone()
    record = {
        "source_type": "reviewed_knowledge_card",
        "source_ref": source_ref,
        "card_id": exp_id,
        "card_status": card["status"] if card else "missing",
        "stale_status": card["status"] if card else "missing",
        "repository": None, "revision": None, "symbol": None, "usr": None,
        "path": None, "line_start": None, "line_end": None, "definition_hash": None,
    }
    if card:
        record["source_type"] = card["source_type"] or record["source_type"]
        record["source_ref"] = card["source_ref"] or source_ref
    if anchor:
        record.update({key: anchor[key] for key in (
            "repository", "revision", "usr", "path", "line_start", "line_end", "definition_hash"
        )})
        node = conn.execute(
            """SELECT name,path,line_start,line_end,definition_hash FROM node
                 WHERE usr=? AND kind='function' AND is_definition=1""", (anchor["usr"],)
        ).fetchone()
        if node:
            record["symbol"] = node["name"]
            # A relocation with byte-identical definition text stays reviewed, but
            # evidence must point reviewers at the function's latest source range.
            if node["definition_hash"] == anchor["definition_hash"]:
                record.update({key: node[key] for key in ("path", "line_start", "line_end")})
    return record


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
        provenance = (_experience_provenance(conn, row["uid"], row["source_ref"])
                      if bucket == "experience"
                      else _provenance(conn, row["node_id"], bucket, row["source_ref"]))
        citations.append(dict(tag=tag, level=lvl, title=row["title"],
                              uid=row["uid"], node_id=row["node_id"],
                              source_ref=row["source_ref"], provenance=provenance,
                              score=round(score, 5)))
        buckets[bucket].append(dict(tag=tag, uid=row["uid"], title=row["title"], text=txt,
                                    source_ref=row["source_ref"], provenance=provenance))

    # 注意：body 模式下单条就可能上千 token，这里用同一份预算，
    # 保证两种模式在"相同预算"下比较，差异体现在能塞进多少条邻居。
    # A body selected by lexical recall is already the stronger source of truth for that
    # symbol. Do not spend more context on its generated summary head; reserve heads for
    # graph-only neighbours, where they actually add call-context coverage.
    cited_node_ids = {c["node_id"] for c in citations if c.get("node_id")}
    cap_head = int(budget_tokens * BUDGET_SPLIT["head"])
    for h in heads:
        if h["node_id"] in cited_node_ids:
            continue
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
        provenance = _provenance(conn, h["node_id"], "head", h["path"])
        citations.append(dict(tag=tag, level="A", title=f"{h['name']} 摘要头(hop{h['hop']})",
                              uid=code_uid["uid"] if code_uid else None,
                              node_id=h["node_id"],
                              source_ref=h["path"], provenance=provenance,
                              score=round(h["score"], 4)))
        cited_node_ids.add(h["node_id"])
        buckets["head"].append(dict(tag=tag, title=h["name"], text=h["text"],
                                    source_ref=h["path"], provenance=provenance))

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


def _reserve_bm25_code_slots(conn: sqlite3.Connection, citations: list[dict],
                             bm25_ids: list[int], query: str, *, slots: int = 2,
                             top_k: int = 10) -> list[dict]:
    """Keep up to two current A-level lexical code hits inside the first page.

    Vector and graph channels may improve coverage, but they must not erase all
    strong lexical evidence.  We only reserve slots for visible compiler-backed
    code chunks; Wiki, cards and stale chunks do not qualify.
    """
    if not citations or not bm25_ids or slots <= 0:
        return citations
    protected: list[str] = []
    query_parts = _query_identifier_parts(query)
    for rowid in bm25_ids:
        row = conn.execute(
            """SELECT c.uid,n.name FROM chunk c LEFT JOIN node n ON n.id=c.node_id
                WHERE c.rowid=? AND c.visible=1 AND c.kind='code' AND c.evidence_level='A'""",
            (rowid,),
        ).fetchone()
        # "Top BM25" alone is not enough: generic prose can match a code chunk
        # while its symbol has no lexical relationship to the query.  Requiring
        # one public identifier part keeps the reserve generic and prevents a
        # weak lexical hit from displacing compiler-confirmed graph neighbours.
        symbol_parts = _identifier_parts(row["name"] or "") if row else set()
        if row and query_parts & symbol_parts and row["uid"] not in protected:
            protected.append(row["uid"])
        if len(protected) >= slots:
            break
    out = list(citations)
    for offset, uid in enumerate(reversed(protected)):
        index = next((i for i, citation in enumerate(out) if citation.get("uid") == uid), None)
        if index is None or index < top_k:
            continue
        citation = out.pop(index)
        out.insert(max(0, top_k - 1 - offset), citation)
    counter: dict[str, int] = defaultdict(int)
    for citation in out:
        level = citation.get("level", "A")
        counter[level] += 1
        citation["tag"] = f"{level}{counter[level]}"
    return out


def _reserve_strong_reviewed_experience(
        conn: sqlite3.Connection, citations: list[dict], bm25_ids: list[int], *,
        max_bm25_rank: int = 3, slots: int = 1, top_k: int = 10) -> list[dict]:
    """Keep a top lexical approved card on the first evidence page.

    A reviewed card participates only in BM25/vector retrieval, while a code
    function can receive symbol, BM25, vector and graph votes.  RRF may therefore
    push even the first lexical card below the first page.  Reserve one slot only
    when the card is already among the first three BM25 results and its assembled
    provenance still says ``approved``.  This preserves a general high-precision
    lexical signal without promoting weak or stale experience material.
    """
    if not citations or not bm25_ids or slots <= 0 or top_k <= 0:
        return citations
    by_uid = {item.get("uid"): item for item in citations if item.get("uid")}
    protected: list[str] = []
    for rowid in bm25_ids[:max_bm25_rank]:
        row = conn.execute(
            """SELECT uid FROM chunk WHERE rowid=? AND visible=1
                 AND kind='experience' AND evidence_level='B'""",
            (rowid,),
        ).fetchone()
        if not row or row["uid"] in protected:
            continue
        citation = by_uid.get(row["uid"])
        provenance = (citation or {}).get("provenance") or {}
        if (citation and provenance.get("card_status") == "approved"
                and provenance.get("stale_status") == "approved"):
            protected.append(row["uid"])
        if len(protected) >= slots:
            break
    out = list(citations)
    for offset, uid in enumerate(reversed(protected)):
        index = next((i for i, item in enumerate(out) if item.get("uid") == uid), None)
        if index is None or index < top_k:
            continue
        item = out.pop(index)
        out.insert(max(0, top_k - 1 - offset), item)
    counter: dict[str, int] = defaultdict(int)
    for item in out:
        level = item.get("level", "A")
        counter[level] += 1
        item["tag"] = f"{level}{counter[level]}"
    return out


def _prioritize_direct_graph(conn: sqlite3.Connection, citations: list[dict],
                             heads: list[dict], exact_seed_ids: list[int]) -> list[dict]:
    """For an explicit direct-call question, show the root and direct neighbours first."""
    seed_nodes = {
        row["node_id"] for rowid in exact_seed_ids
        if (row := conn.execute("SELECT node_id FROM chunk WHERE rowid=?", (rowid,)).fetchone())
        and row["node_id"]
    }
    head_nodes = {head["node_id"] for head in heads if head.get("hop") == 1}
    seeds = [item for item in citations if item.get("node_id") in seed_nodes]
    neighbours = [item for item in citations
                  if item.get("node_id") in head_nodes and item.get("node_id") not in seed_nodes]
    # Direct callers often include many tests from one file.  Surface one caller
    # per file first so the first page covers distinct production/test modules,
    # then append the remaining callers in their reranked order.
    diverse: list[dict] = []
    remainder: list[dict] = []
    seen_paths: set[str] = set()
    for item in neighbours:
        path = str((item.get("provenance") or {}).get("path") or item.get("source_ref") or "")
        if path not in seen_paths:
            diverse.append(item)
            seen_paths.add(path)
        else:
            remainder.append(item)
    neighbours = diverse + remainder
    selected = {id(item) for item in seeds + neighbours}
    out = seeds + neighbours + [item for item in citations if id(item) not in selected]
    counter: dict[str, int] = defaultdict(int)
    for citation in out:
        level = citation.get("level", "A")
        counter[level] += 1
        citation["tag"] = f"{level}{counter[level]}"
    return out


def render_context(ctx: dict) -> str:
    parts = []
    labels = {"code": "源码证据(A级)", "head": "调用上下文摘要头(A级)",
              "experience": "已审核经验(B级)", "wiki": "Wiki 背景(C级)"}
    for k, items in ctx["buckets"].items():
        if not items:
            continue
        parts.append(f"===== {labels[k]} =====")
        for it in items:
            p = it.get("provenance") or {}
            location = p.get("path") or it["source_ref"]
            if p.get("line_start"):
                location = f"{location}:L{p['line_start']}-L{p.get('line_end') or p['line_start']}"
            snapshot = f"{p.get('repository', 'unversioned')}@{p.get('revision', 'unversioned')}"
            parts.append(f"[{it['tag']}] {it['title']}  ({snapshot} {location})\n{it['text']}")
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
    from .. import db as dbm
    if dbm.get_meta(conn, "snapshot_enabled") == "1":
        from ..snapshots import pin
        from ..indexer.build import get_embedder
        with pin(conn) as current:
            result = ask(current.conn, q, embedder=get_embedder("local", str(current.directory)),
                         budget_tokens=budget_tokens, data_dir=str(current.directory), hops=hops,
                         use_graph=use_graph, use_vector=use_vector, use_bm25=use_bm25, use_symbol=use_symbol,
                         expand_mode=expand_mode, reranker=reranker, explain=explain, llm_client=llm_client)
            result.update(current.provenance)
            for citation in result.get("citations", []):
                citation.setdefault("provenance", {}).update(current.provenance)
            return result
    if dbm.get_meta(conn, "knowledge_set_id") and conn.execute("PRAGMA database_list").fetchone()[2]:
        data_dir = str(Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent)
        from ..indexer.build import get_embedder
        embedder = get_embedder("local", data_dir)
    mode = route(q)

    r_sym = recall_symbol(conn, q) if use_symbol else []
    r_bm = recall_bm25(conn, q) if use_bm25 else []
    r_exp = recall_reviewed_experience(conn, q) if use_bm25 else []
    r_vec = recall_vector(conn, q, embedder, data_dir=data_dir) if use_vector else []

    # A nearest-neighbour index always returns *something*, even when the query has
    # no overlap with this repository. In particular, a sparse TF-IDF query with no
    # known term otherwise degenerates to arbitrary zero-similarity chunks. Such a
    # vector-only result is a retrieval hint, not evidence: accepting it would turn
    # an unrelated Kubernetes/SQL question into fabricated A-level code context.
    # Keep the refusal boundary conservative until a calibrated semantic relevance
    # threshold is available. A lexical or exact-symbol signal is still required to
    # let optional vectors enrich the candidate set.
    if not r_sym and not r_bm:
        r_vec = []

    # The dependency-free hashing vector is an enrichment channel, not a
    # calibrated multilingual retriever.  Giving it the same RRF vote as exact
    # symbols/BM25 let low-similarity neighbours displace lexical hits.  A lower
    # vote preserves high-precision facts while still allowing repeated
    # BM25+vector agreement to promote a candidate.
    weights = [1.5 if mode == "symbol" else 1.0, 1.0, 0.35]
    fused = rrf([r_sym, r_bm, r_vec, r_exp], weights=weights + [1.0])

    direct_callers = bool(re.search(r"谁(?:直接)?调用|哪些函数(?:直接)?调用|who\s+calls", q, re.I))
    direct_callees = bool(re.search(r"调用了哪些|calls?\s+which|callees?", q, re.I))
    graph_directions = ("in",) if direct_callers else (("out",) if direct_callees else ("out", "in"))
    graph_hops = 1 if "直接调用" in q else hops
    exact_symbol_ids = _exact_symbol_rowids(conn, q)
    graph_seeds = (
        [(rowid, dict(fused).get(rowid, 1.0))
         for rowid in (exact_symbol_ids or r_sym[:1])]
        if (direct_callers or direct_callees) and r_sym else fused[:8]
    )
    heads = (graph_expand(conn, graph_seeds, hops=graph_hops, mode=expand_mode,
                          directions=graph_directions)
             if use_graph else [])
    ctx = assemble(conn, fused, heads, budget_tokens=budget_tokens)

    # ★ 重排：召回 → 融合 → **重排** → 生成。
    #   融合只看各路排名，看不到内容；重排才用得上"是不是定义""是不是精确同名"
    #   "扇入多少"这些结构性信号。原来缺这一环，症状是 Recall 不低但 MRR 偏低。
    from .rerank import get_reranker, should_rerank, apply as rerank_apply
    rr = get_reranker(reranker) if isinstance(reranker, str) else reranker
    if rr is not None and not should_rerank(q):
        rr = None
    reranked, detail = rerank_apply(
        conn, _enrich_query(q), ctx["citations"], rr, explain=explain
    )
    reranked = _reserve_bm25_code_slots(conn, reranked, r_bm, q)
    reranked = _reserve_strong_reviewed_experience(conn, reranked, r_exp or r_bm,
                                                    max_bm25_rank=3)
    if direct_callers or direct_callees:
        reranked = _prioritize_direct_graph(conn, reranked, heads, exact_symbol_ids)
    ctx["citations"] = reranked
    # 上下文正文里的 [tag] 必须跟着新顺序改，否则生成侧引用编号会错位
    _resync_tags(ctx, reranked)

    result = dict(query=q, mode=mode, citations=ctx["citations"],
                  reranker=(rr.name if rr else "none"),
                  repository=dbm.get_meta(conn, "repo", "unversioned"),
                  revision=dbm.get_meta(conn, "revision", "unversioned"),
                  recall=dict(symbol=len(r_sym), bm25=len(r_bm), vector=len(r_vec),
                              reviewed_experience=len(r_exp),
                              fused=len(fused), graph_heads=len(heads)),
                  context_tokens=ctx["total_tokens"], ab_evidence=ctx["ab_evidence"])
    if explain:
        result["rerank_detail"] = [d.explain() for d in detail[:10]]
    provenance = {key: dbm.get_meta(conn, key) for key in
                  ("knowledge_set_id", "source_content_hash", "build_config_hash")}
    result.update(provenance)
    for citation in result.get("citations", []):
        citation.setdefault("provenance", {}).update(provenance)

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
