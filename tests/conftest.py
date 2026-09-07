"""共享 fixture。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "mini_c"
sys.path.insert(0, str(ROOT / "src"))

from codeatlas import db as dbm                      # noqa: E402
from codeatlas.indexer import build as indexer       # noqa: E402
from codeatlas.parser import ast_walker, compile_db  # noqa: E402
from codeatlas.summary import head                   # noqa: E402


@pytest.fixture(scope="session")
def kb(tmp_path_factory):
    """建一次库，全模块复用。"""
    d = tmp_path_factory.mktemp("kb")
    dbp = d / "kb.db"
    conn = dbm.connect(dbp)

    units, _ = compile_db.load(str(CORPUS), None, [str(CORPUS)], [])
    w = ast_walker.AstWalker(str(CORPUS))
    for u in units:
        w.walk_unit(u.source, u.args, u.directory)
    ast_walker.persist(conn, w.result)
    dbm.set_meta(conn, "repo", "tests/fixtures/mini_c")
    dbm.set_meta(conn, "repository_id", "tests/fixtures/mini_c")
    dbm.set_meta(conn, "revision", "fixture")
    conn.commit()

    head.build(conn, str(CORPUS))
    indexer.build(conn, str(CORPUS), out_dir=str(d))
    yield conn, str(d)
    conn.close()
