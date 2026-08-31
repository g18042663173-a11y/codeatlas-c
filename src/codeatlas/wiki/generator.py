"""M6 分层 Wiki 生成（LLM-Wiki 方法的落地形态）。

三层自底向上：file → module → repo
  files/*.md    输入：文件内所有函数的摘要头 + include 关系
  modules/*.md  输入：该模块下所有 file 页的摘要 + 跨文件调用边
  repo.md       输入：所有 module 页的摘要

三个工程保障（长任务不崩的关键）：
  1. input_hash 跳过    输入没变就不重生成 —— 省 token 的最小增量实现
  2. gen_task 断点续写  中断后重跑能接上，不从头再来
  3. 章节完整性校验     缺必需章节就重试，超限标 failed，不阻塞整批

无 LLM 时用规则版生成：直接从图谱事实拼装。内容不如 LLM 流畅，
但全部是编译器确认的事实，反而更可信 —— 这也是 D3「程序管边界，模型管表达」。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from ..summary import head as head_mod

log = logging.getLogger(__name__)

MAX_ATTEMPT = 3

# schema.md 规定的必需章节。缺任何一个即判定生成失败。
REQUIRED_SECTIONS = {
    "file": ["## 职责", "## 对外接口", "## 调用关系", "## 依赖"],
    "module": ["## 职责", "## 包含文件", "## 关键入口", "## 跨文件调用"],
    "repo": ["## 概览", "## 模块列表", "## 核心调用路径"],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(*parts: str) -> str:
    return hashlib.sha1("::".join(parts).encode()).hexdigest()[:16]


def _module_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else "."


# ---------------------------------------------------------------- 事实收集

def _file_facts(conn: sqlite3.Connection, file_row: sqlite3.Row) -> dict:
    fns = conn.execute(
        """SELECT n.*, s.one_liner, s.fan_in, s.fan_out FROM node n
             LEFT JOIN summary_head s ON s.node_id = n.id
            WHERE n.kind='function' AND n.is_definition=1 AND n.path=?
            ORDER BY s.fan_in DESC NULLS LAST, n.line_start""",
        (file_row["path"],)).fetchall()
    includes = [r["path"] for r in conn.execute(
        """SELECT n.path FROM edge e JOIN node n ON n.id=e.dst
            WHERE e.src=? AND e.kind='includes' AND e.confidence='certain'""",
        (file_row["id"],))]
    # 出向/入向的跨文件调用
    out_calls = conn.execute(
        """SELECT DISTINCT dn.path p, COUNT(*) c FROM edge e
             JOIN node sn ON sn.id=e.src JOIN node dn ON dn.id=e.dst
            WHERE e.kind='calls' AND e.confidence='certain'
              AND sn.path=? AND dn.path IS NOT NULL AND dn.path<>?
            GROUP BY dn.path ORDER BY c DESC LIMIT 8""",
        (file_row["path"], file_row["path"])).fetchall()
    in_calls = conn.execute(
        """SELECT DISTINCT sn.path p, COUNT(*) c FROM edge e
             JOIN node sn ON sn.id=e.src JOIN node dn ON dn.id=e.dst
            WHERE e.kind='calls' AND e.confidence='certain'
              AND dn.path=? AND sn.path IS NOT NULL AND sn.path<>?
            GROUP BY sn.path ORDER BY c DESC LIMIT 8""",
        (file_row["path"], file_row["path"])).fetchall()
    cand = conn.execute(
        """SELECT COUNT(*) c FROM edge e JOIN node sn ON sn.id=e.src
            WHERE e.kind='calls' AND e.confidence='candidate' AND sn.path=?""",
        (file_row["path"],)).fetchone()["c"]
    return dict(fns=fns, includes=includes, out_calls=out_calls,
                in_calls=in_calls, candidate_calls=cand)


# ---------------------------------------------------------------- 规则版渲染

def _render_file(row: sqlite3.Row, f: dict) -> str:
    pub = [x for x in f["fns"] if not x["is_static"]]
    prv = [x for x in f["fns"] if x["is_static"]]
    L = [f"# {row['path']}", "", "## 职责", ""]
    if pub:
        top = pub[:3]
        L.append("本文件提供 " + "、".join(f"`{x['name']}`" for x in top)
                 + f" 等 {len(pub)} 个对外函数"
                 + (f"，另有 {len(prv)} 个内部静态函数。" if prv else "。"))
    else:
        L.append(f"本文件包含 {len(f['fns'])} 个内部函数，不导出对外接口。")

    L += ["", "## 对外接口", ""]
    if pub:
        L += ["| 函数 | 职责 | 扇入 | 位置 |", "|---|---|---|---|"]
        for x in pub[:30]:
            L.append(f"| `{x['name']}` | {(x['one_liner'] or '').replace('|','/')} "
                     f"| {x['fan_in'] or 0} | L{x['line_start']} |")
    else:
        L.append("无。")

    L += ["", "## 调用关系", ""]
    if f["in_calls"]:
        L.append("被以下文件调用：" + "、".join(
            f"`{r['p']}`({r['c']}次)" for r in f["in_calls"]))
    if f["out_calls"]:
        L.append("调用以下文件：" + "、".join(
            f"`{r['p']}`({r['c']}次)" for r in f["out_calls"]))
    if not f["in_calls"] and not f["out_calls"]:
        L.append("无跨文件确定调用。")
    if f["candidate_calls"]:
        L.append("")
        L.append(f"> ⚠️ 另有 {f['candidate_calls']} 条候选调用（函数指针/未解析），"
                 "未纳入上述结论，需人工确认。")

    L += ["", "## 依赖", ""]
    L.append("包含头文件：" + ("、".join(f"`{h}`" for h in f["includes"][:12])
                            if f["includes"] else "无仓库内头文件依赖。"))
    if prv:
        L += ["", "<details><summary>内部静态函数 "
              f"({len(prv)})</summary>", "",
              "、".join(f"`{x['name']}`" for x in prv), "", "</details>"]
    return "\n".join(L)


def _render_module(module: str, files: list[sqlite3.Row], conn) -> str:
    entries = conn.execute(
        """SELECT n.name, n.path, s.one_liner, s.fan_in FROM node n
             JOIN summary_head s ON s.node_id=n.id
            WHERE n.kind='function' AND n.is_definition=1 AND n.is_static=0
              AND (n.path LIKE ? OR (? = '.' AND n.path NOT LIKE '%/%'))
            ORDER BY s.fan_in DESC LIMIT 12""",
        (f"{module}/%", module)).fetchall()
    cross = conn.execute(
        """SELECT sn.path a, dn.path b, COUNT(*) c FROM edge e
             JOIN node sn ON sn.id=e.src JOIN node dn ON dn.id=e.dst
            WHERE e.kind='calls' AND e.confidence='certain'
              AND sn.path<>dn.path AND sn.path IS NOT NULL AND dn.path IS NOT NULL
            GROUP BY sn.path, dn.path ORDER BY c DESC LIMIT 10""").fetchall()

    L = [f"# 模块 {module}", "", "## 职责", "",
         f"本模块包含 {len(files)} 个源文件，"
         f"对外提供 {len(entries)} 个高扇入入口函数。", "",
         "## 包含文件", ""]
    for f in files:
        L.append(f"- [`{f['path']}`](../files/{f['path'].replace('/', '__')}.md)")
    L += ["", "## 关键入口", ""]
    if entries:
        L += ["| 函数 | 职责 | 扇入 | 文件 |", "|---|---|---|---|"]
        for e in entries:
            L.append(f"| `{e['name']}` | {(e['one_liner'] or '').replace('|','/')} "
                     f"| {e['fan_in']} | `{e['path']}` |")
    else:
        L.append("无对外入口。")
    L += ["", "## 跨文件调用", ""]
    if cross:
        for c in cross:
            L.append(f"- `{c['a']}` → `{c['b']}` ({c['c']} 次)")
    else:
        L.append("无。")
    return "\n".join(L)


def _render_repo(conn, modules: dict, stats: dict) -> str:
    hubs = conn.execute(
        """SELECT n.name, n.path, s.fan_in FROM node n JOIN summary_head s ON s.node_id=n.id
            WHERE n.kind='function' AND n.is_definition=1
            ORDER BY s.fan_in DESC LIMIT 10""").fetchall()
    L = ["# 仓库总览", "", "## 概览", "",
         f"- 源文件 {stats['files']} 个，函数定义 {stats['functions']} 个",
         f"- 图节点 {stats['nodes']}，关系 {stats['edges']}"
         f"（确定 {stats['edges_certain']} / 候选 {stats['edges_candidate']}）",
         f"- 模块 {len(modules)} 个", "",
         "> 本页由 CodeAtlas 依据编译器确认的 AST 事实自动生成。"
         "候选关系（函数指针、未解析调用）未纳入上述统计结论。", "",
         "## 模块列表", "", "| 模块 | 文件数 |", "|---|---|"]
    for m, fs in sorted(modules.items(), key=lambda kv: -len(kv[1])):
        L.append(f"| [`{m}`](modules/{m.replace('/', '__')}.md) | {len(fs)} |")
    L += ["", "## 核心调用路径", "",
          "扇入最高的函数（改动风险最大）：", ""]
    for h in hubs:
        L.append(f"- `{h['name']}` ({h['path']}) — 被 {h['fan_in']} 处确定调用")
    return "\n".join(L)


# ---------------------------------------------------------------- 校验

def validate(md: str, level: str) -> tuple[bool, str]:
    missing = [s for s in REQUIRED_SECTIONS[level] if s not in md]
    if missing:
        return False, f"缺少章节: {', '.join(missing)}"
    if len(md) < 60:
        return False, "内容过短"
    return True, ""


# ---------------------------------------------------------------- 主流程

def _upsert_task(conn, tid, target, stage, state, err=""):
    conn.execute(
        """INSERT INTO gen_task(id,target_id,stage,state,attempt,last_error,updated_at)
           VALUES(?,?,?,?,1,?,?)
           ON CONFLICT(id) DO UPDATE SET state=excluded.state,
             attempt=gen_task.attempt+1, last_error=excluded.last_error,
             updated_at=excluded.updated_at""",
        (tid, target, stage, state, err, _now()))


def _write(conn, page_id, level, node_id, title, md, sources, input_hash) -> bool:
    ok, err = validate(md, level)
    conn.execute(
        """INSERT INTO wiki_page(id,level,node_id,title,md,sources,status,version,
                                 input_hash,updated_at)
           VALUES(?,?,?,?,?,?,?,1,?,?)
           ON CONFLICT(id) DO UPDATE SET md=excluded.md, sources=excluded.sources,
             status=excluded.status, version=wiki_page.version+1,
             input_hash=excluded.input_hash, updated_at=excluded.updated_at""",
        (page_id, level, node_id, title, md, json.dumps(sources, ensure_ascii=False),
         "ok" if ok else "failed", input_hash, _now()))
    if not ok:
        log.warning("页面 %s 校验失败: %s", page_id, err)
    return ok


def generate(conn: sqlite3.Connection, repo: str, *, levels: tuple[str, ...] =
             ("file", "module", "repo"), out_dir: str | None = None,
             llm_client=None, resume: bool = True) -> dict:
    st = {"file": 0, "module": 0, "repo": 0, "skipped": 0, "failed": 0, "llm": 0}
    files = conn.execute("SELECT * FROM node WHERE kind='file' ORDER BY path").fetchall()
    modules: dict[str, list] = defaultdict(list)
    for f in files:
        if f["path"]:
            modules[_module_of(f["path"])].append(f)

    # ---- 第 1 层：file ----
    if "file" in levels:
        for f in files:
            if not f["path"]:
                continue
            facts = _file_facts(conn, f)
            if not facts["fns"]:
                continue
            page_id = "files/" + f["path"].replace("/", "__")
            ih = _hash(f["path"], str(len(facts["fns"])),
                       str(len(facts["includes"])), str(facts["candidate_calls"]),
                       "".join(sorted(x["name"] for x in facts["fns"])))
            old = conn.execute("SELECT input_hash,status FROM wiki_page WHERE id=?",
                               (page_id,)).fetchone()
            if resume and old and old["input_hash"] == ih and old["status"] == "ok":
                st["skipped"] += 1
                continue

            _upsert_task(conn, page_id, f["id"], "file", "running")
            md = _render_file(f, facts)
            if llm_client is not None:
                md = _llm_polish(llm_client, md, "file", st)
            srcs = [f"node:{f['id']}"] + [f"node:{x['id']}" for x in facts["fns"][:20]]
            ok = _write(conn, page_id, "file", f["id"], f["path"], md, srcs, ih)
            _upsert_task(conn, page_id, f["id"], "file", "done" if ok else "failed")
            st["file" if ok else "failed"] += 1
        conn.commit()

    # ---- 第 2 层：module（输入是第 1 层的产物）----
    if "module" in levels:
        for m, fs in modules.items():
            page_id = "modules/" + m.replace("/", "__")
            ih = _hash(m, str(len(fs)), "".join(sorted(x["path"] for x in fs)))
            old = conn.execute("SELECT input_hash,status FROM wiki_page WHERE id=?",
                               (page_id,)).fetchone()
            if resume and old and old["input_hash"] == ih and old["status"] == "ok":
                st["skipped"] += 1
                continue
            _upsert_task(conn, page_id, m, "module", "running")
            md = _render_module(m, fs, conn)
            if llm_client is not None:
                md = _llm_polish(llm_client, md, "module", st)
            srcs = [f"wiki:files/{x['path'].replace('/', '__')}" for x in fs]
            ok = _write(conn, page_id, "module", None, f"模块 {m}", md, srcs, ih)
            _upsert_task(conn, page_id, m, "module", "done" if ok else "failed")
            st["module" if ok else "failed"] += 1
        conn.commit()

    # ---- 第 3 层：repo ----
    if "repo" in levels:
        q = lambda s: conn.execute(s).fetchone()[0]
        stats = dict(
            files=q("SELECT COUNT(*) FROM node WHERE kind='file'"),
            functions=q("SELECT COUNT(*) FROM node WHERE kind='function' AND is_definition=1"),
            nodes=q("SELECT COUNT(*) FROM node"), edges=q("SELECT COUNT(*) FROM edge"),
            edges_certain=q("SELECT COUNT(*) FROM edge WHERE confidence='certain'"),
            edges_candidate=q("SELECT COUNT(*) FROM edge WHERE confidence='candidate'"))
        ih = _hash(*(str(v) for v in stats.values()))
        old = conn.execute("SELECT input_hash,status FROM wiki_page WHERE id='repo'").fetchone()
        if not (resume and old and old["input_hash"] == ih and old["status"] == "ok"):
            md = _render_repo(conn, modules, stats)
            srcs = [f"wiki:modules/{m.replace('/', '__')}" for m in modules]
            ok = _write(conn, "repo", "repo", None, "仓库总览", md, srcs, ih)
            _upsert_task(conn, "repo", "repo", "repo", "done" if ok else "failed")
            st["repo" if ok else "failed"] += 1
        else:
            st["skipped"] += 1
        conn.commit()

    if out_dir:
        export(conn, out_dir)
    return st


def _llm_polish(client, md: str, level: str, st: dict) -> str:
    """LLM 只负责把规则版事实改写得更可读，不允许新增事实。

    生成后必须过章节校验；连续失败就退回规则版 —— 宁可干巴，不要幻觉。
    """
    for _ in range(MAX_ATTEMPT):
        try:
            out = client.answer(
                "你在改写一份由程序从 AST 事实生成的代码文档。规则：\n"
                "1. 只能改写措辞，不得新增任何原文没有的事实。\n"
                f"2. 必须保留全部章节标题：{REQUIRED_SECTIONS[level]}\n"
                "3. 保留全部表格和代码引用。只输出 Markdown。",
                "请改写下面的文档使其更易读：", md)
            ok, _e = validate(out, level)
            if ok:
                st["llm"] += 1
                return out
        except Exception as e:
            log.warning("LLM 改写失败: %s", e)
    return md


def export(conn: sqlite3.Connection, out_dir: str) -> int:
    """导出为 LLM-Wiki 的三层目录结构（Obsidian 可直接打开）。"""
    root = Path(out_dir)
    (root / "files").mkdir(parents=True, exist_ok=True)
    (root / "modules").mkdir(parents=True, exist_ok=True)

    pages = conn.execute("SELECT * FROM wiki_page ORDER BY level, id").fetchall()
    for p in pages:
        fm = ("---\n"
              f"level: {p['level']}\n"
              f"title: {p['title']}\n"
              f"status: {p['status']}\n"
              f"version: {p['version']}\n"
              f"sources: {p['sources']}\n"
              f"generated_at: {p['updated_at']}\n"
              "---\n\n")
        target = root / f"{p['id']}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(fm + (p["md"] or ""), encoding="utf-8")

    idx = ["# index", "", "由 CodeAtlas 生成。事实层只读，本目录可重建。", ""]
    for lvl in ("repo", "module", "file"):
        sub = [p for p in pages if p["level"] == lvl]
        if sub:
            idx += [f"## {lvl} ({len(sub)})", ""]
            idx += [f"- [[{p['id']}]] {p['title']}"
                    + ("  ⚠️failed" if p["status"] != "ok" else "") for p in sub[:200]]
            idx.append("")
    (root / "index.md").write_text("\n".join(idx), encoding="utf-8")

    (root / "log.md").write_text(
        "# log\n\n" + "\n".join(
            f"- {p['updated_at']}  {p['id']}  v{p['version']}  {p['status']}"
            for p in pages), encoding="utf-8")
    return len(pages)
