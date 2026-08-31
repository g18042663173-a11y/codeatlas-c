"""SQLite 存储层：DDL 定义与连接管理。

设计要点：
  - 图不用图数据库，用 node/edge 两张表 + 递归 CTE（见 graph/traverse.py）
  - edge.confidence 区分 certain / candidate，是全系统最核心的字段
  - chunk 用 rowid INTEGER PRIMARY KEY 以适配 FTS5 external content 表
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ================= 图层 =================
CREATE TABLE IF NOT EXISTS node (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  name          TEXT NOT NULL,
  usr           TEXT,
  repo          TEXT,
  path          TEXT,
  line_start    INTEGER,
  line_end      INTEGER,
  signature     TEXT,
  is_definition INTEGER DEFAULT 0,
  is_static     INTEGER DEFAULT 0,
  extra         TEXT
);
CREATE INDEX IF NOT EXISTS ix_node_name ON node(name);
CREATE INDEX IF NOT EXISTS ix_node_kind ON node(kind);
CREATE INDEX IF NOT EXISTS ix_node_path ON node(path);
CREATE INDEX IF NOT EXISTS ix_node_usr  ON node(usr);

CREATE TABLE IF NOT EXISTS edge (
  id         INTEGER PRIMARY KEY,
  src        TEXT NOT NULL,
  dst        TEXT NOT NULL,
  kind       TEXT NOT NULL,
  confidence TEXT NOT NULL CHECK (confidence IN ('certain','candidate')),
  reason     TEXT,
  evidence   TEXT,
  UNIQUE(src, dst, kind, confidence)
);
CREATE INDEX IF NOT EXISTS ix_edge_src ON edge(src, kind, confidence);
CREATE INDEX IF NOT EXISTS ix_edge_dst ON edge(dst, kind, confidence);

-- 边与翻译单元的关联（多对多）。
-- ★ 为什么不把 tu 直接放进 edge 的唯一键：
--   同一条逻辑边会被每个 include 该头文件的 TU 各观察一次，
--   lwIP 上实测会让边数从 1.1 万膨胀到 17.4 万（15 倍），
--   而且 "有多少条调用关系" 这个统计口径直接失真。
--   拆成关联表后：edge 存逻辑边（计数正确），edge_source 记来源（增量可撤销），
--   撤销某 TU 时按引用计数回收 —— 没有任何 TU 再引用的边才真正删除。
CREATE TABLE IF NOT EXISTS edge_source (
  edge_id INTEGER NOT NULL,
  tu      TEXT NOT NULL,
  PRIMARY KEY (edge_id, tu)
);
CREATE INDEX IF NOT EXISTS ix_esrc_tu ON edge_source(tu);

-- ================= 检索摘要头 =================
CREATE TABLE IF NOT EXISTS summary_head (
  node_id      TEXT PRIMARY KEY,
  one_liner    TEXT,
  callees      TEXT,
  callers      TEXT,
  headers      TEXT,
  key_types    TEXT,
  key_macros   TEXT,
  pitfalls     TEXT,
  fan_in       INTEGER DEFAULT 0,
  fan_out      INTEGER DEFAULT 0,
  generated_by TEXT DEFAULT 'rule',
  token_len    INTEGER DEFAULT 0,
  src_hash     TEXT
);

-- ================= 检索层 =================
CREATE TABLE IF NOT EXISTS chunk (
  rowid          INTEGER PRIMARY KEY,
  uid            TEXT UNIQUE,
  node_id        TEXT,
  kind           TEXT,
  title          TEXT,
  text           TEXT,
  source_ref     TEXT,
  evidence_level TEXT,
  visible        INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_chunk_node ON chunk(node_id);
CREATE INDEX IF NOT EXISTS ix_chunk_vis  ON chunk(visible);

CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
  title, text,
  content='chunk', content_rowid='rowid',
  tokenize="unicode61 tokenchars '_'"
);

-- ================= 经验库 =================
CREATE TABLE IF NOT EXISTS experience (
  id            TEXT PRIMARY KEY,
  title         TEXT,
  symptom       TEXT,
  hypotheses    TEXT,
  dead_ends     TEXT,
  root_cause    TEXT,
  fix_steps     TEXT,
  verification  TEXT,
  source_type   TEXT,
  source_ref    TEXT,
  status        TEXT DEFAULT 'pending'
                CHECK (status IN ('draft','pending','approved','rejected')),
  reviewer_note TEXT,
  model         TEXT,
  prompt_ver    TEXT,
  created_at    TEXT,
  reviewed_at   TEXT
);
CREATE TABLE IF NOT EXISTS experience_link (
  exp_id     TEXT,
  node_id    TEXT,
  relation   TEXT,
  confidence TEXT,
  PRIMARY KEY (exp_id, node_id)
);

-- ================= Wiki =================
CREATE TABLE IF NOT EXISTS wiki_page (
  id         TEXT PRIMARY KEY,
  level      TEXT,
  node_id    TEXT,
  title      TEXT,
  md         TEXT,
  sources    TEXT,
  status     TEXT,
  version    INTEGER DEFAULT 1,
  input_hash TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS gen_task (
  id         TEXT PRIMARY KEY,
  target_id  TEXT,
  stage      TEXT,
  state      TEXT,
  attempt    INTEGER DEFAULT 0,
  last_error TEXT,
  updated_at TEXT
);

-- ================= 运行元信息 =================
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）知识库。"""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    return conn


def reset(db_path: str | Path) -> None:
    """删库重建（parse 阶段的幂等性靠这个保证）。"""
    p = Path(db_path)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            f.unlink()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default
