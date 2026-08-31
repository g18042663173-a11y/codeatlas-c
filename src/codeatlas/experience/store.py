"""专家经验库 —— 「非单纯 RAG」的落点。

与普通 RAG 的唯一实质区别：**人工审核是硬闸门**。
LLM 抽出的条目一律 status='pending'，审核前在检索中完全不可见
（chunk.visible=0，FTS 索引里根本没有这一行）。

无审核的经验库会把错误经验放大传播，比没有更糟。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger(__name__)

PROMPT_VERSION = "exp-extract-v1"

# 交给 LLM 的抽取 schema。dead_ends（无效尝试）是最有价值的一项，务必抽取 —
# 它记录的是"别人已经试过且不work的路"，这是文档里永远不会写的东西。
EXTRACT_SCHEMA = {
    "title": "≤20字概括",
    "symptom": "问题现象，客观描述",
    "hypotheses": ["排查时提出过的假设"],
    "dead_ends": ["试过但无效的方案"],
    "root_cause": "根因；无法确定则填 null",
    "fix_steps": "修复步骤",
    "verification": "如何验证修复生效",
    "related_symbols": ["涉及的函数/结构体名"],
    "confidence": "high | medium | low",
}

REQUIRED_FIELDS = ("title", "symptom")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate(data: dict) -> tuple[bool, str]:
    """LLM 输出的 schema 校验。不合格就重试，不要凑合入库。"""
    for f in REQUIRED_FIELDS:
        if not data.get(f):
            return False, f"缺少必填字段: {f}"
    for f in ("hypotheses", "dead_ends", "related_symbols"):
        if f in data and not isinstance(data[f], list):
            return False, f"{f} 必须是数组"
    if data.get("confidence") not in (None, "high", "medium", "low"):
        return False, "confidence 取值非法"
    return True, ""


def add(conn: sqlite3.Connection, data: dict, *, source_type: str = "manual",
        source_ref: str = "", model: str = "", status: str = "pending") -> str:
    """写入一条经验。★ 默认 pending，调用方不能绕过。"""
    ok, err = validate(data)
    if not ok:
        raise ValueError(f"经验条目校验失败: {err}")
    if status not in ("draft", "pending"):
        raise ValueError("新建条目只能是 draft 或 pending，不能直接 approved")

    eid = "e-" + hashlib.sha1(
        f"{data['title']}{source_ref}{data['symptom']}".encode()).hexdigest()[:12]

    conn.execute(
        """INSERT OR REPLACE INTO experience
           (id,title,symptom,hypotheses,dead_ends,root_cause,fix_steps,verification,
            source_type,source_ref,status,model,prompt_ver,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (eid, data["title"], data["symptom"],
         json.dumps(data.get("hypotheses", []), ensure_ascii=False),
         json.dumps(data.get("dead_ends", []), ensure_ascii=False),
         data.get("root_cause"), data.get("fix_steps"), data.get("verification"),
         source_type, source_ref, status, model, PROMPT_VERSION, _now()))

    # 关联到代码符号（用于反哺 summary_head.pitfalls）
    for sym in data.get("related_symbols", [])[:10]:
        for n in conn.execute(
                "SELECT id FROM node WHERE name=? AND kind='function' LIMIT 3", (sym,)):
            conn.execute(
                "INSERT OR IGNORE INTO experience_link(exp_id,node_id,relation,confidence)"
                " VALUES(?,?,?,?)", (eid, n["id"], "about", "candidate"))
    conn.commit()
    return eid


def pending(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM experience WHERE status='pending' ORDER BY created_at LIMIT ?",
        (limit,)).fetchall()


def review(conn: sqlite3.Connection, exp_id: str, action: str, note: str = "") -> dict:
    """审核。approve 之后条目才升级为 B 级证据进入检索。"""
    if action not in ("approve", "reject"):
        raise ValueError("action 只能是 approve / reject")
    status = "approved" if action == "approve" else "rejected"
    cur = conn.execute(
        "UPDATE experience SET status=?, reviewer_note=?, reviewed_at=? WHERE id=?",
        (status, note, _now(), exp_id))
    if cur.rowcount == 0:
        raise KeyError(f"经验条目不存在: {exp_id}")
    conn.commit()

    # 同步检索可见性 —— 这一步是闸门真正生效的地方
    from ..indexer.build import refresh_visibility
    refresh_visibility(conn)
    return {"id": exp_id, "status": status}


def import_jsonl(conn: sqlite3.Connection, path: str, *, llm_client=None) -> dict:
    """从会话/工单 JSONL 导入。

    每行两种格式：
      A. 已结构化 {"title":..., "symptom":...}      → 直接校验入库
      B. 原始会话 {"messages":[...]}                → 需要 llm_client 抽取
    """
    stats = {"imported": 0, "skipped": 0, "failed": 0}
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
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
                    source_ref=raw.get("source_ref", f"{path}#L{lineno}"))
                stats["imported"] += 1
            except Exception as e:
                log.warning("第 %d 行导入失败: %s", lineno, e)
                stats["failed"] += 1
    return stats
