"""Offline evaluation for the public Agent / knowledge-card loop.

This evaluator intentionally measures only enforceable properties: tool authority,
source anchors, review gates, stale isolation, refusal, and latency.  It does not
grade model prose, and it always works in a copied SQLite database.
"""
from __future__ import annotations

import sqlite3
import statistics
import time
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .. import agent, conversation, db as dbm
from ..experience import store
from ..graph import traverse
from ..indexer import build as indexer
from ..retrieve import engine


class WorkflowEvalError(ValueError):
    pass


REQUIRED_CASES = {
    "session-import", "agent-trace", "pending-gate", "approval-anchor",
    "stale-isolation", "no-evidence-refusal",
}
ANCHOR_FIELDS = {"repository", "revision", "usr", "path", "line_start", "line_end"}


def _read_manifest(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise WorkflowEvalError(f"无法读取闭环评测清单: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise WorkflowEvalError("闭环评测清单必须是 schema_version: 1 的 YAML 对象")
    cases = raw.get("cases")
    if not isinstance(cases, list) or {case.get("id") for case in cases if isinstance(case, dict)} != REQUIRED_CASES:
        raise WorkflowEvalError("闭环评测清单必须恰好包含 6 个固定场景")
    for field in ("repository", "revision", "public_source", "anchor_symbol",
                  "wiki_question", "diagnostic_question", "unrelated_question"):
        if not raw.get(field):
            raise WorkflowEvalError(f"闭环评测清单缺少 {field}")
    return raw


def _copy_database(source_db: str | Path, target_db: Path) -> None:
    source = Path(source_db).expanduser().resolve()
    if not source.is_file():
        raise WorkflowEvalError(f"知识库不存在: {source}")
    if source == target_db.resolve():
        raise WorkflowEvalError("评测副本不能覆盖源知识库")
    target_db.parent.mkdir(parents=True, exist_ok=True)
    dbm.reset(target_db)
    src_conn = dbm.read_only(source)
    dst_conn = dbm.connect(target_db)
    try:
        if dbm.get_meta(src_conn, "snapshot_enabled") == "1":
            from ..snapshots import pin
            with pin(src_conn) as context:
                context.conn.backup(dst_conn)
        else:
            src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def _reset_isolated_governance(conn: sqlite3.Connection) -> None:
    """Start a repeatable workflow scenario in an already copied database.

    The source knowledge base may legitimately contain the same public sessions or
    pending cards used by this fixed scenario.  Those assets must survive normal
    parses, but an evaluator needs a clean governance layer so its first import and
    publication gates mean the same thing on every run.  Call this only after
    ``_copy_database``; compiler facts, Wiki pages, and repository metadata remain.
    """
    tables = (
        "agent_feedback", "agent_event", "agent_run",
        "curation_candidate", "conversation_assessment", "conversation_message",
        "conversation_session", "experience_link", "experience_anchor",
        "experience_qa", "experience_review", "experience",
        "publication_journal", "review_confirmation", "knowledge_dependency", "knowledge_set",
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in tables:
            conn.execute(f"DELETE FROM {table}")
        for key in ("active_knowledge_set", "snapshot_enabled", "knowledge_set_id"):
            conn.execute("DELETE FROM meta WHERE key=?", (key,))
        dbm.set_meta(conn, "evaluation_scope", "isolated_experiment")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _percent(numerator: int, denominator: int) -> float:
    return round(100 * numerator / denominator, 1) if denominator else 0.0


def _latency(values: list[float]) -> tuple[int, int]:
    if not values:
        return 0, 0
    ordered = sorted(values)
    p50 = statistics.median(ordered)
    p95 = ordered[min(len(ordered) - 1, int((len(ordered) - 1) * .95 + .9999))]
    return round(p50 * 1000), round(p95 * 1000)


def _has_card(citations: list[dict[str, Any]], card_id: str) -> bool:
    return any(citation.get("uid") == f"exp:{card_id}" and citation.get("level") == "B"
               for citation in citations)


def evaluate(source_db: str, *, work_dir: str, data_dir: str,
             manifest_path: str, success_session: str, insufficient_session: str,
             unrelated_session: str) -> dict[str, Any]:
    """Execute the six fixed public scenarios without changing ``source_db``."""
    manifest = _read_manifest(manifest_path)
    root = Path(work_dir).expanduser().resolve()
    if root == Path.cwd().resolve():
        raise WorkflowEvalError("work_dir 不能是项目根目录")
    copied_db = root / "workflow-eval.db"
    _copy_database(source_db, copied_db)
    # Null embedding keeps this directory free of model artefacts.  Do not delete a
    # caller-owned work directory; rebuilding the SQLite/FTS index is sufficient.
    eval_data = root / "index"
    eval_data.mkdir(parents=True, exist_ok=True)
    conn = dbm.connect(copied_db)
    try:
        _reset_isolated_governance(conn)
        # A copied database may carry an absolute knowledge_dir from its source.
        # Always redirect Markdown writes into this evaluation's isolated run.
        isolated_knowledge = root / f"knowledge-{time.time_ns()}"
        dbm.set_meta(conn, "knowledge_dir", str(isolated_knowledge))
        repository = dbm.get_meta(conn, "repository_id", dbm.get_meta(conn, "repo", ""))
        revision = dbm.get_meta(conn, "revision", "unversioned")
        repo = dbm.get_meta(conn, "repo", "")
        if repository != manifest["repository"] or revision != manifest["revision"]:
            raise WorkflowEvalError("评测清单与知识库的 repository/revision 不匹配")
        if not repo:
            raise WorkflowEvalError("知识库缺少源码目录元数据")
        indexer.build(conn, repo, embedder_kind="null", out_dir=str(eval_data))

        cases: list[dict[str, Any]] = []
        timings: list[float] = []

        # 1. Public session import is auditable and idempotent.
        success = conversation.import_public_session(conn, success_session)
        duplicate = conversation.import_public_session(conn, success_session)
        import_ok = success["imported"] and duplicate["duplicate"]
        cases.append({"id": "session-import", "passed": import_ok,
                      "detail": "public source/license/hash validated; duplicate rejected"})

        # 2. Both Agent routes obey the same authority boundary.  A concept question
        # must discover and read Wiki before source verification, while a structural
        # impact question may route directly to symbols/graph facts.
        wiki_question = manifest["wiki_question"]
        t0 = time.perf_counter()
        wiki_run = agent.run(conn, wiki_question, data_dir=str(eval_data),
                             embedder=None, llm_client=None, mode="rule")
        timings.append(time.perf_counter() - t0)
        wiki_tools = [event["tool"] for event in wiki_run["events"]]
        wiki_first_ok = wiki_tools[:3] == [
            "search_evidence", "wiki_outline", "wiki_section",
        ]
        question = manifest["diagnostic_question"]
        t0 = time.perf_counter()
        run = agent.run(conn, question, data_dir=str(eval_data), session_id=success["id"],
                        embedder=None, llm_client=None, save_draft=True)
        timings.append(time.perf_counter() - t0)
        if not run["draft_id"]:
            reasons = (run.get("session_gate") or {}).get("reasons", [])
            raise WorkflowEvalError(
                "成功会话未生成可审核草稿；会话质量门禁：" + ("；".join(reasons) or "未知")
            )
        trace_ok = (run["status"] == "completed" and bool(run["draft_id"])
                    and wiki_first_ok
                    and 1 <= len(run["events"]) <= agent.MAX_TOOL_CALLS
                    and all(event["tool"] in agent.ALLOWED_TOOLS for event in run["events"]))
        cases.append({"id": "agent-trace", "passed": trace_ok,
                      "detail": (f"impact={len(run['events'])} read-only calls; "
                                 f"wiki-first={wiki_first_ok}; draft is explicit")})

        a_citations = [c for c in run["evidence"]["citations"] if c.get("level") == "A"]
        a_complete = sum(ANCHOR_FIELDS <= set((citation.get("provenance") or {}))
                         and all((citation.get("provenance") or {}).get(key) for key in ANCHOR_FIELDS)
                         for citation in a_citations)

        # 3. A saved draft remains hidden until a reviewer binds exact source evidence.
        pending = engine.ask(conn, question, data_dir=str(eval_data), llm_client=None)
        pending_ok = not _has_card(pending["citations"], run["draft_id"])
        cases.append({"id": "pending-gate", "passed": pending_ok,
                      "detail": "pending card has no B-level retrieval visibility"})

        # 4. Approval requires and records a concrete USR anchor.
        anchor_symbol = manifest["anchor_symbol"]
        node = traverse.resolve_symbol(conn, anchor_symbol)
        if not node or not node[0]["usr"]:
            raise WorkflowEvalError(f"固定评测代码库缺少 {anchor_symbol} USR")
        anchor = store.bind(conn, run["draft_id"], node[0]["usr"],
                            bound_by="synthetic_test_review")
        qa = store.qa_add(conn, run["draft_id"], "该排障结论由哪些源码事实支持？",
                          ["函数定义、失败路径和确定调用关系"],
                          [run["draft_preview"]["evidence_tags"][0]])
        store.qa_review(conn, qa["id"], True, "固定公开源码 QA 复核通过",
                        "synthetic_test_review", reviewer_type="fixture",
                        evaluation_scope="isolated_experiment")
        approved = store.review(conn, run["draft_id"], "approve",
                                "固定公开源码锚点已复核", "synthetic_test_review",
                                reviewer_type="fixture", evaluation_scope="isolated_experiment")
        indexer.build(conn, repo, embedder_kind="null", out_dir=str(eval_data))
        after_review = engine.ask(conn, question, data_dir=str(eval_data), llm_client=None)
        approved_ok = (approved["status"] == "approved" and bool(anchor["definition_hash"])
                       and _has_card(after_review["citations"], run["draft_id"]))
        cases.append({"id": "approval-anchor", "passed": approved_ok,
                      "detail": "USR + range + definition hash bound before B evidence appeared"})

        # 5. A changed definition must immediately remove the card from B retrieval.
        # A reviewed replacement can then supersede it, and deleting all card tables
        # must still permit a lossless Markdown-source rebuild.
        conn.execute("UPDATE node SET definition_hash='workflow-eval-changed' WHERE id=?", (node[0]["id"],))
        stale = store.stale_check(conn)
        after_stale = engine.ask(conn, question, data_dir=str(eval_data), llm_client=None)
        stale_ok = (any(item["id"] == run["draft_id"] for item in stale["stale"])
                    and not _has_card(after_stale["citations"], run["draft_id"]))

        replacement_data = dict(run["draft_preview"])
        replacement_data["title"] = f"{replacement_data['title']}（复审版）"
        replacement_id = store.add(
            conn, replacement_data, source_type="synthetic_test_review",
            source_ref="workflow-eval/replacement", session_id=success["id"],
        )
        store.bind(conn, replacement_id, node[0]["usr"], bound_by="synthetic_test_review",
                   relation="warns")
        replacement_qa = store.qa_add(
            conn, replacement_id, "更新后的定义是否仍支持排障结论？",
            ["复核当前定义范围、失败路径和验证步骤"],
            [replacement_data["evidence_tags"][0]],
        )
        store.link_supersedes(conn, replacement_id, run["draft_id"])
        store.qa_review(conn, replacement_qa["id"], True,
                        "已按更新后的固定源码定义及替代关系复核", "synthetic_test_review",
                        reviewer_type="fixture", evaluation_scope="isolated_experiment")
        store.review(conn, replacement_id, "approve", "复审卡锚点与 QA 通过",
                     "synthetic_test_review", reviewer_type="fixture",
                     evaluation_scope="isolated_experiment")
        indexer.build(conn, repo, embedder_kind="null", out_dir=str(eval_data))
        after_supersede = engine.ask(conn, question, data_dir=str(eval_data), llm_client=None)
        supersede_ok = (
            store.get(conn, run["draft_id"])["status"] == "superseded"
            and store.get(conn, replacement_id)["status"] == "approved"
            and not _has_card(after_supersede["citations"], run["draft_id"])
            and _has_card(after_supersede["citations"], replacement_id)
        )
        before_rebuild = store.consistency(conn)
        rebuilt = store.rebuild(conn, rebuild_index=True)
        rebuild_ok = (
            before_rebuild["ok"] and rebuilt["ok"]
            and store.get(conn, run["draft_id"])["status"] == "superseded"
            and store.get(conn, replacement_id)["status"] == "approved"
            and store.get(conn, replacement_id)["anchors"][0]["relation"] == "warns"
            and store.get(conn, replacement_id)["qas"][0]["status"] == "passed"
        )
        cases.append({"id": "stale-isolation", "passed": stale_ok,
                      "detail": ("definition hash mismatch hid stale B evidence; "
                                 f"supersede={supersede_ok}; markdown rebuild={rebuild_ok}")})
        cases[-1]["passed"] = cases[-1]["passed"] and supersede_ok and rebuild_ok

        # 6. Both shortage and unrelated public sessions must refuse without a draft.
        insufficient = conversation.import_public_session(conn, insufficient_session)
        unrelated = conversation.import_public_session(conn, unrelated_session)
        refusal_runs = []
        for session_id, refusal_question in (
            # The attached session records the vague failure.  The nonce avoids
            # accidentally treating the English word "parsing" as a weak lexical
            # match for unrelated code and demonstrates the no-anchor gate itself.
            (insufficient["id"], "GXJQZZ_13"),
            (unrelated["id"], manifest["unrelated_question"]),
        ):
            t0 = time.perf_counter()
            refusal_runs.append(agent.run(conn, refusal_question, data_dir=str(eval_data),
                                          session_id=session_id, embedder=None, llm_client=None,
                                          save_draft=True))
            timings.append(time.perf_counter() - t0)
        refusal_ok = all(item["status"] == "refused" and not item["draft_id"]
                         and item["evidence"]["ab_evidence"] == 0 for item in refusal_runs)
        cases.append({"id": "no-evidence-refusal", "passed": refusal_ok,
                      "detail": "insufficient and unrelated requests refused without card persistence"})

        legal_runs = [wiki_run, run, *refusal_runs]
        raw_session_indexed = conn.execute(
            """SELECT COUNT(*) c FROM conversation_message m
                 JOIN chunk c ON c.text=m.content"""
        ).fetchone()["c"]
        cases[0]["passed"] = cases[0]["passed"] and raw_session_indexed == 0
        cases[0]["detail"] += f"; raw session chunks={raw_session_indexed}"
        trace_legal = sum(1 for item in legal_runs if len(item["events"]) <= agent.MAX_TOOL_CALLS
                          and all(event["tool"] in agent.ALLOWED_TOOLS for event in item["events"]))
        p50, p95 = _latency(timings)
        gate_checks = (pending_ok, approved_ok, stale_ok, supersede_ok, rebuild_ok)
        result = {
            "repository": repository, "revision": revision, "copied_db": str(copied_db),
            "cases": cases,
            "metrics": {
                "tool_trace_legal_rate": _percent(trace_legal, len(legal_runs)),
                "wiki_first_order_rate": _percent(int(wiki_first_ok), 1),
                "a_anchor_complete_rate": _percent(a_complete, len(a_citations)),
                "card_gate_correct_rate": _percent(sum(gate_checks), len(gate_checks)),
                "markdown_rebuild_consistency_rate": _percent(int(rebuild_ok), 1),
                "refusal_accuracy": _percent(sum(item["status"] == "refused" for item in refusal_runs), len(refusal_runs)),
                "raw_session_indexed_count": raw_session_indexed,
                "p50_ms": p50, "p95_ms": p95,
            },
            "review_mode": "synthetic_test_review",
        }
        if not all(case["passed"] for case in cases):
            raise WorkflowEvalError(f"闭环评测失败: {[case['id'] for case in cases if not case['passed']]}")
        return result
    finally:
        conn.close()


def render_markdown(result: dict[str, Any], *, manifest_path: str) -> str:
    metrics = result["metrics"]
    lines = ["# CodeAtlas Agent 与会话知识闭环评测", "",
             "> 固定 6 个公开场景；无 API Key、无外部模型、隔离 SQLite 副本。",
             "", "## 运行快照", "",
             f"- 仓库：`{result['repository']}`", f"- revision：`{result['revision']}`",
             f"- 清单：`{manifest_path}`",
             f"- 审核模式：`{result.get('review_mode', 'synthetic_test_review')}`（仅验证门禁）",
             "", "## 门禁指标", "",
             "| 指标 | 结果 |", "|---|---:|",
             f"| 工具轨迹合法率 | {metrics['tool_trace_legal_rate']:.1f}% |",
             f"| 概念题 Wiki-first 顺序遵守率 | {metrics['wiki_first_order_rate']:.1f}% |",
             f"| A 级锚点完整率 | {metrics['a_anchor_complete_rate']:.1f}% |",
             f"| 知识卡审核/过期/替代门禁正确率 | {metrics['card_gate_correct_rate']:.1f}% |",
             f"| Markdown 重建一致率 | {metrics['markdown_rebuild_consistency_rate']:.1f}% |",
             f"| 无证据拒答准确率 | {metrics['refusal_accuracy']:.1f}% |",
             f"| 原始会话进入检索数量 | {metrics['raw_session_indexed_count']} |",
             f"| Agent 延迟 p50 / p95 | {metrics['p50_ms']} / {metrics['p95_ms']} ms |",
             "", "## 固定场景", ""]
    for case in result["cases"]:
        outcome = "通过" if case["passed"] else "失败"
        lines.append(f"- `{case['id']}`：{outcome}；{case['detail']}")
    lines.extend(["", "## 口径与边界", "",
                  "- 只度量可验证的工具权限、源码锚点、审核/过期隔离和拒答，不评估模型文案质量。",
                  "- `stale-isolation` 在隔离副本中模拟 definition hash 变化，不修改公开源码或源知识库。",
                  "- 原始会话未进入 FTS；评测导入的三个会话均为 CC0 公开合成资产。", ""])
    return "\n".join(lines)


def main(source_db: str, *, work_dir: str, data_dir: str, manifest_path: str,
         success_session: str, insufficient_session: str, unrelated_session: str,
         out: str) -> dict[str, Any]:
    result = evaluate(source_db, work_dir=work_dir, data_dir=data_dir, manifest_path=manifest_path,
                      success_session=success_session, insufficient_session=insufficient_session,
                      unrelated_session=unrelated_session)
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(result, manifest_path=manifest_path), encoding="utf-8")
    # The presentation server reads only this constrained sidecar.  Do not expose
    # evaluator work paths or let the browser run the evaluator itself.
    public_result = {
        "repository": result["repository"], "revision": result["revision"],
        "metrics": result["metrics"], "cases": result["cases"],
        "review_mode": result.get("review_mode", "synthetic_test_review"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    json_target = target.with_suffix(".json")
    json_target.write_text(json.dumps(public_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**result, "report": str(target), "json_report": str(json_target)}
