"""检索摘要头 —— 本项目的核心优化。

问题：结构化调用关系导出成 JSON 极其冗长。做 2 跳图扩展时，
      把每个函数体都塞进上下文，8k 窗口瞬间爆掉。

方案：每个函数额外生成一个 **定长 ≤200 token** 的摘要头：
      一句话职责 + 被调(≤12) + 调用者(≤8) + 关键头文件 + 关键类型/宏 + 踩坑点。
      图多跳扩展时只取摘要头，不取函数体 → 扩得更远 + 塞得进去 同时成立。

成本策略：规则版对全量函数生成（不花钱、秒级）；
          --llm 只对 fan_in >= 3 的高价值函数（通常占 10~15%）改写 one_liner。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

# ★ 生成规则版本。改了 one_liner / 裁剪逻辑就要 +1，否则 src_hash 不变、
#   缓存不失效，改进后的规则永远不会生效（踩过一次）。
RULE_VERSION = "r2"

MAX_TOKENS = 200          # ★ 硬上限，测试里有断言
MAX_CALLEES = 12
MAX_CALLERS = 8
MAX_HEADERS = 8


def est_tokens(text: str) -> int:
    """粗略 token 估算：ASCII 约 4 字符/token，CJK 约 1.5 字符/token。"""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return int(cjk / 1.5 + (len(text) - cjk) / 4) + 1


def _doc_first_line(repo: Path, path: str | None, line_start: int | None) -> str:
    """从函数上方的注释里抠一句话职责（规则版 one_liner）。"""
    if not path or not line_start:
        return ""
    f = repo / path
    if not f.exists():
        return ""
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    # ★ 只接受真正的注释行。之前不加判断地往上抓 6 行，
    #   结果把上一个函数的代码尾巴（"return node; }"）当成了职责描述。
    out: list[str] = []
    for i in range(line_start - 2, max(-1, line_start - 10), -1):
        if i < 0 or i >= len(lines):
            break
        raw = lines[i].strip()
        if not raw:
            if out:
                break          # 注释块与函数之间有空行，视为已结束
            continue
        is_comment = (raw.startswith("//") or raw.startswith("/*")
                      or raw.startswith("*") or raw.endswith("*/"))
        if not is_comment:
            break              # 碰到代码就停，绝不越过
        txt = re.sub(r"^(/\*+|\*+/|\*+|//+)", "", raw)
        txt = re.sub(r"\*+/$", "", txt).strip()
        if txt and not txt.startswith("#"):
            out.append(txt)
    return " ".join(reversed(out))[:80]


def _humanize(name: str) -> str:
    """没注释时退化为函数名分词，好歹给个可读标签。"""
    parts = re.split(r"[_\s]+|(?<=[a-z])(?=[A-Z])", name)
    return " ".join(p for p in parts if p)


def build(conn: sqlite3.Connection, repo: str, *, use_llm: bool = False,
          llm_min_fan_in: int = 3, llm_client=None) -> dict:
    """为全部函数节点生成摘要头。幂等：src_hash 未变则跳过。"""
    repo_p = Path(repo)
    fns = conn.execute(
        "SELECT * FROM node WHERE kind='function' AND is_definition=1"
    ).fetchall()

    stats = {"total": 0, "skipped": 0, "llm": 0, "truncated": 0, "max_tokens": 0}
    llm_targets: list[tuple[str, dict]] = []

    for fn in fns:
        stats["total"] += 1

        callees = [r["name"] for r in conn.execute(
            """SELECT DISTINCT n.name FROM edge e JOIN node n ON n.id = e.dst
                WHERE e.src=? AND e.kind='calls' AND e.confidence='certain'
                  AND n.kind='function' LIMIT ?""", (fn["id"], MAX_CALLEES))]
        callers = [r["name"] for r in conn.execute(
            """SELECT DISTINCT n.name FROM edge e JOIN node n ON n.id = e.src
                WHERE e.dst=? AND e.kind='calls' AND e.confidence='certain'
                  AND n.kind='function' LIMIT ?""", (fn["id"], MAX_CALLERS))]
        fan_in = conn.execute(
            "SELECT COUNT(*) c FROM edge WHERE dst=? AND kind='calls' AND confidence='certain'",
            (fn["id"],)).fetchone()["c"]
        fan_out = conn.execute(
            "SELECT COUNT(*) c FROM edge WHERE src=? AND kind='calls' AND confidence='certain'",
            (fn["id"],)).fetchone()["c"]

        fnode = conn.execute("SELECT id FROM node WHERE kind='file' AND path=?",
                             (fn["path"],)).fetchone()
        headers = []
        if fnode:
            headers = [r["path"] for r in conn.execute(
                """SELECT n.path FROM edge e JOIN node n ON n.id=e.dst
                    WHERE e.src=? AND e.kind='includes' AND e.confidence='certain' LIMIT ?""",
                (fnode["id"], MAX_HEADERS))]

        key_types = re.findall(r"\b([A-Za-z_]\w*(?:_t|_s|_e))\b", fn["signature"] or "")[:6]

        # 踩坑点只来自 **已审核通过** 的经验条目（B 级证据）
        pitfalls = [r["title"] for r in conn.execute(
            """SELECT ex.title FROM experience ex JOIN experience_link l ON l.exp_id=ex.id
                WHERE l.node_id=? AND l.confidence='certain' AND ex.status='approved'
                  AND COALESCE(ex.artifact_scope,'formal')=COALESCE(
                    (SELECT value FROM meta WHERE key='evaluation_scope'),'formal')
                  AND EXISTS (SELECT 1 FROM experience_anchor a
                               WHERE a.exp_id=ex.id AND a.active=1)
                LIMIT 5""", (fn["id"],))]

        doc = _doc_first_line(repo_p, fn["path"], fn["line_start"])
        one_liner = doc or _humanize(fn["name"])

        head = dict(
            node_id=fn["id"], one_liner=one_liner[:80],
            callees=json.dumps(callees, ensure_ascii=False),
            callers=json.dumps(callers, ensure_ascii=False),
            headers=json.dumps(headers, ensure_ascii=False),
            key_types=json.dumps(key_types, ensure_ascii=False),
            key_macros=json.dumps([], ensure_ascii=False),
            pitfalls=json.dumps(pitfalls, ensure_ascii=False),
            fan_in=fan_in, fan_out=fan_out, generated_by="rule",
            src_hash=hashlib.sha1(
                f"{RULE_VERSION}|{fn['path']}{fn['line_start']}{fn['line_end']}"
                f"{len(callees)}{len(callers)}{len(pitfalls)}|{one_liner}"
                .encode()).hexdigest()[:16],
        )

        old = conn.execute("SELECT src_hash FROM summary_head WHERE node_id=?",
                           (fn["id"],)).fetchone()
        if old and old["src_hash"] == head["src_hash"]:
            stats["skipped"] += 1
            continue

        # ★ 定长约束：超了就砍列表，直到达标
        head = _enforce_budget(head)
        stats["max_tokens"] = max(stats["max_tokens"], head["token_len"])
        if head.get("_truncated"):
            stats["truncated"] += 1
            head.pop("_truncated")

        conn.execute(
            """INSERT OR REPLACE INTO summary_head
               (node_id,one_liner,callees,callers,headers,key_types,key_macros,pitfalls,
                fan_in,fan_out,generated_by,token_len,src_hash)
               VALUES(:node_id,:one_liner,:callees,:callers,:headers,:key_types,:key_macros,
                      :pitfalls,:fan_in,:fan_out,:generated_by,:token_len,:src_hash)""", head)

        if use_llm and fan_in >= llm_min_fan_in:
            llm_targets.append((fn["id"], dict(name=fn["name"], sig=fn["signature"],
                                               callees=callees)))

    conn.commit()

    # LLM 只增强高价值节点的 one_liner —— 成本按重要性分配，不是无差别全量调用
    if use_llm and llm_client is not None:
        for node_id, ctx in llm_targets:
            try:
                txt = llm_client.one_liner(ctx)
                if txt:
                    conn.execute(
                        "UPDATE summary_head SET one_liner=?, generated_by='llm' WHERE node_id=?",
                        (txt[:80], node_id))
                    stats["llm"] += 1
            except Exception as e:      # 单点失败不影响整批
                log.warning("LLM one_liner 失败 %s: %s", node_id, e)
        conn.commit()

    return stats


def _enforce_budget(head: dict) -> dict:
    """逐步裁剪列表字段，直到 token_len <= MAX_TOKENS。"""
    truncated = False
    for _ in range(20):
        head["token_len"] = est_tokens(render(head))
        if head["token_len"] <= MAX_TOKENS:
            break
        truncated = True
        for field in ("callees", "callers", "headers", "key_types", "pitfalls"):
            arr = json.loads(head[field])
            if len(arr) > 1:
                head[field] = json.dumps(arr[:-1], ensure_ascii=False)
                break
        else:
            head["one_liner"] = head["one_liner"][:40]
            break
    if truncated:
        head["_truncated"] = True
    return head


def render(head: dict | sqlite3.Row, name: str = "") -> str:
    """把摘要头渲染成塞进 prompt 的紧凑文本。"""
    h = dict(head)

    def arr(k: str) -> list:
        v = h.get(k) or "[]"
        return json.loads(v) if isinstance(v, str) else (v or [])

    lines = [f"### {name or h.get('name','')} — {h.get('one_liner','')}"]
    if arr("callees"):
        lines.append("调用: " + ", ".join(arr("callees")))
    if arr("callers"):
        lines.append("被调: " + ", ".join(arr("callers")))
    if arr("headers"):
        lines.append("头文件: " + ", ".join(arr("headers")))
    if arr("key_types"):
        lines.append("关键类型: " + ", ".join(arr("key_types")))
    if arr("pitfalls"):
        lines.append("⚠ 已知问题: " + "; ".join(arr("pitfalls")))
    lines.append(f"(扇入{h.get('fan_in',0)}/扇出{h.get('fan_out',0)})")
    return "\n".join(lines)


def get(conn: sqlite3.Connection, node_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT s.*, n.name, n.path, n.signature FROM summary_head s
           JOIN node n ON n.id = s.node_id WHERE s.node_id = ?""", (node_id,)).fetchone()
