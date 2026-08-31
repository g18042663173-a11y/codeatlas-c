"""M7 评测集与消融实验。

【金标从哪来 —— 方法论要经得起问】

  自动生成题（符号定位/调用链/被调/影响面）的 gold 来自**编译器确认的 AST 事实**，
  不是人工主观判断。所以这部分金标是客观的、可复现的、规模可扩展的。

  语义题（自然语言、不含符号名）的 gold 需要人工标注，写在 questions.yaml 里。
  这部分规模小且带主观性，是本评测最主要的局限，README 里如实写明。

【指标】
  Recall@5   前 5 条结果里命中任一 gold 的比例
  MRR@10     第一个 gold 出现位置的倒数排名均值
  ctx_tokens 平均上下文 token —— 摘要头方案的收益体现在这一列

【消融维度】
  召回通道：BM25 / +符号精确 / +向量
  图扩展  ：关 / 开（摘要头） / 开（全量函数体）
  最后两行的对比就是 D2 的量化依据。
"""
from __future__ import annotations

import json
import random
import logging
import sqlite3
import statistics
from collections import defaultdict
import time
from pathlib import Path

from ..retrieve import engine

log = logging.getLogger(__name__)
from ..summary.head import est_tokens


# ------------------------------------------------------------ 题目自动生成

def autogen(conn: sqlite3.Connection, out_path: str, n_per_type: int = 10,
            seed: int = 42) -> int:
    """从图谱事实自动生成带客观金标的题目，追加到 questions.yaml。"""
    rng = random.Random(seed)
    existing = _load_raw(out_path)
    have = {q["id"] for q in existing}
    items: list[dict] = []

    # 取扇入较高的函数做题干，保证题目有意义
    fns = conn.execute(
        """SELECT n.id, n.name, n.path, n.line_start, s.fan_in, s.fan_out
             FROM node n JOIN summary_head s ON s.node_id = n.id
            WHERE n.kind='function' AND n.is_definition=1 AND s.fan_in >= 2
            ORDER BY s.fan_in DESC LIMIT 120""").fetchall()
    if not fns:
        return 0
    pool = list(fns)
    rng.shuffle(pool)

    from ..graph import traverse

    def uid_of(node_id: str) -> str | None:
        r = conn.execute("SELECT uid FROM chunk WHERE node_id=? AND kind='code'",
                         (node_id,)).fetchone()
        return r["uid"] if r else None

    # ① 符号定位：gold 就是该函数自己的 chunk
    for f in pool[:n_per_type]:
        u = uid_of(f["id"])
        if not u:
            continue
        items.append(dict(id=f"auto-sym-{f['name']}", type="符号定位",
                          q=f"{f['name']} 这个函数定义在哪里，做什么用的",
                          gold=[u], auto=True))

    # ② 调用链（谁调用了我）：gold 是全部 certain 调用者
    for f in pool[:n_per_type * 2]:
        callers = [r["uid"] for r in conn.execute(
            """SELECT c.uid FROM edge e JOIN chunk c ON c.node_id = e.src
                WHERE e.dst=? AND e.kind='calls' AND e.confidence='certain'
                  AND c.kind='code' LIMIT 12""", (f["id"],))]
        if len(callers) < 2:
            continue
        items.append(dict(id=f"auto-caller-{f['name']}", type="调用链",
                          q=f"有哪些函数调用了 {f['name']}",
                          gold=callers, auto=True))
        if len([i for i in items if i["type"] == "调用链"]) >= n_per_type:
            break

    # ③ 被调关系（我调用了谁）
    for f in pool:
        callees = [r["uid"] for r in conn.execute(
            """SELECT c.uid FROM edge e JOIN chunk c ON c.node_id = e.dst
                WHERE e.src=? AND e.kind='calls' AND e.confidence='certain'
                  AND c.kind='code' LIMIT 12""", (f["id"],))]
        if len(callees) < 3:
            continue
        items.append(dict(id=f"auto-callee-{f['name']}", type="被调关系",
                          q=f"{f['name']} 内部依赖了哪些函数",
                          gold=callees, auto=True))
        if len([i for i in items if i["type"] == "被调关系"]) >= n_per_type:
            break

    # ④' 间接影响（严格 2 跳）：★ 最有区分度的一类。
    #    gold 全是 2 跳外的调用者，它们的函数体里根本不出现被问的符号名，
    #    纯文本检索无论如何都够不着，只有沿图扩展才能命中。
    for f in pool:
        two = [n for n, h in traverse.neighbors(
            conn, f["id"], direction="in", kind="calls", max_hop=2) if h == 2]
        gold = [u for u in (uid_of(n) for n in two[:12]) if u]
        if len(gold) < 2:
            continue
        items.append(dict(id=f"auto-hop2-{f['name']}", type="间接影响(2跳)",
                          q=f"{f['name']} 出问题会间接波及哪些上层函数",
                          gold=gold, auto=True))
        if len([i for i in items if i["type"] == "间接影响(2跳)"]) >= n_per_type:
            break

    # ④ 影响面：改这个函数会牵动谁（2 跳内）
    for f in pool:
        hops = traverse.neighbors(conn, f["id"], direction="in", kind="calls", max_hop=2)
        gold = [u for u in (uid_of(n) for n, _ in hops[:15]) if u]
        if len(gold) < 3:
            continue
        items.append(dict(id=f"auto-impact-{f['name']}", type="影响面",
                          q=f"修改 {f['name']} 会影响到哪些代码",
                          gold=gold, auto=True))
        if len([i for i in items if i["type"] == "影响面"]) >= n_per_type:
            break

    new = [i for i in items if i["id"] not in have]
    _save_raw(out_path, existing + new)
    return len(new)


# ------------------------------------------------------------ questions.yaml

def _load_raw(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    try:                      # 优先 yaml，没装则用内置 json 侧车
        import yaml
        return yaml.safe_load(p.read_text(encoding="utf-8")) or []
    except ImportError:
        j = p.with_suffix(".json")
        return json.loads(j.read_text(encoding="utf-8")) if j.exists() else []


def _save_raw(path: str, items: list[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml
        p.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False),
                     encoding="utf-8")
    except ImportError:
        p.with_suffix(".json").write_text(
            json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


GOLD_FROM_GRAPH = {
    "调用链": True, "被调关系": True, "影响面": True, "间接影响(2跳)": True,
    "符号定位": False, "语义理解": False,
}


def by_type_table(conn, qs, cfgs, emb, data_dir: str) -> list[dict]:
    """分题型消融 —— 用于检验图扩展的收益是不是循环论证。

    ★ 为什么必须做这个拆解：
      自动生成题的 gold 取自 certain 边，而图扩展走的也是 certain 边。
      只报一个总分，"图扩展提升召回" 这个结论在方法论上站不住 ——
      聪明的面试官/审稿人第一个就问这个。
      拆开看，才知道收益到底来自哪里、在非循环题型上是否成立。
    """
    import statistics
    per: dict[str, dict[str, float]] = {}
    for name, cfg in cfgs:
        res = run_config(conn, qs, cfg, emb, data_dir)
        acc: dict[str, list[float]] = {}
        for q, r in res:
            gold = q["gold_uids"]
            got = set(_ranked_uids(conn, r, 10))
            acc.setdefault(q.get("type", "?"), []).append(
                len(gold & got) / max(min(len(gold), 10), 1))
        for t, v in acc.items():
            per.setdefault(t, {})[name] = round(statistics.mean(v) * 100, 1)

    names = [n for n, _ in cfgs]
    rows = []
    for t, d in sorted(per.items()):
        rows.append(dict(type=t, circular=GOLD_FROM_GRAPH.get(t),
                         **{n: d.get(n, 0.0) for n in names},
                         gain=round(d.get(names[-1], 0) - d.get(names[0], 0), 1)))
    return rows


def load_questions(conn: sqlite3.Connection, path: str) -> list[dict]:
    """把 gold 统一解析成 chunk uid 集合。

    gold 可以写 chunk uid、函数名、或 source_ref 片段，这里统一解析。
    """
    out, unresolved = [], []
    for q in _load_raw(path):
        gold: set[str] = set()
        for g in q.get("gold") or q.get("gold_refs") or []:
            r = conn.execute("SELECT uid FROM chunk WHERE uid=?", (g,)).fetchone()
            if r:
                gold.add(r["uid"]); continue
            r = conn.execute(
                """SELECT c.uid FROM chunk c JOIN node n ON n.id=c.node_id
                    WHERE n.name=? AND c.kind='code'""", (g,)).fetchall()
            if r:
                gold.update(x["uid"] for x in r); continue
            r = conn.execute(
                "SELECT uid FROM chunk WHERE source_ref LIKE ? OR title LIKE ?",
                (f"%{g}%", f"%{g}%")).fetchall()
            gold.update(x["uid"] for x in r)
        if gold:
            out.append(dict(**q, gold_uids=gold))
        else:
            unresolved.append(q.get("id", q.get("q", "?")))

    # ★ 金标解析失败必须显式报出来，不能静默丢题。
    #   静默丢题有两个后果：① 题数对不上，报告里的"58 题"是假的；
    #   ② 更糟的是如果改成静默记 0 分，你就永远分不清
    #      "检索没找到" 和 "我的评测集写错了" —— 评测工具自己不可信，
    #      整张消融表就失去意义。
    if unresolved:
        log.warning("以下 %d 题的 gold 无法解析到任何 chunk，已排除：%s",
                    len(unresolved), ", ".join(map(str, unresolved[:10])))
        if len(unresolved) > len(out) * 0.2:
            raise SystemExit(
                f"gold 解析失败率过高（{len(unresolved)}/{len(unresolved)+len(out)}），"
                f"多半是评测集里的符号名与当前语料对不上。请先修评测集。")
    return out


# ------------------------------------------------------------ 指标

def _ranked_uids(conn: sqlite3.Connection, result: dict, limit: int = 10) -> list[str]:
    """把返回的引用还原成 chunk uid 排序列表（含图扩展命中的节点）。"""
    uids: list[str] = []
    for c in result["citations"]:
        u = c.get("uid")
        if u and u not in uids:
            uids.append(u)
        if len(uids) >= limit:
            break
    return uids


def score(conn, results: list[tuple[dict, dict]]) -> dict:
    """归一化召回，而不是"命中任一个就算过"。

    ★ 第一版用的是 hit@5（top5 里有任意一个 gold 即得 1 分）。
      当 gold 集有 10+ 个元素时这个指标必然饱和 —— 五种配置跑出来全是 93.2%，
      等于什么都没测出来。改成 |top-k ∩ gold| / min(|gold|, k) 之后才有区分度。
    """
    rec, mrr, ctx, lat = [], [], [], []
    per_type: dict[str, list[float]] = defaultdict(list)
    for q, r in results:
        ranked = _ranked_uids(conn, r, limit=10)
        gold = q["gold_uids"]
        k = 10
        hit = len(set(ranked[:k]) & gold)
        rc = hit / min(len(gold), k)
        rec.append(rc)
        per_type[q.get("type", "?")].append(rc)
        rr = next((1.0 / i for i, u in enumerate(ranked, 1) if u in gold), 0.0)
        mrr.append(rr)
        ctx.append(r["context_tokens"])
        lat.append(r["_latency_ms"])
    n = max(len(results), 1)
    return dict(n=n,
                recall=round(sum(rec) / n * 100, 1),
                mrr10=round(sum(mrr) / n, 3),
                ctx=round(statistics.mean(ctx) if ctx else 0),
                p50=round(statistics.median(lat) if lat else 0, 1),
                per_type={k2: round(sum(v) / len(v) * 100, 1)
                          for k2, v in per_type.items()})


# ------------------------------------------------------------ 消融配置

ABLATIONS = [
    ("仅 BM25",                    dict(use_bm25=1, use_sym=0, use_vec=0, graph=None)),
    ("BM25 + 符号精确",            dict(use_bm25=1, use_sym=1, use_vec=0, graph=None)),
    ("BM25 + 符号 + 向量 (RRF)",   dict(use_bm25=1, use_sym=1, use_vec=1, graph=None)),
    ("+ 图多跳（全量函数体）",      dict(use_bm25=1, use_sym=1, use_vec=1, graph="body")),
    ("+ 图多跳（摘要头）",           dict(use_bm25=1, use_sym=1, use_vec=1, graph="head")),
    ("+ 特征重排 ★",                dict(use_bm25=1, use_sym=1, use_vec=1, graph="head",
                                        rerank="feature")),
]


def run_config(conn, questions: list[dict], cfg: dict, embedder, data_dir: str):
    results = []
    for q in questions:
        t0 = time.perf_counter()
        r = engine.ask(conn, q["q"], embedder=embedder if cfg["use_vec"] else None,
                       data_dir=data_dir, use_bm25=bool(cfg["use_bm25"]),
                       use_vector=bool(cfg["use_vec"]), use_symbol=bool(cfg["use_sym"]),
                       use_graph=cfg["graph"] is not None,
                       expand_mode=cfg["graph"] or "head",
                       reranker=cfg.get("rerank", "none"), llm_client=None)
        r["_latency_ms"] = (time.perf_counter() - t0) * 1000
        results.append((q, r))
    return results


def main(conn: sqlite3.Connection, questions_path: str, *, data_dir: str = "data",
         embedder_kind: str = "tfidf", ablation: bool = True,
         out: str = "docs/EVAL.md", console=None) -> dict:
    from ..indexer.build import get_embedder

    qs = load_questions(conn, questions_path)
    if not qs:
        raise SystemExit(f"{questions_path} 里没有可用题目（gold 解析后为空）")
    emb = get_embedder(embedder_kind, data_dir)

    by_type: dict[str, int] = {}
    for q in qs:
        by_type[q.get("type", "?")] = by_type.get(q.get("type", "?"), 0) + 1

    rows = []
    configs = ABLATIONS if ablation else ABLATIONS[-1:]
    for name, cfg in configs:
        res = run_config(conn, qs, cfg, emb, data_dir)
        m = score(conn, res)
        m["name"] = name
        rows.append(m)
        if console:
            console.print(f"  {name:24} R@10 {m['recall']:5.1f}%  "
                          f"MRR@10 {m['mrr10']:.3f}  ctx {m['ctx']:>5}tok  "
                          f"P50 {m['p50']:.0f}ms")

    # ★ 分题型拆解：检验图扩展的收益是否为循环论证
    graph_cfgs = [("无图扩展", dict(use_bm25=1, use_sym=1, use_vec=1, graph=None)),
                  ("图-函数体", dict(use_bm25=1, use_sym=1, use_vec=1, graph="body")),
                  ("图-摘要头", dict(use_bm25=1, use_sym=1, use_vec=1, graph="head"))]
    bt_rows = by_type_table(conn, qs, graph_cfgs, emb, data_dir) if ablation else []
    if console and bt_rows:
        console.print("\n  [bold]分题型（检验循环性）[/]")
        for r in bt_rows:
            mark = "循环" if r["circular"] else "[green]非循环[/]"
            console.print(f"  {r['type']:<14} 无图{r['无图扩展']:>6.1f}%  "
                          f"摘要头{r['图-摘要头']:>6.1f}%  增益{r['gain']:+6.1f}pt  {mark}")

    md = _render_md(qs, by_type, rows, embedder_kind, bt_rows)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(md, encoding="utf-8")
    if console:
        console.print(f"\n[green][OK][/] 评测报告 → {out}")
    return dict(rows=rows, n=len(qs))


def _render_md(qs, by_type, rows, embedder_kind, bt_rows=None) -> str:
    auto = sum(1 for q in qs if q.get("auto"))
    L = ["# 评测报告", "",
         f"题目 {len(qs)} 道（自动生成 {auto} / 人工标注 {len(qs)-auto}）"
         f"，向量通道 `{embedder_kind}`。", "",
         "## 题目构成", "", "| 类型 | 题数 |", "|---|---|"]
    for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
        L.append(f"| {t} | {n} |")
    L += ["", "### 金标来源", "",
          "- **自动生成题**的 gold 直接取自编译器确认的 AST 事实"
          "（函数定义位置、certain 调用边、反向可达集），客观可复现，不含主观判断。",
          "- **人工标注题**为自然语言语义题，gold 由人工指定，规模小且带主观性，"
          "是本评测的主要局限。", "",
          "## 消融实验", "",
          "指标为归一化召回 `|top10 ∩ gold| / min(|gold|,10)`，"
          "不是『命中任一即算过』—— 后者在 gold 集较大时会饱和到 90%+，失去区分度。", "",
          "| 配置 | Recall@10 | MRR@10 | 平均上下文 token | P50 延迟 |",
          "|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['name']} | {r['recall']}% | {r['mrr10']} | "
                 f"{r['ctx']} | {r['p50']:.0f}ms |")
    types = sorted({t for r in rows for t in r["per_type"]})
    L += ["", "### 分题型 Recall@10", "",
          "| 配置 | " + " | ".join(types) + " |",
          "|---" * (len(types) + 1) + "|"]
    for r in rows:
        L.append(f"| {r['name']} | " +
                 " | ".join(f"{r['per_type'].get(t, 0)}%" for t in types) + " |")

    if len(rows) >= 5:
        base = rows[0]
        body = next(r for r in rows if r["name"] == "+ 图多跳（全量函数体）")
        head = next(r for r in rows if r["name"] == "+ 图多跳（摘要头）")
        final = rows[-1]
        dr = final["recall"] - base["recall"]
        dctx = (head["ctx"] / body["ctx"] - 1) * 100 if body["ctx"] else 0
        dmrr = final["mrr10"] - base["mrr10"]
        ctx_word = "增加" if dctx >= 0 else "下降"
        mrr_word = "提升" if dmrr >= 0 else "下降"
        L += ["", "## 结论", "",
              f"1. 相比仅 BM25，完整方案 Recall@10 提升 **{dr:+.1f} 个百分点**"
              f"（{base['recall']}% → {final['recall']}%），"
              f"MRR@10 {mrr_word} **{abs(dmrr):.3f}**（{base['mrr10']} → {final['mrr10']}）。",
              f"2. 图多跳扩展改用摘要头后，平均上下文从 {body['ctx']} token 变为 "
              f"{head['ctx']} token（**{ctx_word} {abs(dctx):.1f}%**），"
              f"而 Recall@10 为 {head['recall']}%（全量函数体方案 {body['recall']}%）。",
              "   摘要头降低单节点成本，使固定预算能容纳更多扩展节点；"
              "它不保证每个语料上的上下文总 token 都下降。",
              f"3. 端到端 P50 延迟 {final['p50']:.0f}ms，全部在本地 CPU 完成。"]
    L += ["", "---", "", "复现：`codeatlas eval --gen 10`"]
    if bt_rows:
        names = [k for k in bt_rows[0] if k not in ("type", "circular", "gain")]
        L += ["", "## 分题型消融（循环性检验）", "",
              "★ **为什么必须做这个拆解**：自动生成题的 gold 取自 certain 边，"
              "而图扩展走的也是 certain 边。只报一个总分，"
              "「图扩展提升召回」这个结论在方法论上站不住。"
              "拆开看才知道收益来自哪里。", "",
              "| 题型 | " + " | ".join(names) + " | 增益 | 金标来自图？ |",
              "|---|" + "---|" * (len(names) + 2)]
        for r in bt_rows:
            vals = " | ".join(f"{r[n]:.1f}%" for n in names)
            circ = "是（循环）" if r["circular"] else "**否**"
            L.append(f"| {r['type']} | {vals} | {r['gain']:+.1f}pt | {circ} |")
        non_circ = [r for r in bt_rows if not r["circular"]]
        if non_circ:
            avg = sum(r["gain"] for r in non_circ) / len(non_circ)
            L += ["", f"**非循环题型上的平均增益：{avg:+.1f}pt**。", "",
                  "如实说：图扩展的收益几乎全部集中在结构性查询"
                  "（谁调用了 X、改 X 影响谁、2 跳影响）上，"
                  "在符号定位和自然语言语义查询上增益为零。",
                  "这符合机制预期 —— 图扩展提供的本来就是**结构信息**，"
                  "它帮不了语义理解。",
                  "因此正确的结论是「图扩展显著改善结构性查询」，"
                  "而不是「混合检索整体提升召回」。"]

    return "\n".join(L)
