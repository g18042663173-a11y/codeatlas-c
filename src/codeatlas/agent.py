"""Model-first, constrained diagnostic Agent over trusted CodeAtlas facts."""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import conversation
from . import db as dbm
from .conversation import get_session
from .contracts import (AGENT_TOOL_CONTRACT, agent_action_contract, budget_json_messages,
                        planner_diagnostics, tool_continuation, validate_agent_action)
from .experience import store
from .graph import traverse
from .retrieve import engine
from .wiki import generator as wiki_generator

ALLOWED_TOOLS = set(AGENT_TOOL_CONTRACT)
MAX_TOOL_CALLS = 6
PROMPT_VERSION = "agent-block-context-v4"
AGENT_MODES = {"auto", "model", "rule"}
_TAGS = re.compile(r"\[([ABC]\d+)\]")


class AgentError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _symbols(query: str) -> list[str]:
    return re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", query)[:2]


def _requires_wiki(query: str) -> bool:
    lowered = query.lower()
    structural = ("定位", "定义在哪", "在哪里定义", "caller", "callee", "调用者", "调用链",
                  "影响范围", "impact", "谁调用", "where is")
    return not any(token in lowered for token in structural)


def _phase(tool: str) -> str:
    if tool in ("search_evidence", "wiki_outline"):
        return "discover"
    if tool == "wiki_section":
        return "understand"
    return "verify"


def _rule_plan(conn: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    symbols = _symbols(query)
    if _requires_wiki(query):
        plan.extend([
            {"tool": "search_evidence", "arguments": {"query": query}},
            {"tool": "wiki_outline", "arguments": {}},
        ])
        if conn.execute("SELECT 1 FROM wiki_page WHERE status='ok' LIMIT 1").fetchone():
            page_id, section_name = "repo", "职责与边界"
            if symbols:
                row = conn.execute(
                    "SELECT path FROM node WHERE name=? AND path IS NOT NULL ORDER BY is_definition DESC LIMIT 1",
                    (symbols[0],),
                ).fetchone()
                if row:
                    candidate = "files/" + row["path"].replace("/", "__")
                    if conn.execute(
                        "SELECT 1 FROM wiki_page WHERE id=? AND status='ok'", (candidate,)
                    ).fetchone():
                        page_id = candidate
                        section_name = ("条件与错误路径" if any(
                            token in query.lower() for token in ("失败", "错误", "排障", "异常", "error")
                        ) else "职责与边界")
            plan.append({"tool": "wiki_section", "arguments": {
                "page_id": page_id, "section": section_name,
            }})
    else:
        plan.append({"tool": "search_evidence", "arguments": {"query": query}})
    for symbol in symbols[:1]:
        plan.append({"tool": "resolve_symbol", "arguments": {"symbol": symbol}})
        if _requires_wiki(query):
            plan.append({"tool": "code_read", "arguments": {"usr": symbol}})
        if any(token in query.lower() for token in ("影响", "调用", "impact", "caller", "call chain")):
            plan.append({"tool": "analyze_impact", "arguments": {"symbol": symbol, "depth": 2}})
    return plan[:MAX_TOOL_CALLS]


def _validate_call(raw: Any) -> dict[str, Any]:
    action = _validate_action(raw)
    if action["action"] != "tool":
        raise AgentError("工具调用不能是 finish")
    return {"tool": action["tool"], "arguments": action["arguments"]}


def _validate_action(raw: Any) -> dict[str, Any]:
    try:
        action = validate_agent_action(raw)
    except ValueError as exc:
        raise AgentError(str(exc)) from exc
    if action.get("tool") == "analyze_impact":
        action["arguments"].setdefault("depth", 2)
    return action


def _execute(conn: sqlite3.Connection, call: dict[str, Any], *, embedder,
             data_dir: str) -> dict[str, Any]:
    tool, args = call["tool"], call["arguments"]
    if tool == "search_evidence":
        result = engine.ask(conn, args["query"], embedder=embedder,
                            data_dir=data_dir, llm_client=None)
        return {
            "refused": result["refused"], "ab_evidence": result["ab_evidence"],
            "citations": result["citations"][:8],
            "context": result["context"],
        }
    if tool == "wiki_outline":
        return wiki_generator.outline(conn, **args)
    if tool == "wiki_section":
        return wiki_generator.section(conn, args["page_id"], args["section"],
                                      offset=args.get("offset", 0), budget=args.get("budget", 2000))
    if tool == "code_read":
        return _code_read(conn, args)
    if tool == "resolve_symbol":
        rows = traverse.resolve_symbol(conn, args["symbol"])
        return {"matches": [
            {key: row[key] for key in (
                "id", "name", "usr", "path", "line_start", "line_end", "definition_hash"
            )}
            for row in rows[:5]
        ]}
    rows = traverse.resolve_symbol(conn, args["symbol"])
    if not rows:
        return {"error": "symbol_not_found", "symbol": args["symbol"]}
    result = traverse.impact(conn, rows[0]["id"], max_hop=args["depth"])
    evidence_nodes = [dict(rows[0])]
    evidence_nodes.extend(item for items in result["by_hop"].values() for item in items)
    return {
        "symbol": args["symbol"], "by_hop": result["by_hop"],
        "candidate_impact": result["candidate_impact"],
        "affected_files": result["affected_files"],
        "evidence_nodes": evidence_nodes,
    }


def _code_read(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    repo = Path(dbm.get_meta(conn, "repo", ".")).resolve()
    row = None
    if args.get("usr"):
        row = conn.execute(
            """SELECT * FROM node WHERE kind='function' AND is_definition=1
                 AND (usr=? OR name=?) ORDER BY CASE WHEN usr=? THEN 0 ELSE 1 END,path LIMIT 1""",
            (args["usr"], args["usr"], args["usr"]),
        ).fetchone()
        if row is None:
            return {"error": "symbol_not_found", "usr": args["usr"]}
        rel, start, end = row["path"], row["line_start"], row["line_end"]
    else:
        rel, start, end = args["path"], args["line_start"], args["line_end"]
    target = (repo / rel).resolve()
    from .parser.languages import is_readable_source
    if repo not in target.parents or not is_readable_source(target):
        raise AgentError("code_read 只能读取当前仓库内已解析的 C/C++ 源码")
    if not conn.execute("SELECT 1 FROM node WHERE kind='file' AND path=?", (rel,)).fetchone():
        raise AgentError("code_read 路径不属于当前解析快照")
    if end - start + 1 > 300:
        # A USR may name a definition larger than one read. Do not silently
        # truncate it or lose its location in a generic exception.
        return {"error": "code_read_range_oversize", "status": "oversize",
                "path": rel, "line_start": start, "line_end": end,
                "continuation": {"tool": "code_read", "arguments": {
                    "path": rel, "line_start": start, "line_end": start + 299}}}
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    if start > len(lines):
        return {"error": "line_out_of_range", "path": rel, "line_count": len(lines)}
    end = min(end, len(lines))
    repository = dbm.get_meta(conn, "repository_id", str(repo))
    revision = dbm.get_meta(conn, "revision", "unversioned")
    citation = {
        "tag": "A1", "level": "A", "uid": f"code-range:{rel}:{start}:{end}",
        "title": f"{rel}:L{start}-L{end}", "source_ref": rel,
        "provenance": {"source_type": "source_range", "repository": repository,
                       "revision": revision, "usr": row["usr"] if row else None,
                       "symbol": row["name"] if row else None, "path": rel,
                       "line_start": start, "line_end": end,
                       "definition_hash": row["definition_hash"] if row else None,
                       "stale": False},
    }
    return {"path": rel, "line_start": start, "line_end": end,
            "text": "\n".join(f"{index:>5} | {lines[index - 1]}" for index in range(start, end + 1)),
            "citations": [citation]}


def _citation_tags(result: dict[str, Any]) -> list[str]:
    return [citation["tag"] for citation in result.get("citations", [])
            if citation.get("level") in ("A", "B", "C")]


def _node_citation(conn: sqlite3.Connection, raw: dict[str, Any], tag: str) -> dict[str, Any] | None:
    node_id = raw.get("id")
    row = conn.execute("SELECT * FROM node WHERE id=?", (node_id,)).fetchone() if node_id else None
    if row is None or row["kind"] != "function" or not row["is_definition"]:
        return None
    repository = dbm.get_meta(conn, "repository_id", dbm.get_meta(conn, "repo", "unversioned"))
    revision = dbm.get_meta(conn, "revision", "unversioned")
    return {
        "tag": tag, "level": "A", "uid": f"code:{row['id']}", "node_id": row["id"],
        "title": f"{row['name']} ({row['path']})", "source_ref": row["path"],
        "provenance": {
            "source_type": "compiler_ast", "repository": repository,
            "revision": revision, "usr": row["usr"], "symbol": row["name"],
            "path": row["path"], "line_start": row["line_start"],
            "line_end": row["line_end"], "definition_hash": row["definition_hash"],
            "stale": False,
        },
    }


def _build_registry(conn: sqlite3.Connection, evidence: dict[str, Any],
                    events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    registry = [dict(citation) for citation in evidence.get("citations", [])]
    known = {citation.get("uid") for citation in registry if citation.get("uid")}
    counters: dict[str, int] = defaultdict(int)
    for citation in registry:
        if citation.get("level") in ("A", "B", "C"):
            counters[citation["level"]] = max(
                counters[citation["level"]],
                int(re.sub(r"\D", "", citation.get("tag", "0")) or 0),
            )
    for event in events:
        result = event["result"]
        for raw_citation in result.get("citations", []):
            uid = raw_citation.get("uid")
            level = raw_citation.get("level")
            if not uid or uid in known or level not in ("A", "B", "C"):
                continue
            counters[level] += 1
            citation = dict(raw_citation)
            citation["tag"] = f"{level}{counters[level]}"
            registry.append(citation)
            known.add(uid)
        candidates = list(result.get("matches", [])) + list(result.get("evidence_nodes", []))
        for raw in candidates:
            uid = f"code:{raw.get('id')}" if raw.get("id") else None
            if not uid or uid in known:
                continue
            counters["A"] += 1
            citation = _node_citation(conn, raw, f"A{counters['A']}")
            if citation:
                registry.append(citation)
                known.add(uid)
    by_uid = {citation.get("uid"): citation["tag"] for citation in registry if citation.get("uid")}
    version = {key: dbm.get_meta(conn, key) for key in
               ("knowledge_set_id", "source_content_hash", "build_config_hash")}
    for citation in registry:
        citation.setdefault("provenance", {}).update(version)
    for event in events:
        event.update(version)
        for citation in event["result"].get("citations", []):
            if citation.get("uid") in by_uid:
                citation["tag"] = by_uid[citation["uid"]]
                citation.setdefault("provenance", {}).update(version)
        tags = {by_uid[citation.get("uid")] for citation in event["result"].get("citations", [])
                if citation.get("uid") in by_uid}
        for raw in list(event["result"].get("matches", [])) + list(event["result"].get("evidence_nodes", [])):
            tag = by_uid.get(f"code:{raw.get('id')}")
            if tag:
                tags.add(tag)
        event["citation_tags"] = sorted(tags)
    return registry


def _valid_answer(answer: str | None, citations: list[dict[str, Any]]) -> bool:
    tags = set(_TAGS.findall(answer or ""))
    allowed = {citation.get("tag") for citation in citations
               if citation.get("level") in ("A", "B", "C")}
    return bool(answer and answer.strip() and tags and tags <= allowed)


def _rule_draft(session: dict[str, Any], query: str,
                citations: list[dict[str, Any]]) -> dict[str, Any]:
    symbols = [citation.get("provenance", {}).get("symbol") for citation in citations]
    symbols = [symbol for symbol in symbols if symbol]
    return {
        "title": f"{query[:32]}：公开会话审查卡", "symptom": query,
        "hypotheses": ["先以编译器确认的函数定义与调用关系核对会话结论"],
        "dead_ends": ["仅凭会话文本或相似检索结果直接下结论"],
        "root_cause": "公开合成会话的候选结论；必须由审核者绑定源码锚点后确认。",
        "fix_steps": "定位相关函数，检查确定调用链与失败路径；补充审查意见。",
        "verification": "复现会话描述的输入并核对固定 revision 的函数行为。",
        "related_symbols": symbols[:3], "confidence": "medium",
        "evidence_tags": [c["tag"] for c in citations if c.get("level") in ("A", "B")][:8],
        "session_id": session["id"],
    }


def _valid_card_extraction(card: Any, citations: list[dict[str, Any]]) -> bool:
    if not isinstance(card, dict):
        return False
    ok, _ = store.validate(card)
    if not ok:
        return False
    tags = card.get("evidence_tags")
    allowed = {c["tag"] for c in citations if c.get("level") in ("A", "B")}
    return (isinstance(tags, list) and bool(tags)
            and all(isinstance(tag, str) and tag in allowed for tag in tags))


def _compact_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project event metadata, never slice a tool body or serialized JSON.

    agent-tool-result-v1 omits only planner_diagnostics from the persisted
    result. Replay pointers hash this projection, not the surrounding audit.
    """
    compact = []
    for event in events:
        result = deepcopy(event["result"])
        # Diagnostics belong to the local audit trace, not model evidence.
        result.pop("planner_diagnostics", None)
        compact.append({"tool": event["tool"], "phase": event.get("phase"),
                        "status": event.get("status"),
                        "result_projection": "agent-tool-result-v1",
                        "wiki_available": event.get("wiki_available"),
                        "wiki_required": event.get("wiki_required"),
                        "arguments": deepcopy(event["arguments"]),
                        "result": result})
    return compact


def _prompt_citation(citation):
    """Version is pinned out-of-band; omit duplicate hashes, never source evidence."""
    p = citation.get("provenance", {})
    return {"tag": citation["tag"], "title": citation.get("title"), "usr": p.get("usr"),
            "path": p.get("path", p.get("page_id")), "range": [p.get("line_start"), p.get("line_end")]}


def _model_action(client, query: str, events: list[dict[str, Any]], read_mode="progressive") -> dict[str, Any]:
    if hasattr(client, "next_action"):
        from .llm.client import OpenAICompatClient
        if isinstance(client, OpenAICompatClient):
            return _validate_action(client.next_action(query, _compact_events(events), read_mode=read_mode))
        return _validate_action(client.next_action(query, _compact_events(events)))
    if hasattr(client, "plan_tool"):
        return _validate_action(client.plan_tool(query, _compact_events(events)))
    raise AgentError("模型客户端不支持工具规划")


def _enforce_wiki_order(query: str, events: list[dict[str, Any]], action: dict[str, Any],
                        wiki_available: bool) -> None:
    contract = agent_action_contract(events, wiki_available=wiki_available,
                                    wiki_required=_requires_wiki(query))
    try:
        validate_agent_action(action, allowed_next=contract["allowed_next"])
    except ValueError as exc:
        raise AgentError("动作违反当前已完成工具的 Wiki-first 阶段约束") from exc


def _run_call(conn: sqlite3.Connection, call: dict[str, Any], *, embedder,
              data_dir: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = _execute(conn, call, embedder=embedder, data_dir=data_dir)
    except Exception as exc:
        return {**call, "phase": _phase(call["tool"]), "result": {"error": type(exc).__name__},
                "citation_tags": [], "status": "failed", "duration_ms": round((time.perf_counter()-started)*1000, 1)}
    if call["tool"].startswith("wiki_") and not result.get("error"):
        if call["tool"] == "wiki_outline":
            pages = result.get("pages")
            readable = isinstance(pages, list) and bool(pages) and all(
                isinstance(page, dict) and isinstance(page.get("page_id"), str) and page["page_id"].strip()
                and isinstance(page.get("sections"), list) and bool(page["sections"])
                and all(isinstance(title, str) and title.strip() for title in page["sections"])
                for page in pages)
        else:
            markdown = result.get("markdown")
            readable = isinstance(markdown, str) and bool(re.sub(r"(?m)^#{1,6}\s+.*$", "", markdown).strip())
        if not readable or result.get("wiki_unavailable") or result.get("status") in ("oversize", "failed"):
            result = {**result, "error": call["tool"] + "_unreadable"}
    continuation = tool_continuation(call["tool"], call["arguments"], result)
    if continuation:
        result = {**result, "continuation": continuation}
    return {**call, "phase": _phase(call["tool"]), "result": result,
            "citation_tags": [], "status": "failed" if result.get("error") else "completed",
            "duration_ms": round((time.perf_counter() - started) * 1000, 1)}


def _planner_diagnostic(client, *, error_class=None) -> dict:
    raw = getattr(client, "last_planner_diagnostics", {})
    raw = raw if isinstance(raw, dict) else {}
    return planner_diagnostics(content_hash=raw.get("content_hash"),
                               finish_reason=raw.get("finish_reason"),
                               error_class=error_class or raw.get("error_class"),
                               usage=raw.get("usage"))


def _persist_run(conn: sqlite3.Connection, *, run_id: str, session_id: str | None,
                 query: str, requested_mode: str, execution_mode: str,
                 fallback_reason: str | None, model: str | None, status: str,
                 answer: str | None, answer_source: str, refused: bool,
                 draft: dict[str, Any] | None, events: list[dict[str, Any]],
                 latency_ms: float, draft_id: str | None = None) -> None:
    repository = dbm.get_meta(conn, "repository_id", dbm.get_meta(conn, "repo", "unversioned"))
    revision = dbm.get_meta(conn, "revision", "unversioned")
    legacy_mode = "llm" if execution_mode == "model" else "rule"
    conn.execute(
        """INSERT INTO agent_run(
             id,session_id,query,repository,revision,mode,requested_mode,execution_mode,
             fallback_reason,model,prompt_ver,status,answer,answer_source,refused,
             draft_json,draft_id,latency_ms,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, session_id, query, repository, revision, legacy_mode, requested_mode,
         execution_mode, fallback_reason, model, PROMPT_VERSION, status, answer,
         answer_source, int(refused), _safe_json(draft) if draft else None,
         draft_id, latency_ms, _now()),
    )
    conn.executemany(
        """INSERT INTO agent_event(
             run_id,ordinal,tool_name,arguments,result_json,citation_tags,status,duration_ms,phase,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        [(run_id, index, event["tool"], _safe_json(event["arguments"]),
          _safe_json(event["result"]), _safe_json(event["citation_tags"]),
          event["status"], event["duration_ms"], event["phase"], _now())
         for index, event in enumerate(events, 1)],
    )
    conn.commit()


def run(conn: sqlite3.Connection, query: str, *, data_dir: str = "data", embedder=None,
        session_id: str | None = None, llm_client=None, save_draft: bool = False,
        mode: str = "auto", read_mode: str = "progressive") -> dict[str, Any]:
    if dbm.get_meta(conn, "snapshot_enabled") == "1":
        from .snapshots import pin
        from .indexer.build import get_embedder
        with pin(conn) as context:
            return _run(context.conn, query, data_dir=str(context.directory),
                        embedder=get_embedder("local", str(context.directory)), session_id=session_id,
                        llm_client=llm_client, save_draft=save_draft, mode=mode, read_mode=read_mode,
                        control_conn=conn)
    return _run(conn, query, data_dir=data_dir, embedder=embedder, session_id=session_id,
                llm_client=llm_client, save_draft=save_draft, mode=mode, read_mode=read_mode, control_conn=conn)


def _run(conn, query, *, data_dir, embedder, session_id, llm_client, save_draft,
         mode, read_mode, control_conn):
    started = time.perf_counter()
    if not query.strip():
        raise AgentError("query 不能为空")
    if mode not in AGENT_MODES:
        raise AgentError("mode 只能是 auto / model / rule")
    if read_mode not in ("flat", "progressive"):
        raise AgentError("read_mode 只能是 flat / progressive")
    session = get_session(control_conn, session_id) if session_id else None
    session_gate = conversation.assess_session(control_conn, session_id) if session_id else None
    preflight_start = time.perf_counter()
    evidence = engine.ask(conn, query, embedder=embedder, data_dir=data_dir, llm_client=None)
    model_name = getattr(llm_client, "model", None) if llm_client is not None else None
    model_capable = llm_client is not None and (
        hasattr(llm_client, "next_action") or hasattr(llm_client, "plan_tool")
    )
    execution_mode = "model" if mode != "rule" and model_capable else "rule"
    fallback_reason: str | None = None
    if mode != "rule" and not model_capable:
        fallback_reason = "model_not_configured"
    wiki_available = bool(conn.execute(
        "SELECT 1 FROM wiki_page WHERE status='ok' LIMIT 1"
    ).fetchone())
    if read_mode == "progressive" and _requires_wiki(query) and not wiki_available and fallback_reason is None:
        fallback_reason = "wiki_unavailable"

    preflight = {"tool": "search_evidence", "arguments": {"query": query},
                 "wiki_available": wiki_available,
                 "wiki_required": _requires_wiki(query),
                 "phase": "discover", "status": "completed", "citation_tags": [],
                 "duration_ms": round((time.perf_counter()-preflight_start)*1000, 1),
                 "result": {"refused": evidence["refused"], "ab_evidence": evidence["ab_evidence"],
                            "citations": evidence["citations"], "context": evidence.get("context", "")}}
    events: list[dict[str, Any]] = [preflight]
    planner_log: list[dict] = []
    if read_mode == "progressive" and _requires_wiki(query) and wiki_available:
        preflight["result"]["context"] = "Search discovery only; read Wiki sections then code_read for source text."
    if evidence["refused"]:
        execution_mode = "rule"
        fallback_reason = "no_ab_evidence"
    elif execution_mode == "model":
        seen_calls: set[str] = set()
        before_attempt = 1
        try:
            for _ in range(MAX_TOOL_CALLS - 1):
                before_attempt = len(events)
                try:
                    # Assign stable citation tags/identity before hashing a
                    # planner view; final persistence must not reidentify it.
                    _build_registry(conn, evidence, events)
                    action = _model_action(llm_client, query, events, read_mode)
                    contract = agent_action_contract(events, read_mode=read_mode,
                                                     wiki_available=wiki_available,
                                                     wiki_required=_requires_wiki(query))
                    validate_agent_action(action, allowed_next=contract["allowed_next"])
                except Exception as exc:
                    planner_log.append(_planner_diagnostic(llm_client, error_class=type(exc).__name__))
                    raise
                planner_log.append(_planner_diagnostic(llm_client))
                if action["action"] == "finish":
                    if not events:
                        raise AgentError("模型未调用任何证据工具就结束")
                    events[-1]["result"].setdefault("planner_diagnostics", []).append(planner_log[-1])
                    break
                call = {"tool": action["tool"], "arguments": action["arguments"]}
                fingerprint = _safe_json(call)
                if fingerprint in seen_calls:
                    raise AgentError("模型重复请求相同工具调用")
                seen_calls.add(fingerprint)
                events.append(_run_call(conn, call, embedder=embedder, data_dir=data_dir))
                events[-1]["result"]["planner_diagnostics"] = [planner_log[-1]]
        except Exception as exc:
            execution_mode = "rule"
            fallback_reason = f"model_plan_invalid:{type(exc).__name__}"
            if len(events) == before_attempt and len(events) < MAX_TOOL_CALLS:
                diagnostic = _planner_diagnostic(llm_client, error_class=type(exc).__name__)
                if planner_log:
                    planner_log[-1] = diagnostic
                failed_result = {"error": type(exc).__name__, "planner_diagnostics": [diagnostic]}
                from .contracts import ContextBudgetExceeded
                if isinstance(exc, ContextBudgetExceeded):
                    failed_result["context_budget"] = exc.details
                events.append({"tool": "planning_attempt", "arguments": {}, "phase": "discover",
                               "result": failed_result, "status": "rejected",
                               "citation_tags": [], "duration_ms": 0})
            remaining = MAX_TOOL_CALLS - len(events)
            for call in _rule_plan(conn, query):
                if remaining <= 0:
                    break
                if _safe_json(call) in seen_calls:
                    continue
                if call == {"tool": "search_evidence", "arguments": {"query": query}}:
                    continue
                if call["tool"] not in agent_action_contract(
                        events, read_mode=read_mode, wiki_available=wiki_available,
                        wiki_required=_requires_wiki(query))["allowed_next"]:
                    continue
                events.append(_run_call(conn, call, embedder=embedder, data_dir=data_dir))
                remaining -= 1
    else:
        for call in _rule_plan(conn, query):
            if call == {"tool": "search_evidence", "arguments": {"query": query}}:
                continue
            if len(events) >= MAX_TOOL_CALLS:
                break
            if call["tool"] not in agent_action_contract(
                    events, read_mode=read_mode, wiki_available=wiki_available,
                    wiki_required=_requires_wiki(query))["allowed_next"]:
                continue
            events.append(_run_call(conn, call, embedder=embedder, data_dir=data_dir))

    registry = _build_registry(conn, evidence, events)
    answer: str | None = None
    answer_source = "refusal" if evidence["refused"] else "evidence_pack"
    if not evidence["refused"] and execution_mode == "model" and hasattr(llm_client, "answer"):
        try:
            if hasattr(llm_client, "answer_structured"):
                candidate = llm_client.answer_structured(
                    engine.SYSTEM_PROMPT, query, _compact_events(events), registry)
            else:
                # Keep the legacy answer(system, question, context) API. Budget
                # its standard question wrapper too; the supplied context is
                # complete JSON and never a fragment of a serialized object.
                prefix = f"问题：{query}\n\n证据：\n"
                _, context, _ = budget_json_messages(
                    engine.SYSTEM_PROMPT + prefix,
                    {"query": query, **agent_action_contract(events, read_mode="flat"),
                     "evidence": registry, "prior_events": _compact_events(events)})
                candidate = llm_client.answer(engine.SYSTEM_PROMPT, query, context)
            from .contracts import answer_verification
            if not answer_verification(candidate, registry, conn)["citation_valid"]:
                raise AgentError("模型回答未通过引用校验")
            answer = candidate
            answer_source = "model"  # Expression only; semantic validation remains separate below.
        except Exception as exc:
            execution_mode = "rule"
            fallback_reason = f"model_answer_invalid:{type(exc).__name__}"

    draft_preview = None
    card_warning = None
    if session and session_gate and session_gate["eligible"] and not evidence["refused"]:
        draft_preview = _rule_draft(session, query, registry)
        if execution_mode == "model" and hasattr(llm_client, "extract_knowledge_card"):
            try:
                extracted = llm_client.extract_knowledge_card(session["messages"], store.EXTRACT_SCHEMA)
                if not _valid_card_extraction(extracted, registry):
                    raise AgentError("知识卡未通过字段或引用校验")
                draft_preview = {**extracted, "session_id": session["id"]}
            except Exception as exc:
                card_warning = f"model_card_fallback:{type(exc).__name__}"

    run_id = "run-" + uuid.uuid4().hex[:12]
    status = "refused" if evidence["refused"] else "completed"
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    _persist_run(
        control_conn, run_id=run_id, session_id=session_id, query=query,
        requested_mode=mode, execution_mode=execution_mode,
        fallback_reason=fallback_reason, model=model_name, status=status,
        answer=answer, answer_source=answer_source, refused=evidence["refused"],
        draft=draft_preview, events=events, latency_ms=latency_ms,
    )
    from .contracts import answer_verification
    verification = answer_verification(answer, registry, conn) if answer else {
        "citation_valid": True, "fact_validation": "evidence_only", "answer_correctness": "not_generated", "trusted": False}
    set_id = dbm.get_meta(conn, "knowledge_set_id")
    source_hash = dbm.get_meta(conn, "source_content_hash")
    control_conn.execute("UPDATE agent_run SET knowledge_set_id=?,source_content_hash=?,verification_json=?,revision=?,repository=? WHERE id=?",
                         (set_id, source_hash, _safe_json(verification), dbm.get_meta(conn, "revision"),
                          dbm.get_meta(conn, "repository_id"), run_id))
    control_conn.commit()
    draft_id = save_run_draft(control_conn, run_id)["id"] if save_draft and draft_preview else None
    legacy_mode = "llm" if execution_mode == "model" else "rule"
    return {
        "id": run_id, "mode": legacy_mode, "requested_mode": mode,
        "execution_mode": execution_mode, "fallback_reason": fallback_reason,
        "model": model_name, "prompt_version": PROMPT_VERSION,
        "status": status, "events": events, "evidence": evidence,
        "evidence_registry": registry, "answer": answer,
        "answer_source": answer_source, "session_gate": session_gate,
        "card_warning": card_warning, "draft_preview": draft_preview,
        "draft_id": draft_id, "latency_ms": latency_ms,
        "knowledge_set_id": set_id, "source_content_hash": source_hash, "read_mode": read_mode,
        "verification": verification, "tool_attempts": len(events),
        "planner_diagnostics": planner_log,
    }


def save_run_draft(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """Persist an already-previewed card without executing the Agent again."""
    row = conn.execute("SELECT * FROM agent_run WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(f"Agent run 不存在: {run_id}")
    if row["draft_id"]:
        card = store.get(conn, row["draft_id"])
        return {"id": card["id"], "status": card["status"], "saved": False}
    if row["status"] != "completed" or row["refused"]:
        raise AgentError("拒答或失败的 Agent run 不能生成知识卡草稿")
    if not row["session_id"] or not row["draft_json"]:
        raise AgentError("此 Agent run 没有可保存的会话知识卡预览")
    try:
        preview = json.loads(row["draft_json"])
    except json.JSONDecodeError as exc:
        raise AgentError("此 Agent run 的草稿预览已损坏") from exc
    if not isinstance(preview, dict):
        raise AgentError("此 Agent run 的草稿预览格式无效")
    session = get_session(conn, row["session_id"])
    if not session.get("curation_goal"):
        conversation.set_curation_goal(conn, session["id"], f"沉淀：{row['query'][:80]}")
    ordinal = conn.execute(
        "SELECT COALESCE(MAX(ordinal),0)+1 FROM curation_candidate WHERE session_id=?",
        (session["id"],),
    ).fetchone()[0]
    candidate_id = "cand-" + uuid.uuid4().hex[:12]
    encoded = _safe_json(preview)
    conn.execute(
        """INSERT INTO curation_candidate
           (id,session_id,ordinal,payload_json,original_json,status,model,prompt_ver,created_at,updated_at)
           VALUES(?,?,?,?,?,'proposed',?,?,?,?)""",
        (candidate_id, session["id"], ordinal, encoded, encoded,
         row["model"] or row["mode"], "agent-preview-v1", _now(), _now()),
    )
    conn.commit()
    decision = conversation.decide_candidate(conn, candidate_id, "accept")
    card_id = decision["card_id"]
    conn.execute("UPDATE agent_run SET draft_id=? WHERE id=?", (card_id, run_id))
    conn.commit()
    return {"id": card_id, "status": "pending", "saved": True}


save_draft = save_run_draft


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM agent_run WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(f"Agent run 不存在: {run_id}")
    events = conn.execute(
        "SELECT * FROM agent_event WHERE run_id=? ORDER BY ordinal", (run_id,)
    ).fetchall()
    result = dict(row)
    if result.get("draft_json"):
        try:
            result["draft_preview"] = json.loads(result["draft_json"])
        except json.JSONDecodeError:
            result["draft_preview"] = None
    else:
        result["draft_preview"] = None
    feedback = [dict(item) for item in conn.execute(
        "SELECT * FROM agent_feedback WHERE run_id=? ORDER BY id", (run_id,)
    ).fetchall()]
    return {**result, "events": [
        {**dict(event), "arguments": json.loads(event["arguments"]),
         "result": json.loads(event["result_json"]),
         "citation_tags": json.loads(event["citation_tags"])}
        for event in events
    ], "feedback": feedback}


def add_feedback(conn: sqlite3.Connection, run_id: str, verdict: str, *,
                 evidence_tag: str | None = None, comment: str = "") -> dict[str, Any]:
    """Record user feedback without mutating knowledge or retrieval state."""
    if verdict not in {"helpful", "incorrect", "incomplete"}:
        raise AgentError("verdict 只能是 helpful / incorrect / incomplete")
    if not conn.execute("SELECT 1 FROM agent_run WHERE id=?", (run_id,)).fetchone():
        raise KeyError(f"Agent run 不存在: {run_id}")
    tag = (evidence_tag or "").strip() or None
    if tag:
        allowed: set[str] = set()
        for row in conn.execute(
            "SELECT citation_tags FROM agent_event WHERE run_id=?", (run_id,)
        ):
            try:
                allowed.update(json.loads(row["citation_tags"]))
            except (TypeError, json.JSONDecodeError):
                continue
        if tag not in allowed:
            raise AgentError("反馈证据标签不属于本次 Agent 运行")
    note = comment.strip()
    if len(note) > 2000:
        raise AgentError("反馈说明超过 2000 字符上限")
    created_at = _now()
    cursor = conn.execute(
        """INSERT INTO agent_feedback(run_id,verdict,evidence_tag,comment,created_at)
           VALUES(?,?,?,?,?)""", (run_id, verdict, tag, note, created_at)
    )
    conn.commit()
    return {
        "id": cursor.lastrowid, "run_id": run_id, "verdict": verdict,
        "evidence_tag": tag, "comment": note, "created_at": created_at,
    }


def list_feedback(conn: sqlite3.Connection, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT f.*,r.query,r.repository,r.revision
             FROM agent_feedback f JOIN agent_run r ON r.id=f.run_id
            ORDER BY f.id DESC LIMIT ?""", (max(1, min(limit, 500)),)
    ).fetchall()
    return [dict(row) for row in rows]
