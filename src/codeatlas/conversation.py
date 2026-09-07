"""Public, review-only conversation assets.

Conversation content is deliberately stored outside the retrieval index. It is an
audit input for a reviewer, never an automatically retrievable answer source.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import difflib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db as dbm

PUBLIC_SOURCE = "public_synthetic"
ALLOWED_ROLES = {"user", "assistant", "developer", "reviewer", "tool"}
KNOWLEDGE_ROLES = {
    "symptom", "hypothesis", "dead_end", "evidence", "root_cause", "fix", "verification",
}
REQUIRED_KNOWLEDGE_FIELDS = ("root_cause", "fix_steps", "verification")
PUBLIC_LICENSES = {"CC0-1.0", "MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0"}
_SENSITIVE = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk|pk)_[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:password|secret|token)\s*[:=]\s*\S+", re.I),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
)


class ConversationError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_message(message: Any, ordinal: int) -> dict[str, str | None]:
    if not isinstance(message, dict):
        raise ConversationError(f"messages[{ordinal}] 必须是对象")
    role, content = message.get("role"), message.get("content")
    if role not in ALLOWED_ROLES:
        raise ConversationError(f"messages[{ordinal}].role 不支持: {role!r}")
    if not isinstance(content, str) or not content.strip():
        raise ConversationError(f"messages[{ordinal}].content 不能为空")
    if len(content) > 6000:
        raise ConversationError(f"messages[{ordinal}] 超过 6000 字符上限")
    knowledge_role = message.get("knowledge_role")
    if knowledge_role is not None and knowledge_role not in KNOWLEDGE_ROLES:
        raise ConversationError(
            f"messages[{ordinal}].knowledge_role 不支持: {knowledge_role!r}"
        )
    for pattern in _SENSITIVE:
        if pattern.search(content):
            raise ConversationError(f"messages[{ordinal}] 疑似包含敏感信息，拒绝导入")
    return {"role": role, "knowledge_role": knowledge_role, "content": content.strip()}


def load_public_session(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConversationError(f"无法读取会话资产: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ConversationError("会话资产必须是 schema_version: 1 的 JSON 对象")
    for field in ("id", "source_ref", "license", "repository", "revision", "messages"):
        if not raw.get(field):
            raise ConversationError(f"会话资产缺少 {field}")
    if raw.get("source_type") != PUBLIC_SOURCE:
        raise ConversationError("公开作品集只接受 source_type=public_synthetic")
    if not isinstance(raw["messages"], list) or not raw["messages"]:
        raise ConversationError("messages 必须是非空数组")
    messages = [_validate_message(message, index) for index, message in enumerate(raw["messages"], 1)]
    return {
        "id": str(raw["id"]), "source_type": raw["source_type"],
        "source_ref": str(raw["source_ref"]), "license": str(raw["license"]),
        "repository": str(raw["repository"]), "revision": str(raw["revision"]),
        "messages": messages,
    }


def import_public_session(conn: sqlite3.Connection, path: str | Path) -> dict[str, Any]:
    payload = load_public_session(path)
    repository = dbm.get_meta(conn, "repository_id", dbm.get_meta(conn, "repo", ""))
    revision = dbm.get_meta(conn, "revision", "unversioned")
    if payload["repository"] != repository or payload["revision"] != revision:
        raise ConversationError("会话资产必须绑定当前知识库的公开仓库与固定 revision")
    content_hash = _sha(_canonical(payload))
    existing = conn.execute(
        "SELECT id FROM conversation_session WHERE content_hash=?", (content_hash,)
    ).fetchone()
    if existing:
        return {"id": existing["id"], "imported": False, "duplicate": True}
    session_id = payload["id"]
    if conn.execute("SELECT 1 FROM conversation_session WHERE id=?", (session_id,)).fetchone():
        raise ConversationError(f"会话 id 已存在但内容不同: {session_id}")
    conn.execute(
        """INSERT INTO conversation_session
           (id,source_type,source_ref,license,repository,revision,content_hash,status,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (session_id, payload["source_type"], payload["source_ref"], payload["license"],
         payload["repository"], payload["revision"], content_hash, "imported", _now()),
    )
    conn.executemany(
        """INSERT INTO conversation_message
           (session_id,ordinal,role,knowledge_role,content,content_hash)
           VALUES(?,?,?,?,?,?)""",
        [(session_id, ordinal, message["role"], message.get("knowledge_role"),
          message["content"], _sha(message["content"]))
         for ordinal, message in enumerate(payload["messages"], 1)],
    )
    conn.commit()
    return {"id": session_id, "imported": True, "duplicate": False}


def assess_session(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    """Apply a deterministic publication gate before any model extracts a card.

    This intentionally evaluates metadata and reproducibility signals, not whether a
    model's conclusion sounds plausible. A reviewer still owns the final decision.
    """
    session = get_session(conn, session_id)
    messages = session["messages"]
    text = "\n".join(message["content"] for message in messages)
    user_text = "\n".join(
        message["content"] for message in messages if message["role"] == "user"
    )
    reproducible_signal = bool(user_text and re.search(
        r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b|\b[A-Za-z][A-Za-z0-9_]+\s*\("
        r"|输入|复现|调用|定义|失败|返回", user_text
    ))
    explicitly_missing = bool(re.search(
        r"(?:没有|缺少|未提供|无法提供).{0,24}(?:函数|符号|输入|复现|版本|抓包|配置)",
        user_text,
    ))
    checks = {
        "public_source": session["source_type"] == PUBLIC_SOURCE,
        "public_license": session["license"] in PUBLIC_LICENSES,
        "fixed_revision": bool(session["revision"] and session["revision"] != "unversioned"
                               and len(session["revision"]) >= 7),
        "reproducible_input": reproducible_signal and not explicitly_missing,
        "verification_signal": bool(re.search(
            r"验证|复现|测试|核对|检查|查看|verify|reproduc|test|check", text, re.I
        )),
        "message_structure": len(messages) >= 2 and any(
            message["role"] in {"assistant", "developer", "reviewer", "tool"}
            for message in messages
        ),
        "sensitive_scan": not any(pattern.search(text) for pattern in _SENSITIVE),
    }
    labels = {
        "public_source": "来源不是公开合成语料",
        "public_license": "许可证不在公开白名单",
        "fixed_revision": "缺少固定代码 revision",
        "reproducible_input": "缺少可定位的输入、符号或失败现象",
        "verification_signal": "缺少验证、复现或检查步骤",
        "message_structure": "会话结构不足以形成可审核记录",
        "sensitive_scan": "会话疑似包含敏感信息",
    }
    reasons = [labels[name] for name, passed in checks.items() if not passed]
    eligible = not reasons
    assessed_at = _now()
    conn.execute(
        """INSERT INTO conversation_assessment(session_id,eligible,checks_json,reasons_json,assessed_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(session_id) DO UPDATE SET eligible=excluded.eligible,
             checks_json=excluded.checks_json,reasons_json=excluded.reasons_json,
             assessed_at=excluded.assessed_at""",
        (session_id, int(eligible), json.dumps(checks, ensure_ascii=False, sort_keys=True),
         json.dumps(reasons, ensure_ascii=False), assessed_at),
    )
    conn.commit()
    return {"session_id": session_id, "eligible": eligible, "checks": checks,
            "reasons": reasons, "assessed_at": assessed_at}


def get_session(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM conversation_session WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise KeyError(f"会话不存在: {session_id}")
    messages = conn.execute(
        """SELECT ordinal,role,knowledge_role,content,content_hash
             FROM conversation_message WHERE session_id=? ORDER BY ordinal""",
        (session_id,),
    ).fetchall()
    assessment = conn.execute(
        "SELECT * FROM conversation_assessment WHERE session_id=?", (session_id,)
    ).fetchone()
    assessment_data = None
    if assessment:
        assessment_data = {
            **dict(assessment), "eligible": bool(assessment["eligible"]),
            "checks": json.loads(assessment["checks_json"]),
            "reasons": json.loads(assessment["reasons_json"]),
        }
        assessment_data.pop("checks_json", None)
        assessment_data.pop("reasons_json", None)
    message_rows = [dict(message) for message in messages]
    role_values = {message["knowledge_role"] for message in message_rows
                   if message.get("knowledge_role")}
    candidates = conn.execute(
        "SELECT * FROM curation_candidate WHERE session_id=? ORDER BY ordinal",
        (session_id,),
    ).fetchall()
    return {
        **dict(row), "messages": message_rows, "assessment": assessment_data,
        "knowledge_missing_fields": [
            field for field, role in (
                ("root_cause", "root_cause"), ("fix_steps", "fix"),
                ("verification", "verification"),
            ) if role not in role_values
        ],
        "candidates": [_candidate_dict(candidate) for candidate in candidates],
    }


def list_sessions(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    """Return review metadata only; raw messages stay behind the detail endpoint."""
    rows = conn.execute(
        """SELECT s.id,s.source_type,s.source_ref,s.license,s.repository,s.revision,s.status,s.created_at,
                  COUNT(m.ordinal) AS message_count,a.eligible,a.assessed_at
             FROM conversation_session s
             LEFT JOIN conversation_message m ON m.session_id=s.id
             LEFT JOIN conversation_assessment a ON a.session_id=s.id
            GROUP BY s.id,a.eligible,a.assessed_at
            ORDER BY s.created_at DESC, s.id DESC
            LIMIT ?""", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def set_curation_goal(conn: sqlite3.Connection, session_id: str, goal: str) -> dict[str, Any]:
    if not goal.strip():
        raise ConversationError("沉淀目标不能为空")
    if not conn.execute("SELECT 1 FROM conversation_session WHERE id=?", (session_id,)).fetchone():
        raise KeyError(f"会话不存在: {session_id}")
    conn.execute("UPDATE conversation_session SET curation_goal=? WHERE id=?",
                 (goal.strip(), session_id))
    conn.commit()
    return {"session_id": session_id, "curation_goal": goal.strip()}


def propose_candidates(conn: sqlite3.Connection, session_id: str, *, llm_client=None) -> list[dict[str, Any]]:
    """Create review candidates, never a retrievable card."""
    session = get_session(conn, session_id)
    gate = assess_session(conn, session_id)
    if not gate["eligible"]:
        raise ConversationError("会话未通过质量门禁，不能策划知识")
    goal = session.get("curation_goal")
    if not goal:
        raise ConversationError("请先设置明确的沉淀目标")
    existing = conn.execute(
        "SELECT * FROM curation_candidate WHERE session_id=? ORDER BY ordinal", (session_id,)
    ).fetchall()
    if existing:
        return [_candidate_dict(row) for row in existing]
    messages = session["messages"]
    by_knowledge_role: dict[str, list[dict[str, Any]]] = {}
    for message in messages:
        knowledge_role = message.get("knowledge_role")
        if knowledge_role:
            by_knowledge_role.setdefault(knowledge_role, []).append(message)
    user_messages = [message for message in messages if message["role"] == "user"]
    symptom = "\n".join(
        message["content"] for message in by_knowledge_role.get("symptom", [])
    ) or (user_messages[0]["content"] if user_messages else goal)

    def joined(role: str) -> str:
        return "\n".join(message["content"] for message in by_knowledge_role.get(role, []))
    payloads = None
    if llm_client is not None and hasattr(llm_client, "curate_candidates"):
        raw = llm_client.curate_candidates(messages, goal, 3)
        if isinstance(raw, list) and raw:
            payloads = raw[:3]
    if payloads is None:
        root_cause, fix_steps, verification = (
            joined("root_cause"), joined("fix"), joined("verification")
        )
        missing_fields = [
            field for field, value in (
                ("root_cause", root_cause), ("fix_steps", fix_steps),
                ("verification", verification),
            ) if not value.strip()
        ]
        payloads = [{
            "title": goal[:40], "symptom": symptom[:1200],
            "hypotheses": [message["content"] for message in
                           by_knowledge_role.get("hypothesis", [])],
            "dead_ends": [message["content"] for message in
                          by_knowledge_role.get("dead_end", [])],
            "root_cause": root_cause[:1600],
            "fix_steps": fix_steps[:1600],
            "verification": verification[:1600],
            "related_symbols": [], "confidence": "medium", "evidence_tags": [],
            "evidence_turns": [message["ordinal"] for message in messages],
            "missing_fields": missing_fields,
            "review_status": "ai_assisted_draft",
        }]
    now = _now()
    out = []
    for ordinal, payload in enumerate(payloads, 1):
        if not isinstance(payload, dict) or not payload.get("title") or not payload.get("symptom"):
            continue
        missing_fields = [field for field in REQUIRED_KNOWLEDGE_FIELDS
                          if not str(payload.get(field) or "").strip()]
        payload["missing_fields"] = missing_fields
        payload.setdefault("review_status", "ai_assisted_draft")
        candidate_id = "cand-" + uuid.uuid4().hex[:12]
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        conn.execute(
            """INSERT INTO curation_candidate
               (id,session_id,ordinal,payload_json,original_json,status,model,prompt_ver,created_at,updated_at)
               VALUES(?,?,?,?,?,'proposed',?,?,?,?)""",
            (candidate_id, session_id, ordinal, encoded, encoded,
             getattr(llm_client, "model", "rule") if llm_client else "rule",
             "session-curation-v1", now, now),
        )
        out.append({"id": candidate_id, "session_id": session_id, "ordinal": ordinal,
                    "status": "proposed", "payload": payload})
    conn.commit()
    if not out:
        raise ConversationError("策划结果没有合法候选")
    return out


def _candidate_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {**dict(row), "payload": json.loads(row["payload_json"]),
            "original": json.loads(row["original_json"])}


def decide_candidate(conn: sqlite3.Connection, candidate_id: str, action: str, *,
                     payload: dict[str, Any] | None = None, reason: str = "") -> dict[str, Any]:
    if action not in ("accept", "edit", "correct", "skip"):
        raise ConversationError("action 只能是 accept / edit / correct / skip")
    row = conn.execute("SELECT * FROM curation_candidate WHERE id=?", (candidate_id,)).fetchone()
    if row is None:
        raise KeyError(f"候选不存在: {candidate_id}")
    if row["status"] != "proposed":
        raise ConversationError("候选已经处理，不能重复确认")
    current = json.loads(row["payload_json"])
    now = _now()
    if action == "skip":
        allowed = ("重复", "无价值", "证据不足", "错误")
        if not reason.strip() or not any(label in reason for label in allowed):
            raise ConversationError("skip 必须说明重复、无价值、证据不足或错误")
        conn.execute(
            "UPDATE curation_candidate SET status='skipped',skip_reason=?,updated_at=? WHERE id=?",
            (reason.strip(), now, candidate_id),
        )
        conn.commit()
        return {"id": candidate_id, "status": "skipped", "reason": reason.strip()}
    if action in ("edit", "correct"):
        if not isinstance(payload, dict):
            raise ConversationError(f"{action} 必须提交修改后的候选")
        current = payload
    missing_fields = [field for field in REQUIRED_KNOWLEDGE_FIELDS
                      if not str(current.get(field) or "").strip()]
    if missing_fields:
        raise ConversationError(
            "候选缺少必填字段，不能生成 pending 卡: " + ", ".join(missing_fields)
        )
    current["missing_fields"] = []
    current.setdefault("review_status", "ai_assisted_draft")
    diff = None
    if action == "correct":
        before = json.dumps(json.loads(row["original_json"]), ensure_ascii=False,
                            indent=2, sort_keys=True).splitlines()
        after = json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
        diff = "\n".join(difflib.unified_diff(before, after, fromfile="before", tofile="after"))
        if not diff:
            raise ConversationError("correct 必须产生实际修改")
    # Explicit user confirmation is the only transition that creates a pending card.
    from .experience import store
    session = get_session(conn, row["session_id"])
    card_id = store.add(
        conn, current, source_type=session["source_type"], source_ref=session["source_ref"],
        session_id=session["id"], model=row["model"] or "rule", status="pending",
    )
    status = {"accept": "accepted", "edit": "edited", "correct": "corrected"}[action]
    conn.execute(
        """UPDATE curation_candidate SET payload_json=?,status=?,correction_diff=?,card_id=?,updated_at=?
           WHERE id=?""",
        (json.dumps(current, ensure_ascii=False, sort_keys=True), status, diff, card_id, now,
         candidate_id),
    )
    conn.commit()
    return {"id": candidate_id, "status": status, "card_id": card_id,
            "correction_diff": diff}
