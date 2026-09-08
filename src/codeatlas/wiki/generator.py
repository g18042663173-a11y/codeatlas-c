"""M6 文件/模块/仓库 Wiki 生成（借鉴持久知识编译，不等同于 LLM Wiki 三层）。

三层自底向上：file → module → repo
  files/*.md    输入：文件内所有函数的摘要头 + include 关系
  modules/*.md  输入：该模块下所有 file 页的摘要 + 跨文件调用边
  repo.md       输入：所有 module 页的摘要

三个工程保障（长任务不崩的关键）：
  1. input_hash 跳过    输入没变就不重生成 —— 省 token 的最小增量实现
  2. gen_task 断点续写  中断后重跑能接上，不从头再来
  3. 章节完整性校验     缺必需章节就重试，超限标 failed，不阻塞整批

无 LLM 时用规则版生成：直接从图谱事实拼装。内容不如 LLM 流畅，
但全部是编译器确认的事实，反而更可信 —— 这也是 D3「程序管边界，模型管表达」。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .. import db as dbm
from ..summary import head as head_mod

log = logging.getLogger(__name__)

MAX_ATTEMPT = 3
MECHANISM_DEPTH = 2
MECHANISM_MAX_FUNCTIONS = 6

# schema.md 规定的必需章节。缺任何一个即判定生成失败。
REQUIRED_SECTIONS = {
    "file": ["## 职责与边界", "## 关键入口和接口", "## 确定调用流程", "## 可核验机制说明",
             "## 条件与错误路径", "## 类型、字段与全局引用", "## 候选线索与材料边界"],
    "module": ["## 职责与边界", "## 包含文件", "## 关键入口和接口",
               "## 确定调用流程", "## 可核验机制说明", "## 条件与错误路径", "## 类型、字段与全局引用",
               "## 候选线索与材料边界"],
    "repo": ["## 职责与边界", "## 模块列表", "## 关键入口和接口",
             "## 确定调用流程", "## 可核验机制说明", "## 条件与错误路径", "## 类型、字段与全局引用",
             "## 候选线索与材料边界"],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(*parts: str) -> str:
    return hashlib.sha1("::".join(parts).encode()).hexdigest()[:16]


def _module_of(path: str, repository: str = "") -> str:
    """Stable public module map for the two pinned corpora, directory fallback elsewhere."""
    rid = repository.lower()
    if "cjson" in rid:
        if "/" not in path:
            return "root"
        for prefix, module in (("tests/", "tests"), ("fuzzing/", "fuzzing")):
            if path.startswith(prefix):
                return module
    if "lwip" in rid:
        mappings = (
            ("src/core/ipv4/", "core/ipv4"), ("src/core/ipv6/", "core/ipv6"),
            ("src/core/", "core"), ("src/api/", "api"),
            ("src/netif/", "netif"), ("src/apps/", "apps"),
            ("contrib/", "contrib"),
        )
        for prefix, module in mappings:
            if path.startswith(prefix):
                if module == "apps":
                    tail = path[len(prefix):].split("/", 1)[0]
                    return f"apps/{tail}" if tail else module
                return module
        if path.startswith("src/include/"):
            return "include"
    return path.rsplit("/", 1)[0] if "/" in path else "root"


# ---------------------------------------------------------------- 事实收集

def _mechanism_id(entry: sqlite3.Row) -> str:
    """Stable across line movement; never derived from an evaluation question."""
    # A C USR can repeat for translation-unit entry names such as ``main``.
    # Bind the anchor to the repository-relative definition as well, keeping it
    # stable across line movement without colliding across files.
    return _hash("mechanism-v1", entry["usr"] or "", entry["path"] or "", entry["id"])


def _mechanism(conn: sqlite3.Connection, entry: sqlite3.Row) -> dict:
    """Build a bounded compiler-fact IR for one public entry.

    A certain call edge is only reported as a call.  It is deliberately not
    promoted into a causal or whole-program behaviour statement.
    """
    queue = [(entry["id"], 0)]
    seen: set[str] = set()
    rows: dict[str, sqlite3.Row] = {}
    edges: list[dict] = []
    truncated = False
    while queue:
        node_id, depth = queue.pop(0)
        if node_id in seen:
            continue
        if len(seen) >= MECHANISM_MAX_FUNCTIONS:
            truncated = True
            break
        row = conn.execute(
            "SELECT * FROM node WHERE id=? AND kind='function' AND is_definition=1",
            (node_id,),
        ).fetchone()
        if row is None:
            continue
        seen.add(node_id)
        rows[node_id] = row
        outgoing = conn.execute(
            """SELECT dn.* FROM edge e JOIN node dn ON dn.id=e.dst
                WHERE e.src=? AND e.kind='calls' AND e.confidence='certain'
                  AND dn.kind='function' AND dn.is_definition=1
                ORDER BY dn.path,dn.line_start,dn.name""",
            (node_id,),
        ).fetchall()
        for target in outgoing:
            edges.append({
                "caller_id": node_id, "caller": row["name"], "caller_path": row["path"],
                "caller_line": row["line_start"], "callee_id": target["id"],
                "callee": target["name"], "callee_path": target["path"],
                "callee_line": target["line_start"], "confidence": "certain",
            })
            if depth < MECHANISM_DEPTH and target["id"] not in seen:
                queue.append((target["id"], depth + 1))
            elif depth >= MECHANISM_DEPTH:
                truncated = True
    facts = []
    if seen:
        marks = ",".join("?" for _ in seen)
        for row in conn.execute(
            f"SELECT * FROM semantic_fact WHERE confidence='certain' "
            f"AND function_id IN ({marks}) ORDER BY path,line_start,id",
            sorted(seen),
        ):
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            facts.append({
                "id": row["id"], "function_id": row["function_id"], "kind": row["kind"],
                "path": row["path"], "line_start": row["line_start"],
                "line_end": row["line_end"], "payload": payload,
            })
    candidate_count = 0
    if seen:
        marks = ",".join("?" for _ in seen)
        candidate_count = conn.execute(
            f"SELECT COUNT(*) FROM edge WHERE kind='calls' AND confidence='candidate' "
            f"AND src IN ({marks})", sorted(seen),
        ).fetchone()[0]
    return {
        "id": _mechanism_id(entry), "entry_id": entry["id"], "entry_usr": entry["usr"],
        "entry": entry["name"], "signature": entry["signature"], "path": entry["path"],
        "line_start": entry["line_start"], "line_end": entry["line_end"],
        "functions": [{"id": row["id"], "name": row["name"], "usr": row["usr"],
                       "path": row["path"], "line_start": row["line_start"],
                       "line_end": row["line_end"], "definition_hash": row["definition_hash"]}
                      for row in rows.values()],
        "calls": edges, "facts": facts, "candidate_calls": candidate_count,
        "scope": {"depth": MECHANISM_DEPTH, "max_functions": MECHANISM_MAX_FUNCTIONS,
                  "truncated": truncated or bool(queue)},
        "validation": {"source": "compiler_rule_ir", "model_statement": "not_present"},
    }

def _file_facts(conn: sqlite3.Connection, file_row: sqlite3.Row) -> dict:
    fns = conn.execute(
        """SELECT n.*, s.one_liner, s.fan_in, s.fan_out FROM node n
             LEFT JOIN summary_head s ON s.node_id = n.id
            WHERE n.kind='function' AND n.is_definition=1 AND n.path=?
            ORDER BY s.fan_in DESC NULLS LAST, n.line_start""",
        (file_row["path"],)).fetchall()
    includes = [r["path"] for r in conn.execute(
        """SELECT n.path FROM edge e JOIN node n ON n.id=e.dst
            WHERE e.src=? AND e.kind='includes' AND e.confidence='certain'""",
        (file_row["id"],))]
    # 出向/入向的跨文件调用
    out_calls = conn.execute(
        """SELECT DISTINCT dn.path p, COUNT(*) c FROM edge e
             JOIN node sn ON sn.id=e.src JOIN node dn ON dn.id=e.dst
            WHERE e.kind='calls' AND e.confidence='certain'
              AND sn.path=? AND dn.path IS NOT NULL AND dn.path<>?
            GROUP BY dn.path ORDER BY c DESC LIMIT 8""",
        (file_row["path"], file_row["path"])).fetchall()
    call_flows = conn.execute(
        """SELECT sn.name caller,sn.usr caller_usr,sn.line_start caller_line,
                  dn.name callee,dn.usr callee_usr,dn.path callee_path,dn.line_start callee_line
             FROM edge e JOIN node sn ON sn.id=e.src JOIN node dn ON dn.id=e.dst
            WHERE e.kind='calls' AND e.confidence='certain' AND sn.path=?
            ORDER BY sn.line_start,dn.name LIMIT 40""", (file_row["path"],),
    ).fetchall()
    in_calls = conn.execute(
        """SELECT DISTINCT sn.path p, COUNT(*) c FROM edge e
             JOIN node sn ON sn.id=e.src JOIN node dn ON dn.id=e.dst
            WHERE e.kind='calls' AND e.confidence='certain'
              AND dn.path=? AND sn.path IS NOT NULL AND sn.path<>?
            GROUP BY sn.path ORDER BY c DESC LIMIT 8""",
        (file_row["path"], file_row["path"])).fetchall()
    cand = conn.execute(
        """SELECT COUNT(*) c FROM edge e JOIN node sn ON sn.id=e.src
            WHERE e.kind='calls' AND e.confidence='candidate' AND sn.path=?""",
        (file_row["path"],)).fetchone()["c"]
    references = conn.execute(
        """SELECT e.kind,n.name,n.path,COUNT(*) c FROM edge e
             JOIN node src ON src.id=e.src JOIN node n ON n.id=e.dst
            WHERE src.kind='function' AND src.path=? AND e.confidence='certain'
              AND e.kind IN ('type_use','field_access','global_ref')
            GROUP BY e.kind,n.id ORDER BY e.kind,n.name""",
        (file_row["path"],),
    ).fetchall()
    declarations = conn.execute(
        """SELECT id,kind,name,usr,line_start,line_end,signature,is_definition
             FROM node WHERE path=? AND kind IN ('struct','enum','typedef','macro','global','field')
            ORDER BY line_start,name""", (file_row["path"],),
    ).fetchall()
    branches = conn.execute(
        """SELECT b.*,n.name function_name,n.usr FROM branch_fact b
             JOIN node n ON n.id=b.function_id
            WHERE b.path=? ORDER BY b.line_start LIMIT 80""", (file_row["path"],),
    ).fetchall()
    mechanisms = [_mechanism(conn, fn) for fn in fns if not fn["is_static"]]
    return dict(fns=fns, includes=includes, out_calls=out_calls, call_flows=call_flows,
                in_calls=in_calls, candidate_calls=cand, references=references,
                declarations=declarations, branches=branches,
                mechanisms=mechanisms,
                semantic_facts=conn.execute("SELECT * FROM semantic_fact WHERE path=? ORDER BY line_start,id", (file_row["path"],)).fetchall())


def _file_input_hash(repository: str, revision: str, path: str, facts: dict) -> str:
    payload = {
        "repository": repository, "path": path,
        "build_config_hash": facts.get("build_config_hash", ""),
        "functions": [
            (row["id"], row["definition_hash"], row["signature"], row["one_liner"],
             row["fan_in"], row["fan_out"])
            for row in facts["fns"]
        ],
        "includes": sorted(facts["includes"]),
        "out_calls": [tuple(row) for row in facts["out_calls"]],
        "call_flows": [{key: row[key] for key in row.keys() if not key.endswith("_line")} for row in facts["call_flows"]],
        "in_calls": [tuple(row) for row in facts["in_calls"]],
        "candidate_calls": facts["candidate_calls"],
        "references": [tuple(row) for row in facts["references"]],
        "declarations": [{key: row[key] for key in row.keys() if key not in ("line_start", "line_end")} for row in facts["declarations"]],
        "branches": [(row["function_id"], row["kind"], row["source_hash"]) for row in facts["branches"]],
        "semantic_facts": [row["payload_json"] for row in facts.get("semantic_facts", [])],
        "mechanisms": facts.get("mechanisms", []),
    }
    return _hash(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


# ---------------------------------------------------------------- 规则版渲染

def _render_file(row: sqlite3.Row, f: dict) -> str:
    path = row["path"]
    pub = [x for x in f["fns"] if not x["is_static"]]
    prv = [x for x in f["fns"] if x["is_static"]]
    L = [f"# {path}", "", "## 职责与边界", ""]
    if f["fns"]:
        L.append(f"本页覆盖 {len(f['fns'])} 个函数定义（公开 {len(pub)} / 静态 {len(prv)}）。"
                 f"范围仅限固定构建配置下的 AST 事实。[A:{path}:L{f['fns'][0]['line_start']}]")
    else:
        kinds = defaultdict(int)
        for decl in f["declarations"]:
            kinds[decl["kind"]] += 1
        detail = "、".join(f"{key} {value}" for key, value in sorted(kinds.items())) or "include 关系"
        L.append(f"本文件没有函数定义；仍记录 {detail}，不据此推断运行时行为。[A:{path}:L1]")

    L += ["", "## 关键入口和接口", ""]
    if pub:
        L += ["| 函数 | 职责骨架 | 扇入 | 精确来源 |", "|---|---|---:|---|"]
        for x in pub[:30]:
            L.append(f"| `{x['name']}` | {(x['one_liner'] or '由签名与调用关系生成').replace('|','/')} "
                     f"| {x['fan_in'] or 0} | [A:{path}:L{x['line_start']}-L{x['line_end']}] |")
    else:
        L.append("无公开函数定义；这不是“文件无内容”，而是当前事实不足以给出函数入口。")
    for decl in f["declarations"][:30]:
        L.append(f"- `{decl['kind']}` `{decl['name']}` [A:{path}:L{decl['line_start']}-L{decl['line_end']}]")

    L += ["", "## 确定调用流程", ""]
    if f["call_flows"]:
        for call in f["call_flows"][:30]:
            L.append(f"- `{call['caller']}` → `{call['callee']}` "
                     f"[A:{path}:L{call['caller_line']}] [A:{call['callee_path']}:L{call['callee_line']}]")
    else:
        L.append("当前文件没有编译器确认的函数调用链。")
    if f["in_calls"]:
        L.append("跨文件流入：" + "、".join(f"`{r['p']}`({r['c']})" for r in f["in_calls"]))
    if f["out_calls"]:
        L.append("跨文件流出：" + "、".join(f"`{r['p']}`({r['c']})" for r in f["out_calls"]))

    L += ["", "## 可核验机制说明", ""]
    if not f["mechanisms"]:
        L.append("本文件没有公开函数定义，因此不生成运行机制陈述。")
    for mechanism in f["mechanisms"]:
        mid = mechanism["id"]
        L += [f'<a id="mechanism-{mid}"></a>',
              f"### `{mechanism['entry']}` · `{mid}`", "",
              f"- **适用入口**：`{mechanism['signature'] or mechanism['entry']}` "
              f"[A:{mechanism['path']}:L{mechanism['line_start']}-L{mechanism['line_end']}]",
              f"- **边界**：仅组织 {mechanism['scope']['depth']} 跳内、最多 "
              f"{mechanism['scope']['max_functions']} 个函数的当前构建事实。"]
        if mechanism["calls"]:
            L.append("- **确定调用**：")
            for call in mechanism["calls"]:
                L.append(f"  - `{call['caller']}` → `{call['callee']}` "
                         f"[A:{call['caller_path']}:L{call['caller_line']}] "
                         f"[A:{call['callee_path']}:L{call['callee_line']}]")
        else:
            L.append("- **确定调用**：在有界范围内未观察到函数调用边。")
        conditions = [fact for fact in mechanism["facts"]
                      if fact["kind"] in {"guard", "branch_set", "decider"}]
        writes = [fact for fact in mechanism["facts"] if fact["kind"] == "state_write"]
        exits = [fact for fact in mechanism["facts"] if fact["kind"] == "error_path"]
        if conditions:
            L.append("- **条件与分支事实**：")
            for fact in conditions[:12]:
                payload = fact["payload"]
                condition = payload.get("condition") or payload.get("control_kind") or "显式分支"
                L.append(f"  - `{fact['kind']}`: `{str(condition).replace('`', '')}` "
                         f"[A:{fact['path']}:L{fact['line_start']}-L{fact['line_end']}]")
            if len(conditions) > 12:
                L.append(f"  - 另有 {len(conditions) - 12} 条条件事实未在本页展开；当前说明不完整。")
        if writes:
            L.append("- **状态写入事实**：")
            for fact in writes[:12]:
                payload = fact["payload"]
                L.append(f"  - `{str(payload.get('target', '')).replace('`', '')}` "
                         f"`{payload.get('operator', '=')}` `{str(payload.get('value', '')).replace('`', '')}` "
                         f"[A:{fact['path']}:L{fact['line_start']}-L{fact['line_end']}]")
            if len(writes) > 12:
                L.append(f"  - 另有 {len(writes) - 12} 条状态写入事实未在本页展开；当前说明不完整。")
        if exits:
            L.append("- **退出事实**：")
            for fact in exits[:12]:
                expression = str(fact["payload"].get("expression", "return")).replace("`", "")
                L.append(f"  - `{expression}`；错误语义分类为 `unknown` "
                         f"[A:{fact['path']}:L{fact['line_start']}-L{fact['line_end']}]")
            if len(exits) > 12:
                L.append(f"  - 另有 {len(exits) - 12} 条退出事实未在本页展开；当前说明不完整。")
        boundaries = []
        if mechanism["candidate_calls"]:
            boundaries.append(f"{mechanism['candidate_calls']} 条 candidate 调用未纳入确定流程")
        if mechanism["scope"]["truncated"]:
            boundaries.append("调用闭包超出两跳或六函数，当前说明不完整")
        L.append("- **反例与材料不足**：" + ("；".join(boundaries) if boundaries else
                 "没有额外 candidate 线索；仍不推断跨函数因果、别名或未启用预处理分支。"))

    L += ["", "## 条件与错误路径", ""]
    for fact in f.get("semantic_facts", [])[:40]:
        payload = json.loads(fact["payload_json"])
        L.append(f"- `{fact['kind']}` / `{fact['id']}` [A:{path}:L{fact['line_start']}-L{fact['line_end']}]\n"
                 f"```json\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n```")
    if f["branches"]:
        labels = {"if": "条件分支", "switch": "分派", "conditional": "条件表达式",
                  "early_return": "显式返回", "error_return": "疑似错误返回"}
        for branch in f["branches"][:30]:
            L.append(f"- `{branch['function_name']}`：{labels[branch['kind']]}，"
                     f"范围 L{branch['line_start']}-L{branch['line_end']} "
                     f"[A:{path}:L{branch['line_start']}-L{branch['line_end']}]")
        L.append("以上仅是显式语法范围，不等同于完整控制流、可达性或数据流分析。")
    else:
        L.append("当前构建快照未采集到显式分支或返回范围；不据此断言不存在错误路径。")

    L += ["", "## 类型、字段与全局引用", ""]
    if f["references"]:
        labels = {"type_use": "类型", "field_access": "字段", "global_ref": "全局量"}
        for ref in f["references"][:40]:
            L.append(f"- {labels[ref['kind']]} `{ref['name']}`：{ref['c']} 处确定引用")
    else:
        L.append("未采集到函数内的类型、字段或全局引用。")
    L.append("仓库内 include：" + ("、".join(f"`{h}`" for h in f["includes"][:20])
                                  if f["includes"] else "无"))

    L += ["", "## 候选线索与材料边界", ""]
    if f["candidate_calls"]:
        L.append(f"- 有 {f['candidate_calls']} 条函数指针、取地址或未解析候选调用线索；"
                 "它们不进入确定调用链，需人工确认。")
    else:
        L.append("- 当前没有需复核的调用线索。")
    L.append("- 页面只覆盖当前 repository、revision 和 build_config_hash；缺失编译参数会使结论降级。")
    return "\n".join(L)


def _render_module(module: str, files: list[sqlite3.Row], conn) -> str:
    paths = [row["path"] for row in files]
    placeholders = ",".join("?" for _ in paths) or "NULL"
    entries = conn.execute(
        f"""SELECT n.name,n.path,n.line_start,n.line_end,n.usr,s.one_liner,s.fan_in
              FROM node n LEFT JOIN summary_head s ON s.node_id=n.id
             WHERE n.kind='function' AND n.is_definition=1 AND n.is_static=0
               AND n.path IN ({placeholders}) ORDER BY COALESCE(s.fan_in,0) DESC,n.name LIMIT 20""",
        paths,
    ).fetchall()
    cross = conn.execute(
        f"""SELECT sn.path a,dn.path b,COUNT(*) c FROM edge e
              JOIN node sn ON sn.id=e.src JOIN node dn ON dn.id=e.dst
             WHERE e.kind='calls' AND e.confidence='certain' AND sn.path<>dn.path
               AND sn.path IN ({placeholders})
             GROUP BY sn.path,dn.path ORDER BY c DESC LIMIT 20""", paths,
    ).fetchall()
    branch_count = conn.execute(
        f"SELECT COUNT(*) FROM branch_fact WHERE path IN ({placeholders})", paths,
    ).fetchone()[0]
    refs = conn.execute(
        f"""SELECT e.kind,COUNT(*) c FROM edge e JOIN node n ON n.id=e.src
             WHERE n.path IN ({placeholders}) AND e.confidence='certain'
               AND e.kind IN ('type_use','field_access','global_ref') GROUP BY e.kind""", paths,
    ).fetchall()
    cand = conn.execute(
        f"""SELECT COUNT(*) FROM edge e JOIN node n ON n.id=e.src
             WHERE n.path IN ({placeholders}) AND e.kind='calls' AND e.confidence='candidate'""",
        paths,
    ).fetchone()[0]
    L = [f"# 模块 {module}", "", "## 职责与边界", "",
         f"模块由 {len(files)} 个文件组成；统计严格限定于这些文件，不复用仓库级榜单。", "",
         "## 包含文件", ""]
    L += [f"- [`{f['path']}`](../files/{f['path'].replace('/', '__')}.md)" for f in files]
    L += ["", "## 关键入口和接口", ""]
    if entries:
        for entry in entries:
            L.append(f"- `{entry['name']}` — {entry['one_liner'] or '规则骨架'} "
                     f"[A:{entry['path']}:L{entry['line_start']}-L{entry['line_end']}]")
    else:
        L.append("本模块没有公开函数定义。")
    L += ["", "## 确定调用流程", ""]
    L += ([f"- `{row['a']}` → `{row['b']}`（{row['c']} 条确定调用）" for row in cross]
          or ["模块内文件没有跨文件确定调用。"])
    L += ["", "## 可核验机制说明", ""]
    public_entries = conn.execute(
        f"""SELECT id,name,usr,path,line_start FROM node
              WHERE kind='function' AND is_definition=1 AND is_static=0
                AND path IN ({placeholders}) ORDER BY path,line_start,name""", paths,
    ).fetchall()
    if public_entries:
        for entry in public_entries:
            mid = _mechanism_id(entry)
            file_page = entry["path"].replace("/", "__")
            L.append(f"- [`{entry['name']}`](../files/{file_page}.md#mechanism-{mid}) · `{mid}` "
                     f"[A:{entry['path']}:L{entry['line_start']}]")
    else:
        L.append("本模块没有可组织为机制说明的公开函数入口。")
    L += ["", "## 条件与错误路径", "",
          f"模块文件中记录 {branch_count} 个显式条件/返回源码范围；详见文件页。"
          "它们不构成完整控制流结论。", "", "## 类型、字段与全局引用", ""]
    L += ([f"- `{row['kind']}`：{row['c']} 条确定关系" for row in refs]
          or ["未采集到相关确定引用。"])
    L += ["", "## 候选线索与材料边界", "",
          f"- 候选调用 {cand} 条，未纳入影响分析。",
          "- 子文件页缺失或 stale 时，本模块页不得作为正式 C 级证据。"]
    return "\n".join(L)


def _render_repo(conn, modules: dict, stats: dict) -> str:
    hubs = conn.execute(
        """SELECT n.name,n.path,n.line_start,n.line_end,COALESCE(s.fan_in,0) fan_in
             FROM node n LEFT JOIN summary_head s ON s.node_id=n.id
            WHERE n.kind='function' AND n.is_definition=1
            ORDER BY fan_in DESC,n.name LIMIT 15""").fetchall()
    branch_count = conn.execute("SELECT COUNT(*) FROM branch_fact").fetchone()[0]
    refs = conn.execute(
        """SELECT kind,COUNT(*) c FROM edge WHERE confidence='certain'
             AND kind IN ('type_use','field_access','global_ref') GROUP BY kind""").fetchall()
    L = ["# 仓库总览", "", "## 职责与边界", "",
         f"固定构建快照覆盖 {stats['files']} 个 C/H 文件、{stats['functions']} 个函数定义、"
         f"{len(modules)} 个模块。代码和构建配置是事实来源，Wiki 可重建。", "",
         "## 模块列表", "", "| 模块 | 文件数 |", "|---|---:|"]
    for module, files in sorted(modules.items(), key=lambda item: item[0]):
        L.append(f"| [`{module}`](modules/{module.replace('/', '__')}.md) | {len(files)} |")
    L += ["", "## 关键入口和接口", ""]
    for hub in hubs:
        L.append(f"- `{hub['name']}`：确定扇入 {hub['fan_in']} "
                 f"[A:{hub['path']}:L{hub['line_start']}-L{hub['line_end']}]")
    if not hubs:
        L.append("没有函数定义。")
    L += ["", "## 确定调用流程", "",
          f"图中共有 {stats['edges_certain']} 条 certain 关系；具体流程下钻到模块页与文件页。", "",
          "## 可核验机制说明", ""]
    for module, files in sorted(modules.items(), key=lambda item: item[0]):
        paths = [row["path"] for row in files]
        marks = ",".join("?" for _ in paths) or "NULL"
        count = conn.execute(
            f"SELECT COUNT(*) FROM node WHERE kind='function' AND is_definition=1 "
            f"AND is_static=0 AND path IN ({marks})", paths,
        ).fetchone()[0]
        L.append(f"- [`{module}`](modules/{module.replace('/', '__')}.md)：{count} 个公开入口的有界机制骨架")
    if not modules:
        L.append("没有可用模块。")
    L += ["",
          "## 条件与错误路径", "",
          f"共记录 {branch_count} 个显式语法范围；不包装为完整控制流/数据流。", "",
          "## 类型、字段与全局引用", ""]
    L += ([f"- `{row['kind']}`：{row['c']} 条" for row in refs] or ["未采集到相关关系。"])
    L += ["", "## 候选线索与材料边界", "",
          f"- 候选关系 {stats['edges_candidate']} 条，独立展示且不进入 certain-only 影响分析。",
          "- 页面结论受固定 revision、build_config_hash 与编译诊断约束。"]
    return "\n".join(L)


# ---------------------------------------------------------------- 校验

def validate(md: str, level: str) -> tuple[bool, str]:
    missing = [s for s in REQUIRED_SECTIONS[level] if s not in md]
    if missing:
        return False, f"缺少章节: {', '.join(missing)}"
    if len(md) < 60:
        return False, "内容过短"
    return True, ""


# ---------------------------------------------------------------- 主流程

def _upsert_task(conn, tid, target, stage, state, err=""):
    conn.execute(
        """INSERT INTO gen_task(id,target_id,stage,state,attempt,last_error,updated_at)
           VALUES(?,?,?,?,1,?,?)
           ON CONFLICT(id) DO UPDATE SET state=excluded.state,
             attempt=gen_task.attempt+1, last_error=excluded.last_error,
             updated_at=excluded.updated_at""",
        (tid, target, stage, state, err, _now()))


def _write(conn, page_id, level, node_id, title, md, sources, input_hash) -> bool:
    ok, err = validate(md, level)
    repository = dbm.get_meta(conn, "repository_id", dbm.get_meta(conn, "repo", ""))
    revision = dbm.get_meta(conn, "revision", "unversioned")
    conn.execute(
        """INSERT INTO wiki_page(id,level,node_id,title,md,sources,status,version,
                                 input_hash,repository,revision,updated_at)
           VALUES(?,?,?,?,?,?,?,1,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET md=excluded.md, sources=excluded.sources,
             status=excluded.status, version=wiki_page.version+1,
             input_hash=excluded.input_hash,repository=excluded.repository,
             revision=excluded.revision,updated_at=excluded.updated_at""",
        (page_id, level, node_id, title, md, json.dumps(sources, ensure_ascii=False),
         "ok" if ok else "failed", input_hash, repository, revision, _now()))
    if not ok:
        log.warning("页面 %s 校验失败: %s", page_id, err)
    return ok


def _mark_orphans(conn: sqlite3.Connection, level: str, expected: set[str]) -> int:
    rows = conn.execute("SELECT id FROM wiki_page WHERE level=?", (level,)).fetchall()
    obsolete = [row["id"] for row in rows if row["id"] not in expected]
    if not obsolete:
        return 0
    conn.executemany(
        "UPDATE wiki_page SET status='stale',updated_at=? WHERE id=? AND status<>'stale'",
        [(_now(), page_id) for page_id in obsolete],
    )
    return len(obsolete)


def generate(conn: sqlite3.Connection, repo: str, *, levels: tuple[str, ...] =
             ("file", "module", "repo"), out_dir: str | None = None,
             llm_client=None, resume: bool = True) -> dict:
    st = {"file": 0, "module": 0, "repo": 0, "skipped": 0, "failed": 0,
          "stale": 0, "llm": 0}
    repository = dbm.get_meta(conn, "repository_id", dbm.get_meta(conn, "repo", ""))
    revision = dbm.get_meta(conn, "revision", "unversioned")
    build_config_hash = dbm.get_meta(conn, "build_config_hash", "unconfigured")
    files = conn.execute("SELECT * FROM node WHERE kind='file' ORDER BY path").fetchall()
    modules: dict[str, list] = defaultdict(list)
    for f in files:
        if f["path"]:
            modules[_module_of(f["path"], repository)].append(f)

    # ---- 第 1 层：file ----
    if "file" in levels:
        expected_file_pages: set[str] = set()
        for f in files:
            if not f["path"]:
                continue
            facts = _file_facts(conn, f)
            facts["build_config_hash"] = build_config_hash
            page_id = "files/" + f["path"].replace("/", "__")
            expected_file_pages.add(page_id)
            ih = _file_input_hash(repository, revision, f["path"], facts)
            old = conn.execute("SELECT input_hash,status FROM wiki_page WHERE id=?",
                               (page_id,)).fetchone()
            if resume and old and old["input_hash"] == ih and old["status"] == "ok":
                conn.execute("UPDATE wiki_page SET md=?,repository=?,revision=? WHERE id=?",
                             (_render_file(f, facts), repository, revision, page_id))
                st["skipped"] += 1
                continue

            _upsert_task(conn, page_id, f["id"], "file", "running")
            md = _render_file(f, facts)
            if llm_client is not None:
                md = _llm_polish(llm_client, md, "file", st)
            # A bounded mechanism may display a frontier call whose callee is not
            # expanded into ``functions``.  Both ends still participate in the
            # rendered claim and therefore must invalidate the page when either
            # source definition changes.
            mechanism_nodes = {
                node["id"] for item in facts["mechanisms"] for node in item["functions"]
            }
            mechanism_nodes.update(
                endpoint
                for item in facts["mechanisms"]
                for call in item["calls"]
                for endpoint in (call["caller_id"], call["callee_id"])
            )
            srcs = [f"node:{f['id']}"] + [f"node:{x['id']}" for x in facts["fns"]]
            srcs += [f"node:{node_id}" for node_id in sorted(mechanism_nodes)
                     if node_id != f["id"] and all(node_id != x["id"] for x in facts["fns"])]
            ok = _write(conn, page_id, "file", f["id"], f["path"], md, srcs, ih)
            _upsert_task(conn, page_id, f["id"], "file", "done" if ok else "failed")
            st["file" if ok else "failed"] += 1
        st["stale"] += _mark_orphans(conn, "file", expected_file_pages)
        conn.commit()

    # ---- 第 2 层：module（输入是第 1 层的产物）----
    if "module" in levels:
        expected_module_pages: set[str] = set()
        for m, fs in modules.items():
            page_id = "modules/" + m.replace("/", "__")
            expected_module_pages.add(page_id)
            child_inputs = []
            for file_row in fs:
                child = conn.execute(
                    "SELECT input_hash,status FROM wiki_page WHERE id=?",
                    ("files/" + file_row["path"].replace("/", "__"),),
                ).fetchone()
                child_inputs.append((file_row["path"], child["input_hash"] if child else None,
                                     child["status"] if child else None))
            ih = _hash(repository, build_config_hash, m,
                       json.dumps(child_inputs, sort_keys=True))
            old = conn.execute("SELECT input_hash,status FROM wiki_page WHERE id=?",
                               (page_id,)).fetchone()
            if resume and old and old["input_hash"] == ih and old["status"] == "ok":
                st["skipped"] += 1
                conn.execute("UPDATE wiki_page SET md=?,repository=?,revision=? WHERE id=?",
                             (_render_module(m, fs, conn), repository, revision, page_id))
                continue
            _upsert_task(conn, page_id, m, "module", "running")
            md = _render_module(m, fs, conn)
            if llm_client is not None:
                md = _llm_polish(llm_client, md, "module", st)
            srcs = [f"wiki:files/{x['path'].replace('/', '__')}" for x in fs]
            ok = _write(conn, page_id, "module", None, f"模块 {m}", md, srcs, ih)
            _upsert_task(conn, page_id, m, "module", "done" if ok else "failed")
            st["module" if ok else "failed"] += 1
        st["stale"] += _mark_orphans(conn, "module", expected_module_pages)
        conn.commit()

    # ---- 第 3 层：repo ----
    if "repo" in levels:
        q = lambda s: conn.execute(s).fetchone()[0]
        stats = dict(
            files=q("SELECT COUNT(*) FROM node WHERE kind='file'"),
            functions=q("SELECT COUNT(*) FROM node WHERE kind='function' AND is_definition=1"),
            nodes=q("SELECT COUNT(*) FROM node"), edges=q("SELECT COUNT(*) FROM edge"),
            edges_certain=q("SELECT COUNT(*) FROM edge WHERE confidence='certain'"),
            edges_candidate=q("SELECT COUNT(*) FROM edge WHERE confidence='candidate'"))
        module_inputs = [tuple(row) for row in conn.execute(
            "SELECT id,input_hash,status FROM wiki_page WHERE level='module' ORDER BY id"
        )]
        ih = _hash(repository, build_config_hash, *(str(v) for v in stats.values()),
                   json.dumps(module_inputs, sort_keys=True))
        old = conn.execute("SELECT input_hash,status FROM wiki_page WHERE id='repo'").fetchone()
        if not (resume and old and old["input_hash"] == ih and old["status"] == "ok"):
            md = _render_repo(conn, modules, stats)
            srcs = [f"wiki:modules/{m.replace('/', '__')}" for m in modules]
            ok = _write(conn, "repo", "repo", None, "仓库总览", md, srcs, ih)
            _upsert_task(conn, "repo", "repo", "repo", "done" if ok else "failed")
            st["repo" if ok else "failed"] += 1
        else:
            st["skipped"] += 1
            conn.execute("UPDATE wiki_page SET revision=?,repository=? WHERE id='repo'", (revision, repository))
        conn.commit()

    from ..dependencies import record_wiki
    record_wiki(conn)
    # If an indexed Wiki page became stale, remove it from FTS immediately. Newly
    # generated pages join the index on the next normal index build.
    from ..indexer.build import refresh_visibility
    refresh_visibility(conn)

    if out_dir:
        export(conn, out_dir)
    report = integrity(conn, out_dir=out_dir)
    st["integrity"] = report
    if not report["ok"]:
        raise ValueError("Wiki 完整性校验失败: " + "; ".join(report["errors"][:8]))
    return st


def _llm_polish(client, md: str, level: str, st: dict) -> str:
    """LLM 只负责把规则版事实改写得更可读，不允许新增事实。

    生成后必须过章节校验；连续失败就退回规则版 —— 宁可干巴，不要幻觉。
    """
    for _ in range(MAX_ATTEMPT):
        try:
            out = client.answer(
                "你在改写一份由程序从 AST 事实生成的代码文档。规则：\n"
                "1. 只能改写措辞，不得新增任何原文没有的事实。\n"
                f"2. 必须保留全部章节标题：{REQUIRED_SECTIONS[level]}\n"
                "3. 保留全部表格和代码引用。只输出 Markdown。",
                "请改写下面的文档使其更易读：", md)
            ok, _e = validate(out, level)
            baseline_refs = set(re.findall(r"\[A:[^\]]+\]", md))
            output_refs = set(re.findall(r"\[A:[^\]]+\]", out))
            # A model may reorder or remove prose, but it cannot invent an anchor and
            # it must retain every compiler citation used by the reliable skeleton.
            citations_ok = output_refs <= baseline_refs and baseline_refs <= output_refs
            exact_blocks = sorted(out.split("\n\n")) == sorted(md.split("\n\n"))
            if ok and citations_ok and exact_blocks:
                st["llm"] += 1
                return out
        except Exception as e:
            log.warning("LLM 改写失败: %s", e)
    return md


def integrity(conn: sqlite3.Connection, *, out_dir: str | None = None) -> dict:
    """Validate page coverage, source back-links and exported local Markdown links."""
    errors: list[str] = []
    current = {row["id"]: row for row in conn.execute(
        "SELECT * FROM wiki_page WHERE status='ok'"
    )}
    for file_row in conn.execute("SELECT id,path FROM node WHERE kind='file' AND path IS NOT NULL"):
        page_id = "files/" + file_row["path"].replace("/", "__")
        if page_id not in current:
            errors.append(f"缺少文件页:{file_row['path']}")
    mechanism_ids: list[str] = []
    for page in current.values():
        try:
            sources = json.loads(page["sources"] or "[]")
        except json.JSONDecodeError:
            errors.append(f"sources 非法:{page['id']}")
            continue
        for source in sources:
            if source.startswith("node:"):
                exists = conn.execute("SELECT 1 FROM node WHERE id=?", (source[5:],)).fetchone()
            elif source.startswith("wiki:"):
                exists = current.get(source[5:])
            else:
                exists = None
            if not exists:
                errors.append(f"回链缺失:{page['id']}->{source}")
        if page["level"] == "file":
            mechanism_ids.extend(re.findall(r'<a id="mechanism-([0-9a-f]{16})"></a>', page["md"] or ""))
    expected_mechanisms = conn.execute(
        "SELECT COUNT(*) FROM node WHERE kind='function' AND is_definition=1 AND is_static=0"
    ).fetchone()[0]
    if len(mechanism_ids) != expected_mechanisms:
        errors.append(f"机制覆盖不完整:{len(mechanism_ids)}/{expected_mechanisms}")
    if len(mechanism_ids) != len(set(mechanism_ids)):
        errors.append("机制 ID 不唯一")

    # Recompute every module's scoped cross-file calls and compare them with the
    # rendered page.  This prevents an accidentally reused repository-level Top N
    # from passing a link-only integrity check.
    repository = dbm.get_meta(conn, "repository_id", dbm.get_meta(conn, "repo", ""))
    module_files: dict[str, list[str]] = defaultdict(list)
    for row in conn.execute("SELECT path FROM node WHERE kind='file' AND path IS NOT NULL"):
        module_files[_module_of(row["path"], repository)].append(row["path"])
    module_scope_checked = 0
    module_scope_ok = True
    call_pattern = re.compile(
        r"^- `([^`]+)` → `([^`]+)`（(\d+) 条确定调用）$", re.M
    )
    for module, paths in module_files.items():
        page_id = "modules/" + module.replace("/", "__")
        page = current.get(page_id)
        if page is None:
            module_scope_ok = False
            errors.append(f"缺少模块页:{module}")
            continue
        placeholders = ",".join("?" for _ in paths)
        expected_rows = conn.execute(
            f"""SELECT sn.path a,dn.path b,COUNT(*) c FROM edge e
                  JOIN node sn ON sn.id=e.src JOIN node dn ON dn.id=e.dst
                 WHERE e.kind='calls' AND e.confidence='certain' AND sn.path<>dn.path
                   AND sn.path IN ({placeholders})
                 GROUP BY sn.path,dn.path ORDER BY c DESC LIMIT 20""", paths,
        ).fetchall()
        expected = {(row["a"], row["b"], int(row["c"])) for row in expected_rows}
        actual = {(a, b, int(count)) for a, b, count in call_pattern.findall(page["md"] or "")}
        expected_sources = {f"wiki:files/{path.replace('/', '__')}" for path in paths}
        try:
            actual_sources = set(json.loads(page["sources"] or "[]"))
        except json.JSONDecodeError:
            actual_sources = set()
        module_scope_checked += 1
        if actual != expected or actual_sources != expected_sources:
            module_scope_ok = False
            errors.append(f"模块调用作用域不匹配:{module}")
    if out_dir:
        root = Path(out_dir).resolve()
        for page in current.values():
            source_path = root / f"{page['id']}.md"
            for target in re.findall(r"\[[^\]]+\]\(([^)#]+\.md)\)", page["md"] or ""):
                resolved = (source_path.parent / target).resolve()
                if root not in resolved.parents and resolved != root:
                    errors.append(f"链接越界:{page['id']}->{target}")
                elif not resolved.exists():
                    errors.append(f"链接断开:{page['id']}->{target}")
    return {
        "ok": not errors, "errors": errors,
        "pages": len(current),
        "module_scope_checked": module_scope_checked,
        "module_scope_ok": module_scope_ok,
        "mechanism_count": len(mechanism_ids),
        "mechanism_expected": expected_mechanisms,
        "mechanism_integrity_ok": len(mechanism_ids) == expected_mechanisms == len(set(mechanism_ids)),
        "file_coverage": conn.execute(
            "SELECT COUNT(*) FROM node WHERE kind='file' AND path IS NOT NULL"
        ).fetchone()[0],
    }


def _read_fence(line: str, fence: str | None) -> str | None:
    marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
    if marker and fence is None:
        return marker[1]
    if (marker and fence and marker[1][0] == fence[0]
            and len(marker[1]) >= len(fence) and not marker[2].strip()):
        return None
    return fence


def _readable_sections(markdown: str) -> list[tuple[str, str]]:
    """Find real level-two headings, never headings inside code/JSON fences."""
    lines = markdown.splitlines(keepends=True)
    headings, fence = [], None
    for index, line in enumerate(lines):
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if fence is None and heading:
            headings.append((index, heading[1]))
        fence = _read_fence(line, fence)
    return [(title, "".join(lines[index + 1:headings[n + 1][0] if n + 1 < len(headings) else len(lines)]).strip())
            for n, (index, title) in enumerate(headings)]


def _section_read_blocks(markdown: str) -> list[str]:
    """Lossless paragraphs/list entries; fences and whole mechanism entries stay atomic.

    Section headings remain attached to the first body block. A mechanism's
    claim, conditions and boundary remain together through its next anchor or
    level-three heading. No sentence-length/byte slicing is used.
    """
    cuts, position, fence = [0], 0, None
    content, blank, list_item, mechanism, anchor = False, False, False, False, False
    paragraph_done = False
    for line in markdown.splitlines(keepends=True):
        if fence is None:
            new_anchor = bool(re.match(r'^<a\s+id=["\']mechanism-', line))
            subheading = bool(re.match(r"^###\s+", line))
            bullet = bool(re.match(r"^(?:[-+*]|\d+[.)])\s+", line))
            boundary = ((new_anchor or (subheading and not anchor)) and content)
            if not mechanism and line.strip() and content and ((blank and paragraph_done) or (bullet and list_item)):
                boundary = True
            if boundary:
                cuts.append(position)
                content, list_item = False, False
            if new_anchor or subheading:
                mechanism = True
                anchor = new_anchor
            elif line.strip():
                anchor = False
            blank = not line.strip()
            if bullet:
                list_item = True
            if line.strip() and not new_anchor and not re.match(r"^#{2,6}\s+", line):
                content = True
        fence = _read_fence(line, fence)
        if line.strip():
            # A visual blank line alone is not proof that a conditional
            # sentence ended. Keep unfinished prose with its following block.
            paragraph_done = bool(re.search(r"[.!?。！？][*_`]*$", line.rstrip())
                                  or re.match(r"^ {0,3}(?:`{3,}|~{3,})\s*$", line))
        position += len(line)
    return [markdown[start:end] for start, end in zip(cuts, cuts[1:] + [len(markdown)])]


def _wiki_read_oversize(tool: str, arguments: dict, required_budget: int) -> dict:
    continuation = ({"tool": tool, "arguments": {**arguments, "budget": required_budget}}
                    if required_budget <= 8000 else None)
    return {"error": tool + "_block_oversize", "status": "oversize",
            "required_budget": required_budget, "maximum_budget": 8000,
            "continuation": continuation, "next_offset": None, "truncated": True}


def outline(conn: sqlite3.Connection, module: str | None = None, *, offset: int = 0, budget: int = 2000) -> dict:
    """Return only current Wiki structure; stale/failed pages are never exposed."""
    rows = conn.execute(
        "SELECT id,level,title,md,input_hash,repository,revision FROM wiki_page "
        "WHERE status='ok' ORDER BY CASE level WHEN 'repo' THEN 0 WHEN 'module' THEN 1 ELSE 2 END,id"
    ).fetchall()
    pages = []
    for row in rows:
        if module and module.lower() not in (row["id"] + " " + row["title"]).lower():
            continue
        sections = [title for title, _ in _readable_sections(row["md"] or "")]
        if not sections:
            continue
        pages.append({"page_id": row["id"], "level": row["level"],
                      "title": row["title"], "sections": sections,
                      "section_ids": {title: _hash(row["id"], title)[:16] for title in sections}})
    base = {"pages": [], "wiki_unavailable": not bool(pages),
            "next_offset": None, "truncated": False, "pagination_version": "wiki-blocks-v1",
            "knowledge_set_id": dbm.get_meta(conn, "knowledge_set_id"),
            "build_config_hash": dbm.get_meta(conn, "build_config_hash", "")}
    if type(offset) is not int or offset < 0 or type(budget) is not int or budget < 1:
        return {**base, "error": "wiki_outline_invalid_pagination"}
    selected = []
    for item in pages[offset:]:
        needed = (len(json.dumps(selected + [item], ensure_ascii=False, separators=(",", ":")).encode()) + 2) // 3
        if needed > min(budget, 8000):
            if not selected:
                arguments = {"offset": offset, **({"module": module} if module else {})}
                return {**base, **_wiki_read_oversize("wiki_outline", arguments, needed)}
            break
        selected.append(item)
    next_offset = offset + len(selected)
    if not selected:
        return {**base, "error": "wiki_outline_empty"}
    return {**base, "pages": selected,
            "next_offset": next_offset if next_offset < len(pages) else None,
            "truncated": next_offset < len(pages)}


def section(conn: sqlite3.Connection, page_id: str, section_name: str, *, offset: int = 0, budget: int = 2000) -> dict:
    row = conn.execute(
        "SELECT * FROM wiki_page WHERE id=? AND status='ok'", (page_id,)
    ).fetchone()
    if row is None:
        return {"error": "wiki_page_unavailable", "page_id": page_id}
    wanted = section_name.removeprefix("##").strip()
    chunks = _readable_sections(row["md"] or "")
    selected = None
    for heading, body in chunks:
        if heading.strip() == wanted or wanted.lower() in heading.lower() or _hash(page_id, heading.strip())[:16] == wanted:
            if not body.strip():
                return {"error": "wiki_section_empty", "page_id": page_id, "section": wanted, "markdown": ""}
            selected = f"## {heading.strip()}\n{body.strip()}"
            break
    if selected is None:
        return {"error": "wiki_section_not_found", "page_id": page_id,
                "available": [heading for heading, _ in chunks]}
    citation = {
        "tag": "C1", "level": "C", "uid": f"wiki:{page_id}",
        "title": f"{row['title']} / {wanted}", "source_ref": page_id,
        "provenance": {
            "source_type": "generated_wiki", "repository": row["repository"],
            "revision": row["revision"], "page_id": page_id,
            "input_hash": row["input_hash"], "stale": False,
            "build_config_hash": dbm.get_meta(conn, "build_config_hash", ""),
        },
    }
    base = {"page_id": page_id, "section": wanted, "markdown": "", "next_offset": None,
            "truncated": False, "pagination_version": "wiki-blocks-v1",
            "knowledge_set_id": dbm.get_meta(conn, "knowledge_set_id"), "citations": [citation]}
    if type(offset) is not int or offset < 0 or type(budget) is not int or budget < 1:
        return {**base, "error": "wiki_section_invalid_pagination"}
    blocks = _section_read_blocks(selected)
    offsets = [0]
    for block in blocks:
        offsets.append(offsets[-1] + len(block.encode()))
    if offset not in offsets:
        safe_offset = max(start for start in offsets[:-1] if start <= offset)
        return {**base, "error": "wiki_section_invalid_offset", "continuation": {
            "tool": "wiki_section", "arguments": {"page_id": page_id, "section": wanted,
                                                      "offset": safe_offset, "budget": min(budget, 8000)}}}
    if offset == offsets[-1]:
        return {**base, "error": "wiki_section_empty_page"}
    parts, used = [], 0
    for block in blocks[offsets.index(offset):]:
        size = len(block.encode())
        if used + size > min(budget, 8000) * 3:
            if not parts:
                arguments = {"page_id": page_id, "section": wanted, "offset": offset}
                return {**base, **_wiki_read_oversize("wiki_section", arguments, (size + 2) // 3)}
            break
        parts.append(block)
        used += size
    end = offset + used
    return {**base, "markdown": "".join(parts),
            "next_offset": end if end < offsets[-1] else None, "truncated": end < offsets[-1]}


def export(conn: sqlite3.Connection, out_dir: str) -> int:
    """导出 CodeAtlas 的文件/模块/仓库页面；不是 raw/wiki/schema 三层。"""
    root = Path(out_dir)
    (root / "files").mkdir(parents=True, exist_ok=True)
    (root / "modules").mkdir(parents=True, exist_ok=True)

    pages = conn.execute("SELECT * FROM wiki_page ORDER BY level, id").fetchall()
    for p in pages:
        fm = ("---\n"
              f"level: {p['level']}\n"
              f"title: {p['title']}\n"
              f"status: {p['status']}\n"
              f"version: {p['version']}\n"
              f"repository: {p['repository'] or ''}\n"
              f"revision: {p['revision'] or ''}\n"
              f"input_hash: {p['input_hash'] or ''}\n"
              f"sources: {p['sources']}\n"
              f"generated_at: {p['updated_at']}\n"
              "---\n\n")
        target = root / f"{p['id']}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(fm + (p["md"] or ""), encoding="utf-8")

    idx = ["# index", "", "由 CodeAtlas 生成。事实层只读，本目录可重建。", ""]
    for lvl in ("repo", "module", "file"):
        sub = [p for p in pages if p["level"] == lvl]
        if sub:
            idx += [f"## {lvl} ({len(sub)})", ""]
            idx += [f"- [[{p['id']}]] {p['title']}"
                    + ("  ⚠️failed" if p["status"] != "ok" else "") for p in sub[:200]]
            idx.append("")
    (root / "index.md").write_text("\n".join(idx), encoding="utf-8")

    (root / "log.md").write_text(
        "# log\n\n" + "\n".join(
            f"- {p['updated_at']}  {p['id']}  v{p['version']}  {p['status']}"
            for p in pages), encoding="utf-8")
    return len(pages)
