"""Shared paired-trial and frozen answer-key contracts; never sent to the model."""
from __future__ import annotations

import json
import copy
import random
import re
import statistics
from pathlib import Path

import yaml

from ..contracts import digest

VERSION = "paired-answer-eval-v5"
REVIEW_VERSION = "source-grounded-review-v5"

# Only scoring/projection fields are excluded. Unknown raw fields remain bound.
_SCORE_FIELDS = {"trial_id", "raw_trial_hash", "correct", "complete", "answer_verdict",
                 "human_review", "answer_review", "review_status", "review_history",
                 "effect_review_eligible", "human_release_eligible"}


def raw_trial(trial):
    return {k: v for k, v in trial.items() if k not in _SCORE_FIELDS}


def run_hash(report):
    """Immutable run identity, separate from the model used to judge its answers."""
    return digest({"identity": report.get("identity"), "protocol": report.get("protocol"),
                   "model": report.get("model"), "temperature": report.get("temperature"),
                   "runs_per_task": report.get("runs_per_task"),
                   "raw_trials": {arm: [{"id": row["id"], "type": row["type"],
                       "question": row.get("question"),
                       "mechanism_id": row.get("mechanism_id"),
                       "case_id": row.get("case_id"),
                       "repetitions": [raw_trial(t) for t in row["repetitions"]]}
                       for row in rows] for arm, rows in report["raw_trials"].items()}})


def review_protocol_hash(reviews):
    """Changing judges creates a new scoring version, not a new Agent run."""
    participants = []
    def collect(item):
        participants.append({k: item.get(k) for k in
                             ("reviewer_kind", "reviewer", "provider", "model", "agent", "prompt_hash")})
        for child in item.get("independent_reviews", []):
            collect(child)
        if isinstance(item.get("adjudication"), dict):
            collect(item["adjudication"])
    for item in reviews.get("items", []):
        collect(item)
    unique = {digest(item): item for item in participants}
    return digest({"version": REVIEW_VERSION, "protocol": reviews.get("review_protocol", {}),
                   "judge_calibration": reviews.get("judge_calibration"),
                   "participants": [unique[key] for key in sorted(unique)]})


def review_cost(reviews):
    """Aggregate only measured judge usage; missing observations remain null."""
    records = []
    for item in reviews.get("items", []):
        independent = item.get("independent_reviews")
        records.extend(independent if isinstance(independent, list) else [item])
        if isinstance(item.get("adjudication"), dict):
            records.append(item["adjudication"])
    agents = [row for row in records if row.get("reviewer_kind") == "agent"]
    humans = [row for row in records if row.get("reviewer_kind") == "human"]
    def total(field, rows):
        values = [row.get(field) for row in rows]
        if not rows or any(not isinstance(value, (int, float)) or isinstance(value, bool)
                           or value < 0 for value in values):
            return None
        return sum(values)
    return {
        "agent_review_calls": len(agents),
        "input_tokens": total("input_tokens", agents),
        "output_tokens": total("output_tokens", agents),
        "cost_usd": total("cost_usd", agents),
        "latency_ms": total("latency_ms", agents),
        "human_review_count": len(humans),
        "human_review_seconds": total("duration_seconds", humans),
        "usage_complete": bool(agents) and all(
            row.get(field) is not None for row in agents
            for field in ("input_tokens", "output_tokens", "cost_usd", "latency_ms")),
    }


def _reviewer(record, expected_input_hash=None):
    if not isinstance(record, dict):
        raise ValueError("review must be an object")
    kind = record.get("reviewer_kind")
    if kind not in (None, "human", "agent"):
        raise ValueError("reviewer_kind must be human or agent")
    if not record.get("reviewer") or not record.get("reviewed_at") or not record.get("note"):
        raise ValueError("reviewer, dated review and rationale required")
    if kind == "agent":
        if any(not isinstance(record.get(k), str) or not record[k].strip()
               for k in ("provider", "model", "agent")):
            raise ValueError("agent reviewer requires provider, model and agent identity")
        if any(not re.fullmatch(r"[0-9a-f]{64}", str(record.get(k, "")))
               for k in ("prompt_hash", "input_hash")):
            raise ValueError("agent reviewer requires prompt_hash and input_hash SHA-256")
        if expected_input_hash is not None and record["input_hash"] != expected_input_hash:
            raise ValueError("agent review input_hash differs from the frozen review packet")
        for field in ("input_tokens", "output_tokens", "cost_usd", "latency_ms"):
            value = record.get(field)
            if (value is not None and (not isinstance(value, (int, float))
                    or isinstance(value, bool) or value < 0)):
                raise ValueError(f"agent reviewer {field} must be nonnegative or null")
    elif kind == "human":
        duration = record.get("duration_seconds")
        if (duration is not None and (not isinstance(duration, (int, float))
                or isinstance(duration, bool) or duration < 0)):
            raise ValueError("human review duration_seconds must be nonnegative or null")
    return kind


def review_origin(record, expected_input_hash=None, *, outcome_fields=(
        "verdict", "completeness", "point_reviews", "false_claim_present")):
    """Validate declared provenance; separate agents are not independent models."""
    independent = record.get("independent_reviews")
    if independent is None:
        kind = _reviewer(record, expected_input_hash)
        status = {"human": "human_reviewed", "agent": "single_agent_reviewed"}.get(kind, "legacy_unknown")
        if record.get("verdict") == "unresolved":
            status = "unresolved"
        return {"review_status": status, "selected": copy.deepcopy(record),
                "effect_review_eligible": status == "human_reviewed",
                "human_release_eligible": status == "human_reviewed"}
    if record.get("reviewer_kind") != "agent" or not isinstance(independent, list) or len(independent) != 2:
        raise ValueError("formal agent review requires exactly two independent agent reviews")
    agents = set()
    for item in independent:
        if _reviewer(item, expected_input_hash) != "agent":
            raise ValueError("independent reviewers must be explicitly agent reviewers")
        if item.get("independent") is not True or item.get("peer_reviews_visible") is not False:
            raise ValueError("independent reviews must declare isolated inputs without peer reviews")
        agents.add(item["agent"])
    if len(agents) != 2:
        raise ValueError("two distinct agent identities required")
    outcomes = [{key: item.get(key) for key in outcome_fields} for item in independent]
    selected = independent[0] if outcomes[0] == outcomes[1] else None
    adjudication = record.get("adjudication")
    if adjudication is not None:
        # The arbiter's input includes both original, separately hashed reviews.
        arbiter_input = digest({"packet_input_hash": expected_input_hash,
                                "independent_reviews": independent})
        if _reviewer(adjudication, arbiter_input) != "agent" or adjudication["agent"] in agents:
            raise ValueError("adjudication requires a third distinct agent")
        if adjudication.get("resolves_review_hashes") != [digest(item) for item in independent]:
            raise ValueError("adjudication must bind both original independent review hashes")
        selected = adjudication
    resolved = selected is not None and selected.get("verdict") != "unresolved"
    return {"review_status": "ai_reviewed" if resolved else "unresolved",
            "selected": copy.deepcopy(selected), "effect_review_eligible": resolved,
            "human_release_eligible": False}


def answer_key_input_hash(value):
    return digest({key: value.get(key) for key in
                   ("task_set_hash", "split", "tasks", "frozen_at", "not_used_for_tuning")})


def _source_root(tasks_path, manifest):
    """Resolve only the two pinned public corpora used by proof bundles."""
    project = Path(__file__).resolve().parents[3]
    repository = str((manifest or {}).get("repository", "")).lower()
    if "davegamble/cjson" in repository:
        return project / "corpus/cJSON"
    if "lwip" in repository:
        return project / "corpus/lwip"
    return None


def _line_excerpt(path, specification):
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    pieces = []
    for token in str(specification).split(","):
        match = re.fullmatch(r"\s*(\d+)(?:-(\d+))?\s*", token)
        if not match:
            raise ValueError("answer-key source lines must use N or N-M ranges")
        start, end = int(match[1]), int(match[2] or match[1])
        if not 1 <= start <= end <= len(lines):
            raise ValueError("answer-key source range is outside the pinned file")
        pieces.append(f"{path.name}:{start}-{end}\n" + "\n".join(lines[start - 1:end]))
    return "\n\n".join(pieces)


def _materialize_evidence(row, tasks_path, manifest):
    """Turn compact source ranges into the exact bytes shown to answer judges.

    The answer-key YAML remains the hashed source of truth.  This derived payload
    prevents a judge from grading only a prose gold statement while still keeping
    corpus selection restricted to the two public, pinned checkouts.
    """
    result = copy.deepcopy(row)
    payload = result.get("evidence_payload")
    if isinstance(payload, list):
        return result
    if not isinstance(payload, dict):
        result["evidence_payload"] = []
        return result
    source_root = _source_root(tasks_path, manifest)
    materialized = []
    if source_root is not None:
        for item in payload.get("source", []):
            if not isinstance(item, dict):
                raise ValueError("answer-key source evidence must be an object")
            relative = Path(str(item.get("path", "")))
            source = (source_root / relative).resolve()
            if (source_root.resolve() not in source.parents or not source.is_file()
                    or not item.get("lines")):
                raise ValueError("answer-key source evidence escaped or is missing")
            materialized.append({
                "kind": "pinned_source", "repository": manifest.get("repository"),
                "revision": manifest.get("revision"), "path": relative.as_posix(),
                "lines": str(item["lines"]), "file_sha256": digest(source.read_bytes()),
                "text": _line_excerpt(source, item["lines"]),
                "permalink": item.get("permalink"),
            })
    experiment = payload.get("experiment")
    if isinstance(experiment, dict):
        materialized.append({"kind": "experiment_declaration",
            "executed": experiment.get("executed") is True,
            "text": json.dumps(experiment, ensure_ascii=False, sort_keys=True)})
    result["evidence_payload"] = materialized
    return result


def evaluation_hash(report):
    return digest([report["protocol"], report["answer_key"], report["model"],
                   report["temperature"], report["runs_per_task"]])


def ensure_new_outputs(*paths):
    resolved = [Path(p).resolve() for p in paths]
    if len(set(resolved)) != len(resolved) or any(p.exists() for p in resolved):
        raise ValueError("output exists or paths collide; choose a new run name and retain earlier trials")


def schedule(tasks, variants, runs, seed=17):
    if isinstance(runs, bool) or runs < 1 or runs % 2 == 0:
        raise ValueError("runs 必须是正奇数；重复试次不是独立题目")
    rng = random.Random(seed)
    result = []
    for repetition in range(runs):
        order = list(tasks)
        rng.shuffle(order)
        for task in order:
            arms = list(variants)
            rng.shuffle(arms)
            result.extend({"task_id": task["id"], "repetition": repetition, "variant": arm}
                          for arm in arms)
    return result


def usage_start(client):
    return {"requests": getattr(client, "request_count", None),
            "totals": dict(getattr(client, "usage_totals", {}) or {}),
            "observations": dict(getattr(client, "usage_observations", {}) or {}),
            "measured_cost_usd": getattr(client, "measured_cost_usd", None),
            "reserved_cost_usd": getattr(client, "reserved_cost_usd", None)}


def usage_delta(client, before):
    after = usage_start(client)
    requests = (after["requests"] - before["requests"]
                if isinstance(after["requests"], int) and isinstance(before["requests"], int) else None)
    result = {"requests": requests}
    for field, output in (("prompt_tokens", "input_tokens"),
                          ("completion_tokens", "output_tokens"), ("total_tokens", "tokens")):
        observed = after["observations"].get(field, 0) - before["observations"].get(field, 0)
        measured = requests is not None and (requests == 0 or observed == requests)
        result[output] = (max(0, after["totals"].get(field, 0) - before["totals"].get(field, 0))
                          if measured else None)
    result["usage_complete"] = all(result[k] is not None for k in ("input_tokens", "output_tokens", "tokens"))
    for field, output in (("measured_cost_usd", "cost_usd"),
                          ("reserved_cost_usd", "cost_upper_bound_usd")):
        before_value, after_value = before.get(field), after.get(field)
        result[output] = (max(0.0, after_value - before_value)
                          if isinstance(before_value, (int, float))
                          and isinstance(after_value, (int, float)) else None)
    return result


def answer_key(path, tasks_path, tasks):
    """Frozen gold with explicit review provenance, never evidence of independence."""
    if path is None:
        return {"status": "not_provided", "split": "development_regression", "tasks": {},
                "effect_eligible": False, "effect_review_eligible": False,
                "human_release_eligible": False, "review_status": "unresolved",
                "reason": "semantic_gold_and_holdout_not_reviewed"}
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    task_manifest = yaml.safe_load(Path(tasks_path).read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict) or value.get("task_set_hash") != digest(Path(tasks_path).read_bytes()):
        raise ValueError("answer key must bind the unchanged task-set hash")
    if value.get("schema_version") not in {1, 2} or value.get("split") not in {"development_regression", "held_out"}:
        raise ValueError("invalid answer-key schema or split")
    rows = value.get("tasks")
    if not isinstance(rows, dict) or not {t["id"] for t in tasks} <= set(rows):
        raise ValueError("answer key must cover every selected task exactly once")
    for row in rows.values():
        points = row.get("expected_points", [])
        if not points or not isinstance(row.get("forbidden_claims"), list):
            raise ValueError("expected points and explicit forbidden claims required")
        ids = [p.get("id") for p in points]
        if len(set(ids)) != len(ids) or not all(ids):
            raise ValueError("unique expected-point ids required")
        if any(not p.get("text") or not p.get("evidence") for p in points):
            raise ValueError("each answer point needs a source/experiment evidence reference")
    origin = {"review_status": "unresolved", "effect_review_eligible": False,
              "human_release_eligible": False, "selected": None}
    if value.get("status") == "approved":
        review = value.get("review") or value
        if review.get("reviewer_kind") is None and review.get("independent_reviews") is None:
            origin = {"review_status": "legacy_unknown", "effect_review_eligible": False,
                      "human_release_eligible": False, "selected": review}
        else:
            origin = review_origin(review, answer_key_input_hash(value), outcome_fields=("verdict",))
    approved = (value.get("status") == "approved" and bool(value.get("frozen_at"))
                and origin["effect_review_eligible"]
                and (origin["selected"] or {}).get("verdict", "approved") == "approved"
                and value.get("not_used_for_tuning") is True
                and value["split"] == "held_out")
    selected_rows = {t["id"]: _materialize_evidence(rows[t["id"]], tasks_path, task_manifest)
                     for t in tasks}
    return {**value, "tasks": selected_rows,
            "content_hash": digest(Path(path).read_bytes()),
            "effect_eligible": approved, "effect_review_eligible": approved,
            "human_release_eligible": approved and origin["human_release_eligible"],
            "review_status": origin["review_status"], "review_input_hash": answer_key_input_hash(value),
            "boundary": "Declared review provenance; AI consensus is not human review, and hashes cannot prove no prior exposure."}


def paired_effect(raw, treatment, baseline, field, kinds=None, seed=17):
    """Average repeats within task, then bootstrap tasks (not repeated trials)."""
    values = {}
    for variant in (treatment, baseline):
        values[variant] = {row["id"]: statistics.mean(t[field] for t in row["repetitions"])
                           for row in raw[variant] if (kinds is None or row["type"] in kinds)}
    ids = sorted(values[treatment])
    if not ids or set(ids) != set(values[baseline]):
        return {"task_count": 0, "delta_pt": None, "ci95_pt": None}
    delta = [100 * (values[treatment][key] - values[baseline][key]) for key in ids]
    rng = random.Random(seed)
    boot = sorted(statistics.mean(rng.choices(delta, k=len(delta))) for _ in range(2000))
    return {"task_count": len(ids), "delta_pt": statistics.mean(delta),
            "ci95_pt": [boot[49], boot[1949]] if len(ids) > 1 else None,
            "unit": "paired_task_mean", "bootstrap_seed": seed}


def grouped_paired_effect(raw, treatment, baseline, field, kinds=None, seed=17):
    """Pair tasks, then aggregate and bootstrap their declared independent unit."""
    by_arm = {}
    groups = {}
    for variant in (treatment, baseline):
        by_arm[variant] = {}
        for row in raw[variant]:
            if kinds is not None and row["type"] not in kinds:
                continue
            values = [trial.get(field) for trial in row["repetitions"]]
            if not values or any(value is None for value in values):
                continue
            by_arm[variant][row["id"]] = statistics.mean(values)
            groups[row["id"]] = row.get("mechanism_id") or row.get("case_id") or row["id"]
    ids = sorted(set(by_arm[treatment]) & set(by_arm[baseline]))
    if not ids:
        return {"task_count": 0, "mechanism_count": 0, "delta_pt": None,
                "ci95_pt": None, "unit": "paired_mechanism_mean"}
    mechanism_values = {}
    for task_id in ids:
        mechanism_values.setdefault(groups[task_id], []).append(
            100 * (by_arm[treatment][task_id] - by_arm[baseline][task_id]))
    deltas = [statistics.mean(values) for values in mechanism_values.values()]
    rng = random.Random(seed)
    boot = sorted(statistics.mean(rng.choices(deltas, k=len(deltas))) for _ in range(2000))
    independent_unit = "case" if all(
        next(row for row in raw[treatment] if row["id"] == task_id).get("case_id")
        and not next(row for row in raw[treatment] if row["id"] == task_id).get("mechanism_id")
        for task_id in ids
    ) else "mechanism"
    return {"task_count": len(ids), "mechanism_count": len(deltas),
            "group_count": len(deltas), "group_unit": independent_unit,
            "delta_pt": statistics.mean(deltas),
            "ci95_pt": [boot[49], boot[1949]] if len(deltas) > 1 else None,
            "unit": f"paired_{independent_unit}_mean", "bootstrap_seed": seed,
            "mechanisms": sorted(mechanism_values)}


def repository_grouped_paired_effect(reports, treatment, baseline, field, kinds=None, seed=17):
    """Equal-weight repositories after task repeats and mechanisms are averaged.

    The value-proof benchmark has two intentionally different corpora.  Pooling
    all tasks would let the corpus with more mechanisms dominate the headline
    number, so every bootstrap draw resamples mechanisms *within* a repository
    and only then averages repository means.
    """
    repositories = []
    task_count = 0
    for report in reports:
        raw = report.get("raw_trials") or {}
        if treatment not in raw or baseline not in raw:
            continue
        by_arm, groups = {}, {}
        for variant in (treatment, baseline):
            by_arm[variant] = {}
            for row in raw[variant]:
                if kinds is not None and row.get("type") not in kinds:
                    continue
                values = [trial.get(field) for trial in row.get("repetitions", [])]
                if not values or any(value is None for value in values):
                    continue
                by_arm[variant][row["id"]] = statistics.mean(values)
                groups[row["id"]] = row.get("mechanism_id") or row["id"]
        ids = sorted(set(by_arm[treatment]) & set(by_arm[baseline]))
        mechanism_values = {}
        for task_id in ids:
            mechanism_values.setdefault(groups[task_id], []).append(
                100 * (by_arm[treatment][task_id] - by_arm[baseline][task_id]))
        if mechanism_values:
            repositories.append({
                "repository": report.get("repository") or (report.get("identity") or {}).get("repository"),
                "mechanisms": [statistics.mean(values) for values in mechanism_values.values()],
                "mechanism_ids": sorted(mechanism_values),
            })
            task_count += len(ids)
    if not repositories:
        return {"repository_count": 0, "task_count": 0, "mechanism_count": 0,
                "delta_pt": None, "ci95_pt": None,
                "unit": "equal_repository_of_paired_mechanism_means"}
    observed = statistics.mean(statistics.mean(row["mechanisms"]) for row in repositories)
    rng = random.Random(seed)
    boot = []
    for _ in range(2000):
        per_repository = [statistics.mean(rng.choices(row["mechanisms"], k=len(row["mechanisms"])))
                          for row in repositories]
        boot.append(statistics.mean(per_repository))
    boot.sort()
    return {
        "repository_count": len(repositories), "task_count": task_count,
        "mechanism_count": sum(len(row["mechanisms"]) for row in repositories),
        "delta_pt": observed,
        "ci95_pt": [boot[49], boot[1949]] if sum(
            len(row["mechanisms"]) for row in repositories) > 1 else None,
        "unit": "equal_repository_of_paired_mechanism_means",
        "bootstrap_seed": seed,
        "repositories": [{"repository": row["repository"],
                          "mechanism_count": len(row["mechanisms"]),
                          "mechanisms": row["mechanism_ids"]} for row in repositories],
    }


def validate_pairs(report):
    raw = report["raw_trials"]
    expected = None
    runs = report.get("runs_per_task")
    for rows in raw.values():
        signature = [(row["id"], row["type"], len(row["repetitions"])) for row in rows]
        if len({r[0] for r in signature}) != len(signature) or not signature:
            return False
        if any(not r[2] or (runs is not None and r[2] != runs) for r in signature):
            return False
        for row in rows:
            if any("repetition" in t for t in row["repetitions"]):
                if sorted(t.get("repetition", -1) for t in row["repetitions"]) != list(range(len(row["repetitions"]))):
                    return False
        if expected is not None and sorted(signature) != expected:
            return False
        expected = sorted(signature)
    plan = report.get("protocol", {}).get("schedule")
    if plan is not None:
        actual = {(variant, row["id"], t.get("repetition"), t.get("sequence"))
                  for variant, rows in raw.items() for row in rows for t in row["repetitions"]}
        expected_plan = {(e["variant"], e["task_id"], e["repetition"], i) for i, e in enumerate(plan)}
        if actual != expected_plan:
            return False
    declared = report.get("protocol", {}).get("variants")
    return len(raw) >= 2 and (declared is None or set(declared) == set(raw))


def error_trial(exc):
    # Exception text may include remote request headers; preserve type, not secrets.
    return {"answer": None, "citations": [], "refused": False,
            "native_planning": False, "fallback_reason": f"trial_error:{type(exc).__name__}",
            "error_type": type(exc).__name__, "events": [], "tool_calls": 0,
            "citation_valid": False, "final_safe": False}


def budget_exhausted(trial):
    marker = "BudgetExceeded"
    return (trial.get("error_type") == marker
            or marker in str(trial.get("fallback_reason") or ""))


def render(report):
    if report["status"] == "not_run":
        return (
            "# CodeAtlas 知识层对照\n\n"
            f"尚未运行：`{report['reason']}`。没有效果数字。\n\n"
            f"- 冻结题目：{report.get('task_count', 0)}\n"
            f"- 计划答案试次：{report.get('planned_trial_count', 0)}\n"
            f"- 已执行答案试次：{report.get('executed_trial_count', 0)}\n"
            f"- 题集 SHA-256：`{report.get('task_set_hash') or 'unbound'}`\n"
            f"- 答案键 SHA-256：`{report.get('answer_key_hash') or 'unbound'}`\n"
            f"- 答案键复核：`{report.get('review_status', 'unresolved')}`\n\n"
            "`not_run` 只表示材料、版本和预算协议已冻结；不能据此填写 STAR 效果数字。\n"
        )
    if report["status"] == "partial_budget":
        completed = sum(len(row["repetitions"]) for rows in report["raw_trials"].values()
                        for row in rows)
        return ("# CodeAtlas 模型对照\n\n"
                f"预算上限在第 {completed} 个已记录试次后触发；报告为 `partial_budget`。\n\n"
                "原始成功、失败和回退试次均已保留，但配对不完整，不能生成效果结论。\n")
    fmt = lambda x: "待审/未测" if x is None else f"{x:.1f}"
    lines = ["# CodeAtlas 模型对照", "", report["boundary"], "",
             f"评审来源：`{report.get('review_status', 'legacy_unknown')}`；"
             "AI 复核不等于人工盲审，同模型的多个 agent 可能共享错误。", "",
             f"协议：`{VERSION}`；范围：`{report['task_scope']}`；每题 {report['runs_per_task']} 次。",
             "重复试次用于测波动，不扩充独立样本数；安全统计所有试次。", "",
             "| 方案 | 题数 | 试次 | 待审 | 正确率 % | 完整度 % | 引用标签合法率 % |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for key, arm in report["variants"].items():
        m = arm["metrics"]
        lines.append(f"| {arm.get('name', key)} | {len(report['raw_trials'][key])} | {m['trial_count']} | "
                     f"{m['pending']} | {fmt(m['accuracy'])} | {fmt(m['completeness'])} | "
                     f"{fmt(m['effective_citation_rate'])} |")
    lines += ["", "## 门禁与成对差异", "",
              "锚点命中率仅衡量召回；带真引用的错误结论仍由答案评审判错。",
              "成本未测；上下文节省只使用同一代码题群的完整服务 usage，缺失不按 0 计算。", "",
              "```json", json.dumps(report["acceptance"], ensure_ascii=False, indent=2), "```", "",
              "未完成语义 gold 冻结、明确来源的复核或留出验证时，不得用于效果声明；"
              "AI 评分结果必须注明 AI 复核来源。", ""]
    return "\n".join(lines)
