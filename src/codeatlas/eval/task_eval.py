"""轻量、可复核的开发任务评测。

这不是第二套检索系统：它只把已有的 ``ask`` / ``impact`` 输出按人工维护的
任务清单判定，并把版本、锚点、证据完整性和失败原因写进一份 Markdown 报告。
默认允许 ``draft`` 任务生成预览报告，但该报告会明确标记为待人工确认；传入
``require_approved=True`` 才能作为公开引用前的门禁。
"""
from __future__ import annotations

import json
import math
import statistics
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .. import db as dbm
from ..graph import traverse
from ..retrieve import engine


RETRIEVAL_TYPES = {"定位", "调用链", "语义检索"}
IMPACT_TYPE = "影响分析"
REFUSAL_TYPE = "无证据拒答"
EXPERIENCE_TYPE = "已审核经验复用"
ALL_TYPES = RETRIEVAL_TYPES | {IMPACT_TYPE, REFUSAL_TYPE, EXPERIENCE_TYPE}
REVIEW_STATES = {"draft", "approved"}

RETRIEVAL_CONFIGS = (
    ("仅 BM25", dict(use_bm25=True, use_symbol=False, use_vector=False,
                       use_graph=False, reranker="none")),
    ("BM25 + 符号", dict(use_bm25=True, use_symbol=True, use_vector=False,
                         use_graph=False, reranker="none")),
    ("+ 本地向量", dict(use_bm25=True, use_symbol=True, use_vector=True,
                        use_graph=False, reranker="none")),
    ("+ certain 图扩展", dict(use_bm25=True, use_symbol=True, use_vector=True,
                              use_graph=True, expand_mode="head", reranker="none")),
    ("完整无模型", dict(use_bm25=True, use_symbol=True, use_vector=True,
                        use_graph=True, expand_mode="head", reranker="feature")),
)


class TaskEvalError(ValueError):
    """A task manifest cannot be evaluated against the current code snapshot."""


def _read_manifest(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise TaskEvalError(f"任务清单不存在: {path}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TaskEvalError("任务清单根节点必须是 YAML 对象")
    return raw


def _path(value: str | None) -> str:
    return (value or "").replace("\\", "/").lstrip("./")


def _anchor_label(anchor: dict[str, Any]) -> str:
    line = f":L{anchor['line_start']}" if anchor.get("line_start") else ""
    return f"{anchor['symbol']} ({anchor['path']}{line})"


def _validate_anchor_shape(anchor: Any, task_id: str, field: str) -> dict[str, Any]:
    if not isinstance(anchor, dict):
        raise TaskEvalError(f"{task_id}.{field} 必须是对象")
    for key in ("symbol", "path"):
        if not isinstance(anchor.get(key), str) or not anchor[key].strip():
            raise TaskEvalError(f"{task_id}.{field} 缺少 {key}")
    if "line_start" in anchor and (
        not isinstance(anchor["line_start"], int) or anchor["line_start"] <= 0
    ):
        raise TaskEvalError(f"{task_id}.{field}.line_start 必须是正整数")
    return dict(anchor, path=_path(anchor["path"]))


def _resolve_anchor(conn: sqlite3.Connection, anchor: dict[str, Any]) -> sqlite3.Row:
    where = ["kind='function'", "is_definition=1", "name=?", "path=?"]
    args: list[Any] = [anchor["symbol"], anchor["path"]]
    if anchor.get("line_start"):
        where.append("line_start=?")
        args.append(anchor["line_start"])
    rows = conn.execute(
        f"SELECT * FROM node WHERE {' AND '.join(where)} ORDER BY id", args
    ).fetchall()
    if not rows:
        raise TaskEvalError(f"锚点无法解析: {_anchor_label(anchor)}")
    if len(rows) > 1:
        raise TaskEvalError(
            f"锚点不唯一，请补充 line_start: {_anchor_label(anchor)}"
        )
    return rows[0]


def _validate_permalink(task: dict[str, Any], revision: str) -> None:
    url = task.get("source_permalink")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise TaskEvalError(f"{task['id']} 缺少公开 source_permalink")
    if revision not in url:
        raise TaskEvalError(
            f"{task['id']} 的 source_permalink 未绑定任务集 revision"
        )


def load_task_set(conn: sqlite3.Connection, path: str, *,
                  require_approved: bool = False) -> dict[str, Any]:
    """Read, validate and bind a task manifest to the current database snapshot."""
    manifest = _read_manifest(path)
    if manifest.get("schema_version") != 1:
        raise TaskEvalError("仅支持 schema_version: 1 的任务清单")

    required = ("name", "repository", "revision", "public_source", "reviewer",
                "reviewed_at", "failure_policy", "tasks")
    missing = [key for key in required if not manifest.get(key)]
    if missing:
        raise TaskEvalError(f"任务清单缺少字段: {', '.join(missing)}")
    if not isinstance(manifest["reviewed_at"], str):
        raise TaskEvalError("reviewed_at 必须是带引号的 ISO 日期字符串")
    if not isinstance(manifest["tasks"], list) or not manifest["tasks"]:
        raise TaskEvalError("tasks 必须是非空列表")

    db_repo = dbm.get_meta(conn, "repository_id", dbm.get_meta(conn, "repo", ""))
    db_revision = dbm.get_meta(conn, "revision", "unversioned")
    if manifest["repository"] != db_repo:
        raise TaskEvalError(
            f"任务集仓库与数据库不一致: {manifest['repository']} != {db_repo}"
        )
    if manifest["revision"] != db_revision:
        raise TaskEvalError(
            f"任务集 revision 与数据库不一致: {manifest['revision']} != {db_revision}"
        )

    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for raw in manifest["tasks"]:
        if not isinstance(raw, dict):
            raise TaskEvalError("每道任务必须是对象")
        task = dict(raw)
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id.strip() or task_id in seen:
            raise TaskEvalError(f"任务 id 缺失或重复: {task_id!r}")
        seen.add(task_id)
        task_type = task.get("type")
        if task_type not in ALL_TYPES:
            raise TaskEvalError(f"{task_id} 的 type 不支持: {task_type!r}")
        if task.get("status") not in REVIEW_STATES:
            raise TaskEvalError(f"{task_id}.status 只能是 draft 或 approved")
        task["reviewer"] = task.get("reviewer") or manifest["reviewer"]
        task["reviewed_at"] = task.get("reviewed_at") or manifest["reviewed_at"]
        if not isinstance(task["reviewer"], str) or not task["reviewer"].strip():
            raise TaskEvalError(f"{task_id} 缺少审核人")
        if not isinstance(task.get("question"), str) or not task["question"].strip():
            raise TaskEvalError(f"{task_id} 缺少 question")
        if not isinstance(task.get("rationale"), str) or not task["rationale"].strip():
            raise TaskEvalError(f"{task_id} 缺少 rationale")
        _validate_permalink(task, manifest["revision"])

        if task_type in RETRIEVAL_TYPES:
            anchors = task.get("expected_anchors")
            if not isinstance(anchors, list) or not anchors:
                raise TaskEvalError(f"{task_id} 必须提供 expected_anchors")
            task["expected_anchors"] = [
                _validate_anchor_shape(a, task_id, "expected_anchors") for a in anchors
            ]
            for anchor in task["expected_anchors"]:
                _resolve_anchor(conn, anchor)
        elif task_type == IMPACT_TYPE:
            task["target"] = _validate_anchor_shape(task.get("target"), task_id, "target")
            _resolve_anchor(conn, task["target"])
            if not isinstance(task.get("depth"), int) or task["depth"] < 1:
                raise TaskEvalError(f"{task_id}.depth 必须是正整数")
            anchors = task.get("expected_certain")
            if not isinstance(anchors, list) or not anchors:
                raise TaskEvalError(f"{task_id} 必须提供 expected_certain")
            task["expected_certain"] = [
                _validate_anchor_shape(a, task_id, "expected_certain") for a in anchors
            ]
            for anchor in task["expected_certain"]:
                _resolve_anchor(conn, anchor)
        elif task_type == EXPERIENCE_TYPE:
            if not isinstance(task.get("expected_card_title"), str) or not task["expected_card_title"].strip():
                raise TaskEvalError(f"{task_id} 必须提供 expected_card_title")
            task["main_anchor"] = _validate_anchor_shape(
                task.get("main_anchor"), task_id, "main_anchor"
            )
            _resolve_anchor(conn, task["main_anchor"])
        else:  # REFUSAL_TYPE
            if task.get("expected_anchors") or task.get("expected_certain"):
                raise TaskEvalError(f"{task_id} 是拒答题，不能包含代码 gold")

        if require_approved and task["status"] != "approved":
            raise TaskEvalError(f"{task_id} 尚未审核，不能生成可公开引用的结果")
        validated.append(task)

    bound = dict(manifest)
    bound.update(tasks=validated, db_repository=db_repo, db_revision=db_revision)
    return bound


def _citation_matches(citation: dict[str, Any], anchor: dict[str, Any]) -> bool:
    provenance = citation.get("provenance") or {}
    if provenance.get("symbol") != anchor["symbol"]:
        return False
    if _path(provenance.get("path")) != _path(anchor["path"]):
        return False
    return not anchor.get("line_start") or provenance.get("line_start") == anchor["line_start"]


def _provenance_complete(citation: dict[str, Any], manifest: dict[str, Any]) -> bool:
    provenance = citation.get("provenance") or {}
    return (
        citation.get("level") == "A"
        and bool(provenance.get("source_type"))
        and provenance.get("repository") == manifest["repository"]
        and provenance.get("revision") == manifest["revision"]
        and bool(provenance.get("usr"))
        and bool(provenance.get("path"))
        and isinstance(provenance.get("line_start"), int)
        and provenance["line_start"] > 0
        and isinstance(provenance.get("line_end"), int)
        and provenance["line_end"] >= provenance["line_start"]
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return round(ordered[index], 1)


def _run_ask(conn: sqlite3.Connection, question: str, config: dict[str, Any], *,
             embedder, data_dir: str) -> tuple[dict[str, Any], float]:
    start = time.perf_counter()
    result = engine.ask(
        conn, question,
        embedder=embedder if config["use_vector"] else None,
        data_dir=data_dir,
        use_bm25=config["use_bm25"],
        use_symbol=config["use_symbol"],
        use_vector=config["use_vector"],
        use_graph=config["use_graph"],
        expand_mode=config.get("expand_mode", "head"),
        reranker=config["reranker"],
        llm_client=None,
    )
    return result, round((time.perf_counter() - start) * 1000, 1)


def _evaluate_retrieval_task(conn: sqlite3.Connection, task: dict[str, Any],
                             manifest: dict[str, Any], *, embedder, data_dir: str) -> dict[str, Any]:
    attempts: dict[str, dict[str, Any]] = {}
    for name, config in RETRIEVAL_CONFIGS:
        result, latency = _run_ask(conn, task["question"], config,
                                   embedder=embedder, data_dir=data_dir)
        ranked = result.get("citations", [])[:10]
        matched: list[tuple[int, dict[str, Any]]] = [
            (rank, citation)
            for rank, citation in enumerate(ranked, 1)
            if any(_citation_matches(citation, anchor)
                   for anchor in task["expected_anchors"])
        ]
        evidence_ok = any(_provenance_complete(citation, manifest)
                          for _, citation in matched)
        errors: list[str] = []
        if not matched:
            errors.append("top-10 未命中预期代码锚点")
        elif not evidence_ok:
            errors.append("命中引用缺少完整 A 级 provenance")
        attempts[name] = dict(
            hit=bool(matched),
            mrr=(round(1 / matched[0][0], 3) if matched else 0.0),
            evidence_complete=evidence_ok,
            latency_ms=latency,
            context_tokens=result.get("context_tokens", 0),
            errors=errors,
            matched_tag=(matched[0][1].get("tag") if matched else None),
            refused=result.get("refused", False),
        )
    full = attempts["完整无模型"]
    return dict(id=task["id"], type=task["type"], status=task["status"],
                reviewer=task["reviewer"], reviewed_at=task["reviewed_at"],
                question=task["question"], permalink=task["source_permalink"],
                anchors=task["expected_anchors"], rationale=task["rationale"],
                attempts=attempts, passed=full["hit"] and full["evidence_complete"],
                errors=full["errors"])


def _node_key(node: dict[str, Any] | sqlite3.Row) -> tuple[str, str, int | None]:
    return (node["name"], _path(node["path"]), node["line_start"])


def _evaluate_impact_task(conn: sqlite3.Connection, task: dict[str, Any]) -> dict[str, Any]:
    target = _resolve_anchor(conn, task["target"])
    start = time.perf_counter()
    result = traverse.impact(conn, target["id"], max_hop=task["depth"])
    latency = round((time.perf_counter() - start) * 1000, 1)

    predicted = {
        _node_key(item)
        for items in result["by_hop"].values()
        for item in items
    }
    gold = {_node_key(_resolve_anchor(conn, anchor)) for anchor in task["expected_certain"]}
    candidate = {_node_key(item) for item in result["candidate_impact"]}
    overlap = predicted & gold
    precision = len(overlap) / len(predicted) if predicted else 0.0
    recall = len(overlap) / len(gold) if gold else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    candidate_independent = not bool(predicted & candidate)
    errors: list[str] = []
    if not candidate_independent:
        errors.append("candidate 影响混入了 certain 影响集合")
    if predicted != gold:
        errors.append("certain 影响集合与人工 gold 不一致")
    return dict(
        id=task["id"], type=task["type"], status=task["status"],
        reviewer=task["reviewer"], reviewed_at=task["reviewed_at"],
        question=task["question"], permalink=task["source_permalink"],
        target=task["target"], expected_certain=task["expected_certain"],
        rationale=task["rationale"], precision=round(precision, 3),
        recall=round(recall, 3), f1=round(f1, 3), latency_ms=latency,
        depth=task["depth"],
        predicted_count=len(predicted), candidate_count=len(candidate),
        candidate_independent=candidate_independent,
        passed=not errors, errors=errors,
    )


def _evaluate_refusal_task(conn: sqlite3.Connection, task: dict[str, Any], *,
                           embedder, data_dir: str) -> dict[str, Any]:
    config = dict(RETRIEVAL_CONFIGS[-1][1])
    result, latency = _run_ask(conn, task["question"], config,
                               embedder=embedder, data_dir=data_dir)
    passed = result.get("refused") is True and result.get("ab_evidence") == 0
    errors = [] if passed else ["未同时满足 refused=true 与 A/B 证据为 0"]
    return dict(
        id=task["id"], type=task["type"], status=task["status"],
        reviewer=task["reviewer"], reviewed_at=task["reviewed_at"],
        question=task["question"], permalink=task["source_permalink"],
        rationale=task["rationale"], refused=result.get("refused"),
        ab_evidence=result.get("ab_evidence"), latency_ms=latency,
        context_tokens=result.get("context_tokens", 0), passed=passed, errors=errors,
    )


def _evaluate_experience_task(conn: sqlite3.Connection, task: dict[str, Any], *,
                              embedder, data_dir: str) -> dict[str, Any]:
    attempts: dict[str, dict[str, Any]] = {}
    for name, config in RETRIEVAL_CONFIGS:
        result, latency = _run_ask(conn, task["question"], config,
                                   embedder=embedder, data_dir=data_dir)
        ranked = result.get("citations", [])[:10]
        matches = [(rank, citation) for rank, citation in enumerate(ranked, 1)
                   if citation.get("level") == "B"
                   and citation.get("title") == task["expected_card_title"]]
        evidence_ok = False
        if matches:
            provenance = matches[0][1].get("provenance") or {}
            anchor = task["main_anchor"]
            evidence_ok = (
                provenance.get("card_status") == "approved"
                and provenance.get("stale_status") == "approved"
                and provenance.get("symbol") == anchor["symbol"]
                and _path(provenance.get("path")) == anchor["path"]
                and bool(provenance.get("definition_hash"))
            )
        attempts[name] = {
            "hit": bool(matches), "mrr": round(1 / matches[0][0], 3) if matches else 0.0,
            "evidence_complete": evidence_ok, "latency_ms": latency,
            "context_tokens": result.get("context_tokens", 0),
            "errors": ([] if matches and evidence_ok else
                       ["未命中当前 approved B 卡或主锚点 provenance 不完整"]),
        }
    full = attempts["完整无模型"]
    approved_exists = bool(conn.execute(
        """SELECT 1 FROM experience WHERE title=? AND status='approved'
           AND COALESCE(artifact_scope,'formal')='formal'""",
        (task["expected_card_title"],),
    ).fetchone())
    return {
        "id": task["id"], "type": task["type"], "status": task["status"],
        "reviewer": task["reviewer"], "reviewed_at": task["reviewed_at"],
        "question": task["question"], "permalink": task["source_permalink"],
        "expected_card_title": task["expected_card_title"],
        "main_anchor": task["main_anchor"], "rationale": task["rationale"],
        "attempts": attempts, "passed": full["hit"] and full["evidence_complete"],
        "errors": full["errors"],
        "blocked_by_review": not approved_exists,
    }


def _retrieval_metrics(results: list[dict[str, Any]], config_name: str) -> dict[str, Any]:
    attempts = [result["attempts"][config_name] for result in results]
    n = len(attempts)
    latencies = [item["latency_ms"] for item in attempts]
    return dict(
        n=n,
        recall=round(sum(item["hit"] for item in attempts) / n * 100, 1) if n else 0.0,
        mrr=round(sum(item["mrr"] for item in attempts) / n, 3) if n else 0.0,
        evidence=round(sum(item["evidence_complete"] for item in attempts) / n * 100, 1)
        if n else 0.0,
        context=round(statistics.mean(item["context_tokens"] for item in attempts)) if n else 0,
        p50=_percentile(latencies, 0.50),
        p95=_percentile(latencies, 0.95),
    )


def _impact_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    return dict(
        n=n,
        precision=round(statistics.mean(item["precision"] for item in results), 3) if n else 0.0,
        recall=round(statistics.mean(item["recall"] for item in results), 3) if n else 0.0,
        f1=round(statistics.mean(item["f1"] for item in results), 3) if n else 0.0,
        p50=_percentile([item["latency_ms"] for item in results], 0.50),
        p95=_percentile([item["latency_ms"] for item in results], 0.95),
    )


def _refusal_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    return dict(
        n=n,
        accuracy=round(sum(item["passed"] for item in results) / n * 100, 1) if n else 0.0,
        p50=_percentile([item["latency_ms"] for item in results], 0.50),
        p95=_percentile([item["latency_ms"] for item in results], 0.95),
    )


def _mark(passed: bool) -> str:
    return "通过" if passed else "失败"


def failure_groups(results):
    failed = [row for row in results if not row["passed"]]
    return ([row for row in failed if row.get("blocked_by_review")],
            [row for row in failed if not row.get("blocked_by_review")])


def _render_report(conn: sqlite3.Connection, manifest: dict[str, Any], *, tasks_path: str, out: str,
                   db_path: str | None, data_dir: str, embedder_kind: str,
                   retrieval: list[dict[str, Any]], impacts: list[dict[str, Any]],
                   refusals: list[dict[str, Any]], experiences: list[dict[str, Any]]) -> str:
    statuses = Counter(task["status"] for task in manifest["tasks"])
    reviewed = statuses.get("approved", 0) == len(manifest["tasks"])
    review_label = "清单标记已审核；仅作开发回归" if reviewed else "待人工确认，不可写入简历指标"
    command = (
        f"codeatlas task-eval{' --db ' + db_path if db_path else ''}"
        f" --tasks {tasks_path}"
        f"{(' --data-dir ' + data_dir) if not Path(data_dir).is_absolute() else ''}"
        f" --embedder {embedder_kind} --out {out}"
    )
    L = [f"# {manifest['name']} — 轻量任务评测", "",
         f"> **审核状态：{review_label}**。任务 gold 由人工维护；本报告展示"
         "可执行任务的验证结果，不替代自动消融报告。", "",
         "> 已用于开发调整，不是独立留出集；召回通过不代表模型理解更好。", "",
         "## 运行快照", "", "| 项目 | 值 |", "|---|---|",
         f"| 公开语料 | {manifest['public_source']} |",
         f"| 仓库 | `{manifest['repository']}` |",
         f"| Git revision | `{manifest['revision']}` |",
         f"| 解析模式 | {('degraded' if dbm.get_meta(conn, 'degraded', '0') == '1' else 'compile database')} |",
         f"| 任务清单 | `{tasks_path}` |",
         f"| 审核人 / 日期 | {manifest['reviewer']} / {manifest['reviewed_at']} |",
         f"| 失败规则 | {manifest['failure_policy']} |",
         f"| 审核状态 | draft {statuses.get('draft', 0)} / approved {statuses.get('approved', 0)} |",
         f"| 命令 | `{command}` |", "",
         "## 汇总", ""]
    L += [f"### 检索任务（{len(retrieval)} 题；逐层消融）", "",
          "| 配置 | Recall@10 | MRR@10 | 命中锚点的完整 A 级 provenance | 平均上下文 token | P50 / P95 |",
          "|---|---:|---:|---:|---:|---:|"]
    for name, _ in RETRIEVAL_CONFIGS:
        m = _retrieval_metrics(retrieval, name)
        L.append(f"| {name} | {m['recall']}% | {m['mrr']} | {m['evidence']}% | "
                 f"{m['context']} | {m['p50']:.0f} / {m['p95']:.0f}ms |")
    impact = _impact_metrics(impacts)
    refusal = _refusal_metrics(refusals)
    experience = _retrieval_metrics(experiences, "完整无模型")
    baseline = _retrieval_metrics(retrieval, "仅 BM25")
    full = _retrieval_metrics(retrieval, "完整无模型")
    recall_gain = round(full["recall"] - baseline["recall"], 1)
    mrr_delta = round(full["mrr"] - baseline["mrr"], 3)
    acceptance = recall_gain >= 10.0 and mrr_delta >= -0.02
    L += ["", "### 结构与拒答任务（当前完整无模型方案）", "",
          "| 能力 | 题数 | 指标 | 延迟 P50 / P95 |", "|---|---:|---|---:|",
          f"| certain 影响集合 | {impact['n']} | P {impact['precision']} / R {impact['recall']} / F1 {impact['f1']} | {impact['p50']:.0f} / {impact['p95']:.0f}ms |",
          f"| 无证据拒答 | {refusal['n']} | 准确率 {refusal['accuracy']}% | {refusal['p50']:.0f} / {refusal['p95']:.0f}ms |",
          f"| 已审核经验复用 | {experience['n']} | B 卡 Recall@10 {experience['recall']}% / MRR {experience['mrr']} | {experience['p50']:.0f} / {experience['p95']:.0f}ms |",
          "", "### 开发回归检索门槛（非模型效果门槛）", "",
          f"- 完整方案相对 BM25-only：Recall@10 {recall_gain:+.1f}pt，MRR@10 {mrr_delta:+.3f}。",
          f"- 门槛：Recall@10 至少 +10pt 且 MRR@10 不低于 -0.02；本轮：**{_mark(acceptance)}**。",
          "", "## 逐题结果", ""]
    for result in retrieval:
        bm25 = result["attempts"]["仅 BM25"]
        full = result["attempts"]["完整无模型"]
        L += [f"### {result['id']} · {result['type']} · {_mark(result['passed'])}", "",
             f"- 审核：`{result['status']}`；{result['reviewer']} / {result['reviewed_at']}；问题：{result['question']}",
             f"- 预期锚点：{'；'.join(_anchor_label(a) for a in result['anchors'])}",
             f"- 源码：[permalink]({result['permalink']})；判定理由：{result['rationale']}",
             f"- 仅 BM25：命中={bm25['hit']}，MRR={bm25['mrr']}，provenance 完整={bm25['evidence_complete']}。",
             f"- 完整无模型：命中={full['hit']}，MRR={full['mrr']}，provenance 完整={full['evidence_complete']}。",
             ""]
    for result in impacts:
        L += [f"### {result['id']} · 影响分析 · {_mark(result['passed'])}", "",
             f"- 审核：`{result['status']}`；{result['reviewer']} / {result['reviewed_at']}；目标：{_anchor_label(result['target'])}；深度：{result.get('depth', '见清单')}",
             f"- certain gold：{'；'.join(_anchor_label(a) for a in result['expected_certain'])}",
             f"- 源码：[permalink]({result['permalink']})；判定理由：{result['rationale']}",
             f"- 结果：P={result['precision']} / R={result['recall']} / F1={result['f1']}；"
             f"candidate={result['candidate_count']}（独立展示={result['candidate_independent']}）。", ""]
    for result in refusals:
        L += [f"### {result['id']} · 无证据拒答 · {_mark(result['passed'])}", "",
             f"- 审核：`{result['status']}`；{result['reviewer']} / {result['reviewed_at']}；问题：{result['question']}",
             f"- 语料范围参考：[permalink]({result['permalink']})；判定理由：{result['rationale']}",
             f"- 结果：refused={result['refused']}，A/B 证据={result['ab_evidence']}。", ""]
    for result in experiences:
        full_exp = result["attempts"]["完整无模型"]
        L += [f"### {result['id']} · 已审核经验复用 · {_mark(result['passed'])}", "",
              f"- 审核：`{result['status']}`；{result['reviewer']} / {result['reviewed_at']}；问题：{result['question']}",
              f"- 预期 B 卡：`{result['expected_card_title']}`；主锚点：{_anchor_label(result['main_anchor'])}",
              f"- 结果：命中={full_exp['hit']}，MRR={full_exp['mrr']}，锚点完整={full_exp['evidence_complete']}。", ""]
    failed = [result for group in (retrieval, impacts, refusals, experiences) for result in group
              if not result["passed"]]
    blocked, unexpected = failure_groups(failed)
    L += ["## 失败案例", "",
          f"> 真实能力失败 {len(unexpected)}；因没有当前 approved B 卡而被审核门禁阻塞 "
          f"{len(blocked)}。两类结果不会合并包装。", ""]
    if not failed:
        L.append("本轮没有失败任务；结果仍只适用于本报告记录的固定公开快照与小规模人工题集。")
    else:
        for result in unexpected:
            L.append(f"- **真实失败 · {result['id']}**：{'；'.join(result['errors'])}")
        for result in blocked:
            L.append(f"- **blocked_by_review · {result['id']}**：{'；'.join(result['errors'])}")
    L += ["", "## 口径与边界", "",
          "- 定位、调用链和语义检索只检查 top-10 是否命中人工指定源码锚点；完整方案的"
          "命中还必须是含仓库、revision、USR、文件和行号的 A 级证据。",
          "- 影响分析只对 certain 反向可达集计分；candidate 只作独立线索展示，绝不并入 F1。",
          "- 拒答题要求 `refused=true` 且 `A/B=0`；全程显式传入 `llm_client=None`，没有模型调用。",
          f"- 本集只有 {len(manifest['tasks'])} 道人工维护任务，用于求职演示与失败复盘；它不具备统计显著性，"
          "不替代自动生成回归集的消融报告。", ""]
    return "\n".join(L)


def main(conn: sqlite3.Connection, tasks_path: str, *, data_dir: str = "data",
         embedder_kind: str = "tfidf", out: str = "docs/TASK-EVAL-CJSON.md",
         db_path: str | None = None, require_approved: bool = False,
         console=None) -> dict[str, Any]:
    """Run the task set and write its human-reviewable report."""
    from ..indexer.build import get_embedder
    if dbm.get_meta(conn, "snapshot_enabled") == "1":
        from ..snapshots import pin
        with pin(conn) as context:
            result = main(context.conn, tasks_path, data_dir=str(context.directory),
                          embedder_kind=embedder_kind, out=out, db_path=db_path,
                          require_approved=require_approved, console=console)
            result.update(context.provenance)
            Path(out).with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            return result

    manifest = load_task_set(conn, tasks_path, require_approved=require_approved)
    embedder = get_embedder(embedder_kind, data_dir)
    retrieval: list[dict[str, Any]] = []
    impacts: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    experiences: list[dict[str, Any]] = []
    for task in manifest["tasks"]:
        if task["type"] in RETRIEVAL_TYPES:
            result = _evaluate_retrieval_task(conn, task, manifest,
                                              embedder=embedder, data_dir=data_dir)
            retrieval.append(result)
        elif task["type"] == IMPACT_TYPE:
            result = _evaluate_impact_task(conn, task)
            impacts.append(result)
        elif task["type"] == EXPERIENCE_TYPE:
            result = _evaluate_experience_task(conn, task, embedder=embedder, data_dir=data_dir)
            experiences.append(result)
        else:
            result = _evaluate_refusal_task(conn, task, embedder=embedder, data_dir=data_dir)
            refusals.append(result)
        if console:
            outcome = "审核阻塞" if result.get("blocked_by_review") else _mark(result["passed"])
            console.print(f"  {task['id']:<22} {outcome}")

    markdown = _render_report(conn, manifest, tasks_path=tasks_path, out=out,
                              db_path=db_path, data_dir=data_dir, embedder_kind=embedder_kind,
                              retrieval=retrieval, impacts=impacts, refusals=refusals,
                              experiences=experiences)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(markdown, encoding="utf-8")
    if console:
        console.print(f"[green][OK][/] 任务评测报告 → {out}")
    baseline = _retrieval_metrics(retrieval, "仅 BM25")
    full = _retrieval_metrics(retrieval, "完整无模型")
    acceptance = {
        "recall_gain_pt": round(full["recall"] - baseline["recall"], 1),
        "mrr_delta": round(full["mrr"] - baseline["mrr"], 3),
    }
    acceptance["passed"] = (
        acceptance["recall_gain_pt"] >= 10.0 and acceptance["mrr_delta"] >= -0.02
    )
    failed = [item for group in (retrieval, impacts, refusals, experiences) for item in group
              if not item["passed"]]
    from .acceptance_eval import git_identity
    project_root = Path(__file__).resolve().parents[3]
    result = dict(
        benchmark_role="development_regression", proves_answer_quality=False,
        manifest=manifest, retrieval=retrieval, impacts=impacts,
        refusals=refusals, experiences=experiences, acceptance=acceptance, out=out,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        reproducibility={
            "task_manifest_sha256": __import__("hashlib").sha256(
                Path(tasks_path).read_bytes()
            ).hexdigest(),
            "build_config_hash": dbm.get_meta(conn, "build_config_hash", ""),
            "parse_stats": json.loads(dbm.get_meta(conn, "parse_stats", "{}") or "{}"),
            "codeatlas": git_identity(project_root),
        },
        summary={
            "baseline": baseline,
            "full": full,
            "impact": _impact_metrics(impacts),
            "refusal": _refusal_metrics(refusals),
            "experience": _retrieval_metrics(experiences, "完整无模型"),
            "passed_tasks": len(manifest["tasks"]) - len(failed),
            "total_tasks": len(manifest["tasks"]),
            "blocked_by_review": [item["id"] for item in failed
                                  if item.get("blocked_by_review")],
            "unexpected_failures": [item["id"] for item in failed
                                    if not item.get("blocked_by_review")],
            "failures": [
                {"id": item["id"], "type": item["type"], "errors": item["errors"]}
                for item in failed
            ],
        },
    )
    json_target = Path(out).with_suffix(".json")
    json_target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    result["json_report"] = str(json_target)
    return result
