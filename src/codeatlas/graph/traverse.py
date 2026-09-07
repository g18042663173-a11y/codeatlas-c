"""图遍历 —— 不用 Neo4j，用 SQLite 递归 CTE。

十万级节点在单机上，递归 CTE 完全够用，还少一个部署依赖。
所有遍历接口都封装在这里，将来真要换图数据库，只改这一个文件。

★ 全文件所有 SQL 都显式带 `confidence = 'certain'`，绝不依赖默认值。
"""
from __future__ import annotations

import sqlite3

# 正向：我调用了谁
_FORWARD = """
WITH RECURSIVE reach(id, hop) AS (
    SELECT ?, 0
    UNION
    SELECT e.dst, r.hop + 1
      FROM edge e JOIN reach r ON e.src = r.id
     WHERE e.kind = ?
       AND e.confidence = 'certain'
       AND r.hop < ?
)
SELECT id, MIN(hop) AS hop FROM reach WHERE hop > 0 GROUP BY id ORDER BY hop, id
"""

# 反向：谁调用了我（影响分析的核心）
_BACKWARD = """
WITH RECURSIVE reach(id, hop) AS (
    SELECT ?, 0
    UNION
    SELECT e.src, r.hop + 1
      FROM edge e JOIN reach r ON e.dst = r.id
     WHERE e.kind = ?
       AND e.confidence = 'certain'
       AND r.hop < ?
)
SELECT id, MIN(hop) AS hop FROM reach WHERE hop > 0 GROUP BY id ORDER BY hop, id
"""


def neighbors(conn: sqlite3.Connection, node_id: str, *, direction: str = "out",
              kind: str = "calls", max_hop: int = 2) -> list[tuple[str, int]]:
    """多跳邻居。返回 [(node_id, 最短跳数)]，只走 certain 边。"""
    sql = _FORWARD if direction == "out" else _BACKWARD
    rows = conn.execute(sql, (node_id, kind, max_hop)).fetchall()
    return [(r["id"], r["hop"]) for r in rows]


def resolve_symbol(conn: sqlite3.Connection, name: str,
                   kinds: tuple[str, ...] = ("function", "struct", "macro", "typedef",
                                             "enum", "global")) -> list[sqlite3.Row]:
    """按名字找符号，定义优先于声明。同名符号全部返回，由调用方消歧。"""
    q = f"""
      SELECT * FROM node
       WHERE name = ? AND kind IN ({','.join('?' * len(kinds))})
       ORDER BY is_definition DESC, kind, path
    """
    return conn.execute(q, (name, *kinds)).fetchall()


def direct_edges(conn: sqlite3.Connection, node_id: str, *, direction: str = "out",
                 kind: str = "calls", confidence: str = "certain") -> list[sqlite3.Row]:
    """一跳邻居的完整信息（含 reason / evidence），用于展示。"""
    if direction == "out":
        q = """SELECT n.*, e.reason, e.evidence FROM edge e JOIN node n ON n.id = e.dst
                WHERE e.src = ? AND e.kind = ? AND e.confidence = ?"""
    else:
        q = """SELECT n.*, e.reason, e.evidence FROM edge e JOIN node n ON n.id = e.src
                WHERE e.dst = ? AND e.kind = ? AND e.confidence = ?"""
    return conn.execute(q, (node_id, kind, confidence)).fetchall()


def impact(conn: sqlite3.Connection, node_id: str, max_hop: int = 3) -> dict:
    """变更影响分析。

    结论部分只用 certain 边；candidate 影响单独一栏并标注需人工确认。
    """
    hops = neighbors(conn, node_id, direction="in", kind="calls", max_hop=max_hop)
    by_hop: dict[int, list[dict]] = {}
    files: set[str] = set()
    modules: set[str] = set()

    for nid_, hop in hops:
        row = conn.execute("SELECT * FROM node WHERE id = ?", (nid_,)).fetchone()
        if row is None:
            continue
        by_hop.setdefault(hop, []).append(
            dict(id=row["id"], name=row["name"], path=row["path"],
                 line_start=row["line_start"], line_end=row["line_end"])
        )
        if row["path"]:
            files.add(row["path"])
            modules.add(row["path"].rsplit("/", 1)[0] if "/" in row["path"] else ".")

    # 受影响头文件：谁 include 了本符号所在文件
    self_row = conn.execute("SELECT * FROM node WHERE id = ?", (node_id,)).fetchone()
    headers: list[str] = []
    if self_row and self_row["path"]:
        fnode = conn.execute(
            "SELECT id FROM node WHERE kind='file' AND path = ?", (self_row["path"],)
        ).fetchone()
        if fnode:
            for hid, _ in neighbors(conn, fnode["id"], direction="in",
                                    kind="includes", max_hop=2):
                r = conn.execute("SELECT path FROM node WHERE id = ?", (hid,)).fetchone()
                if r:
                    headers.append(r["path"])

    # ⚠️ candidate 影响：单独一栏，永不混入上面的结论
    cand = conn.execute(
        """SELECT n.id, n.name, n.path, n.line_start, n.line_end, e.reason
              FROM edge e JOIN node n ON n.id = e.src
            WHERE e.dst = ? AND e.kind='calls' AND e.confidence='candidate'""",
        (node_id,),
    ).fetchall()

    # 相关的已审核经验（B 级证据），用于给出回归建议
    exps = conn.execute(
        """SELECT ex.title, ex.verification FROM experience ex
            JOIN experience_link l ON l.exp_id = ex.id
           WHERE l.node_id = ? AND l.confidence='certain' AND ex.status = 'approved'
             AND COALESCE(ex.artifact_scope,'formal')=COALESCE(
               (SELECT value FROM meta WHERE key='evaluation_scope'),'formal')
             AND EXISTS (SELECT 1 FROM experience_anchor a
                          WHERE a.exp_id=ex.id AND a.active=1)""",
        (node_id,),
    ).fetchall()

    return {
        "symbol": dict(self_row) if self_row else None,
        "by_hop": by_hop,
        "affected_files": sorted(files),
        "affected_modules": sorted(modules),
        "affected_headers": sorted(set(headers)),
        "candidate_impact": [dict(id=c["id"], name=c["name"], path=c["path"],
                                  line_start=c["line_start"], line_end=c["line_end"],
                                  reason=c["reason"])
                             for c in cand],
        "regression_hints": [dict(title=e["title"], verification=e["verification"])
                             for e in exps],
    }
