"""增量解析测试。

【为什么这组测试特别重要】
增量的错误全是**静默**的：图里多一条早已删除的调用边，不会报错、不会崩，
只会让影响分析给出幽灵依赖 —— 而这恰恰是这个工具最不该出错的地方。
所以这里每个用例都在验证"该消失的东西真的消失了"。
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from codeatlas import db as dbm
from codeatlas.parser import ast_walker, compile_db, incremental as inc

SRC_A = """
#include "lib.h"
int helper(int x) { return x + 1; }
int entry(int x)  { return helper(x); }
"""

SRC_A_MODIFIED = """
#include "lib.h"
int helper(int x) { return x + 1; }
int entry(int x)  { return x + 2; }        /* 不再调用 helper */
"""

SRC_B = """
#include "lib.h"
int other(int x) { return shared_inline(x); }
"""

HDR = """
#ifndef LIB_H
#define LIB_H
static inline int shared_inline(int v) { return v * 2; }
int helper(int x);
#endif
"""


@pytest.fixture
def proj(tmp_path):
    """一个最小 C 工程：a.c / b.c / lib.h。"""
    (tmp_path / "lib.h").write_text(HDR, encoding="utf-8")
    (tmp_path / "a.c").write_text(SRC_A, encoding="utf-8")
    (tmp_path / "b.c").write_text(SRC_B, encoding="utf-8")
    return tmp_path


def _parse(proj: Path, db_path: Path, *, full: bool):
    if full and db_path.exists():
        dbm.reset(db_path)
    conn = dbm.connect(db_path)
    inc.ensure_schema(conn)

    units, _ = compile_db.load(str(proj), None, [str(proj)], [])
    if not full:
        todo, removed, stats = inc.plan(conn, units, str(proj))
        inc.purge_tu(conn, [u.source for u in todo] + removed)
        units = todo
    else:
        stats = {"reuse": 0, "changed": 0, "new": len(units), "removed": 0,
                 "total": len(units)}

    w = ast_walker.AstWalker(str(proj))
    for u in units:
        diag = w.walk_unit(u.source, u.args, u.directory)
        if w.last_tu is not None:
            inc.record(conn, u.source, inc.collect_deps(w.last_tu, str(proj)),
                       u.args, max(diag, 0))
    ast_walker.persist(conn, w.result)
    if not full:
        inc.gc_orphan_nodes(conn)
    conn.commit()
    return conn, stats


def _calls(conn: sqlite3.Connection, caller: str, callee: str) -> int:
    return conn.execute(
        """SELECT COUNT(*) c FROM edge e
             JOIN node s ON s.id = e.src
             JOIN node d ON d.id = e.dst
            WHERE s.name=? AND d.name=? AND e.kind='calls' AND e.confidence='certain'""",
        (caller, callee)).fetchone()["c"]


# ==================== 基本行为 ====================

def test_no_change_reuses_everything(proj, tmp_path):
    db = tmp_path / "kb.db"
    conn, _ = _parse(proj, db, full=True)
    conn.close()

    conn = dbm.connect(db)
    units, _ = compile_db.load(str(proj), None, [str(proj)], [])
    _, removed, stats = inc.plan(conn, units, str(proj))
    assert stats["reuse"] == stats["total"], "内容没变却判定为需要重解析"
    assert not removed


def test_modifying_c_only_affects_that_tu(proj, tmp_path):
    db = tmp_path / "kb.db"
    conn, _ = _parse(proj, db, full=True)
    conn.close()

    (proj / "a.c").write_text(SRC_A_MODIFIED, encoding="utf-8")
    conn = dbm.connect(db)
    units, _ = compile_db.load(str(proj), None, [str(proj)], [])
    todo, _, stats = inc.plan(conn, units, str(proj))
    assert stats["changed"] == 1
    assert stats["reuse"] == 1
    assert Path(todo[0].source).name == "a.c"


def test_modifying_header_invalidates_all_dependents(proj, tmp_path):
    """★ 改头文件必须波及所有 include 它的 TU —— 指纹算的是依赖闭包，不是单文件。"""
    db = tmp_path / "kb.db"
    conn, _ = _parse(proj, db, full=True)
    conn.close()

    (proj / "lib.h").write_text(HDR + "\n/* changed */\n", encoding="utf-8")
    conn = dbm.connect(db)
    units, _ = compile_db.load(str(proj), None, [str(proj)], [])
    _, _, stats = inc.plan(conn, units, str(proj))
    assert stats["changed"] == 2, "改头文件只失效了部分依赖方"
    assert stats["reuse"] == 0


# ==================== 边的撤销正确性（核心）====================

def test_removed_call_disappears_from_graph(proj, tmp_path):
    """★★ 最关键：删掉一个调用后，图里必须真的没有这条边。

    这是增量最容易出的静默错误 —— 边留在图里不会报错，
    只会让影响分析给出幽灵依赖。
    """
    db = tmp_path / "kb.db"
    conn, _ = _parse(proj, db, full=True)
    assert _calls(conn, "entry", "helper") == 1
    conn.close()

    (proj / "a.c").write_text(SRC_A_MODIFIED, encoding="utf-8")
    conn, _ = _parse(proj, db, full=False)
    assert _calls(conn, "entry", "helper") == 0, "已删除的调用边仍留在图里（幽灵依赖）"


def test_shared_header_edge_survives_partial_reparse(proj, tmp_path):
    """★★ 只重解析 a.c 时，b.c 观察到的边不能被误删。

    lib.h 里的 inline 函数会被两个 TU 各观察一次，
    如果按 tu 粗暴删除，b.c 的边会跟着 a.c 一起没掉。
    引用计数式回收就是为了防这个。
    """
    db = tmp_path / "kb.db"
    conn, _ = _parse(proj, db, full=True)
    before = _calls(conn, "other", "shared_inline")
    assert before == 1
    conn.close()

    (proj / "a.c").write_text(SRC_A_MODIFIED, encoding="utf-8")
    conn, _ = _parse(proj, db, full=False)
    assert _calls(conn, "other", "shared_inline") == before, \
        "重解析 a.c 误删了 b.c 观察到的边"


def test_edge_count_matches_full_rebuild(proj, tmp_path):
    """★★ 增量结果必须和全量重建**逐边一致**。

    这是增量正确性的最强判据：任何漏删、多删、漏加都会让两边对不上。
    """
    db_inc = tmp_path / "inc.db"
    conn, _ = _parse(proj, db_inc, full=True)
    conn.close()
    (proj / "a.c").write_text(SRC_A_MODIFIED, encoding="utf-8")
    conn, _ = _parse(proj, db_inc, full=False)
    inc_edges = {tuple(r) for r in conn.execute(
        """SELECT s.name, d.name, e.kind, e.confidence FROM edge e
             JOIN node s ON s.id=e.src JOIN node d ON d.id=e.dst""")}
    conn.close()

    db_full = tmp_path / "full.db"
    conn, _ = _parse(proj, db_full, full=True)
    full_edges = {tuple(r) for r in conn.execute(
        """SELECT s.name, d.name, e.kind, e.confidence FROM edge e
             JOIN node s ON s.id=e.src JOIN node d ON d.id=e.dst""")}

    assert inc_edges == full_edges, (
        f"增量与全量不一致：\n多出 {inc_edges - full_edges}\n缺失 {full_edges - inc_edges}")


def test_deleted_file_is_cleaned_up(proj, tmp_path):
    """源文件被删除后，它贡献的节点和边要一起清掉。"""
    db = tmp_path / "kb.db"
    conn, _ = _parse(proj, db, full=True)
    assert conn.execute(
        "SELECT COUNT(*) c FROM node WHERE name='other'").fetchone()["c"] == 1
    conn.close()

    (proj / "b.c").unlink()
    conn, _ = _parse(proj, db, full=False)
    assert conn.execute(
        "SELECT COUNT(*) c FROM node WHERE name='other'").fetchone()["c"] == 0


# ==================== 边数不膨胀 ====================

def test_logical_edges_not_duplicated_per_tu(proj, tmp_path):
    """★ 同一条逻辑边被多个 TU 观察时，edge 表只能有一行。

    早期版本把 tu 放进唯一键，lwIP 上边数从 1.1 万膨胀到 17.4 万，
    而且"有多少条调用关系"这个统计口径直接失真。
    """
    db = tmp_path / "kb.db"
    conn, _ = _parse(proj, db, full=True)

    dup = conn.execute(
        """SELECT src,dst,kind,confidence,COUNT(*) c FROM edge
            GROUP BY src,dst,kind,confidence HAVING c > 1""").fetchall()
    assert not dup, f"逻辑边出现重复行: {len(dup)} 组"

    # 但关联表里应该有多条来源记录
    multi = conn.execute(
        """SELECT edge_id, COUNT(*) c FROM edge_source
            GROUP BY edge_id HAVING c > 1 LIMIT 1""").fetchone()
    assert multi is not None, "共享头文件的边应当被多个 TU 记录来源"


def test_args_change_triggers_reparse(proj, tmp_path):
    """改编译参数（比如 -D 宏）也必须触发重解析，不能只看文件内容。"""
    db = tmp_path / "kb.db"
    conn, _ = _parse(proj, db, full=True)
    conn.close()

    conn = dbm.connect(db)
    units, _ = compile_db.load(str(proj), None, [str(proj)], ["EXTRA_FLAG=1"])
    _, _, stats = inc.plan(conn, units, str(proj))
    assert stats["changed"] == 2, "编译参数变化未触发重解析"
