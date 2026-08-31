"""CodeAtlas 命令行入口。每个阶段一条命令，全部幂等、可中断续跑。"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import db as dbm

app = typer.Typer(add_completion=False, help="大型 C 工程 AI 知识库")
exp_app = typer.Typer(help="专家经验库")
app.add_typer(exp_app, name="exp")
console = Console()

DEFAULT_DB = "data/kb.db"


def _setup_log(verbose: bool) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")


# --------------------------------------------------------------------- init

@app.command()
def init(db: str = DEFAULT_DB, force: bool = False):
    """建库。"""
    if force:
        dbm.reset(db)
    conn = dbm.connect(db)
    n = conn.execute("SELECT COUNT(*) c FROM node").fetchone()["c"]
    console.print(f"[green][OK][/] 知识库就绪: {db}  (现有节点 {n})")


# -------------------------------------------------------------------- parse

@app.command()
def parse(repo: str, db: str = DEFAULT_DB, compile_db: str | None = None,
          include: list[str] = typer.Option([], "--include", "-I"),
          define: list[str] = typer.Option([], "--define", "-D"),
          limit: int = 0, force: bool = False, incremental: bool = True,
          verbose: bool = False):
    """解析 C 工程 → 节点 / 边（含 certain / candidate 双置信度）。

    默认增量：只重解析内容或编译参数发生变化的翻译单元。
    --force 强制全量重建。
    """
    _setup_log(verbose)
    from .parser import ast_walker, compile_db as cdb, incremental as inc

    full_rebuild = force or not Path(db).exists()
    if full_rebuild:
        dbm.reset(db)
    conn = dbm.connect(db)
    inc.ensure_schema(conn)

    units, degraded = cdb.load(repo, compile_db, include, define)
    if limit:
        units = units[:limit]
    if degraded:
        console.print("[yellow][WARN] 降级模式[/]：无 compile_commands.json，解析质量下降")

    t0 = time.time()
    plan_stats = {}
    if incremental and not full_rebuild:
        todo, removed, plan_stats = inc.plan(conn, units, repo)
        purged = inc.purge_tu(conn, [u.source for u in todo] + removed)
        if plan_stats["reuse"] == plan_stats["total"] and not removed:
            console.print(f"[green][OK][/] 无变更，全部 {plan_stats['total']} 个翻译单元复用 "
                          f"({time.time() - t0:.2f}s)")
            return
        console.print(f"[cyan]增量[/] 复用{plan_stats['reuse']} 变更{plan_stats['changed']} "
                      f"新增{plan_stats['new']} 删除{plan_stats['removed']} "
                      f"撤销旧边{purged}")
        units = todo

    walker = ast_walker.AstWalker(repo)
    with console.status(f"解析 {len(units)} 个翻译单元…") as st:
        for i, u in enumerate(units, 1):
            diag = walker.walk_unit(u.source, u.args, u.directory)
            # ★ 即使是全量重建也要记录 TU 状态，否则下一次增量无从比对
            #   （之前这里挂在 incremental 上，导致 --force 之后增量永远启动不了）
            if walker.last_tu is not None:
                inc.record(conn, u.source, inc.collect_deps(walker.last_tu, repo),
                           u.args, max(diag, 0))
            if i % 10 == 0 or i == len(units):
                st.update(f"解析中 {i}/{len(units)}  "
                          f"节点{len(walker.result.nodes)} 边{len(walker.result.edges)}")
    ast_walker.persist(conn, walker.result)
    if not full_rebuild:
        inc.gc_orphan_nodes(conn)
        inc.stale_summary_and_chunks(conn)
    conn.commit()
    dt = time.time() - t0

    s = walker.result.stats
    dbm.set_meta(conn, "repo", os.path.abspath(repo))
    dbm.set_meta(conn, "degraded", "1" if degraded else "0")
    conn.commit()

    cert = conn.execute("SELECT COUNT(*) c FROM edge WHERE confidence='certain'").fetchone()["c"]
    cand = conn.execute("SELECT COUNT(*) c FROM edge WHERE confidence='candidate'").fetchone()["c"]

    t = Table(title=f"解析完成 ({dt:.1f}s)")
    t.add_column("指标"); t.add_column("值", justify="right")
    t.add_row("翻译单元 成功/失败", f"{s['tu_ok']} / {s['tu_failed']}")
    t.add_row("编译诊断错误", str(s["diag_error"]))
    t.add_row("节点总数", str(conn.execute("SELECT COUNT(*) c FROM node").fetchone()["c"]))
    qn = lambda k: conn.execute("SELECT COUNT(*) c FROM node WHERE kind=?", (k,)).fetchone()["c"]
    t.add_row("  函数(定义)", str(conn.execute(
        "SELECT COUNT(*) c FROM node WHERE kind='function' AND is_definition=1").fetchone()["c"]))
    t.add_row("  结构体/枚举/宏", f"{qn('struct')}/{qn('enum')}/{qn('macro')}")
    t.add_row("USR 去重命中", str(s["node_dedup"]))
    t.add_row("[green]确定边 certain[/]", str(cert))
    t.add_row("[yellow]候选边 candidate[/]", str(cand))
    t.add_row("  ├ 函数指针", str(s["call_candidate_fnptr"]))
    t.add_row("  ├ 取地址", str(s["call_candidate_addrtaken"]))
    t.add_row("  └ 未解析", str(s["call_candidate_unresolved"]))
    console.print(t)


# ---------------------------------------------------------------- summary

@app.command(name="summary")
def summary_cmd(db: str = DEFAULT_DB, use_llm: bool = False, verbose: bool = False):
    """生成检索摘要头（≤200 token/函数）。"""
    _setup_log(verbose)
    from .summary import head
    conn = dbm.connect(db)
    repo = dbm.get_meta(conn, "repo", ".")
    s = head.build(conn, repo, use_llm=use_llm)
    console.print(f"[green][OK][/] 摘要头 {s['total']} 个 "
                  f"(跳过{s['skipped']} 截断{s['truncated']} LLM增强{s['llm']}) "
                  f"最大 token {s['max_tokens']}")


# ------------------------------------------------------------------ index

@app.command(name="index")
def index_cmd(db: str = DEFAULT_DB, embedder: str = "null", data_dir: str = "data",
              verbose: bool = False):
    """构建 BM25 / 向量索引。embedder=null 时退化为纯 BM25+图。"""
    _setup_log(verbose)
    from .indexer import build
    conn = dbm.connect(db)
    repo = dbm.get_meta(conn, "repo", ".")
    s = build.build(conn, repo, embedder_kind=embedder, out_dir=data_dir)
    console.print(f"[green][OK][/] chunk {s['chunks']} (可见{s['visible']} "
                  f"[yellow]待审核隐藏{s['hidden_pending']}[/]) "
                  f"向量{s['vectors']} embedder={s['embedder']}")


# -------------------------------------------------------------------- ask

@app.command()
def ask(question: str, db: str = DEFAULT_DB, budget: int = 8000, hops: int = 2,
        embedder: str = "null", data_dir: str = "data",
        no_graph: bool = False, no_vector: bool = False, no_bm25: bool = False,
        show_context: bool = False):
    """提问。无 LLM key 时返回证据包，全流程照样可用。"""
    from .retrieve import engine
    from .indexer.build import get_embedder
    from .llm.client import get_client

    conn = dbm.connect(db)
    emb = None if (no_vector or embedder == "null") else get_embedder(embedder)
    r = engine.ask(conn, question, embedder=emb, budget_tokens=budget, hops=hops,
                   data_dir=data_dir, use_graph=not no_graph,
                   use_vector=not no_vector, use_bm25=not no_bm25,
                   llm_client=get_client())

    console.print(f"\n[bold]Q:[/] {question}")
    console.print(f"[dim]模式={r['mode']}  召回 符号{r['recall']['symbol']}/"
                  f"BM25 {r['recall']['bm25']}/向量{r['recall']['vector']}"
                  f"→融合{r['recall']['fused']}  图扩展摘要头{r['recall']['graph_heads']}"
                  f"  上下文{r['context_tokens']}token  A/B级证据{r['ab_evidence']}[/]\n")

    if r["refused"]:
        console.print(f"[yellow]{r['answer']}[/]")
        return
    if r["answer"]:
        console.print(r["answer"])
    else:
        console.print("[dim]（未配置 LLM，输出证据包）[/]")
        if show_context:
            console.print(r["evidence_pack"][:4000])

    t = Table(title="证据引用")
    t.add_column("编号"); t.add_column("级"); t.add_column("标题"); t.add_column("位置")
    for c in r["citations"][:15]:
        t.add_row(c["tag"], c["level"], (c["title"] or "")[:44], (c["source_ref"] or "")[:40])
    console.print(t)


# ------------------------------------------------------------------ impact

@app.command()
def impact(symbol: str, db: str = DEFAULT_DB, depth: int = 3):
    """变更影响分析。结论只用 certain 边，candidate 单列。"""
    from .graph import traverse
    conn = dbm.connect(db)
    cands = traverse.resolve_symbol(conn, symbol)
    if not cands:
        console.print(f"[red]未找到符号 {symbol}[/]"); raise typer.Exit(1)
    if len(cands) > 1:
        console.print(f"[yellow]同名符号 {len(cands)} 个，取定义优先的那个[/]")
    node = cands[0]
    r = traverse.impact(conn, node["id"], max_hop=depth)

    console.print(f"\n[bold]改动 {node['name']}[/] ({node['path']}:{node['line_start']})\n")
    total = 0
    for hop in sorted(r["by_hop"]):
        names = [x["name"] for x in r["by_hop"][hop]]
        total += len(names)
        label = "直接调用者" if hop == 1 else f"{hop} 跳影响"
        console.print(f"[green]{label}[/] ({len(names)}): {', '.join(names[:12])}"
                      + (" …" if len(names) > 12 else ""))
    console.print(f"\n受影响文件 ({len(r['affected_files'])}): "
                  f"{', '.join(r['affected_files'][:8])}")
    console.print(f"受影响模块 ({len(r['affected_modules'])}): "
                  f"{', '.join(r['affected_modules'][:8])}")
    if r["affected_headers"]:
        console.print(f"受影响头文件: {', '.join(r['affected_headers'][:8])}")
    if r["candidate_impact"]:
        console.print(f"\n[yellow][WARN] 潜在影响 (candidate, 需人工确认) "
                      f"({len(r['candidate_impact'])}):[/]")
        for c in r["candidate_impact"][:8]:
            console.print(f"  · {c['name']}  [dim]({c['reason']})[/]")
    if r["regression_hints"]:
        console.print("\n[cyan]回归建议（来自已审核经验）:[/]")
        for h in r["regression_hints"]:
            console.print(f"  · {h['title']}: {h['verification']}")
    console.print(f"\n[dim]确定影响面 {total} 个函数 / {len(r['affected_files'])} 个文件[/]")


# -------------------------------------------------------------------- wiki

@app.command()
def wiki(db: str = DEFAULT_DB, out: str = "kb/wiki", level: str = "all",
         no_resume: bool = False, use_llm: bool = False, verbose: bool = False):
    """生成分层 Wiki（file→module→repo）。断点续写，输入未变则跳过。"""
    _setup_log(verbose)
    from .wiki import generator
    from .llm.client import get_client
    conn = dbm.connect(db)
    repo = dbm.get_meta(conn, "repo", ".")
    levels = ("file", "module", "repo") if level == "all" else (level,)
    s = generator.generate(conn, repo, levels=levels, out_dir=out,
                           llm_client=get_client() if use_llm else None,
                           resume=not no_resume)
    console.print(f"[green][OK][/] Wiki: file {s['file']} / module {s['module']} / "
                  f"repo {s['repo']}  跳过{s['skipped']} "
                  f"[red]失败{s['failed']}[/] LLM改写{s['llm']}  → {out}/")


# --------------------------------------------------------------------- exp

@exp_app.command("import")
def exp_import(file: str, db: str = DEFAULT_DB):
    """导入经验（JSONL）。★ 全部落为 pending。"""
    from .experience import store
    conn = dbm.connect(db)
    s = store.import_jsonl(conn, file)
    console.print(f"[green][OK][/] 导入 {s['imported']} 条（待审核）"
                  f" 跳过{s['skipped']} 失败{s['failed']}")


@exp_app.command("pending")
def exp_pending(db: str = DEFAULT_DB):
    """查看待审核队列。"""
    from .experience import store
    conn = dbm.connect(db)
    rows = store.pending(conn)
    t = Table(title=f"待审核 {len(rows)} 条")
    t.add_column("ID"); t.add_column("标题"); t.add_column("现象")
    for r in rows:
        t.add_row(r["id"], r["title"][:30], (r["symptom"] or "")[:50])
    console.print(t)


@exp_app.command("review")
def exp_review(exp_id: str, action: str, note: str = "", db: str = DEFAULT_DB):
    """审核。approve 后才进入检索。"""
    from .experience import store
    conn = dbm.connect(db)
    r = store.review(conn, exp_id, action, note)
    console.print(f"[green][OK][/] {r['id']} → {r['status']}（检索可见性已同步）")


# -------------------------------------------------------------------- eval

@app.command(name="eval")
def eval_cmd(db: str = DEFAULT_DB, questions: str = "eval/questions.yaml",
             data_dir: str = "data", embedder: str = "tfidf",
             ablation: bool = True, out: str = "docs/EVAL.md", gen: int = 0):
    """跑评测集 + 消融实验，输出 Markdown 表格。"""
    from .eval import run as evalrun
    conn = dbm.connect(db)
    if gen:
        n = evalrun.autogen(conn, questions, n_per_type=gen)
        console.print(f"[green][OK][/] 自动生成 {n} 题 → {questions}")
    evalrun.main(conn, questions, data_dir=data_dir, embedder_kind=embedder,
                 ablation=ablation, out=out, console=console)


# ------------------------------------------------------------------- stats

@app.command()
def stats(db: str = DEFAULT_DB, as_json: bool = False):
    """全量统计 —— 简历数字的来源。"""
    conn = dbm.connect(db)
    q = lambda s: conn.execute(s).fetchone()[0]
    d = {
        "repo": dbm.get_meta(conn, "repo"),
        "nodes": q("SELECT COUNT(*) FROM node"),
        "functions": q("SELECT COUNT(*) FROM node WHERE kind='function' AND is_definition=1"),
        "structs": q("SELECT COUNT(*) FROM node WHERE kind='struct'"),
        "macros": q("SELECT COUNT(*) FROM node WHERE kind='macro'"),
        "files": q("SELECT COUNT(*) FROM node WHERE kind='file'"),
        "edges": q("SELECT COUNT(*) FROM edge"),
        "edges_certain": q("SELECT COUNT(*) FROM edge WHERE confidence='certain'"),
        "edges_candidate": q("SELECT COUNT(*) FROM edge WHERE confidence='candidate'"),
        "calls_certain": q("SELECT COUNT(*) FROM edge WHERE kind='calls' AND confidence='certain'"),
        "summary_heads": q("SELECT COUNT(*) FROM summary_head"),
        "head_max_token": q("SELECT COALESCE(MAX(token_len),0) FROM summary_head"),
        "head_avg_token": round(q("SELECT COALESCE(AVG(token_len),0) FROM summary_head"), 1),
        "chunks": q("SELECT COUNT(*) FROM chunk"),
        "chunks_visible": q("SELECT COUNT(*) FROM chunk WHERE visible=1"),
        "exp_pending": q("SELECT COUNT(*) FROM experience WHERE status='pending'"),
        "exp_approved": q("SELECT COUNT(*) FROM experience WHERE status='approved'"),
        "wiki_pages": q("SELECT COUNT(*) FROM wiki_page"),
        "wiki_ok": q("SELECT COUNT(*) FROM wiki_page WHERE status='ok'"),
    }
    if as_json:
        print(json.dumps(d, ensure_ascii=False, indent=2)); return
    t = Table(title="知识库统计")
    t.add_column("指标"); t.add_column("值", justify="right")
    for k, v in d.items():
        t.add_row(k, str(v))
    console.print(t)


@app.command()
def serve(db: str = DEFAULT_DB, data_dir: str = "data", embedder: str = "tfidf",
          host: str = "127.0.0.1", port: int = 8000):
    """启动 Web 服务（问答 / 影响分析 / 审核）。默认只绑本机。"""
    import uvicorn
    os.environ["CODEATLAS_DB"] = db
    os.environ["CODEATLAS_DATA"] = data_dir
    os.environ["CODEATLAS_EMBEDDER"] = embedder
    console.print(f"[green]→[/] http://{host}:{port}")
    uvicorn.run("codeatlas.server.app:app", host=host, port=port, log_level="warning")


def main():
    app()


if __name__ == "__main__":
    main()
