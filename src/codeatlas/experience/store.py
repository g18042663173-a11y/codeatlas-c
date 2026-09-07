"""Review-gated knowledge cards and their exact code anchors."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import db as dbm

log = logging.getLogger(__name__)
PROMPT_VERSION = "knowledge-card-v2-curated"
EXTRACT_SCHEMA = {
    "title": "≤40字概括", "symptom": "问题现象", "hypotheses": ["排查假设"],
    "dead_ends": ["已验证无效的尝试"], "root_cause": "仅限会话与代码证据覆盖的结论",
    "fix_steps": "可执行修复或排查步骤", "verification": "验证方式",
    "evidence_turns": ["形成结论所依据的会话轮次，例如 1,3,4"],
    "related_symbols": ["仅作审核候选，不是锚点"], "confidence": "high | medium | low",
    "evidence_tags": ["当前证据包中的 A/B 标签，例如 A1"],
}
REQUIRED_FIELDS = ("title", "symptom")
ANCHOR_RELATIONS = {"explains", "warns", "fixes", "constrains"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate(data: dict[str, Any]) -> tuple[bool, str]:
    for field in REQUIRED_FIELDS:
        if not data.get(field):
            return False, f"缺少必填字段: {field}"
    for field in ("hypotheses", "dead_ends", "related_symbols", "evidence_tags", "evidence_turns"):
        if field in data and not isinstance(data[field], list):
            return False, f"{field} 必须是数组"
    if data.get("confidence") not in (None, "high", "medium", "low"):
        return False, "confidence 取值非法"
    return True, ""


def _knowledge_root(conn: sqlite3.Connection) -> Path:
    configured = dbm.get_meta(conn, "knowledge_dir", "")
    if configured:
        return Path(configured).resolve()
    row = conn.execute("PRAGMA database_list").fetchone()
    db_path = Path(row["file"]) if row and row["file"] else Path.cwd() / "data" / "kb.db"
    root = db_path.parent / f"{db_path.stem}-knowledge"
    return root.resolve()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _markdown(row: sqlite3.Row | dict[str, Any], anchors: list[dict[str, Any]],
              qas: list[dict[str, Any]] | None = None,
              reviews: list[dict[str, Any]] | None = None) -> str:
    card = dict(row)
    qas = qas or []
    reviews = reviews or []
    record_fields = (
        "id", "title", "symptom", "hypotheses", "dead_ends", "root_cause",
        "fix_steps", "verification", "source_type", "source_ref", "session_id",
        "content_hash", "evidence_tags", "evidence_turns", "status", "supersedes_id",
        "reviewer_note", "stale_reason", "model", "prompt_ver", "created_at",
        "reviewed_at", "updated_at",
        "dependency_manifest", "review_bundle_hash", "publication_op_id",
        "experiment_manifest", "artifact_scope",
    )
    payload = {
        "schema_version": 2,
        "record": {key: card.get(key) for key in record_fields},
        "anchors": [{key: anchor.get(key) for key in (
            "repository", "revision", "usr", "path", "line_start", "line_end",
            "definition_hash", "relation", "active", "bound_at", "bound_by",
        )} for anchor in anchors],
        "qas": [{key: qa.get(key) for key in (
            "question", "expected_points", "evidence_tags", "status", "reviewer",
            "note", "created_at", "reviewed_at",
            "bundle_hash", "reviewer_type", "evaluation_scope", "model",
            "agent_id", "prompt_hash", "input_hash",
        )} for qa in qas],
        "reviews": [{key: review.get(key) for key in (
            "action", "reviewer", "reviewer_type", "evaluation_scope", "model",
            "agent_id", "prompt_hash", "input_hash", "note", "created_at",
        )} for review in reviews],
    }
    payload["checksum"] = _hash(payload)
    hypotheses = json.loads(card.get("hypotheses") or "[]")
    dead_ends = json.loads(card.get("dead_ends") or "[]")
    evidence_turns = json.loads(card.get("evidence_turns") or "[]")
    lines = ["---", json.dumps(payload, ensure_ascii=False, indent=2), "---", "",
             f"# {card['title']}", "", "## 问题", card.get("symptom") or "", "",
             "## 排查假设", *[f"- {item}" for item in hypotheses], "",
             "## 无效尝试", *[f"- {item}" for item in dead_ends], "",
             "## 真实根因", card.get("root_cause") or "证据不足，未形成结论。", "",
             "## 解决步骤", card.get("fix_steps") or "待补充。", "",
             "## 验证方式", card.get("verification") or "待补充。", "",
             "## 证据轮次", "、".join(str(item) for item in evidence_turns) or "待补充。", "",
             "## QA", *[
                 f"- [{qa.get('status','pending')}] {qa.get('question','')} — {qa.get('note') or '待审核'}"
                 for qa in qas
             ], ""]
    return "\n".join(lines)


def _snapshot(conn: sqlite3.Connection, exp_id: str, **overrides: Any) -> tuple[dict, list, list, list]:
    row = conn.execute("SELECT * FROM experience WHERE id=?", (exp_id,)).fetchone()
    if row is None:
        raise KeyError(f"知识卡不存在: {exp_id}")
    card = {**dict(row), **overrides}
    anchors = [dict(item) for item in conn.execute(
        "SELECT * FROM experience_anchor WHERE exp_id=? ORDER BY active DESC,id", (exp_id,)
    )]
    qas = [dict(item) for item in conn.execute(
        "SELECT * FROM experience_qa WHERE exp_id=? ORDER BY id", (exp_id,)
    )]
    reviews = [dict(item) for item in conn.execute(
        "SELECT * FROM experience_review WHERE exp_id=? ORDER BY id", (exp_id,)
    )]
    return card, anchors, qas, reviews


def _card_path(conn: sqlite3.Connection, exp_id: str, status: str) -> Path:
    folder = "cards" if status in ("approved", "stale", "superseded") else "drafts"
    return _knowledge_root(conn) / folder / f"{exp_id}.md"


def _sync_file(conn: sqlite3.Connection, exp_id: str, **overrides: Any) -> Path:
    card, anchors, qas, reviews = _snapshot(conn, exp_id, **overrides)
    target = _card_path(conn, exp_id, card["status"])
    _atomic_write(target, _markdown(card, anchors, qas, reviews))
    return target


def add(conn: sqlite3.Connection, data: dict[str, Any], *, source_type: str = "manual",
        source_ref: str = "", model: str = "", status: str = "pending",
        session_id: str | None = None, artifact_scope: str | None = None) -> str:
    ok, error = validate(data)
    if not ok:
        raise ValueError(f"知识卡校验失败: {error}")
    if status not in ("draft", "pending"):
        raise ValueError("新建知识卡只能是 draft 或 pending")
    if session_id and not conn.execute("SELECT 1 FROM conversation_session WHERE id=?", (session_id,)).fetchone():
        raise KeyError(f"会话不存在: {session_id}")
    content = {field: data.get(field) for field in EXTRACT_SCHEMA}
    content_hash = _hash(content)
    card_id = "e-" + hashlib.sha1(f"{data['title']}{source_ref}{content_hash}".encode()).hexdigest()[:12]
    now = _now()
    scope = artifact_scope or dbm.get_meta(conn, "evaluation_scope", "formal")
    if scope not in ("formal", "isolated_experiment"):
        raise ValueError("artifact_scope 必须是 formal 或 isolated_experiment")
    try:
        conn.execute(
            """INSERT OR REPLACE INTO experience
               (id,title,symptom,hypotheses,dead_ends,root_cause,fix_steps,verification,
                source_type,source_ref,session_id,content_hash,evidence_tags,evidence_turns,
                status,model,prompt_ver,created_at,updated_at,artifact_scope)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (card_id, data["title"], data["symptom"],
             json.dumps(data.get("hypotheses", []), ensure_ascii=False),
             json.dumps(data.get("dead_ends", []), ensure_ascii=False),
             data.get("root_cause"), data.get("fix_steps"), data.get("verification"),
             source_type, source_ref, session_id, content_hash,
             json.dumps(data.get("evidence_tags", []), ensure_ascii=False),
             json.dumps(data.get("evidence_turns", []), ensure_ascii=False),
             status, model, PROMPT_VERSION, now, now, scope),
        )
        path = _sync_file(conn, card_id)
        conn.execute("UPDATE experience SET draft_path=? WHERE id=?", (str(path), card_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return card_id


def pending(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM experience WHERE status IN ('pending','stale') ORDER BY updated_at DESC LIMIT ?", (limit,)
    ).fetchall()


def get(conn: sqlite3.Connection, exp_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM experience WHERE id=?", (exp_id,)).fetchone()
    if row is None:
        raise KeyError(f"知识卡不存在: {exp_id}")
    anchors = conn.execute(
        "SELECT * FROM experience_anchor WHERE exp_id=? ORDER BY active DESC,id DESC", (exp_id,)
    ).fetchall()
    reviews = conn.execute(
        "SELECT * FROM experience_review WHERE exp_id=? ORDER BY id DESC", (exp_id,)
    ).fetchall()
    qas = conn.execute(
        "SELECT * FROM experience_qa WHERE exp_id=? ORDER BY id", (exp_id,)
    ).fetchall()
    successor = conn.execute(
        "SELECT id,status,title FROM experience WHERE supersedes_id=? ORDER BY updated_at DESC LIMIT 1",
        (exp_id,),
    ).fetchone()
    return {**dict(row), "anchors": [dict(anchor) for anchor in anchors],
            "reviews": [dict(review) for review in reviews], "qas": [dict(qa) for qa in qas],
            "successor": dict(successor) if successor else None,
            "current_review_bundle_hash": review_bundle(conn, exp_id)}


def link_supersedes(conn: sqlite3.Connection, replacement_id: str, prior_id: str) -> dict[str, Any]:
    """Declare lineage without hiding the prior card until replacement approval."""
    if replacement_id == prior_id:
        raise ValueError("知识卡不能替代自身")
    replacement = conn.execute(
        "SELECT id,status,supersedes_id FROM experience WHERE id=?", (replacement_id,)
    ).fetchone()
    prior = conn.execute("SELECT id,status FROM experience WHERE id=?", (prior_id,)).fetchone()
    if replacement is None or prior is None:
        raise KeyError("替代卡或原知识卡不存在")
    if replacement["status"] not in ("draft", "pending", "stale"):
        raise ValueError("只有待审核或过期卡可以声明替代关系")
    if prior["status"] not in ("approved", "stale"):
        raise ValueError("只能替代 approved 或 stale 知识卡")
    cursor = prior_id
    seen = {replacement_id}
    while cursor:
        if cursor in seen:
            raise ValueError("知识卡替代关系不能形成环")
        seen.add(cursor)
        row = conn.execute("SELECT supersedes_id FROM experience WHERE id=?", (cursor,)).fetchone()
        cursor = row["supersedes_id"] if row else None
    conn.execute(
        "UPDATE experience SET supersedes_id=?,updated_at=? WHERE id=?",
        (prior_id, _now(), replacement_id),
    )
    path = _sync_file(conn, replacement_id)
    conn.execute("UPDATE experience SET draft_path=? WHERE id=?", (str(path), replacement_id))
    conn.commit()
    return {"id": replacement_id, "supersedes_id": prior_id, "effective": False}


def bind(conn: sqlite3.Connection, exp_id: str, usr: str, *, bound_by: str = "local-reviewer",
         relation: str = "explains") -> dict[str, Any]:
    if relation not in ANCHOR_RELATIONS:
        raise ValueError("relation 只能是 explains / warns / fixes / constrains")
    card = conn.execute("SELECT * FROM experience WHERE id=?", (exp_id,)).fetchone()
    if card is None:
        raise KeyError(f"知识卡不存在: {exp_id}")
    repository = dbm.get_meta(conn, "repository_id", dbm.get_meta(conn, "repo", ""))
    revision = dbm.get_meta(conn, "revision", "unversioned")
    if not repository or revision == "unversioned":
        raise ValueError("当前知识库没有可审核的 Git repository/revision")
    rows = conn.execute(
        """SELECT * FROM node WHERE usr=? AND kind='function' AND is_definition=1
           ORDER BY path,line_start""", (usr,)
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("USR 必须唯一解析到一个函数定义")
    node = rows[0]
    if not node["definition_hash"]:
        raise ValueError("目标函数缺少 definition_hash；请先重新解析")
    conn.execute("UPDATE experience_anchor SET active=0 WHERE exp_id=? AND active=1", (exp_id,))
    conn.execute(
        """INSERT OR REPLACE INTO experience_anchor
           (exp_id,repository,revision,usr,path,line_start,line_end,definition_hash,relation,active,bound_at,bound_by)
           VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
        (exp_id, repository, revision, node["usr"], node["path"], node["line_start"],
         node["line_end"], node["definition_hash"], relation, _now(), bound_by),
    )
    # Compatibility projection for impact/summary queries. This row is written only
    # after a concrete USR has been verified, never from a name guess.
    conn.execute("DELETE FROM experience_link WHERE exp_id=?", (exp_id,))
    conn.execute(
        "INSERT INTO experience_link(exp_id,node_id,relation,confidence) VALUES(?,?,?,?)",
        (exp_id, node["id"], relation, "certain"),
    )
    evidence_tags = json.loads(card["evidence_tags"] or "[]")
    if not any(str(tag).startswith("A") for tag in evidence_tags):
        # A curation-only candidate has no Agent run registry.  The exact USR
        # binding itself is compiler-backed evidence, so assign the first A tag
        # here and let QA refer to that auditable main anchor.
        evidence_tags.append("A1")
    conn.execute(
        """UPDATE experience SET status='pending', stale_reason=NULL,evidence_tags=?,updated_at=?
             WHERE id=?""",
        (json.dumps(evidence_tags, ensure_ascii=False), _now(), exp_id),
    )
    from .. import dependencies
    manifest = dependencies.capture(conn, exp_id, node["id"])
    conn.execute("UPDATE experience SET dependency_manifest=?,review_bundle_hash=NULL WHERE id=?",
                 (json.dumps(manifest, sort_keys=True), exp_id))
    conn.execute("UPDATE experience_qa SET status='pending',bundle_hash=NULL WHERE exp_id=?", (exp_id,))
    path = _sync_file(conn, exp_id)
    conn.execute("UPDATE experience SET draft_path=? WHERE id=?", (str(path), exp_id))
    conn.commit()
    return {**{key: node[key] for key in ("usr", "path", "line_start", "line_end", "definition_hash")},
            "relation": relation}


def set_dependencies(conn: sqlite3.Connection, exp_id: str, *,
                     entity_names: list[str] | None = None,
                     macro_names: list[str] | None = None,
                     call_depth: int = 0) -> dict[str, Any]:
    """Replace inferred dependencies with a reviewable claim-specific manifest.

    Dependency curation changes the material under review, so every QA result and
    confirmation is invalidated.  Published cards cannot be edited in place; a
    new pending candidate must be created instead.
    """
    card = conn.execute("SELECT status FROM experience WHERE id=?", (exp_id,)).fetchone()
    if card is None:
        raise KeyError(f"知识卡不存在: {exp_id}")
    if card["status"] not in ("draft", "pending", "stale"):
        raise ValueError("已发布知识卡不能原地修改依赖；请创建新的待审核替代卡")
    anchor = conn.execute(
        "SELECT usr FROM experience_anchor WHERE exp_id=? AND active=1", (exp_id,)
    ).fetchone()
    if anchor is None:
        raise ValueError("请先绑定精确源码锚点")
    node = conn.execute(
        "SELECT id FROM node WHERE usr=? AND kind='function' AND is_definition=1",
        (anchor["usr"],),
    ).fetchone()
    if node is None:
        raise ValueError("当前源码中找不到主锚点")
    from .. import dependencies
    manifest = dependencies.capture(
        conn, exp_id, node["id"], entity_names=entity_names or [],
        macro_names=macro_names or [], call_depth=call_depth,
    )
    now = _now()
    conn.execute(
        "UPDATE experience SET status='pending',dependency_manifest=?,review_bundle_hash=NULL,"
        "stale_reason=NULL,updated_at=? WHERE id=?",
        (json.dumps(manifest, sort_keys=True), now, exp_id),
    )
    conn.execute(
        "UPDATE experience_qa SET status='pending',bundle_hash=NULL,reviewed_at=NULL WHERE exp_id=?",
        (exp_id,),
    )
    path = _sync_file(conn, exp_id)
    conn.execute("UPDATE experience SET draft_path=? WHERE id=?", (str(path), exp_id))
    conn.commit()
    return {
        "id": exp_id,
        "status": "pending",
        "dependencies": manifest,
        "entity_names": sorted(set(entity_names or [])),
        "macro_names": sorted(set(macro_names or [])),
        "call_depth": call_depth,
    }


def _anchors_current(conn: sqlite3.Connection, exp_id: str) -> tuple[bool, str]:
    repository = dbm.get_meta(conn, "repository_id", dbm.get_meta(conn, "repo", ""))
    anchors = conn.execute(
        "SELECT * FROM experience_anchor WHERE exp_id=? AND active=1", (exp_id,)
    ).fetchall()
    if not anchors:
        return False, "缺少精确源码锚点"
    for anchor in anchors:
        if anchor["repository"] != repository:
            return False, "锚点仓库与当前知识库不一致"
        node = conn.execute(
            "SELECT definition_hash FROM node WHERE usr=? AND kind='function' AND is_definition=1",
            (anchor["usr"],),
        ).fetchone()
        if node is None:
            return False, f"USR 已消失: {anchor['usr']}"
        if node["definition_hash"] != anchor["definition_hash"]:
            return False, f"函数定义已变化: {anchor['usr']}"
    from ..dependencies import check
    return check(conn, exp_id)


def review_bundle(conn, exp_id: str) -> str:
    """Bind QA to content, exact anchors, dependencies, experiments and QA definitions."""
    card, anchors, qas, _ = _snapshot(conn, exp_id)
    fields = ("title", "symptom", "hypotheses", "dead_ends", "root_cause", "fix_steps",
              "verification", "source_type", "source_ref", "session_id", "content_hash",
              "evidence_tags", "evidence_turns", "supersedes_id", "dependency_manifest", "experiment_manifest")
    fields = fields + ("artifact_scope",)
    anchor_fields = ("repository", "revision", "usr", "path", "line_start", "line_end", "definition_hash", "relation")
    project = Path(__file__).resolve().parents[3]
    experiment_state = {}
    for item in json.loads(card.get("experiment_manifest") or "[]"):
        path = (project / item["path"]).resolve()
        if (project / "examples/reproductions") not in path.parents:
            raise ValueError("experiment path outside public reproduction assets")
        experiment_state[item["path"]] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
    return _hash({"card": {key: card.get(key) for key in fields}, "experiments": experiment_state,
                  "anchors": [{key: a.get(key) for key in anchor_fields} for a in anchors if a["active"]],
                  "qas": [{key: q.get(key) for key in ("question", "expected_points", "evidence_tags")} for q in qas]})


def _experiments_current(conn, exp_id: str) -> tuple[bool, str]:
    """Validate the public reproduction assets bound into a formal review bundle."""
    row = conn.execute(
        "SELECT experiment_manifest FROM experience WHERE id=?", (exp_id,)
    ).fetchone()
    if row is None:
        return False, "card_missing"
    try:
        items = json.loads(row["experiment_manifest"] or "[]")
    except json.JSONDecodeError:
        return False, "复现实验清单不是有效 JSON"
    if not items:
        return False, "缺少公开复现实验材料"
    project = Path(__file__).resolve().parents[3]
    reproduction_root = (project / "examples/reproductions").resolve()
    for item in items:
        if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
            return False, "复现实验条目缺少 path 或 sha256"
        path = (project / item["path"]).resolve()
        if reproduction_root not in path.parents or not path.is_file():
            return False, f"复现实验材料不可用: {item.get('path')}"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != item["sha256"]:
            return False, f"复现实验材料已变化: {item['path']}"
    return True, ""


def confirm_review(conn, exp_id, bundle_hash, reviewer, reviewer_type="human"):
    if reviewer_type != "human":
        raise ValueError("正式内容确认只能由 human 类型审核者完成")
    if bundle_hash != review_bundle(conn, exp_id):
        raise ValueError("review_conflict: card content changed; inspect the new diff")
    conn.execute("""INSERT OR REPLACE INTO review_confirmation
                    (exp_id,bundle_hash,reviewer,reviewer_type,confirmed_at)
                    VALUES(?,?,?,?,?)""",
                 (exp_id, bundle_hash, reviewer, reviewer_type, _now()))
    conn.commit()
    return {"bundle_hash": bundle_hash, "reviewer": reviewer}


def attach_experiments(conn, exp_id, paths):
    project = Path(__file__).resolve().parents[3]
    items = []
    card = get(conn, exp_id)
    if card["status"] not in ("pending", "draft", "stale"):
        raise ValueError("published cards require a new review candidate")
    for value in paths:
        path = (project / value).resolve()
        if (project / "examples/reproductions") not in path.parents or not path.is_file():
            raise ValueError("only public reproduction assets can be attached")
        items.append({"path": str(path.relative_to(project)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    conn.execute("UPDATE experience SET experiment_manifest=?,review_bundle_hash=NULL WHERE id=?", (json.dumps(items, sort_keys=True), exp_id))
    conn.execute("UPDATE experience_qa SET status='pending',bundle_hash=NULL WHERE exp_id=?", (exp_id,))
    _sync_file(conn, exp_id)
    conn.commit()
    return items


def approved_current(conn, exp_id):
    valid, reason = _anchors_current(conn, exp_id)
    if not valid:
        return valid, reason
    row = conn.execute("SELECT review_bundle_hash,artifact_scope FROM experience WHERE id=?", (exp_id,)).fetchone()
    if not row:
        return False, "card_missing"
    active_scope = dbm.get_meta(conn, "evaluation_scope", "formal")
    if (row["artifact_scope"] or "formal") != active_scope:
        return False, "artifact_scope_not_visible"
    if active_scope == "formal":
        experiments_valid, reason = _experiments_current(conn, exp_id)
        if not experiments_valid:
            return False, reason
    bundle = review_bundle(conn, exp_id)
    if row["review_bundle_hash"] != bundle:
        return False, "review_bundle_changed"
    actor_type = "human" if active_scope == "formal" else None
    qa = conn.execute(
        """SELECT 1 FROM experience_qa WHERE exp_id=? AND status='passed' AND bundle_hash=?
           AND evaluation_scope=? AND (? IS NULL OR reviewer_type=?)""",
        (exp_id, bundle, active_scope, actor_type, actor_type),
    ).fetchone()
    if not qa:
        return False, "qa_no_longer_valid"
    review = conn.execute(
        """SELECT reviewer_type,evaluation_scope FROM experience_review
           WHERE exp_id=? AND action='approve' ORDER BY id DESC LIMIT 1""", (exp_id,)
    ).fetchone()
    if not review or review["evaluation_scope"] != active_scope:
        return False, "approval_scope_invalid"
    if active_scope == "formal" and review["reviewer_type"] != "human":
        return False, "formal_approval_requires_human"
    if active_scope == "formal" and not conn.execute(
        """SELECT 1 FROM review_confirmation
             WHERE exp_id=? AND bundle_hash=? AND reviewer_type='human' LIMIT 1""",
        (exp_id, bundle),
    ).fetchone():
        return False, "formal_approval_requires_current_confirmation"
    return True, ""


def qa_add(conn: sqlite3.Connection, exp_id: str, question: str,
           expected_points: list[str], evidence_tags: list[str]) -> dict[str, Any]:
    card = conn.execute("SELECT evidence_tags,status FROM experience WHERE id=?", (exp_id,)).fetchone()
    if card is None:
        raise KeyError(f"知识卡不存在: {exp_id}")
    if card["status"] not in ("draft", "pending", "stale"):
        raise ValueError("只有待审核或 stale 卡片可以新增 QA")
    if not question.strip() or not expected_points:
        raise ValueError("QA 必须包含问题与期望回答要点")
    allowed = set(json.loads(card["evidence_tags"] or "[]"))
    if not evidence_tags or any(not re_tag.startswith(("A", "B")) or re_tag not in allowed
                                for re_tag in evidence_tags):
        raise ValueError("QA 证据标签必须来自卡片已有的 A/B 引用")
    now = _now()
    cursor = conn.execute(
        """INSERT INTO experience_qa
           (exp_id,question,expected_points,evidence_tags,status,created_at)
           VALUES(?,?,?,?,?,?)""",
        (exp_id, question.strip(), json.dumps(expected_points, ensure_ascii=False),
         json.dumps(evidence_tags, ensure_ascii=False), "pending", now),
    )
    path = _sync_file(conn, exp_id)
    conn.execute("UPDATE experience SET draft_path=?,updated_at=? WHERE id=?",
                 (str(path), now, exp_id))
    conn.commit()
    return {"id": cursor.lastrowid, "exp_id": exp_id, "status": "pending"}


def _validate_review_actor(reviewer_type: str, evaluation_scope: str,
                           model: str | None, agent_id: str | None,
                           prompt_hash: str | None, input_hash: str | None) -> None:
    if reviewer_type not in ("human", "agent", "fixture"):
        raise ValueError("reviewer_type 必须是 human / agent / fixture")
    if evaluation_scope not in ("formal", "isolated_experiment"):
        raise ValueError("evaluation_scope 必须是 formal 或 isolated_experiment")
    if reviewer_type == "agent" and not all((model, agent_id, prompt_hash, input_hash)):
        raise ValueError("agent 审核必须记录 model、agent_id、prompt_hash 和 input_hash")
    if reviewer_type == "fixture" and evaluation_scope != "isolated_experiment":
        raise ValueError("fixture 审核只能用于 isolated_experiment")


def qa_review(conn: sqlite3.Connection, qa_id: int, passed: bool, note: str,
              reviewer: str = "local-reviewer", reviewer_type: str = "human",
              evaluation_scope: str = "formal", model: str | None = None,
              agent_id: str | None = None, prompt_hash: str | None = None,
              input_hash: str | None = None) -> dict[str, Any]:
    if not note.strip():
        raise ValueError("QA 审核必须填写意见")
    qa = conn.execute("SELECT * FROM experience_qa WHERE id=?", (qa_id,)).fetchone()
    if qa is None:
        raise KeyError(f"QA 不存在: {qa_id}")
    _validate_review_actor(reviewer_type, evaluation_scope, model, agent_id,
                           prompt_hash, input_hash)
    status = "passed" if passed else "failed"
    now = _now()
    bundle = review_bundle(conn, qa["exp_id"])
    conn.execute(
        """UPDATE experience_qa SET status=?,reviewer=?,note=?,reviewed_at=?,bundle_hash=?,
           reviewer_type=?,evaluation_scope=?,model=?,agent_id=?,prompt_hash=?,input_hash=?
           WHERE id=?""",
        (status, reviewer, note.strip(), now, bundle, reviewer_type, evaluation_scope,
         model, agent_id, prompt_hash, input_hash, qa_id),
    )
    path = _sync_file(conn, qa["exp_id"])
    conn.execute("UPDATE experience SET draft_path=?,updated_at=? WHERE id=?",
                 (str(path), now, qa["exp_id"]))
    conn.commit()
    return {"id": qa_id, "exp_id": qa["exp_id"], "status": status}


def review(conn: sqlite3.Connection, exp_id: str, action: str, note: str = "",
           reviewer: str = "local-reviewer", expected_bundle_hash: str | None = None,
           reviewer_type: str = "human", evaluation_scope: str = "formal",
           model: str | None = None, agent_id: str | None = None,
           prompt_hash: str | None = None, input_hash: str | None = None) -> dict[str, Any]:
    if action not in ("approve", "reject"):
        raise ValueError("action 只能是 approve / reject")
    if not note.strip():
        raise ValueError("审核必须填写 reviewer note")
    row = conn.execute("SELECT * FROM experience WHERE id=?", (exp_id,)).fetchone()
    if row is None:
        raise KeyError(f"知识卡不存在: {exp_id}")
    if row["status"] not in ("draft", "pending", "stale"):
        raise ValueError(f"当前状态不能再次审核: {row['status']}")
    _validate_review_actor(reviewer_type, evaluation_scope, model, agent_id,
                           prompt_hash, input_hash)
    artifact_scope = row["artifact_scope"] or "formal"
    if action == "approve" and artifact_scope == "formal" and (
            reviewer_type != "human" or evaluation_scope != "formal"):
        raise ValueError("正式知识卡只能由 human 审核者在 formal 范围批准")
    if artifact_scope == "isolated_experiment" and evaluation_scope != "isolated_experiment":
        raise ValueError("实验知识卡只能在 isolated_experiment 范围审核")
    superseded_id = row["supersedes_id"] if action == "approve" else None
    if superseded_id:
        prior = conn.execute(
            "SELECT status FROM experience WHERE id=?", (superseded_id,)
        ).fetchone()
        if prior is None or prior["status"] not in ("approved", "stale"):
            raise ValueError("待替代知识卡已不存在或状态不再允许替代")
    if action == "approve":
        bundle = review_bundle(conn, exp_id)
        if expected_bundle_hash and expected_bundle_hash != bundle:
            raise ValueError("review_conflict: card changed since review preview")
        if not all((row[key] or "").strip() for key in ("root_cause", "fix_steps", "verification")):
            raise ValueError("不能批准：root_cause / fix_steps / verification 不完整")
        valid, reason = _anchors_current(conn, exp_id)
        if not valid:
            raise ValueError(f"不能批准：{reason}")
        if artifact_scope == "formal":
            experiments_valid, experiment_reason = _experiments_current(conn, exp_id)
            if not experiments_valid:
                raise ValueError(f"不能批准：{experiment_reason}")
        qa_scope = "formal" if artifact_scope == "formal" else "isolated_experiment"
        qa_type = "human" if artifact_scope == "formal" else reviewer_type
        if not conn.execute(
            """SELECT 1 FROM experience_qa WHERE exp_id=? AND status='passed'
               AND bundle_hash=? AND reviewer_type=? AND evaluation_scope=? LIMIT 1""",
            (exp_id, bundle, qa_type, qa_scope),
        ).fetchone():
            raise ValueError("不能批准：至少需要一条已通过的 QA")
        if artifact_scope == "formal" and not conn.execute(
            """SELECT 1 FROM review_confirmation
                 WHERE exp_id=? AND bundle_hash=? AND reviewer=?
                   AND reviewer_type='human' LIMIT 1""",
            (exp_id, bundle, reviewer),
        ).fetchone():
            raise ValueError("不能批准：当前 review_bundle 尚未由该 human 审核者明确确认")
        status = "approved"
    else:
        status = "rejected"
    now = _now()
    operation_id = "pub-" + __import__("uuid").uuid4().hex
    planned_files: list[tuple[Path, bytes | None, str]] = []

    def stage_file(card_id: str, planned_status: str, planned_note: str,
                   review_action: str) -> tuple[Path, str]:
        card, anchors, qas, reviews = _snapshot(
            conn, card_id, status=planned_status, updated_at=now,
            reviewer_note=planned_note if card_id == exp_id else None,
            reviewed_at=now if card_id == exp_id else None,
            stale_reason=(None if card_id == exp_id else f"由已审核知识卡 {exp_id} 替代"),
            publication_op_id=operation_id,
            review_bundle_hash=review_bundle(conn, card_id),
        )
        reviews.append({"action": review_action, "reviewer": reviewer,
                        "reviewer_type": reviewer_type, "evaluation_scope": evaluation_scope,
                        "model": model, "agent_id": agent_id, "prompt_hash": prompt_hash,
                        "input_hash": input_hash,
                        "note": note.strip() if card_id == exp_id else f"由 {exp_id} 替代",
                        "created_at": now})
        target = _card_path(conn, card_id, planned_status)
        backup = target.read_bytes() if target.exists() else None
        rendered = _markdown(card, anchors, qas, reviews)
        planned_files.append((target, backup, rendered))
        return target, rendered

    primary_path, rendered = stage_file(
        exp_id, status, f"{reviewer}: {note.strip()}", action,
    )
    prior_path = prior_rendered = None
    if superseded_id:
        prior_path, prior_rendered = stage_file(
            superseded_id, "superseded", f"由已审核知识卡 {exp_id} 替代", "supersede",
        )
    try:
        from .. import publication
        publication.prepare(conn, [(target, rendered) for target, _, rendered in planned_files], operation_id)
        conn.execute(
            """UPDATE experience SET status=?,reviewer_note=?,reviewed_at=?,updated_at=?,
               card_markdown=?,canonical_path=?,stale_reason=NULL WHERE id=?""",
            (status, f"{reviewer}: {note.strip()}", now, now, rendered,
             str(primary_path) if status == "approved" else None, exp_id),
        )
        conn.execute(
            """INSERT INTO experience_review
               (exp_id,action,reviewer,reviewer_type,evaluation_scope,model,agent_id,
                prompt_hash,input_hash,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (exp_id, action, reviewer, reviewer_type, evaluation_scope, model, agent_id,
             prompt_hash, input_hash, note.strip(), now),
        )
        if superseded_id:
            conn.execute(
                """UPDATE experience SET status='superseded',stale_reason=?,updated_at=?,
                   card_markdown=?,canonical_path=? WHERE id=?""",
                (f"由已审核知识卡 {exp_id} 替代", now, prior_rendered,
                 str(prior_path), superseded_id),
            )
            conn.execute(
                "INSERT INTO experience_review(exp_id,action,reviewer,note,created_at) VALUES(?,?,?,?,?)",
                (superseded_id, "supersede", reviewer, f"由 {exp_id} 替代", now),
            )
        for card_id in (exp_id, superseded_id):
            if card_id:
                conn.execute("UPDATE experience SET publication_op_id=?,review_bundle_hash=? WHERE id=?",
                             (operation_id, review_bundle(conn, card_id), card_id))
        publication.commit(conn, operation_id)
    except Exception:
        conn.rollback()
        publication.recover(conn)
        raise
    from ..indexer.build import refresh_visibility
    refresh_visibility(conn)
    result = {"id": exp_id, "status": status}
    if superseded_id:
        result["superseded_id"] = superseded_id
    return result


def stale_check(conn: sqlite3.Connection) -> dict[str, Any]:
    stale: list[dict[str, str]] = []
    rows = conn.execute("SELECT id FROM experience WHERE status='approved'").fetchall()
    for row in rows:
        # A force parse rebuilds node identities from source and clears this
        # derived link projection. Recreate it only after the exact anchor has
        # passed repository/USR/definition-hash validation.
        conn.execute("DELETE FROM experience_link WHERE exp_id=?", (row["id"],))
        valid, reason = approved_current(conn, row["id"])
        if not valid:
            now = _now()
            card, anchors, qas, reviews = _snapshot(
                conn, row["id"], status="stale", stale_reason=reason, updated_at=now,
            )
            reviews.append({"action": "stale", "reviewer": "system",
                            "note": reason, "created_at": now})
            target = _card_path(conn, row["id"], "stale")
            operation_id = "pub-" + __import__("uuid").uuid4().hex
            card["publication_op_id"] = operation_id
            rendered = _markdown(card, anchors, qas, reviews)
            from .. import publication
            op_id = publication.prepare(conn, [(target, rendered)], operation_id)
            conn.execute(
                """UPDATE experience SET status='stale',stale_reason=?,updated_at=?,
                   card_markdown=?,canonical_path=? WHERE id=?""",
                (reason, now, rendered, str(target), row["id"]),
            )
            conn.execute(
                "INSERT INTO experience_review(exp_id,action,reviewer,note,created_at) VALUES(?,?,?,?,?)",
                (row["id"], "stale", "system", reason, now),
            )
            stale.append({"id": row["id"], "reason": reason})
            conn.execute("UPDATE experience SET publication_op_id=? WHERE id=?", (op_id, row["id"]))
            publication.commit(conn, op_id)
        else:
            anchor = conn.execute(
                "SELECT usr,relation FROM experience_anchor WHERE exp_id=? AND active=1",
                (row["id"],),
            ).fetchone()
            node = conn.execute(
                "SELECT id FROM node WHERE usr=? AND kind='function' AND is_definition=1",
                (anchor["usr"],),
            ).fetchone()
            conn.execute(
                "INSERT OR REPLACE INTO experience_link(exp_id,node_id,relation,confidence) "
                "VALUES(?,?,?,?)",
                (row["id"], node["id"], anchor["relation"], "certain"),
            )
    if stale:
        from ..indexer.build import refresh_visibility
        refresh_visibility(conn)
    else:
        conn.commit()
    return {"stale": stale, "checked": len(rows)}


def export_markdown(conn: sqlite3.Connection, exp_id: str, out_dir: str | Path) -> Path:
    row = conn.execute("SELECT * FROM experience WHERE id=?", (exp_id,)).fetchone()
    if row is None:
        raise KeyError(f"知识卡不存在: {exp_id}")
    if row["status"] != "approved":
        raise ValueError("只有 approved 知识卡可以导出公开 Markdown")
    card, anchors, qas, reviews = _snapshot(conn, exp_id)
    rendered = _markdown(card, anchors, qas, reviews)
    root = Path(out_dir); root.mkdir(parents=True, exist_ok=True)
    path = root / f"{exp_id}.md"
    path.write_text(rendered, encoding="utf-8")
    conn.execute("UPDATE experience SET card_markdown=?,updated_at=? WHERE id=?", (rendered, _now(), exp_id))
    conn.commit()
    return path


def _parse_markdown(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) != 3 or parts[0].strip():
        raise ValueError(f"知识卡缺少 JSON frontmatter: {path}")
    payload = json.loads(parts[1])
    checksum = payload.pop("checksum", None)
    if not checksum or checksum != _hash(payload):
        raise ValueError(f"知识卡 checksum 不一致: {path}")
    payload["checksum"] = checksum
    if payload.get("schema_version") != 2 or not isinstance(payload.get("record"), dict):
        raise ValueError(f"知识卡 schema_version 不支持: {path}")
    return payload, text


def consistency(conn: sqlite3.Connection) -> dict[str, Any]:
    errors: list[str] = []
    checked = 0
    for row in conn.execute(
        "SELECT id,status,draft_path,canonical_path FROM experience ORDER BY id"
    ):
        raw_path = (row["canonical_path"] if row["status"] in ("approved", "stale", "superseded")
                    else row["draft_path"])
        if not raw_path:
            errors.append(f"{row['id']}:缺少 Markdown 路径")
            continue
        path = Path(raw_path)
        if not path.exists():
            errors.append(f"{row['id']}:Markdown 不存在")
            continue
        try:
            payload, _ = _parse_markdown(path)
            if payload["record"].get("id") != row["id"] or payload["record"].get("status") != row["status"]:
                errors.append(f"{row['id']}:Markdown 与数据库状态不一致")
            checked += 1
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
    return {"ok": not errors, "checked": checked, "errors": errors}


def rebuild(conn: sqlite3.Connection, *, rebuild_index: bool = True) -> dict[str, Any]:
    """Delete derived card rows and reconstruct them from Markdown source files."""
    from .. import publication
    publication.recover(conn)
    root = _knowledge_root(conn)
    documents: dict[str, tuple[dict[str, Any], str, Path]] = {}
    for folder in (root / "drafts", root / "cards"):
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.md")):
            payload, text = _parse_markdown(path)
            record = payload["record"]
            op = record.get("publication_op_id")
            if record.get("status") in ("approved", "stale", "superseded"):
                committed = op and conn.execute("SELECT files_json FROM publication_journal WHERE op_id=? AND state='committed'", (op,)).fetchone()
                if committed and not any(entry["after"] == text and Path(entry["path"]).resolve() == path.resolve()
                                         for entry in json.loads(committed[0])):
                    raise ValueError("published Markdown differs from committed journal; explicit re-review required")
                if not committed:
                    # Legacy files are import candidates, never an approval bypass.
                    record["status"] = "pending"
            card_id = record.get("id")
            if not card_id:
                raise ValueError(f"知识卡缺少 id: {path}")
            if card_id in documents and folder.name == "cards" and documents[card_id][2].parent.name == "cards":
                raise ValueError("duplicate canonical card ID; refusing historical replay")
            # Canonical cards are scanned after drafts and intentionally win.
            documents[card_id] = (payload, text, path)
    if not documents:
        raise ValueError(f"没有可重建的知识卡 Markdown: {root}")
    try:
        for table in ("experience_link", "experience_anchor", "experience_qa",
                      "experience_review", "experience"):
            conn.execute(f"DELETE FROM {table}")
        experience_columns = _columns_for(conn, "experience")
        for card_id, (payload, text, path) in documents.items():
            record = dict(payload["record"])
            validation_record = dict(record)
            for list_field in ("hypotheses", "dead_ends", "evidence_tags", "evidence_turns"):
                if isinstance(validation_record.get(list_field), str):
                    validation_record[list_field] = json.loads(validation_record[list_field] or "[]")
            ok, error = validate(validation_record)
            if not ok:
                raise ValueError(f"{card_id}: {error}")
            record["card_markdown"] = text
            if record.get("status") in ("approved", "stale", "superseded"):
                record["canonical_path"] = str(path)
            else:
                record["draft_path"] = str(path)
            fields = [key for key in record if key in experience_columns]
            conn.execute(
                f"INSERT INTO experience({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                [record[key] for key in fields],
            )
            for anchor in payload.get("anchors", []):
                conn.execute(
                    """INSERT INTO experience_anchor
                       (exp_id,repository,revision,usr,path,line_start,line_end,definition_hash,
                        relation,active,bound_at,bound_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (card_id, anchor["repository"], anchor["revision"], anchor["usr"],
                     anchor["path"], anchor["line_start"], anchor["line_end"],
                     anchor["definition_hash"], anchor.get("relation") or "explains",
                     int(anchor.get("active", 1)), anchor.get("bound_at") or _now(),
                     anchor.get("bound_by") or "markdown-rebuild"),
                )
                node = conn.execute(
                    "SELECT id FROM node WHERE usr=? AND kind='function' AND is_definition=1",
                    (anchor["usr"],),
                ).fetchone()
                if node and anchor.get("active", 1):
                    conn.execute(
                        "INSERT OR REPLACE INTO experience_link(exp_id,node_id,relation,confidence) VALUES(?,?,?,?)",
                        (card_id, node["id"], anchor.get("relation") or "explains", "certain"),
                    )
            for qa in payload.get("qas", []):
                conn.execute(
                    """INSERT INTO experience_qa
                       (exp_id,question,expected_points,evidence_tags,status,reviewer,note,created_at,reviewed_at,
                        reviewer_type,evaluation_scope,model,agent_id,prompt_hash,input_hash)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (card_id, qa["question"], qa["expected_points"], qa["evidence_tags"],
                     qa.get("status") or "pending", qa.get("reviewer"), qa.get("note"),
                     qa.get("created_at") or _now(), qa.get("reviewed_at"),
                     qa.get("reviewer_type") or "legacy_unknown",
                     qa.get("evaluation_scope") or "formal", qa.get("model"),
                     qa.get("agent_id"), qa.get("prompt_hash"), qa.get("input_hash")),
                )
                conn.execute("UPDATE experience_qa SET bundle_hash=? WHERE id=last_insert_rowid()", (qa.get("bundle_hash"),))
            conn.execute("DELETE FROM knowledge_dependency WHERE owner_type='card' AND owner_id=?", (card_id,))
            for dependency in json.loads(record.get("dependency_manifest") or "[]"):
                conn.execute("INSERT INTO knowledge_dependency VALUES('card',?,?,?,?)",
                             (card_id, dependency["dependency_id"], dependency["kind"], dependency["fingerprint"]))
            for review in payload.get("reviews", []):
                conn.execute(
                    """INSERT INTO experience_review
                       (exp_id,action,reviewer,reviewer_type,evaluation_scope,model,agent_id,
                        prompt_hash,input_hash,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (card_id, review["action"], review["reviewer"],
                     review.get("reviewer_type") or "legacy_unknown",
                     review.get("evaluation_scope") or "formal", review.get("model"),
                     review.get("agent_id"), review.get("prompt_hash"), review.get("input_hash"),
                     review["note"], review.get("created_at") or _now()),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if rebuild_index:
        from ..indexer import build as index_build
        index_build.build(conn, dbm.get_meta(conn, "repo", "."),
                          embedder_kind="null", out_dir=str(root.parent))
    report = consistency(conn)
    return {"cards": len(documents), "index_rebuilt": rebuild_index, **report}


def _columns_for(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def import_jsonl(conn: sqlite3.Connection, path: str, *, llm_client=None) -> dict[str, int]:
    """Legacy JSONL import. Raw messages remain pending and never enter retrieval directly."""
    stats = {"imported": 0, "skipped": 0, "failed": 0}
    with open(path, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if not (line := line.strip()):
                continue
            try:
                raw = json.loads(line)
                if "messages" in raw:
                    if llm_client is None:
                        stats["skipped"] += 1
                        continue
                    data = llm_client.extract_experience(raw["messages"], EXTRACT_SCHEMA)
                else:
                    data = raw
                add(conn, data, source_type=raw.get("source_type", "session"),
                    source_ref=raw.get("source_ref", f"{path}#L{lineno}"),
                    model=("openai-compatible" if "messages" in raw else ""))
                stats["imported"] += 1
            except Exception as exc:
                log.warning("第 %d 行导入失败: %s", lineno, exc)
                stats["failed"] += 1
    return stats


def _governed(function):
    """Serialize every legacy/API/CLI governance mutation with snapshot publication."""
    from functools import wraps
    @wraps(function)
    def wrapped(conn, *args, **kwargs):
        from .. import snapshots, publication
        if not conn.execute("PRAGMA database_list").fetchone()[2]:
            return function(conn, *args, **kwargs)  # isolated in-memory test projection
        with snapshots.writer_lock(conn):
            publication.recover(conn)
            before = dbm.get_meta(conn, "governance_revision", "0")
            result = function(conn, *args, **kwargs)
            if function.__name__ in (
                    "bind", "qa_add", "qa_review", "set_dependencies",
                    "link_supersedes", "rebuild", "attach_experiments"):
                dbm.set_meta(conn, "governance_revision", str(int(dbm.get_meta(conn, "governance_revision", "0")) + 1))
                conn.commit()
            active = dbm.get_meta(conn, "active_knowledge_set")
            if active and before != dbm.get_meta(conn, "governance_revision", "0"):
                snapshots.activate(conn, active, rollback=True)
            return result
    return wrapped


for _name in (
        "bind", "qa_add", "qa_review", "review", "stale_check",
        "set_dependencies", "link_supersedes", "rebuild", "confirm_review",
        "attach_experiments"):
    globals()[_name] = _governed(globals()[_name])
