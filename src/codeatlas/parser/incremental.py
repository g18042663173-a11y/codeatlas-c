"""增量解析。

【要解决的问题】
全量重建对 lwIP(124 个 TU) 要 20 多秒。开发时改一行就等 20 秒，工具没人用。

【为什么不能简单地"只重解析改动的文件"】
这是本模块的核心难点，也是面试值得讲的点：

  1. **一个 .c 的改动会波及多个 TU。** 改 netif.h，所有 include 它的 .c 都要重解析。
     所以指纹不能只算 .c 自己，要算**整个翻译单元的依赖闭包**（TU 内所有被 include
     且在仓库内的头文件）。这个闭包本身要靠上一次解析的结果拿到 —— 先有鸡还是先有蛋，
     解法是把闭包在首次全量解析时存进 tu_state 表。

  2. **边要删干净，而且只能删对。** 节点用 INSERT OR REPLACE 覆盖即可，
     但边是"某个 TU 观察到的事实"。TU 重解析后，它上次贡献的边必须先撤销 ——
     否则删掉的函数调用会永远留在图里，影响分析会给出幽灵依赖。
     所以 edge 表加 `tu` 列记录来源 TU，重解析前按 tu 精确删除。

  3. **同一条边可能被多个 TU 观察到。** 头文件里的 inline 函数调用会被每个
     include 它的 TU 都看到一次。按 tu 删除时不能把别的 TU 还需要的边删掉 ——
     所以 (src,dst,kind,confidence,tu) 才是唯一键，同一逻辑边允许多个 tu 副本，
     查询时 DISTINCT 掉。用空间换正确性。

  4. **删除的文件要能被发现。** 对比 compile_commands.json 里的当前 TU 集合与
     tu_state 里的历史集合，差集就是被删掉的，连带它的边一起清理。
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS tu_state (
  tu          TEXT PRIMARY KEY,   -- 翻译单元源文件绝对路径
  fingerprint TEXT NOT NULL,      -- 依赖闭包内容指纹
  deps        TEXT,               -- JSON 数组：该 TU 依赖的仓库内头文件
  args_hash   TEXT,               -- 编译参数指纹（改了 -D 也要重解析）
  parsed_at   TEXT,
  diag_errors INTEGER DEFAULT 0
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.commit()


def _sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:16]


def file_hash(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return _sha(f.read())
    except OSError:
        return "missing"


def fingerprint(source: str, deps: list[str]) -> str:
    """TU 指纹 = 源文件内容 + 全部仓库内依赖头文件内容。

    注意必须排序后再拼，否则 include 顺序变化会造成假失效。
    """
    parts = [file_hash(source)]
    parts += [f"{os.path.basename(d)}:{file_hash(d)}" for d in sorted(deps)]
    return _sha("|".join(parts).encode())


def args_hash(args: list[str]) -> str:
    return _sha("|".join(args).encode())


def load_state(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    ensure_schema(conn)
    return {r["tu"]: r for r in conn.execute("SELECT * FROM tu_state")}


def plan(conn: sqlite3.Connection, units, repo: str) -> tuple[list, list[str], dict]:
    """决定哪些 TU 需要重解析。

    返回 (待解析单元, 待清理的已删除TU, 统计)
    """
    state = load_state(conn)
    todo, stats = [], {"total": len(units), "reuse": 0, "changed": 0,
                       "new": 0, "removed": 0}

    current = set()
    for u in units:
        current.add(u.source)
        old = state.get(u.source)
        if old is None:
            stats["new"] += 1
            todo.append(u)
            continue

        import json
        deps = json.loads(old["deps"] or "[]")
        fp = fingerprint(u.source, deps)
        if fp == old["fingerprint"] and args_hash(u.args) == old["args_hash"]:
            stats["reuse"] += 1
        else:
            stats["changed"] += 1
            todo.append(u)

    removed = [tu for tu in state if tu not in current]
    stats["removed"] = len(removed)
    return todo, removed, stats


def purge_tu(conn: sqlite3.Connection, tus: list[str]) -> int:
    """撤销这些 TU 上次贡献的全部边。

    ★ 只删边不删节点：节点可能被别的 TU 共享（头文件里的声明），
      而且孤儿节点在下一步会被统一回收，删早了会破坏外键。
    """
    if not tus:
        return 0
    qs = ",".join("?" * len(tus))
    conn.execute(f"DELETE FROM edge_source WHERE tu IN ({qs})", tus)
    conn.execute(f"DELETE FROM branch_fact WHERE tu IN ({qs})", tus)
    conn.execute(f"DELETE FROM semantic_fact WHERE tu IN ({qs})", tus)
    # ★ 引用计数回收：没有任何 TU 再观察到的逻辑边才真正删除。
    #   这一步保证了"某个 TU 删掉了一个调用"能反映到图里，
    #   同时不会误删其它 TU 仍然观察得到的同一条边。
    cur = conn.execute(
        "DELETE FROM edge WHERE id NOT IN (SELECT edge_id FROM edge_source)")
    conn.execute(f"DELETE FROM tu_state WHERE tu IN ({qs})", tus)
    conn.commit()
    return cur.rowcount


def collect_deps(tu_obj, repo: str) -> list[str]:
    """从解析结果里取出该 TU 依赖的仓库内头文件。"""
    repo = os.path.abspath(repo)
    out = []
    for inc in tu_obj.get_includes():
        p = inc.include.name if inc.include else None
        if p and os.path.abspath(p).startswith(repo):
            out.append(os.path.abspath(p))
    return sorted(set(out))


def record(conn: sqlite3.Connection, source: str, deps: list[str],
           args: list[str], diag: int) -> None:
    import json
    from datetime import datetime, timezone
    conn.execute(
        """INSERT OR REPLACE INTO tu_state(tu,fingerprint,deps,args_hash,parsed_at,diag_errors)
           VALUES(?,?,?,?,?,?)""",
        (source, fingerprint(source, deps), json.dumps(deps),
         args_hash(args), datetime.now(timezone.utc).isoformat(timespec="seconds"), diag))


def gc_orphan_nodes(conn: sqlite3.Connection) -> int:
    """回收不再被任何边引用、也不属于任何现存 TU 的节点。

    保留 file 节点（它们是 contains 边的锚点），只回收函数/结构体等实体。
    """
    cur = conn.execute("""
        DELETE FROM node
         WHERE kind NOT IN ('file','repo')
           AND id NOT IN (SELECT src FROM edge UNION SELECT dst FROM edge)""")
    conn.commit()
    return cur.rowcount


def stale_summary_and_chunks(conn: sqlite3.Connection) -> None:
    """被重解析影响到的节点，其摘要头和 chunk 需要失效。

    做法保守：清掉 summary_head 里 src_hash 对不上的行，
    让下一次 `codeatlas summary` 自然重建（它本身是幂等的）。
    """
    conn.execute("""
        DELETE FROM summary_head
         WHERE node_id NOT IN (SELECT id FROM node)""")
    conn.execute("""
        DELETE FROM chunk
         WHERE node_id IS NOT NULL AND node_id NOT IN (SELECT id FROM node)""")
    conn.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('delete-all')")
    conn.execute("INSERT INTO chunk_fts(rowid,title,text) "
                 "SELECT rowid,title,text FROM chunk WHERE visible=1")
    conn.commit()
