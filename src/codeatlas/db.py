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
  definition_hash TEXT,
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

-- 显式控制分支只记录源码事实和范围，不推断完整控制流/数据流。
CREATE TABLE IF NOT EXISTS branch_fact (
  id             TEXT PRIMARY KEY,
  function_id    TEXT NOT NULL,
  tu             TEXT NOT NULL,
  path           TEXT NOT NULL,
  line_start     INTEGER NOT NULL,
  line_end       INTEGER NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('if','switch','conditional','early_return','error_return')),
  condition_text TEXT,
  source_hash    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_branch_fn ON branch_fact(function_id, line_start);
CREATE INDEX IF NOT EXISTS ix_branch_tu ON branch_fact(tu);

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
  session_id    TEXT,
  content_hash  TEXT,
  evidence_tags TEXT,
  evidence_turns TEXT,
  card_markdown TEXT,
  draft_path    TEXT,
  canonical_path TEXT,
  artifact_scope TEXT NOT NULL DEFAULT 'formal'
                 CHECK (artifact_scope IN ('formal','isolated_experiment')),
  status        TEXT DEFAULT 'pending'
                CHECK (status IN ('draft','pending','approved','rejected','stale','superseded')),
  supersedes_id TEXT,
  reviewer_note TEXT,
  stale_reason  TEXT,
  model         TEXT,
  prompt_ver    TEXT,
  created_at    TEXT,
  reviewed_at   TEXT,
  updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS experience_link (
  exp_id     TEXT,
  node_id    TEXT,
  relation   TEXT,
  confidence TEXT,
  PRIMARY KEY (exp_id, node_id)
);

-- 原始会话只用于审核、不能进入 FTS。公开资产必须有明确来源和许可证。
CREATE TABLE IF NOT EXISTS conversation_session (
  id           TEXT PRIMARY KEY,
  source_type  TEXT NOT NULL,
  source_ref   TEXT NOT NULL,
  license       TEXT NOT NULL,
  repository   TEXT NOT NULL,
  revision     TEXT NOT NULL,
  content_hash TEXT NOT NULL UNIQUE,
  status       TEXT NOT NULL CHECK (status IN ('imported','rejected')),
  curation_goal TEXT,
  created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_message (
  session_id   TEXT NOT NULL,
  ordinal      INTEGER NOT NULL,
  role         TEXT NOT NULL CHECK (role IN ('user','assistant','developer','reviewer','tool')),
  knowledge_role TEXT CHECK (knowledge_role IS NULL OR knowledge_role IN
                    ('symptom','hypothesis','dead_end','evidence','root_cause','fix','verification')),
  content      TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  PRIMARY KEY (session_id, ordinal)
);
CREATE TABLE IF NOT EXISTS conversation_assessment (
  session_id   TEXT PRIMARY KEY,
  eligible     INTEGER NOT NULL CHECK (eligible IN (0,1)),
  checks_json  TEXT NOT NULL,
  reasons_json TEXT NOT NULL,
  assessed_at  TEXT NOT NULL
);

-- 会话先被策划成候选，再由用户逐条确认；未确认候选永不进入知识卡或索引。
CREATE TABLE IF NOT EXISTS curation_candidate (
  id              TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL,
  ordinal         INTEGER NOT NULL,
  payload_json    TEXT NOT NULL,
  original_json   TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('proposed','accepted','edited','corrected','skipped')),
  correction_diff TEXT,
  skip_reason     TEXT,
  card_id         TEXT,
  model           TEXT,
  prompt_ver      TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  UNIQUE(session_id, ordinal)
);

-- 仅 active=1 的锚点用于当前卡片审核；旧锚点保留为审计历史。
CREATE TABLE IF NOT EXISTS experience_anchor (
  id              INTEGER PRIMARY KEY,
  exp_id          TEXT NOT NULL,
  repository      TEXT NOT NULL,
  revision        TEXT NOT NULL,
  usr             TEXT NOT NULL,
  path            TEXT NOT NULL,
  line_start      INTEGER NOT NULL,
  line_end        INTEGER NOT NULL,
  definition_hash TEXT NOT NULL,
  relation        TEXT NOT NULL DEFAULT 'explains'
                  CHECK (relation IN ('explains','warns','fixes','constrains')),
  active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  bound_at        TEXT NOT NULL,
  bound_by        TEXT NOT NULL,
  UNIQUE(exp_id, repository, revision, usr, definition_hash)
);
CREATE INDEX IF NOT EXISTS ix_exp_anchor_active ON experience_anchor(exp_id, active);

CREATE TABLE IF NOT EXISTS experience_qa (
  id              INTEGER PRIMARY KEY,
  exp_id          TEXT NOT NULL,
  question        TEXT NOT NULL,
  expected_points TEXT NOT NULL,
  evidence_tags   TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','passed','failed')),
  reviewer        TEXT,
  note            TEXT,
  created_at      TEXT NOT NULL,
  reviewed_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_exp_qa_card ON experience_qa(exp_id, id);

-- 审核意见不可被一条可变字段覆盖；审核页展示的是这份 append-only 历史。
CREATE TABLE IF NOT EXISTS experience_review (
  id          INTEGER PRIMARY KEY,
  exp_id      TEXT NOT NULL,
  action      TEXT NOT NULL CHECK (action IN ('approve','reject','stale','supersede','migrate')),
  reviewer    TEXT NOT NULL,
  reviewer_type TEXT NOT NULL DEFAULT 'legacy_unknown',
  evaluation_scope TEXT NOT NULL DEFAULT 'formal',
  model TEXT,
  agent_id TEXT,
  prompt_hash TEXT,
  input_hash TEXT,
  note        TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_exp_review_card ON experience_review(exp_id, id);

-- Agent 只记录可复核的轨迹，不记录或索引任何外部原始会话。
CREATE TABLE IF NOT EXISTS agent_run (
  id            TEXT PRIMARY KEY,
  session_id    TEXT,
  query         TEXT NOT NULL,
  repository    TEXT NOT NULL,
  revision      TEXT NOT NULL,
  mode          TEXT NOT NULL CHECK (mode IN ('rule','llm')),
  requested_mode TEXT CHECK (requested_mode IN ('auto','model','rule')),
  execution_mode TEXT CHECK (execution_mode IN ('model','rule')),
  fallback_reason TEXT,
  model         TEXT,
  prompt_ver    TEXT,
  answer_source TEXT,
  latency_ms    REAL,
  status        TEXT NOT NULL CHECK (status IN ('completed','refused','failed')),
  answer        TEXT,
  refused       INTEGER NOT NULL DEFAULT 0 CHECK (refused IN (0,1)),
  draft_json    TEXT,
  draft_id      TEXT,
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_event (
  run_id        TEXT NOT NULL,
  ordinal       INTEGER NOT NULL,
  tool_name     TEXT NOT NULL,
  arguments     TEXT NOT NULL,
  result_json   TEXT NOT NULL,
  citation_tags TEXT NOT NULL,
  status        TEXT DEFAULT 'completed',
  duration_ms   REAL,
  phase         TEXT CHECK (phase IN ('discover','understand','verify')),
  created_at    TEXT NOT NULL,
  PRIMARY KEY (run_id, ordinal)
);
CREATE INDEX IF NOT EXISTS ix_agent_event_run ON agent_event(run_id, ordinal);

-- 用户反馈只进入待处理队列。它不能直接改写回答、知识卡或检索索引。
CREATE TABLE IF NOT EXISTS agent_feedback (
  id            INTEGER PRIMARY KEY,
  run_id        TEXT NOT NULL,
  verdict       TEXT NOT NULL CHECK (verdict IN ('helpful','incorrect','incomplete')),
  evidence_tag  TEXT,
  comment       TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_agent_feedback_run ON agent_feedback(run_id, id);

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
  repository TEXT,
  revision   TEXT,
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

CREATE TABLE IF NOT EXISTS knowledge_set (
  id TEXT PRIMARY KEY, state TEXT NOT NULL, directory TEXT NOT NULL,
  base_active TEXT, governance_revision INTEGER NOT NULL DEFAULT 0,
  manifest_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
  error TEXT
);
CREATE TABLE IF NOT EXISTS invalidation_event (
  id INTEGER PRIMARY KEY, knowledge_set_id TEXT, target TEXT NOT NULL,
  trigger_id TEXT, rule TEXT NOT NULL, dependency_path TEXT,
  before_hash TEXT, after_hash TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_fingerprint (
  node_id TEXT PRIMARY KEY, signature_hash TEXT, semantic_hash TEXT,
  skeleton_hash TEXT, dependency_hash TEXT, complete INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS semantic_fact (
  id TEXT PRIMARY KEY, function_id TEXT NOT NULL, kind TEXT NOT NULL,
  payload_json TEXT NOT NULL, confidence TEXT NOT NULL,
  path TEXT, line_start INTEGER, line_end INTEGER, tu TEXT
);
CREATE INDEX IF NOT EXISTS ix_semantic_function ON semantic_fact(function_id);
CREATE TABLE IF NOT EXISTS knowledge_dependency (
  owner_type TEXT NOT NULL, owner_id TEXT NOT NULL, dependency_id TEXT NOT NULL,
  kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
  PRIMARY KEY(owner_type,owner_id,dependency_id,kind)
);
CREATE TABLE IF NOT EXISTS publication_journal (
  op_id TEXT PRIMARY KEY, state TEXT NOT NULL, files_json TEXT NOT NULL,
  created_at TEXT NOT NULL, committed_at TEXT
);
CREATE TABLE IF NOT EXISTS review_confirmation (
  exp_id TEXT NOT NULL, bundle_hash TEXT NOT NULL, reviewer TEXT NOT NULL,
  reviewer_type TEXT NOT NULL DEFAULT 'human',
  confirmed_at TEXT NOT NULL, PRIMARY KEY(exp_id,bundle_hash)
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）知识库。"""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    _migrate(conn)
    return conn


def read_only(db_path: str | Path) -> sqlite3.Connection:
    """Read the live control DB including WAL, without DDL or schema migrations."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def open_existing(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=rw", uri=True,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def query(db_path: str | Path) -> sqlite3.Connection:
    """Pin a CLI query connection to one active immutable set; caller closes it."""
    control = read_only(db_path)
    active = get_meta(control, "active_knowledge_set")
    if active:
        from .snapshots import readonly
        directory = control.execute("SELECT directory FROM knowledge_set WHERE id=?", (active,)).fetchone()[0]
        facts = readonly(Path(directory) / "facts.db")
        control.close()
        return facts
    return control


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, declaration: str) -> None:
    name = declaration.split()[0]
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {declaration}")


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return (row["sql"] or "") if row else ""


def _rebuild_experience_statuses(conn: sqlite3.Connection) -> None:
    """SQLite cannot alter CHECK constraints; preserve every available legacy field."""
    old_columns = _columns(conn, "experience")
    conn.executescript("""
    CREATE TABLE experience_new (
      id TEXT PRIMARY KEY, title TEXT, symptom TEXT, hypotheses TEXT, dead_ends TEXT,
      root_cause TEXT, fix_steps TEXT, verification TEXT, source_type TEXT, source_ref TEXT,
      session_id TEXT, content_hash TEXT, evidence_tags TEXT, evidence_turns TEXT,
      card_markdown TEXT, draft_path TEXT, canonical_path TEXT,
      status TEXT DEFAULT 'pending'
        CHECK (status IN ('draft','pending','approved','rejected','stale','superseded')),
      supersedes_id TEXT,
      reviewer_note TEXT, stale_reason TEXT, model TEXT, prompt_ver TEXT,
      created_at TEXT, reviewed_at TEXT, updated_at TEXT
    );
    """)
    target_columns = [
        "id", "title", "symptom", "hypotheses", "dead_ends", "root_cause",
        "fix_steps", "verification", "source_type", "source_ref", "session_id",
        "content_hash", "evidence_tags", "evidence_turns", "card_markdown",
        "draft_path", "canonical_path", "status", "supersedes_id",
        "reviewer_note", "stale_reason", "model", "prompt_ver", "created_at",
        "reviewed_at", "updated_at",
    ]
    selected = [name if name in old_columns else "NULL" for name in target_columns]
    conn.execute(
        f"INSERT INTO experience_new({','.join(target_columns)}) "
        f"SELECT {','.join(selected)} FROM experience"
    )
    conn.executescript("DROP TABLE experience; ALTER TABLE experience_new RENAME TO experience;")


def _rebuild_experience_reviews(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE experience_review_new (
      id INTEGER PRIMARY KEY, exp_id TEXT NOT NULL,
      action TEXT NOT NULL CHECK (action IN ('approve','reject','stale','supersede','migrate')),
      reviewer TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
    );
    INSERT INTO experience_review_new(id,exp_id,action,reviewer,note,created_at)
      SELECT id,exp_id,action,reviewer,note,created_at FROM experience_review;
    DROP TABLE experience_review;
    ALTER TABLE experience_review_new RENAME TO experience_review;
    CREATE INDEX IF NOT EXISTS ix_exp_review_card ON experience_review(exp_id, id);
    """)


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply safe, idempotent upgrades to databases created by earlier portfolio builds."""
    _add_column(conn, "node", "definition_hash TEXT")
    if "'superseded'" not in _table_sql(conn, "experience"):
        _rebuild_experience_statuses(conn)
    for declaration in (
        "session_id TEXT", "content_hash TEXT", "evidence_tags TEXT", "card_markdown TEXT",
        "stale_reason TEXT", "updated_at TEXT", "supersedes_id TEXT",
        "evidence_turns TEXT", "draft_path TEXT", "canonical_path TEXT",
        "dependency_manifest TEXT", "review_bundle_hash TEXT", "publication_op_id TEXT",
        "experiment_manifest TEXT",
        "artifact_scope TEXT NOT NULL DEFAULT 'formal'",
    ):
        _add_column(conn, "experience", declaration)
    for declaration in (
        "draft_id TEXT", "requested_mode TEXT", "execution_mode TEXT",
        "fallback_reason TEXT", "model TEXT", "prompt_ver TEXT",
        "answer_source TEXT", "latency_ms REAL",
        "knowledge_set_id TEXT", "source_content_hash TEXT", "verification_json TEXT",
    ):
        _add_column(conn, "agent_run", declaration)
    for declaration in (
        "status TEXT DEFAULT 'completed'", "duration_ms REAL",
        "phase TEXT CHECK (phase IN ('discover','understand','verify'))",
    ):
        _add_column(conn, "agent_event", declaration)
    for declaration in ("repository TEXT", "revision TEXT"):
        _add_column(conn, "wiki_page", declaration)
    if "'supersede'" not in _table_sql(conn, "experience_review"):
        _rebuild_experience_reviews(conn)
    _add_column(conn, "conversation_session", "curation_goal TEXT")
    _add_column(conn, "conversation_message", "knowledge_role TEXT")
    _add_column(conn, "experience_anchor", "relation TEXT DEFAULT 'explains'")

    # Tables added after the initial DDL must also exist for an already-open database.
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS conversation_session (
      id TEXT PRIMARY KEY, source_type TEXT NOT NULL, source_ref TEXT NOT NULL,
      license TEXT NOT NULL, repository TEXT NOT NULL, revision TEXT NOT NULL,
      content_hash TEXT NOT NULL UNIQUE, status TEXT NOT NULL
        CHECK (status IN ('imported','rejected')), created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS conversation_message (
      session_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
      role TEXT NOT NULL CHECK (role IN ('user','assistant','developer','reviewer','tool')),
      knowledge_role TEXT CHECK (knowledge_role IS NULL OR knowledge_role IN
        ('symptom','hypothesis','dead_end','evidence','root_cause','fix','verification')),
      content TEXT NOT NULL, content_hash TEXT NOT NULL,
      PRIMARY KEY (session_id, ordinal)
    );
    CREATE TABLE IF NOT EXISTS conversation_assessment (
      session_id TEXT PRIMARY KEY, eligible INTEGER NOT NULL CHECK (eligible IN (0,1)),
      checks_json TEXT NOT NULL, reasons_json TEXT NOT NULL, assessed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS curation_candidate (
      id TEXT PRIMARY KEY, session_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
      payload_json TEXT NOT NULL, original_json TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('proposed','accepted','edited','corrected','skipped')),
      correction_diff TEXT, skip_reason TEXT, card_id TEXT, model TEXT, prompt_ver TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(session_id, ordinal)
    );
    CREATE TABLE IF NOT EXISTS experience_anchor (
      id INTEGER PRIMARY KEY, exp_id TEXT NOT NULL, repository TEXT NOT NULL,
      revision TEXT NOT NULL, usr TEXT NOT NULL, path TEXT NOT NULL,
      line_start INTEGER NOT NULL, line_end INTEGER NOT NULL,
      definition_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
      bound_at TEXT NOT NULL, bound_by TEXT NOT NULL,
      UNIQUE(exp_id, repository, revision, usr, definition_hash)
    );
    CREATE INDEX IF NOT EXISTS ix_exp_anchor_active ON experience_anchor(exp_id, active);
    CREATE TABLE IF NOT EXISTS experience_qa (
      id INTEGER PRIMARY KEY, exp_id TEXT NOT NULL, question TEXT NOT NULL,
      expected_points TEXT NOT NULL, evidence_tags TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','passed','failed')),
      reviewer TEXT, note TEXT, created_at TEXT NOT NULL, reviewed_at TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_exp_qa_card ON experience_qa(exp_id, id);
    CREATE TABLE IF NOT EXISTS branch_fact (
      id TEXT PRIMARY KEY, function_id TEXT NOT NULL, tu TEXT NOT NULL, path TEXT NOT NULL,
      line_start INTEGER NOT NULL, line_end INTEGER NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('if','switch','conditional','early_return','error_return')),
      condition_text TEXT, source_hash TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_branch_fn ON branch_fact(function_id, line_start);
    CREATE INDEX IF NOT EXISTS ix_branch_tu ON branch_fact(tu);
    CREATE TABLE IF NOT EXISTS experience_review (
      id INTEGER PRIMARY KEY, exp_id TEXT NOT NULL,
      action TEXT NOT NULL CHECK (action IN ('approve','reject','stale','supersede','migrate')),
      reviewer TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_exp_review_card ON experience_review(exp_id, id);
    CREATE TABLE IF NOT EXISTS agent_run (
      id TEXT PRIMARY KEY, session_id TEXT, query TEXT NOT NULL, repository TEXT NOT NULL,
      revision TEXT NOT NULL, mode TEXT NOT NULL CHECK (mode IN ('rule','llm')),
      requested_mode TEXT CHECK (requested_mode IN ('auto','model','rule')),
      execution_mode TEXT CHECK (execution_mode IN ('model','rule')),
      fallback_reason TEXT, model TEXT, prompt_ver TEXT, answer_source TEXT, latency_ms REAL,
      status TEXT NOT NULL CHECK (status IN ('completed','refused','failed')),
      answer TEXT, refused INTEGER NOT NULL DEFAULT 0 CHECK (refused IN (0,1)),
      draft_json TEXT, draft_id TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS agent_event (
      run_id TEXT NOT NULL, ordinal INTEGER NOT NULL, tool_name TEXT NOT NULL,
      arguments TEXT NOT NULL, result_json TEXT NOT NULL, citation_tags TEXT NOT NULL,
      status TEXT DEFAULT 'completed', duration_ms REAL,
      phase TEXT CHECK (phase IN ('discover','understand','verify')),
      created_at TEXT NOT NULL, PRIMARY KEY (run_id, ordinal)
    );
    CREATE INDEX IF NOT EXISTS ix_agent_event_run ON agent_event(run_id, ordinal);
    CREATE TABLE IF NOT EXISTS agent_feedback (
      id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,
      verdict TEXT NOT NULL CHECK (verdict IN ('helpful','incorrect','incomplete')),
      evidence_tag TEXT, comment TEXT, created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_agent_feedback_run ON agent_feedback(run_id, id);
    """)

    # Historic approved rows relied only on symbol-name guesses. They are never B-level
    # evidence until a reviewer adds a verified, active source anchor.
    conn.execute("""
      UPDATE experience SET status='pending', stale_reason=NULL,
        reviewer_note=CASE WHEN COALESCE(reviewer_note,'') = ''
          THEN '迁移：旧条目缺少精确源码锚点，需重新审核'
          ELSE reviewer_note || '\\n迁移：缺少精确源码锚点，需重新审核' END
       WHERE status='approved'
         AND NOT EXISTS (
           SELECT 1 FROM experience_anchor a
            WHERE a.exp_id=experience.id AND a.active=1
         )
    """)
    # The second publication gate was added after exact anchors.  An old approved
    # row with an anchor but without a reviewer-passed QA must not retain B-level
    # authority merely because it predates the new methodology.
    conn.execute("""
      UPDATE experience SET status='pending', stale_reason=NULL,
        reviewer_note=CASE WHEN COALESCE(reviewer_note,'') = ''
          THEN '迁移：旧条目缺少已通过 QA，需重新审核'
          ELSE reviewer_note || '\n迁移：缺少已通过 QA，需重新审核' END
       WHERE status='approved'
         AND NOT EXISTS (
           SELECT 1 FROM experience_qa q
            WHERE q.exp_id=experience.id AND q.status='passed'
         )
    """)
    # Older cards had only a mutable reviewer_note. Preserve it as a marked legacy
    # record once, so the new review page does not pretend there was no history.
    conn.execute("""
      INSERT INTO experience_review(exp_id,action,reviewer,note,created_at)
      SELECT id,'migrate','legacy',reviewer_note,COALESCE(reviewed_at,created_at,'unknown')
        FROM experience
       WHERE COALESCE(reviewer_note,'') <> ''
         AND NOT EXISTS (SELECT 1 FROM experience_review r WHERE r.exp_id=experience.id)
    """)
    _add_column(conn, "experience_qa", "bundle_hash TEXT")
    for declaration in (
        "reviewer_type TEXT NOT NULL DEFAULT 'legacy_unknown'",
        "evaluation_scope TEXT NOT NULL DEFAULT 'formal'",
        "model TEXT", "agent_id TEXT", "prompt_hash TEXT", "input_hash TEXT",
    ):
        _add_column(conn, "experience_qa", declaration)
        _add_column(conn, "experience_review", declaration)
    _add_column(conn, "review_confirmation", "reviewer_type TEXT NOT NULL DEFAULT 'human'")
    conn.execute("""
      UPDATE experience SET status='pending', stale_reason=NULL,
        reviewer_note=CASE WHEN COALESCE(reviewer_note,'')=''
          THEN '迁移：旧批准记录缺少结构化 human/formal 审核来源，需重新审核'
          ELSE reviewer_note || '\n迁移：缺少结构化 human/formal 审核来源，需重新审核' END
       WHERE status='approved' AND COALESCE(artifact_scope,'formal')='formal'
         AND NOT EXISTS (
           SELECT 1 FROM experience_review r WHERE r.exp_id=experience.id
             AND r.action='approve' AND r.reviewer_type='human'
             AND r.evaluation_scope='formal'
         )
    """)
    conn.commit()


def reset(db_path: str | Path) -> None:
    """删库重建（parse 阶段的幂等性靠这个保证）。"""
    p = Path(db_path)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            f.unlink()


def reset_code_facts(conn: sqlite3.Connection) -> None:
    """Clear rebuildable code projections while preserving governed assets.

    A forced parse is a code refresh, not permission to erase conversations,
    pending/approved cards, QA, review history, Agent traces, or feedback.  The
    old implementation deleted the entire SQLite file and silently detached the
    Markdown drafts from their audit records.  Keep those assets and rebuild only
    compiler/Wiki/search tables; ``stale_check`` validates their anchors after the
    new AST has been persisted.
    """
    code_tables = (
        "edge_source", "edge", "branch_fact", "semantic_fact", "entity_fingerprint", "summary_head", "chunk",
        "wiki_page", "gen_task", "tu_state", "node", "experience_link",
    )
    existing = {
        row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }
    try:
        conn.execute("BEGIN IMMEDIATE")
        if "chunk_fts" in existing:
            conn.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('delete-all')")
        for table in code_tables:
            if table in existing:
                conn.execute(f"DELETE FROM {table}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default
