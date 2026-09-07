"""Optional real-model evaluation for the constrained Agent protocol.

The public benchmark measures whether a configured OpenAI-compatible model can use
the local read-only tool protocol without bypassing CodeAtlas safety gates.  It does
not grade prose style or treat model output as ground truth.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import agent, db as dbm
from . import task_eval

_TAGS = re.compile(r"\[([ABC]\d+)\]")
ABC_VARIANTS = {
    "A": "Agent + Code",
    "B": "Agent + 固定 Skill + Code",
    "C": "Agent + CodeAtlas + Code",
}
_BASELINE_STOP = {
    "the", "and", "for", "with", "where", "which", "what", "how", "json",
    "tcp", "函数", "哪里", "如何", "哪些", "怎样", "实现", "入口", "定义",
}
BASELINE_TOOLS = {"repo_search", "code_read"}
BASELINE_MAX_TOOL_CALLS = agent.MAX_TOOL_CALLS
BASELINE_CODE_READ_MAX_LINES = 300


class ModelEvalError(ValueError):
    pass


def _percent(value: int, total: int) -> float:
    return round(value / total * 100, 1) if total else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * percentile) - 1)], 1)


def _citation_safe(run: dict[str, Any]) -> bool:
    if run["answer_source"] == "model":
        tags = set(_TAGS.findall(run.get("answer") or ""))
        allowed = {
            citation.get("tag") for citation in run.get("evidence_registry", [])
            if citation.get("level") in ("A", "B", "C")
        }
        return bool(tags) and tags <= allowed
    if run["status"] == "refused":
        return (run["answer_source"] == "refusal"
                and run["evidence"]["ab_evidence"] == 0
                and not run.get("answer"))
    # A visible deterministic fallback returns the evidence pack without inventing
    # model prose; this is a safe final state even though native planning failed.
    return run["answer_source"] == "evidence_pack" and not run.get("answer")


def _task_hash(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def evaluate(conn: sqlite3.Connection, tasks_path: str, *, client,
             data_dir: str = "data", embedder=None,
             require_approved: bool = True) -> dict[str, Any]:
    if dbm.get_meta(conn, "snapshot_enabled") == "1":
        from ..snapshots import pin
        from ..indexer.build import get_embedder
        with pin(conn) as context:
            isolated = sqlite3.connect(":memory:")
            isolated.row_factory = sqlite3.Row
            context.conn.backup(isolated)
            try:
                return evaluate(isolated, tasks_path, client=client, data_dir=str(context.directory),
                                embedder=get_embedder("local", str(context.directory)), require_approved=require_approved)
            finally:
                isolated.close()
    if client is None:
        raise ModelEvalError("未配置 OpenAI-compatible 模型；请设置 LLM_API_KEY")
    manifest = task_eval.load_task_set(
        conn, tasks_path, require_approved=require_approved,
    )
    if hasattr(client, "reset_metrics"):
        client.reset_metrics()

    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    native_total = 0
    native_success = 0
    safety_success = 0
    legal_success = 0
    for task in manifest["tasks"]:
        started = time.perf_counter()
        run = agent.run(
            conn, task["question"], data_dir=data_dir, embedder=embedder,
            llm_client=client, mode="model",
        )
        latency = round((time.perf_counter() - started) * 1000, 1)
        latencies.append(latency)
        legal = (
            len(run["events"]) <= agent.MAX_TOOL_CALLS
            and all(event["tool"] in agent.ALLOWED_TOOLS for event in run["events"])
        )
        safe = legal and _citation_safe(run)
        expected_refusal = task["type"] == task_eval.REFUSAL_TYPE
        native = False
        if not expected_refusal:
            native_total += 1
            native = (
                run["execution_mode"] == "model"
                and run["answer_source"] == "model"
                and not run["fallback_reason"]
            )
            native_success += int(native)
        legal_success += int(legal)
        safety_success += int(safe)
        rows.append({
            "id": task["id"], "type": task["type"],
            "execution_mode": run["execution_mode"],
            "answer_source": run["answer_source"],
            "fallback_reason": run["fallback_reason"],
            "tool_calls": len(run["events"]), "tool_trace_legal": legal,
            "citation_gate_safe": safe, "native_success": native,
            "latency_ms": latency,
        })

    total = len(rows)
    native_rate = _percent(native_success, native_total)
    safety_rate = _percent(safety_success, total)
    result = {
        "repository": manifest["repository"], "revision": manifest["revision"],
        "task_set": manifest["name"], "task_set_hash": _task_hash(tasks_path),
        "model": getattr(client, "model", "openai-compatible"),
        "prompt_version": agent.PROMPT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tasks": rows,
        "metrics": {
            "task_count": total, "native_task_count": native_total,
            "native_planning_success_rate": native_rate,
            "fallback_rate": _percent(
                sum(bool(row["fallback_reason"]) for row in rows), total,
            ),
            "tool_trace_legal_rate": _percent(legal_success, total),
            "final_safety_gate_rate": safety_rate,
            "p50_ms": _percentile(latencies, .50),
            "p95_ms": _percentile(latencies, .95),
            "requests": getattr(client, "request_count", None),
            "usage": dict(getattr(client, "usage_totals", {}) or {}),
            "cost_usd": getattr(client, "measured_cost_usd", None),
            "cost_upper_bound_usd": getattr(client, "reserved_cost_usd", None),
        },
    }
    result["acceptance"] = {
        "native_planning_min": 90.0, "final_safety_min": 100.0,
        "passed": native_rate >= 90.0 and safety_rate == 100.0,
    }
    from . import rubric
    result["identity"] = rubric.identity(conn, tasks_path)
    return result


def _direct_code_search(conn: sqlite3.Connection, question: str, *, limit: int = 8) -> list[dict[str, Any]]:
    """Dependency-free source baseline used only by the optional A/B/C comparison.

    It reads public ``.c/.h`` files directly and does not use Wiki, graph, cards,
    FTS or vectors.  This keeps A/B honest: the only difference between them is
    the fixed reasoning instruction, while C gets the CodeAtlas evidence layer.
    """
    from ..retrieve.engine import _enrich_query

    repo = Path(dbm.get_meta(conn, "repo", ".")).expanduser().resolve()
    terms = {
        token.lower() for token in re.findall(
            r"[A-Za-z_][A-Za-z0-9_]{1,}|[\u4e00-\u9fff]{2,}",
            _enrich_query(question),
        )
        if token.lower() not in _BASELINE_STOP and len(token) >= 2
    }
    if not terms or not repo.is_dir():
        return []
    scored: list[tuple[float, str, int, int, str]] = []
    for path in sorted((*repo.rglob("*.c"), *repo.rglob("*.h"))):
        try:
            relative = path.resolve().relative_to(repo).as_posix()
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, ValueError):
            continue
        for start in range(0, len(lines), 60):
            block = "\n".join(lines[start:start + 90])
            lowered = block.lower()
            matched = [term for term in terms if term in lowered]
            if not matched:
                continue
            exact_ident = max(
                (3.0 for term in matched if "_" in term and re.search(
                    rf"\b{re.escape(term)}\b", lowered
                )), default=0.0,
            )
            score = len(matched) / len(terms) + exact_ident
            scored.append((score, relative, start + 1, min(len(lines), start + 90), block))
    scored.sort(key=lambda row: (-row[0], row[1], row[2]))
    return [
        {
            "tag": f"A{index}", "level": "A", "path": path,
            "line_start": start, "line_end": end, "text": text,
        }
        for index, (_score, path, start, end, text) in enumerate(scored[:limit], 1)
    ]


def _baseline_code_read(conn: sqlite3.Connection, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    repo = Path(dbm.get_meta(conn, "repo", ".")).expanduser().resolve()
    relative = str(arguments.get("path") or "").strip()
    if not relative or Path(relative).is_absolute():
        raise ModelEvalError("code_read path 必须是仓库内相对路径")
    target = (repo / relative).resolve()
    try:
        target.relative_to(repo)
    except ValueError as exc:
        raise ModelEvalError("code_read 路径越界") from exc
    if target.suffix.lower() not in {".c", ".h"} or not target.is_file():
        raise ModelEvalError("code_read 只允许当前仓库内的 .c/.h 文件")
    try:
        line_start = max(1, int(arguments.get("line_start") or 1))
        line_end = int(arguments.get("line_end") or line_start + 89)
    except (TypeError, ValueError) as exc:
        raise ModelEvalError("code_read 行号必须是整数") from exc
    line_end = max(line_start, min(line_end, line_start + BASELINE_CODE_READ_MAX_LINES - 1))
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    line_end = min(line_end, len(lines))
    if line_start > len(lines):
        return []
    return [{
        "level": "A", "path": relative, "line_start": line_start,
        "line_end": line_end, "text": "\n".join(lines[line_start - 1:line_end]),
    }]


def _register_baseline_evidence(registry: list[dict[str, Any]],
                                incoming: list[dict[str, Any]]) -> list[str]:
    known = {(row["path"], row["line_start"], row["line_end"]): row["tag"]
             for row in registry}
    tags: list[str] = []
    for item in incoming:
        key = (item["path"], item["line_start"], item["line_end"])
        tag = known.get(key)
        if tag is None:
            tag = f"A{len(registry) + 1}"
            row = {**item, "tag": tag}
            registry.append(row)
            known[key] = tag
        tags.append(tag)
    return tags


def _baseline_action(client, query: str, events: list[dict[str, Any]], *,
                     fixed_skill: bool) -> dict[str, Any]:
    if not hasattr(client, "next_baseline_action"):
        raise ModelEvalError("模型客户端未实现公平基线的迭代工具协议")
    action = client.next_baseline_action(query, events, fixed_skill=fixed_skill)
    if not isinstance(action, dict) or action.get("action") not in {"tool", "finish"}:
        raise ModelEvalError("模型返回了无效基线动作")
    if action["action"] == "finish":
        return action
    if action.get("tool") not in BASELINE_TOOLS or not isinstance(action.get("arguments"), dict):
        raise ModelEvalError("模型请求了非白名单基线工具")
    return action


def _baseline_once(conn: sqlite3.Connection, task: dict[str, Any], *, client,
                   fixed_skill: bool) -> dict[str, Any]:
    registry: list[dict[str, Any]] = []
    initial = _direct_code_search(conn, task["question"])
    initial_tags = _register_baseline_evidence(registry, initial)
    # Match CodeAtlas C's deterministic first discovery step.  Planning calls,
    # tool-attempt limits and answer budgets therefore start from the same point.
    events: list[dict[str, Any]] = [{
        "step": 1, "tool": "repo_search", "arguments": {"query": task["question"]},
        "citation_tags": initial_tags, "result": [dict(row) for row in initial[:8]],
        "preflight": True,
    }]
    before_tokens = int((getattr(client, "usage_totals", {}) or {}).get("total_tokens", 0))
    before_input = int((getattr(client, "usage_totals", {}) or {}).get("prompt_tokens", 0))
    started = time.perf_counter()
    fallback_reason = None
    native_planning = False
    if not registry:
        return {
            "refused": True, "answer": None, "citations": [],
            "tool_calls": len(events),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "tokens": 0, "events": events, "native_planning": False,
            "fallback_reason": "no_source_evidence", "input_tokens": 0,
        }
    try:
        for step in range(1, BASELINE_MAX_TOOL_CALLS):
            action = _baseline_action(
                client, task["question"], events, fixed_skill=fixed_skill,
            )
            if action["action"] == "finish":
                native_planning = True
                break
            tool = action["tool"]
            arguments = action["arguments"]
            if tool == "repo_search":
                query = str(arguments.get("query") or "").strip()
                if not query:
                    raise ModelEvalError("repo_search query 不能为空")
                found = _direct_code_search(conn, query)
            else:
                found = _baseline_code_read(conn, arguments)
            tags = _register_baseline_evidence(registry, found)
            events.append({
                "step": step + 1, "tool": tool, "arguments": arguments,
                "citation_tags": tags,
                "result": [dict(row) for row in found[:8]],
            })
        else:
            # Same as C: a legal sixth tool consumes the budget, then summarize.
            # No hidden seventh planning request is required to say finish.
            native_planning = True
    except Exception as exc:  # local safety fallback; do not execute an unsafe action
        fallback_reason = f"baseline_plan_invalid:{type(exc).__name__}"
        if len(events) < BASELINE_MAX_TOOL_CALLS:
            events.append({"tool": "planning_attempt", "status": "rejected", "arguments": {},
                           "result": {"error": type(exc).__name__}, "citation_tags": []})

    context = "\n\n".join(
        f"[{row['tag']}] {row['path']}:L{row['line_start']}-L{row['line_end']}\n{row['text']}"
        for row in registry
    )
    system = (
        "你是代码库助手。只能依据提供的源码片段回答，每个结论必须引用 [A#]。"
        "没有证据就明确拒答。"
    )
    if fixed_skill:
        system += (
            "固定工作法：先确认问题类型和入口，再核对定义、调用关系、分支与错误路径；"
            "区分已验证事实和推测，最后列出验证步骤。"
        )
    answer = None
    if fallback_reason is None:
        try:
            answer = client.answer(system, task["question"], context)
        except Exception as exc:
            fallback_reason = f"baseline_answer_invalid:{type(exc).__name__}"
    latency = round((time.perf_counter() - started) * 1000, 1)
    after_tokens = int((getattr(client, "usage_totals", {}) or {}).get("total_tokens", 0))
    return {
        "refused": False, "answer": answer, "citations": registry,
        "tool_calls": len(events), "latency_ms": latency,
        "tokens": max(0, after_tokens - before_tokens), "events": events,
        "native_planning": native_planning and fallback_reason is None,
        "fallback_reason": fallback_reason,
        "input_tokens": max(0, int((getattr(client, "usage_totals", {}) or {}).get("prompt_tokens", 0)) - before_input),
    }


def _abc_grade(task: dict[str, Any], run: dict[str, Any], *, variant: str) -> dict[str, Any]:
    citations = run.get("citations", [])
    tags = set(_TAGS.findall(run.get("answer") or ""))
    allowed = {citation.get("tag") for citation in citations}
    citation_valid = ((run.get("refused", False) and not citations)
                      or bool(tags) and tags <= allowed)
    if task["type"] == task_eval.REFUSAL_TYPE:
        return {"correct": bool(run.get("refused")), "complete": 1.0 if run.get("refused") else 0.0,
                "citation_valid": citation_valid}
    if task["type"] == task_eval.EXPERIENCE_TYPE:
        matched = any(
            citation.get("level") == "B"
            and citation.get("title") == task["expected_card_title"]
            for citation in citations
        )
        return {"correct": False, "complete": 0.0, "card_retrieval_hit": matched,
                "answer_verdict": "pending_human_review", "citation_valid": citation_valid}
    anchors = (task.get("expected_anchors") or task.get("expected_certain")
               or ([task["target"]] if task.get("target") else []))
    hits = 0
    for anchor in anchors:
        for citation in citations:
            provenance = citation.get("provenance") or citation
            if provenance.get("path") != anchor["path"]:
                continue
            line = anchor.get("line_start")
            if (not line or provenance.get("line_start", 0) <= line
                    <= provenance.get("line_end", 0)):
                hits += 1
                break
    complete = hits / len(anchors) if anchors else 0.0
    # Retrieval tasks accept any gold anchor; impact tasks require the full set.
    correct = complete == 1.0 if task["type"] == task_eval.IMPACT_TYPE else hits > 0
    # A gold anchor hit is recall, not proof of the model's assertion. Free-form
    # answers stay pending until a blinded human rubric is applied to this trial.
    return {"correct": False, "complete": 0.0, "citation_valid": citation_valid,
            "anchor_recall": complete, "retrieval_hit": correct,
            "answer_verdict": "pending_human_review"}


def _representative_tasks(tasks: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    """Select a stable, type-balanced smoke subset without changing gold data."""
    if limit is None:
        return list(tasks)
    if limit < 1 or limit > len(tasks):
        raise ModelEvalError(f"limit 必须在 1..{len(tasks)} 之间")
    groups: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        groups.setdefault(task["type"], []).append(task)
    selected: list[dict[str, Any]] = []
    offset = 0
    while len(selected) < limit:
        added = False
        for rows in groups.values():
            if offset < len(rows):
                selected.append(rows[offset])
                added = True
                if len(selected) == limit:
                    break
        if not added:
            break
        offset += 1
    return selected


def _value_smoke_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select two code, one experience and one refusal question per corpus."""
    code = [task for task in tasks
            if task["type"] not in {task_eval.EXPERIENCE_TYPE, task_eval.REFUSAL_TYPE}]
    semantic = [task for task in code if task["type"] == "语义检索"]
    experience = [task for task in tasks if task["type"] == task_eval.EXPERIENCE_TYPE]
    refusal = [task for task in tasks if task["type"] == task_eval.REFUSAL_TYPE]
    selected_code = semantic[:2] if len(semantic) >= 2 else code[:2]
    if len(selected_code) != 2 or not experience or not refusal:
        raise ModelEvalError("value smoke requires two code, one experience and one refusal task")
    selected_ids = {task["id"] for task in [*selected_code, experience[0], refusal[0]]}
    return [task for task in tasks if task["id"] in selected_ids]


def evaluate_abc(conn: sqlite3.Connection, tasks_path: str, *, client,
                 data_dir: str = "data", embedder=None, runs: int = 3,
                 require_approved: bool = True,
                 limit: int | None = None, seed: int = 17,
                 answer_key_path: str | None = None,
                 smoke_profile: str | None = None) -> dict[str, Any]:
    from .. import db as dbm
    if dbm.get_meta(conn, "snapshot_enabled") == "1":
        from ..snapshots import pin
        from ..indexer.build import get_embedder
        with pin(conn) as context:
            return _evaluate_abc(context.conn, tasks_path, client=client, data_dir=str(context.directory),
                                 embedder=get_embedder("local", str(context.directory)), runs=runs,
                                 require_approved=require_approved, limit=limit, seed=seed,
                                 answer_key_path=answer_key_path, smoke_profile=smoke_profile)
    return _evaluate_abc(conn, tasks_path, client=client, data_dir=data_dir, embedder=embedder,
                         runs=runs, require_approved=require_approved, limit=limit, seed=seed,
                         answer_key_path=answer_key_path, smoke_profile=smoke_profile)


def agent_trial(run):
    """Preserve raw safety/fallback events separately from answer correctness."""
    events = run.get("events", [])
    return {
        "refused": run["status"] == "refused", "answer": run.get("answer"),
        "answer_source": run.get("answer_source"),
        "citations": run.get("evidence_registry", []), "tool_calls": len(events),
        "native_planning": run.get("execution_mode") == "model" and not run.get("fallback_reason"),
        "fallback_reason": run.get("fallback_reason"), "events": events,
        "verification": run.get("verification"),
        "final_safe": _citation_safe(run) and len(events) <= agent.MAX_TOOL_CALLS
                      and all(e.get("tool") in agent.ALLOWED_TOOLS | {"planning_attempt"} for e in events),
    }


def _evaluate_abc(conn, tasks_path, *, client, data_dir, embedder, runs, require_approved, limit,
                  seed=17, answer_key_path=None, smoke_profile=None):
    from . import rubric, protocol
    from ..contracts import digest
    if client is None:
        raise ModelEvalError("未配置 OpenAI-compatible 模型；A/B/C 对照尚未运行")
    try:
        protocol.schedule([], ABC_VARIANTS, runs, seed)
    except ValueError as exc:
        raise ModelEvalError(str(exc)) from exc
    manifest = task_eval.load_task_set(conn, tasks_path, require_approved=require_approved)
    if smoke_profile not in {None, "value"}:
        raise ModelEvalError("unknown smoke profile")
    if smoke_profile and limit is not None:
        raise ModelEvalError("smoke profile and numeric limit are mutually exclusive")
    selected = (_value_smoke_tasks(manifest["tasks"])
                if smoke_profile == "value" else _representative_tasks(manifest["tasks"], limit))
    is_smoke = limit is not None or smoke_profile is not None
    gold = protocol.answer_key(answer_key_path, tasks_path, selected)
    raw = {v: [{"id": t["id"], "type": t["type"], "repetitions": []} for t in selected] for v in ABC_VARIANTS}
    rows = {(v, row["id"]): row for v in raw for row in raw[v]}
    tasks = {t["id"]: t for t in selected}
    order = protocol.schedule(selected, ABC_VARIANTS, runs, seed)
    budget_exhausted = False
    for number, event in enumerate(order):
        isolated = sqlite3.connect(":memory:")
        isolated.row_factory = sqlite3.Row
        conn.backup(isolated)
        task, variant = tasks[event["task_id"]], event["variant"]
        before = protocol.usage_start(client)
        started = time.perf_counter()
        try:
            if variant in ("A", "B"):
                once = _baseline_once(isolated, task, client=client, fixed_skill=variant == "B")
            else:
                once = agent_trial(agent.run(isolated, task["question"], data_dir=data_dir,
                                            embedder=embedder, llm_client=client, mode="model"))
        except Exception as exc:
            once = protocol.error_trial(exc)
        finally:
            isolated.close()
        once.update(protocol.usage_delta(client, before))
        once.update(latency_ms=round((time.perf_counter() - started) * 1000, 1),
                    sequence=number, repetition=event["repetition"])
        rows[(variant, task["id"])]["repetitions"].append({**once, **_abc_grade(task, once, variant=variant)})
        if protocol.budget_exhausted(once):
            budget_exhausted = True
            break
    config = {
        "version": protocol.VERSION, "seed": seed, "schedule": order,
        "max_tool_calls": BASELINE_MAX_TOOL_CALLS, "max_code_read_lines": BASELINE_CODE_READ_MAX_LINES,
        "estimated_input_token_limit": 8000, "output_token_limit": 1200, "temperature": .1,
        "A_tools": sorted(BASELINE_TOOLS), "B_tools": sorted(BASELINE_TOOLS), "C_tools": sorted(agent.ALLOWED_TOOLS),
        "deterministic_discovery_preflight": True,
        "preflight_counts_toward_six_tool_attempts": True,
        "smoke_profile": smoke_profile,
        "experience_information_parity": False,
    }
    result = {
        "report_kind": "abc", "status": ("partial_budget" if budget_exhausted else
                                           "smoke" if is_smoke else "completed"),
        "task_scope": ("partial_budget" if budget_exhausted else
                        "representative_smoke" if is_smoke else "full"),
        "task_count": len(selected), "repository": manifest["repository"], "revision": manifest["revision"],
        "task_set": str(tasks_path), "task_set_hash": _task_hash(tasks_path),
        "model": getattr(client, "model", "openai-compatible"), "prompt_version": agent.PROMPT_VERSION,
        "temperature": .1, "runs_per_task": runs,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variants": {v: {"name": ABC_VARIANTS[v], "tasks": [{"id": t["id"]} for t in selected]} for v in ABC_VARIANTS},
        "raw_trials": raw, "protocol": config, "answer_key": gold, "acceptance": {"gates": {}},
        "identity": rubric.identity(conn, tasks_path),
        "boundary": "A/B/C 比较整套系统，不单独归因于 Wiki。经验题中 A/B 未提供同等会话信息，故只能说明知识可用性，不能证明知识策划更优；增益与 token 门槛仅使用代码题。",
    }
    result["evaluation_hash"] = digest([config, gold, result["model"], result["temperature"], runs])
    result["blind_review"] = rubric.blind_packet(result)
    if budget_exhausted:
        result["answer_review_status"] = "unresolved"
        result["review_status"] = "unresolved"
        result["acceptance"].update(
            passed=False, effect_observed=False, human_release_eligible=False,
            gates={"budget_complete": False, "paired_trials_complete": False})
        return result
    rubric.finalize(result)
    return result


def render_markdown(result: dict[str, Any], *, tasks_path: str) -> str:
    metrics = result["metrics"]
    acceptance = result["acceptance"]
    outcome = "通过" if acceptance["passed"] else "未通过"
    usage = metrics["usage"]
    lines = [
        "# CodeAtlas 可选真实模型 Agent 评测", "",
        "> 只评价工具规划协议、回退与引用安全门禁；不评价模型文案质量。", "",
        "## 运行快照", "",
        f"- 仓库：`{result['repository']}`",
        f"- revision：`{result['revision']}`",
        f"- 模型：`{result['model']}`",
        f"- Agent prompt：`{result['prompt_version']}`",
        f"- 题集：`{tasks_path}`",
        f"- 题集 SHA-256：`{result['task_set_hash']}`", "",
        "## 指标", "", "| 指标 | 结果 |", "|---|---:|",
        f"| 原生合法规划率 | {metrics['native_planning_success_rate']:.1f}% |",
        f"| 回退率 | {metrics['fallback_rate']:.1f}% |",
        f"| 工具轨迹合法率 | {metrics['tool_trace_legal_rate']:.1f}% |",
        f"| 最终安全门禁 | {metrics['final_safety_gate_rate']:.1f}% |",
        f"| 延迟 p50 / p95 | {metrics['p50_ms']:.1f} / {metrics['p95_ms']:.1f} ms |",
        f"| 请求数 | {metrics['requests'] if metrics['requests'] is not None else 'provider 未提供'} |",
        f"| prompt / completion / total token | {usage.get('prompt_tokens', '未提供')} / "
        f"{usage.get('completion_tokens', '未提供')} / {usage.get('total_tokens', '未提供')} |",
        f"| 调用成本 | {metrics['cost_usd'] if metrics['cost_usd'] is not None else '未测'} |", "",
        "## 验收", "",
        f"原生合法规划率 ≥90%、最终安全门禁 100%：**{outcome}**。", "",
        "## 逐题协议结果", "",
        "| 任务 | 类型 | 执行 | 调用 | 安全 | 回退 |", "|---|---|---|---:|---|---|",
    ]
    for row in result["tasks"]:
        lines.append(
            f"| `{row['id']}` | {row['type']} | {row['execution_mode']} / {row['answer_source']} | "
            f"{row['tool_calls']} | {'通过' if row['citation_gate_safe'] else '失败'} | "
            f"{row['fallback_reason'] or '-'} |"
        )
    lines += ["", "## 边界", "",
              "- API Key 只从环境变量读取；报告、数据库与运行轨迹均不含 Key。",
              "- 拒答题在本地证据门禁处结束，不应为了测试模型而绕过无证据拒答。",
              "- 只有显式配置每百万输入/输出 token 价格时才计算 `cost_usd`；否则保持未测。",
              "- 报告只保存任务 ID 和协议结果，不保存外部模型的原始请求或完整回答。", ""]
    return "\n".join(lines)


def main(conn: sqlite3.Connection, tasks_path: str, *, client, data_dir: str,
         embedder=None, out: str = "docs/MODEL-EVAL.md",
         json_out: str | None = None, require_approved: bool = True) -> dict[str, Any]:
    result = evaluate(conn, tasks_path, client=client, data_dir=data_dir,
                      embedder=embedder, require_approved=require_approved)
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(result, tasks_path=tasks_path), encoding="utf-8")
    sidecar = Path(json_out) if json_out else target.with_suffix(".json")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**result, "report": str(target), "json_report": str(sidecar)}


def render_abc_markdown(result: dict[str, Any], *, tasks_path: str) -> str:
    from .protocol import render
    return render(result)


def main_abc(conn: sqlite3.Connection, tasks_path: str, *, client, data_dir: str,
             embedder=None, runs: int = 3, out: str = "docs/MODEL-ABC-CJSON.md",
             json_out: str | None = None, require_approved: bool = True,
             limit: int | None = None, seed: int = 17,
             answer_key_path: str | None = None) -> dict[str, Any]:
    from .protocol import ensure_new_outputs
    target = Path(out)
    sidecar = Path(json_out) if json_out else target.with_suffix(".json")
    ensure_new_outputs(target, sidecar, sidecar.with_suffix(".review.json"))
    result = evaluate_abc(
        conn, tasks_path, client=client, data_dir=data_dir, embedder=embedder,
        runs=runs, require_approved=require_approved, limit=limit, seed=seed, answer_key_path=answer_key_path,
    )
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_abc_markdown(result, tasks_path=tasks_path), encoding="utf-8")
    sidecar = Path(json_out) if json_out else target.with_suffix(".json")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sidecar.with_suffix(".review.json").write_text(
        json.dumps(result["blind_review"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**result, "report": str(target), "json_report": str(sidecar)}
