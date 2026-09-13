"""Read-only file/module view over already-extracted certain edges."""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from .modules import module_of, repository_label


def _repo_meta(conn: sqlite3.Connection) -> tuple[str, str]:
    from .. import db as dbm

    repository = repository_label(conn)
    revision = dbm.get_meta(conn, "revision", "unversioned")
    return repository, revision


def resolve_file(conn: sqlite3.Connection, path: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM node WHERE kind='file' AND path=? ORDER BY path LIMIT 1", (path,)
    ).fetchone()
    if row:
        return row
    rows = conn.execute(
        """SELECT * FROM node WHERE kind='file' AND path IS NOT NULL
             AND (path=? OR path LIKE ?) ORDER BY path""",
        (path, "%/" + path),
    ).fetchall()
    if not rows:
        raise KeyError(f"未找到文件 {path}")
    if len(rows) > 1:
        raise KeyError("文件名不唯一，请改用仓库内相对路径：" +
                       "、".join(row["path"] for row in rows[:8]))
    return rows[0]


def repo_outline(conn: sqlite3.Connection) -> dict:
    repository, revision = _repo_meta(conn)
    files = conn.execute(
        "SELECT id,path FROM node WHERE kind='file' AND path IS NOT NULL ORDER BY path"
    ).fetchall()
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in files:
        functions = conn.execute(
            """SELECT COUNT(*) FROM node
                WHERE kind='function' AND is_definition=1 AND path=?""",
            (row["path"],),
        ).fetchone()[0]
        grouped[module_of(row["path"], repository)].append(
            {"path": row["path"], "functions": functions}
        )
    modules = [
        {
            "name": name,
            "files": items,
            "file_count": len(items),
            "functions": sum(item["functions"] for item in items),
        }
        for name, items in sorted(grouped.items())
    ]
    includes = [
        {"src": row["src"], "dst": row["dst"]}
        for row in conn.execute(
            """SELECT a.path src, b.path dst
                 FROM edge e
                 JOIN node a ON a.id=e.src
                 JOIN node b ON b.id=e.dst
                WHERE e.kind='includes' AND e.confidence='certain'
                  AND a.kind='file' AND b.kind='file'
                ORDER BY a.path, b.path"""
        )
    ]
    cross_file_calls = [
        {"src": row["src"], "dst": row["dst"], "count": row["c"]}
        for row in conn.execute(
            """SELECT a.path src, b.path dst, COUNT(*) c
                 FROM edge e
                 JOIN node a ON a.id=e.src
                 JOIN node b ON b.id=e.dst
                WHERE e.kind='calls' AND e.confidence='certain'
                  AND a.path IS NOT NULL AND b.path IS NOT NULL AND a.path<>b.path
                GROUP BY a.path, b.path
                ORDER BY c DESC, a.path, b.path"""
        )
    ]
    return {
        "repository": repository,
        "revision": revision,
        "modules": modules,
        "includes": includes,
        "cross_file_calls": cross_file_calls,
    }


def file_card(conn: sqlite3.Connection, path: str) -> dict:
    repository, revision = _repo_meta(conn)
    file_row = resolve_file(conn, path)
    rel = file_row["path"]
    symbols = [
        {
            "kind": row["kind"],
            "name": row["name"],
            "line_start": row["line_start"],
            "line_end": row["line_end"],
            "is_definition": row["is_definition"],
        }
        for row in conn.execute(
            """SELECT kind,name,line_start,line_end,is_definition FROM node
                WHERE path=? AND kind IN ('function','struct','class','enum','typedef','macro','global')
                ORDER BY is_definition DESC, kind, line_start, name""",
            (rel,),
        )
    ]
    includes_out = [
        row["path"]
        for row in conn.execute(
            """SELECT b.path FROM edge e
                 JOIN node b ON b.id=e.dst
                WHERE e.src=? AND e.kind='includes' AND e.confidence='certain'
                ORDER BY b.path""",
            (file_row["id"],),
        )
    ]
    includes_in = [
        row["path"]
        for row in conn.execute(
            """SELECT a.path FROM edge e
                 JOIN node a ON a.id=e.src
                WHERE e.dst=? AND e.kind='includes' AND e.confidence='certain'
                ORDER BY a.path""",
            (file_row["id"],),
        )
    ]
    calls_out = [
        {"src": row["src"], "dst": row["dst"], "callee": row["callee"], "count": row["c"]}
        for row in conn.execute(
            """SELECT a.path src, b.path dst, b.name callee, COUNT(*) c
                 FROM edge e
                 JOIN node a ON a.id=e.src
                 JOIN node b ON b.id=e.dst
                WHERE e.kind='calls' AND e.confidence='certain'
                  AND a.path=? AND b.path IS NOT NULL AND a.path<>b.path
                GROUP BY a.path, b.path, b.name
                ORDER BY c DESC, b.name""",
            (rel,),
        )
    ]
    calls_in = [
        {"src": row["src"], "dst": row["dst"], "caller": row["caller"], "count": row["c"]}
        for row in conn.execute(
            """SELECT a.path src, b.path dst, a.name caller, COUNT(*) c
                 FROM edge e
                 JOIN node a ON a.id=e.src
                 JOIN node b ON b.id=e.dst
                WHERE e.kind='calls' AND e.confidence='certain'
                  AND b.path=? AND a.path IS NOT NULL AND a.path<>b.path
                GROUP BY a.path, b.path, a.name
                ORDER BY c DESC, a.name""",
            (rel,),
        )
    ]
    inherits = [
        {"src": row["src"], "dst": row["dst"]}
        for row in conn.execute(
            """SELECT a.name src, b.name dst
                 FROM edge e
                 JOIN node a ON a.id=e.src
                 JOIN node b ON b.id=e.dst
                WHERE e.kind='inherits' AND e.confidence='certain'
                  AND (a.path=? OR b.path=?)
                ORDER BY a.name, b.name""",
            (rel, rel),
        )
    ]
    candidate_calls = conn.execute(
        """SELECT COUNT(*) FROM edge e JOIN node a ON a.id=e.src
            WHERE e.kind='calls' AND e.confidence='candidate' AND a.path=?""",
        (rel,),
    ).fetchone()[0]
    return {
        "repository": repository,
        "revision": revision,
        "path": rel,
        "module": module_of(rel, repository),
        "symbols": symbols,
        "includes_out": includes_out,
        "includes_in": includes_in,
        "calls_out": calls_out,
        "calls_in": calls_in,
        "inherits": inherits,
        "candidate_calls": candidate_calls,
    }
