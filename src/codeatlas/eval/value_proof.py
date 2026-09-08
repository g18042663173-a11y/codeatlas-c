"""Offline gates for a new Wiki/card value-proof campaign.

This module never calls a model.  It inventories source-grounded entry
mechanisms, excludes anything exposed by earlier materials, freezes hashes and
computes request ceilings before a paid campaign may be prepared.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from .. import db
from ..contracts import digest


class ValueProofError(ValueError):
    pass


def _load(path: Path) -> dict:
    if not path.is_file():
        raise ValueProofError(f"proof input is missing: {path}")
    if path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
    else:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueProofError(f"proof input must be an object: {path}")
    return value


def _all_text(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(str(key) + "\n" + _all_text(item) for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(_all_text(item) for item in value)
    return "" if value is None else str(value)


def _closure(conn: sqlite3.Connection, start: sqlite3.Row, *, depth: int = 2,
             maximum: int = 6) -> dict:
    queue = [(start["id"], 0)]
    nodes: dict[str, sqlite3.Row] = {}
    calls: list[tuple[str, str]] = []
    truncated = False
    while queue:
        node_id, level = queue.pop(0)
        if node_id in nodes:
            continue
        if len(nodes) >= maximum:
            truncated = True
            break
        row = conn.execute(
            "SELECT * FROM node WHERE id=? AND kind='function' AND is_definition=1", (node_id,)
        ).fetchone()
        if row is None:
            continue
        nodes[node_id] = row
        outgoing = conn.execute(
            """SELECT dn.id,dn.name,dn.path,dn.line_start FROM edge e
                 JOIN node dn ON dn.id=e.dst
                WHERE e.src=? AND e.kind='calls' AND e.confidence='certain'
                  AND dn.kind='function' AND dn.is_definition=1
                ORDER BY dn.path,dn.line_start,dn.name""", (node_id,),
        ).fetchall()
        for target in outgoing:
            calls.append((node_id, target["id"]))
            if level < depth and target["id"] not in nodes:
                queue.append((target["id"], level + 1))
            elif level >= depth:
                truncated = True
    marks = ",".join("?" for _ in nodes) or "NULL"
    fact_rows = conn.execute(
        f"SELECT id,function_id,kind,payload_json,path,line_start,line_end FROM semantic_fact "
        f"WHERE confidence='certain' AND function_id IN ({marks}) ORDER BY path,line_start,id",
        sorted(nodes),
    ).fetchall()
    facts = [{key: row[key] for key in row.keys()} for row in fact_rows]
    return {
        "nodes": [{"id": row["id"], "name": row["name"], "usr": row["usr"],
                   "path": row["path"], "line_start": row["line_start"],
                   "line_end": row["line_end"], "definition_hash": row["definition_hash"]}
                  for row in nodes.values()],
        "calls": calls, "facts": facts,
        "scope": {"depth": depth, "max_functions": maximum,
                  "truncated": truncated or bool(queue)},
    }


def _matches_prefix(path: str, prefixes: list[str]) -> bool:
    return any(path == prefix or path.startswith(prefix) for prefix in prefixes)


def _round_robin(candidates: list[dict], count: int) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in candidates:
        groups[item["module"]].append(item)
    for rows in groups.values():
        rows.sort(key=lambda item: (item["path"], item["line_start"], item["entry"], item["id"]))
    selected = []
    names = sorted(groups)
    while names and len(selected) < count:
        remaining = []
        for name in names:
            if groups[name] and len(selected) < count:
                selected.append(groups[name].pop(0))
            if groups[name]:
                remaining.append(name)
        names = remaining
    return selected


def _module(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else "root"


def _review_closure(closure: dict) -> dict:
    """Freeze source anchors and hashes, not megabytes of derived payload text."""
    fact_refs = [{
        "id": row["id"], "function_id": row["function_id"], "kind": row["kind"],
        "path": row["path"], "line_start": row["line_start"],
        "line_end": row["line_end"], "payload_hash": digest(row["payload_json"]),
    } for row in closure["facts"]]
    return {
        "nodes": closure["nodes"],
        "calls": closure["calls"],
        "fact_count": len(fact_refs),
        "fact_kinds": sorted({row["kind"] for row in fact_refs}),
        "facts_hash": digest(fact_refs),
        "scope": closure["scope"],
    }


def inventory(manifest_path: str | Path, *, project_root: str | Path = ".") -> dict:
    root = Path(project_root).resolve()
    manifest_file = (root / manifest_path).resolve() if not Path(manifest_path).is_absolute() else Path(manifest_path)
    manifest = _load(manifest_file)
    if manifest.get("schema_version") != 1:
        raise ValueProofError("value-proof inventory requires schema_version 1")
    target = manifest.get("target_mechanisms_per_repository")
    if type(target) is not int or target < 1:
        raise ValueProofError("target_mechanisms_per_repository must be a positive integer")
    exposure_paths = []
    exposure_text = []
    exposure_hashes = {}
    for raw in manifest.get("exposure_inputs", []):
        path = (root / raw).resolve()
        if root not in path.parents:
            raise ValueProofError("exposure input escapes project root")
        payload = _load(path)
        exposure_paths.append(str(path.relative_to(root)))
        exposure_text.append(_all_text(payload))
        exposure_hashes[str(path.relative_to(root))] = digest(path.read_bytes())
    exposed = "\n".join(exposure_text)

    corpora = []
    selected_ids: list[str] = []
    for spec in manifest.get("corpora", []):
        corpus_id = spec.get("id")
        database = (root / str(spec.get("database", ""))).resolve()
        if root not in database.parents or not database.is_file():
            raise ValueProofError(f"invalid database for {corpus_id}")
        conn = db.read_only(database)
        try:
            repository = db.get_meta(conn, "repository_id", db.get_meta(conn, "repo", ""))
            revision = db.get_meta(conn, "revision", "")
            if repository != spec.get("repository") or revision != spec.get("revision"):
                raise ValueProofError(f"snapshot identity mismatch for {corpus_id}")
            prefixes = spec.get("production_paths")
            if not isinstance(prefixes, list) or not prefixes or not all(isinstance(v, str) for v in prefixes):
                raise ValueProofError(f"production_paths are required for {corpus_id}")
            entries = conn.execute(
                """SELECT * FROM node WHERE kind='function' AND is_definition=1
                     AND is_static=0 AND usr IS NOT NULL ORDER BY path,line_start,name,id"""
            ).fetchall()
            automatic = []
            excluded = []
            fingerprints: set[str] = set()
            for entry in entries:
                if not entry["path"] or not _matches_prefix(entry["path"], prefixes):
                    continue
                closure = _closure(conn, entry)
                closure_names = sorted({node["name"] for node in closure["nodes"] if node["name"]})
                entry_exposed = bool(re.search(
                    r"(?<![A-Za-z0-9_])" + re.escape(entry["name"]) + r"(?![A-Za-z0-9_])",
                    exposed,
                ))
                shared_exposed = [name for name in closure_names if name != entry["name"] and
                                  re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) +
                                            r"(?![A-Za-z0-9_])", exposed)]
                # Exposure is defined by the public operation/entry, not by a
                # helper that happens to be shared with an older mechanism.
                # Shared helpers remain visible for the two source reviewers to
                # reject if the operation is not semantically independent.
                if entry_exposed:
                    excluded.append({"entry": entry["name"], "reason": "historical_exposure",
                                     "matched_symbols": [entry["name"]]})
                    continue
                fact_kinds = sorted({row["kind"] for row in closure["facts"]})
                if not closure["calls"] and not fact_kinds:
                    excluded.append({"entry": entry["name"], "reason": "insufficient_mechanism_facts"})
                    continue
                fingerprint = digest({
                    "entry": entry["usr"],
                    "members": sorted(node["usr"] or node["id"] for node in closure["nodes"]),
                    "calls": sorted(closure["calls"]),
                    "facts": [(row["function_id"], row["kind"], row["payload_json"])
                              for row in closure["facts"]],
                })
                if fingerprint in fingerprints:
                    excluded.append({"entry": entry["name"], "reason": "duplicate_mechanism_fingerprint"})
                    continue
                fingerprints.add(fingerprint)
                automatic.append({
                    "id": "proof-mechanism-" + digest([corpus_id, entry["usr"]])[:16],
                    "repository_id": corpus_id, "entry": entry["name"], "usr": entry["usr"],
                    "path": entry["path"], "line_start": entry["line_start"],
                    "line_end": entry["line_end"], "definition_hash": entry["definition_hash"],
                    "module": _module(entry["path"]), "mechanism_fingerprint": fingerprint,
                    "closure": _review_closure(closure),
                    "source_validation": "pending_ai_review",
                    "shared_exposed_dependencies": shared_exposed,
                })
            selected = _round_robin(automatic, target)
            selected_ids.extend(item["id"] for item in selected)
            status = "candidate_ready_for_source_review" if len(automatic) >= target else "coverage_blocked"
            corpora.append({
                "id": corpus_id, "repository": repository, "revision": revision,
                "database_hash": digest(database.read_bytes()), "production_paths": prefixes,
                "automatic_candidate_count": len(automatic), "target_count": target,
                "selected_count": len(selected), "status": status,
                "selected_candidates": selected, "excluded_count": len(excluded),
                "exclusion_summary": {reason: sum(row["reason"] == reason for row in excluded)
                                      for reason in sorted({row["reason"] for row in excluded})},
            })
        finally:
            conn.close()
    if not corpora:
        raise ValueProofError("inventory requires at least one corpus")
    base = {
        "schema_version": 1, "kind": "value_proof_coverage_inventory",
        "target_mechanisms_per_repository": target,
        "exposure_inputs": exposure_paths, "exposure_hashes": exposure_hashes,
        "corpora": corpora,
        "selected_candidate_ids": sorted(selected_ids),
        "selection_policy": "production public entries; two certain-call hops; six functions; "
                            "exact entry exposure exclusion; shared helpers flagged for source review; "
                            "module round-robin",
        "boundary": "Candidates are not questions or effect evidence. Two source reviews are required "
                    "before protocol_ready; insufficient coverage is never filled by splitting functions.",
    }
    base["inventory_hash"] = digest(base)
    base["status"] = ("pending_ai_source_review" if all(row["status"].startswith("candidate_ready")
                                                          for row in corpora)
                      else "coverage_blocked")
    base["coverage_ready"] = False
    return base


_INVENTORY_PROJECTION_FIELDS = {
    "inventory_hash", "status", "coverage_ready", "source_reviews",
    "reviewed_inventory_hash",
}


def validate_inventory_integrity(report: dict) -> dict:
    """Fail closed when either frozen coverage or its derived gate was changed.

    Review attestations are a separately sealed projection.  This keeps the
    inventory hash stable after review while making status and coverage_ready
    deterministic rather than caller-controlled booleans.
    """
    if not isinstance(report, dict):
        raise ValueProofError("coverage inventory must be an object")
    frozen = {key: value for key, value in report.items()
              if key not in _INVENTORY_PROJECTION_FIELDS}
    if report.get("inventory_hash") != digest(frozen):
        raise ValueProofError("coverage inventory content changed")
    corpora = report.get("corpora")
    if not isinstance(corpora, list) or not corpora:
        raise ValueProofError("coverage inventory requires corpora")
    blocked = False
    for corpus in corpora:
        if not isinstance(corpus, dict):
            raise ValueProofError("coverage corpus must be an object")
        target = corpus.get("target_count")
        selected = corpus.get("selected_count")
        candidates = corpus.get("automatic_candidate_count")
        if any(type(value) is not int or value < 0
               for value in (target, selected, candidates)) or target < 1:
            raise ValueProofError("coverage corpus counts are invalid")
        expected_corpus_status = (
            "candidate_ready_for_source_review"
            if candidates >= target and selected == target
            else "coverage_blocked"
        )
        if corpus.get("status") != expected_corpus_status:
            raise ValueProofError("coverage corpus status conflicts with counts")
        blocked = blocked or expected_corpus_status == "coverage_blocked"

    reviews = report.get("source_reviews")
    reviewed_hash = report.get("reviewed_inventory_hash")
    if reviews is None:
        if reviewed_hash is not None:
            raise ValueProofError("unreviewed coverage cannot have a review seal")
        expected_status = "coverage_blocked" if blocked else "pending_ai_source_review"
        expected_ready = False
    else:
        if not isinstance(reviews, list) or len(reviews) != 2:
            raise ValueProofError("coverage requires exactly two review projections")
        reviewers = set()
        for review in reviews:
            if not isinstance(review, dict) or set(review) != {
                    "reviewer_id", "reviewer_type", "input_hash", "status"}:
                raise ValueProofError("coverage review projection is invalid")
            reviewer = review.get("reviewer_id")
            if (not isinstance(reviewer, str) or not reviewer.strip()
                    or reviewer in reviewers):
                raise ValueProofError("coverage review projections require distinct reviewers")
            reviewers.add(reviewer)
            if (review.get("reviewer_type") != "ai" or review.get("status") != "approved"
                    or not re.fullmatch(r"[0-9a-f]{64}", str(review.get("input_hash", "")))):
                raise ValueProofError("coverage review projection is invalid")
        if reviewed_hash != digest([report["inventory_hash"], reviews]):
            raise ValueProofError("coverage review seal changed")
        expected_status = "coverage_blocked" if blocked else "approved"
        expected_ready = not blocked
    if report.get("status") != expected_status or report.get("coverage_ready") is not expected_ready:
        raise ValueProofError("coverage gate conflicts with frozen inventory")
    return report


def validate_reviews(report: dict, reviews: list[dict]) -> dict:
    """Bind two independent AI attestations to one immutable inventory."""
    validate_inventory_integrity(report)
    if report.get("source_reviews") is not None:
        raise ValueProofError("coverage inventory is already reviewed")
    expected = set(report.get("selected_candidate_ids", []))
    identities = set()
    valid = []
    for review in reviews:
        if review.get("schema_version") != 1 or review.get("reviewer_type") != "ai":
            raise ValueProofError("coverage review must be schema 1 ai_reviewed")
        if review.get("inventory_hash") != report["inventory_hash"]:
            raise ValueProofError("coverage review belongs to another inventory")
        reviewer = review.get("reviewer_id")
        if not reviewer or reviewer in identities:
            raise ValueProofError("coverage reviews require distinct reviewer ids")
        identities.add(reviewer)
        if review.get("status") != "approved" or set(review.get("checked_candidate_ids", [])) != expected:
            raise ValueProofError("coverage review must approve the complete selected set")
        valid.append({"reviewer_id": reviewer, "reviewer_type": "ai",
                      "input_hash": digest(review), "status": "approved"})
    if len(valid) != 2:
        raise ValueProofError("coverage requires exactly two independent AI source reviews")
    output = dict(report)
    output["source_reviews"] = valid
    blocked = any(row.get("status") == "coverage_blocked" for row in report["corpora"])
    output["status"] = "coverage_blocked" if blocked else "approved"
    output["coverage_ready"] = output["status"] == "approved"
    output["reviewed_inventory_hash"] = digest([report["inventory_hash"], valid])
    return validate_inventory_integrity(output)


def render_inventory(report: dict) -> str:
    """Render the same frozen coverage result for humans without changing it."""
    lines = [
        "# CodeAtlas Wiki 收益实验覆盖清点",
        "",
        f"- 状态：`{report.get('status', 'unknown')}`",
        f"- 覆盖门禁：`{'通过' if report.get('coverage_ready') else '未通过'}`",
        f"- 清点哈希：`{report.get('inventory_hash', 'missing')}`",
        "",
        "| 语料 | 合格未曝光机制 | 目标 | 已选 | 状态 |",
        "|---|---:|---:|---:|---|",
    ]
    for row in report.get("corpora", []):
        lines.append(
            f"| {row.get('id', '?')} | {row.get('automatic_candidate_count', 0)} "
            f"| {row.get('target_count', 0)} | {row.get('selected_count', 0)} "
            f"| `{row.get('status', 'unknown')}` |"
        )
    lines += [
        "",
        "## 判定边界",
        "",
        report.get("boundary", "未记录。"),
        "",
        "该结果只回答是否有足够的未曝光公开入口进入双份源码复核。"
        "它不是题集、模型效果或费用授权。覆盖不足时不会拆分函数、复用旧题或增加语料凑数。",
        "",
    ]
    return "\n".join(lines)


def request_budget(*, wiki_questions: int = 88, wiki_arms: int = 2,
                   wiki_runs: int = 3, reuse_cases: int = 12,
                   reuse_questions_per_case: int = 3, reuse_arms: int = 3,
                   reuse_runs: int = 3, max_agent_requests: int = 7,
                   development_rounds: int = 2, product_count: int | None = None) -> dict:
    """Return calculable ceilings; never infer provider prices or authorization."""
    values = (wiki_questions, wiki_arms, wiki_runs, reuse_cases,
              reuse_questions_per_case, reuse_arms, reuse_runs,
              max_agent_requests, development_rounds)
    if any(type(value) is not int or value < 1 for value in values):
        raise ValueProofError("request-budget counts must be positive integers")
    wiki_answers = wiki_questions * wiki_arms * wiki_runs
    reuse_answers = reuse_cases * reuse_questions_per_case * reuse_arms * reuse_runs
    formal_answers = wiki_answers + reuse_answers
    formal = {
        "wiki_answer_trials": wiki_answers,
        "wiki_generation_requests": wiki_answers * max_agent_requests,
        "reuse_answer_trials": reuse_answers,
        "reuse_generation_requests": reuse_answers,
        "double_review_requests": formal_answers * 2,
        "worst_case_arbitration_requests": formal_answers,
        "judge_qualification_requests": 36,
    }
    formal["request_upper_bound"] = sum(value for key, value in formal.items()
                                          if key.endswith("requests"))
    per_development_round = {
        "wiki_generation_requests": 24 * max_agent_requests,
        "reuse_generation_requests": 54,
        "double_review_requests": 78 * 2,
        "worst_case_arbitration_requests": 78,
    }
    per_development_round["request_upper_bound"] = sum(per_development_round.values())
    development = {
        "protocol_probe_requests": 8,
        "rounds": development_rounds,
        "per_round": per_development_round,
        "request_upper_bound": 8 + development_rounds * per_development_round["request_upper_bound"],
    }
    if product_count is not None and (type(product_count) is not int or product_count < 0):
        raise ValueProofError("product_count must be a nonnegative integer or null")
    production = {
        "product_count": product_count,
        "max_requests_per_product": 8,
        "request_upper_bound": None if product_count is None else product_count * 8,
        "status": "unpriced_until_product_count_is_frozen" if product_count is None else "counted",
    }
    return {
        "schema_version": 1, "development": development, "formal": formal,
        "knowledge_production": production,
        "estimated_cost_usd": None,
        "authorization": "not_granted_by_dry_run",
        "boundary": "Answer trials are not request counts. Pricing is unknown until explicit rates are supplied; "
                    "source-review agent usage without provider usage remains unmeasured.",
    }


_REQUIRED_EFFECT_GATES = {
    "answer_review_complete", "paired_trials_complete", "frozen_semantic_holdout",
    "review_provenance", "review_packet_complete", "judge_calibration",
    "full_three_repeat_run", "protocol_identity_bound", "raw_trials_bound",
    "review_integrity_bound", "snapshot_pinned",
}


def _effect_eligible(report: dict) -> bool:
    """Reuse the evaluator's real frozen-gold and review gates."""
    answer_key = report.get("answer_key") or {}
    gates = (report.get("acceptance") or {}).get("gates") or {}
    return (answer_key.get("effect_eligible") is True
            and answer_key.get("effect_review_eligible") is True
            and report.get("effect_review_eligible") is True
            and all(gates.get(name) is True for name in _REQUIRED_EFFECT_GATES))


def _trial_score(trial: dict, key: str):
    if key in trial:
        return trial[key]
    review = trial.get("answer_review") or trial.get("human_review") or {}
    selected = review.get("selected_score") or review
    return selected.get(key)


def _complete_scored_trials(raw: dict, *, require_false_claim: bool) -> bool:
    for rows in raw.values():
        for row in rows:
            for trial in row.get("repetitions") or []:
                correct = _trial_score(trial, "correct")
                complete = _trial_score(trial, "complete")
                if (type(correct) is not bool
                        or not isinstance(complete, (int, float)) or isinstance(complete, bool)
                        or not 0 <= complete <= 1
                        or trial.get("review_status") not in {"ai_reviewed", "human_reviewed"}):
                    return False
                if require_false_claim and type(_trial_score(trial, "false_claim_present")) is not bool:
                    return False
    return True


def wiki_claim(reports: list[dict], *, confidence_level: float = .975,
               threshold_pt: float = 10.0) -> dict:
    """Project the sole inferential Wiki claim from complete frozen reports."""
    from . import protocol
    repositories = [report.get("repository") for report in reports]

    def complete_repository(report: dict) -> bool:
        raw = report.get("raw_trials") or {}
        rows = raw.get("source_only") or []
        peer = raw.get("wiki_flat") or []
        refusal = [row for row in rows if "拒答" in str(row.get("type", ""))
                   or str(row.get("type", "")).lower() in {"refusal", "unanswerable"}]
        mechanisms = [row for row in rows if row not in refusal]
        return (
            report.get("task_scope") == "full"
            and _effect_eligible(report)
            and len(rows) == len(peer) == 44
            and len(refusal) == 4 and len(mechanisms) == 40
            and len({row.get("mechanism_id") for row in mechanisms}) == 40
            and all(row.get("mechanism_id") for row in mechanisms)
            and {row.get("id") for row in rows} == {row.get("id") for row in peer}
            and {row.get("id"): (row.get("type"), row.get("mechanism_id")) for row in rows}
                == {row.get("id"): (row.get("type"), row.get("mechanism_id")) for row in peer}
            and all(len(row.get("repetitions") or []) == 3 for arm in raw.values() for row in arm)
            and _complete_scored_trials(raw, require_false_claim=True)
        )

    eligible = (len(reports) == 2 and len(set(repositories)) == 2
                and all(isinstance(value, str) and value for value in repositories)
                and all(
        report.get("status") == "completed"
        and report.get("answer_review_status") == "completed"
        and report.get("runs_per_task") == 3
        and set((report.get("raw_trials") or {})) == {"source_only", "wiki_flat"}
        and complete_repository(report)
        and protocol.validate_pairs(report)
        for report in reports
    ))
    effect = (protocol.repository_grouped_paired_effect(
        reports, "wiki_flat", "source_only", "correct",
        kinds={"定位", "调用链", "语义检索", "影响分析"},
        confidence_level=confidence_level,
    ) if eligible else {
        "repository_count": 0, "task_count": 0, "mechanism_count": 0,
        "delta_pt": None, "confidence_interval_pt": None,
        "confidence_level": confidence_level,
    })
    interval = effect.get("confidence_interval_pt")
    supported = bool(eligible and effect.get("delta_pt") is not None
                     and effect.get("repository_count") == 2
                     and effect.get("task_count") == 80
                     and effect.get("mechanism_count") == 80
                     and effect["delta_pt"] >= threshold_pt and interval and interval[0] > 0)
    return {
        "claim": "mechanism_wiki_improves_explanation_correctness",
        "eligible": eligible, "benefit_supported": supported,
        "threshold_pt": threshold_pt, "confidence_level": confidence_level,
        "effect": effect,
        "status": "supported" if supported else "insufficient_evidence",
        "boundary": "Correctness is the sole inferential endpoint; completeness and token use are secondary.",
    }


def fixed_case_card_claim(report: dict) -> dict:
    """Describe twelve frozen cases without claiming population non-inferiority."""
    from . import protocol
    raw = report.get("raw_trials") or {}
    arms = ("raw_session", "generic_summary", "structured_card")
    cases = {row.get("case_id") for row in raw.get("raw_session", []) if row.get("case_id")}

    def complete_case_matrix() -> bool:
        expected_ids = {row.get("id") for row in raw.get("raw_session", [])}
        expected_mapping = {row.get("id"): row.get("case_id")
                            for row in raw.get("raw_session", [])}
        for arm in arms:
            rows = raw.get(arm) or []
            if (len(rows) != 36 or {row.get("id") for row in rows} != expected_ids
                    or {row.get("id"): row.get("case_id") for row in rows} != expected_mapping):
                return False
            counts: dict[str, int] = defaultdict(int)
            for row in rows:
                counts[row.get("case_id")] += 1
                repetitions = row.get("repetitions") or []
                if len(repetitions) != 3:
                    return False
                for trial in repetitions:
                    complete = _trial_score(trial, "complete")
                    tokens = trial.get("input_tokens")
                    if (type(_trial_score(trial, "correct")) is not bool
                            or type(_trial_score(trial, "false_claim_present")) is not bool
                            or not isinstance(complete, (int, float)) or isinstance(complete, bool)
                            or not 0 <= complete <= 1
                            or not isinstance(tokens, (int, float)) or isinstance(tokens, bool)
                            or tokens < 0):
                        return False
            if set(counts) != cases or any(count != 3 for count in counts.values()):
                return False
        return True

    eligible = (report.get("status") == "completed"
                and report.get("answer_review_status") == "completed"
                and report.get("task_scope") == "fixed_public_cases"
                and report.get("runs_per_task") == 3
                and _effect_eligible(report)
                and set(raw) == set(arms)
                and len(cases) == 12 and complete_case_matrix()
                and protocol.validate_pairs(report))

    def trials(arm, case=None):
        return [trial for row in raw.get(arm, []) if case is None or row.get("case_id") == case
                for trial in row.get("repetitions", [])]

    usage_complete = eligible and all(isinstance(trial.get("input_tokens"), (int, float))
                                      for arm in raw for trial in trials(arm))
    totals = {arm: (sum(trial["input_tokens"] for trial in trials(arm))
                    if usage_complete else None) for arm in raw}
    reduction = (100 * (1 - totals["structured_card"] / totals["raw_session"])
                 if usage_complete and totals["raw_session"] else None)
    per_case = []
    no_decline = eligible
    no_new_false_claim = eligible
    for case in sorted(cases) if eligible else []:
        item = {"case_id": case}
        for arm in ("raw_session", "structured_card"):
            rows = trials(arm, case)
            item[arm] = {
                "correct": sum(bool(_trial_score(row, "correct")) for row in rows) / len(rows) if rows else None,
                "complete": sum(float(_trial_score(row, "complete")) for row in rows) / len(rows) if rows else None,
                "false_claims": sum(bool(_trial_score(row, "false_claim_present")) for row in rows),
            }
        no_decline = no_decline and all(
            item["structured_card"][field] >= item["raw_session"][field]
            for field in ("correct", "complete")
        )
        no_new_false_claim = (no_new_false_claim
                              and item["structured_card"]["false_claims"] == 0)
        per_case.append(item)
    supported = bool(eligible and reduction is not None and reduction >= 20
                     and no_decline and no_new_false_claim)
    return {
        "claim": "fixed_public_case_card_compression",
        "eligible": eligible, "benefit_supported": supported,
        "status": "supported" if supported else "not_observed",
        "input_tokens": totals, "input_token_reduction_pct": reduction,
        "per_case": per_case, "quality_not_observed_to_decline": no_decline,
        "no_new_false_claim": no_new_false_claim,
        "boundary": "Observation is limited to the twelve frozen public cases; no population non-inferiority claim.",
    }
