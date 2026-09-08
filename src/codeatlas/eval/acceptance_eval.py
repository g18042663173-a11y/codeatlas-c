"""Unified cJSON/lwIP acceptance and public-claim verification.

This module deliberately sits *above* the existing parser, Wiki, task and
workflow evaluators.  It does not implement another retrieval path.  Its job is
to run the pinned pipelines, validate their artefacts, classify failures, and
make the publication state explicit.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from .. import db as dbm
from ..contracts import digest
from ..wiki import generator as wiki_generator


STATUSES = {
    "verified", "locally_verified_not_published", "publicly_verified",
    "contradicted", "unverifiable", "stale",
}
MODES = {"fast", "full", "release"}
REQUIRED_EDGE_KINDS = {"calls", "includes", "type_use", "field_access", "global_ref"}
KNOWLEDGE_METRICS = {
    "tool_trace_legal_rate", "wiki_first_order_rate", "a_anchor_complete_rate",
    "card_gate_correct_rate", "markdown_rebuild_consistency_rate",
    "refusal_accuracy", "raw_session_indexed_count",
}
IMPLEMENTATION_INPUTS = (
    "src", "tests", "eval", "examples", "knowledge", ".github/workflows",
    "demo.sh", "pyproject.toml", "uv.lock",
)


class AcceptanceEvalError(ValueError):
    """The requested acceptance run cannot be trusted or completed."""


def _load_yaml(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        value = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AcceptanceEvalError(f"无法读取 YAML：{target}: {exc}") from exc
    if not isinstance(value, dict):
        raise AcceptanceEvalError(f"YAML 根节点必须是对象：{target}")
    return value


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceEvalError(f"无法读取 JSON 报告：{target}: {exc}") from exc
    if not isinstance(value, dict):
        raise AcceptanceEvalError(f"JSON 报告根节点必须是对象：{target}")
    return value


def _proof_runtime_identity(kind: str, root: Path) -> dict[str, str]:
    """Hashes that make an old proof report stale after evaluator changes."""
    codeatlas = root / "src/codeatlas"
    if kind == "wiki_knowledge":
        from .rubric import RUBRIC
        from ..contracts import POLICY_VERSION
        return {
            "implementation_hash": digest({
                str(item.relative_to(codeatlas)): digest(item.read_bytes())
                for item in sorted(codeatlas.rglob("*.py"))
            }),
            "rubric_hash": digest(RUBRIC),
            "policy_hash": digest(POLICY_VERSION),
            "prompt_hash": digest((codeatlas / "llm/client.py").read_bytes()),
        }
    if kind == "knowledge_reuse":
        from . import knowledge_reuse_eval, protocol, rubric
        return {
            "evaluator_hash": digest(Path(knowledge_reuse_eval.__file__).read_bytes()),
            "shared_protocol_hash": digest(Path(protocol.__file__).read_bytes()),
            "rubric_hash": digest(Path(rubric.__file__).read_bytes()),
            "model_client_hash": digest((codeatlas / "llm/client.py").read_bytes()),
            "prompt_hash": digest(knowledge_reuse_eval.SYSTEM),
        }
    if kind == "maintenance":
        from . import maintenance_eval
        return {
            "evaluator_hash": _sha256(Path(maintenance_eval.__file__)),
            "dependency_policy_hash": _sha256(codeatlas / "dependencies.py"),
        }
    return {}


def load_corpora(path: str | Path) -> dict[str, Any]:
    raw = _load_yaml(path)
    if raw.get("schema_version") != 1 or not isinstance(raw.get("corpora"), list):
        raise AcceptanceEvalError("双语料清单必须是 schema_version: 1 且包含 corpora 列表")
    required = {
        "id", "role", "official_url", "checkout_url", "source_relation", "revision",
        "source_root", "compile_db", "database", "data_dir", "wiki_dir",
        "task_manifest", "task_report", "workflow_manifest", "workflow_report",
        "current_task_manifest", "current_task_report",
        "success_session", "insufficient_session", "unrelated_session",
        "formal_card",
    }
    seen: set[str] = set()
    corpora: list[dict[str, Any]] = []
    for item in raw["corpora"]:
        if not isinstance(item, dict):
            raise AcceptanceEvalError("corpora 条目必须是对象")
        missing = sorted(key for key in required if not item.get(key))
        if missing:
            raise AcceptanceEvalError(f"语料条目缺少字段：{', '.join(missing)}")
        if item["id"] in seen:
            raise AcceptanceEvalError(f"重复语料 id：{item['id']}")
        if item["source_relation"] not in {"official", "mirror"}:
            raise AcceptanceEvalError(f"{item['id']} source_relation 只能是 official/mirror")
        if item["role"] not in {"correctness", "scale"}:
            raise AcceptanceEvalError(f"{item['id']} role 只能是 correctness/scale")
        formal_card = item.get("formal_card")
        formal_required = {"id", "review_bundle_hash", "release", "release_sha256"}
        if (not isinstance(formal_card, dict)
                or any(not formal_card.get(key) for key in formal_required)):
            raise AcceptanceEvalError(
                f"{item['id']} formal_card 必须声明 "
                "id、review_bundle_hash、release 与 release_sha256"
            )
        seen.add(item["id"])
        corpora.append(dict(item))
    if seen != {"cjson", "lwip"}:
        raise AcceptanceEvalError("双语料清单必须恰好声明 cjson 与 lwip")
    return {**raw, "corpora": corpora}


def load_claims(path: str | Path) -> dict[str, Any]:
    raw = _load_yaml(path)
    if raw.get("schema_version") != 1 or not isinstance(raw.get("claims"), list):
        raise AcceptanceEvalError("声明清单必须是 schema_version: 1 且包含 claims 列表")
    allowed = {"source_identity", "task_count", "failure_count", "publication"}
    seen: set[str] = set()
    for claim in raw["claims"]:
        if not isinstance(claim, dict) or not claim.get("id") or claim.get("kind") not in allowed:
            raise AcceptanceEvalError("声明必须包含唯一 id 和受支持的 kind")
        if claim["id"] in seen:
            raise AcceptanceEvalError(f"重复声明 id：{claim['id']}")
        seen.add(claim["id"])
    return raw


def _git(project_root: Path, *args: str, check: bool = False) -> str:
    result = subprocess.run(
        ["git", *args], cwd=project_root, text=True, capture_output=True, check=False,
    )
    if check and result.returncode:
        raise AcceptanceEvalError(result.stderr.strip() or f"git {' '.join(args)} 失败")
    return result.stdout.strip() if result.returncode == 0 else ""


def implementation_hash(project_root: str | Path) -> str:
    """Hash the executable/evaluation inputs while excluding generated reports.

    A report-only commit necessarily changes Git HEAD after an evaluation.  This
    content hash lets release verification prove that the implementation and
    fixtures are still byte-identical to the version that produced the full
    snapshot, without pretending the earlier dirty HEAD was the tested commit.
    """
    root = Path(project_root).resolve()
    files: list[Path] = []
    for value in IMPLEMENTATION_INPUTS:
        target = root / value
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(path for path in target.rglob("*") if path.is_file())
    files = sorted(
        (path for path in files if "__pycache__" not in path.parts
         and path.suffix not in {".pyc", ".pyo"}),
        key=lambda path: str(path.relative_to(root)),
    )
    digest = hashlib.sha256()
    for path in files:
        relative = str(path.relative_to(root)).replace(os.sep, "/")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256(path)))
        digest.update(b"\0")
    return digest.hexdigest()


def git_identity(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root).resolve()
    revision = _git(root, "rev-parse", "HEAD") or "unversioned"
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    return {
        "revision": revision,
        "branch": _git(root, "branch", "--show-current") or "detached",
        "dirty": bool(status),
        "dirty_entries": len(status.splitlines()) if status else 0,
        "origin": _git(root, "remote", "get-url", "origin"),
        "implementation_hash": implementation_hash(root),
    }


def _tracked(project_root: Path, path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return False
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(rel)], cwd=project_root,
        text=True, capture_output=True, check=False,
    )
    return result.returncode == 0


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _canonical_git_url(value: str) -> str:
    url = value.strip().removesuffix("/").removesuffix(".git")
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url.split(":", 1)[1]
    return url


def _source_checkout(corpus: dict[str, Any], root: Path) -> dict[str, Any]:
    source_root = _resolve(root, corpus["source_root"])
    if not source_root.is_dir():
        return {"ok": False, "error": "source_root_missing"}

    def source_git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(source_root), *args], text=True,
            capture_output=True, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    revision = source_git("rev-parse", "HEAD")
    origin = source_git("remote", "get-url", "origin")
    revision_ok = revision == corpus["revision"]
    origin_ok = _canonical_git_url(origin) == _canonical_git_url(corpus["checkout_url"])
    return {
        "ok": revision_ok and origin_ok,
        "revision": revision or None, "expected_revision": corpus["revision"],
        "origin": origin or None, "expected_origin": corpus["checkout_url"],
        "revision_ok": revision_ok, "origin_ok": origin_ok,
    }


def _compile_database(corpus: dict[str, Any], root: Path) -> dict[str, Any]:
    source_root = _resolve(root, corpus["source_root"])
    compile_db = _resolve(root, corpus["compile_db"])
    if not source_root.is_dir() or not compile_db.is_file():
        return {"ok": False, "error": "source_root_or_compile_db_missing"}
    try:
        entries = json.loads(compile_db.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "error": "compile_db_invalid"}
    if not isinstance(entries, list) or not entries:
        return {"ok": False, "error": "compile_db_empty"}
    escaped: list[str] = []
    for item in entries:
        if not isinstance(item, dict) or not item.get("file"):
            escaped.append("<invalid-entry>")
            continue
        directory = Path(item.get("directory") or source_root)
        if not directory.is_absolute():
            directory = (root / directory).resolve()
        source = Path(item["file"])
        resolved = source.resolve() if source.is_absolute() else (directory / source).resolve()
        if source_root != resolved and source_root not in resolved.parents:
            escaped.append(str(resolved))
    return {
        "ok": not escaped,
        "translation_units": len(entries),
        "sha256": _sha256(compile_db),
        "escaped_files": escaped[:10],
    }


def _task_gold(manifest_path: Path, corpus: dict[str, Any]) -> dict[str, Any]:
    raw = _load_yaml(manifest_path)
    tasks = raw.get("tasks") if isinstance(raw.get("tasks"), list) else []
    complete = bool(tasks) and all(
        task.get("id") and task.get("status") == "approved" and task.get("rationale")
        and task.get("source_permalink") and (task.get("reviewer") or raw.get("reviewer"))
        and (task.get("reviewed_at") or raw.get("reviewed_at"))
        for task in tasks if isinstance(task, dict)
    ) and len(tasks) == sum(isinstance(task, dict) for task in tasks)
    return {
        "ok": complete and raw.get("repository") == corpus["checkout_url"]
        and raw.get("revision") == corpus["revision"] and bool(raw.get("failure_policy")),
        "count": len(tasks), "sha256": _sha256(manifest_path),
        "reviewer": raw.get("reviewer"), "reviewed_at": raw.get("reviewed_at"),
    }


def _failure_breakdown(report: dict[str, Any]) -> dict[str, Any]:
    blocked = []
    unexpected = []
    for failure in (report.get("summary") or {}).get("failures") or []:
        if failure.get("type") == "已审核经验复用" and any(
            "approved B" in error or "B 卡" in error for error in failure.get("errors") or []
        ):
            blocked.append(failure)
        else:
            unexpected.append(failure)
    return {
        "blocked_by_review": blocked,
        "unexpected": unexpected,
        "blocked_count": len(blocked),
        "unexpected_count": len(unexpected),
    }


def _validate_task_report(report_path: Path, corpus: dict[str, Any], gold: dict[str, Any]) -> dict[str, Any]:
    if not report_path.is_file():
        return {"ok": False, "error": "task_report_missing"}
    report = _read_json(report_path)
    manifest = report.get("manifest") or {}
    summary = report.get("summary") or {}
    acceptance = report.get("acceptance") or {}
    reproducibility = report.get("reproducibility") or {}
    identity_ok = (
        manifest.get("repository") == corpus["checkout_url"]
        and manifest.get("revision") == corpus["revision"]
        and len(manifest.get("tasks") or []) == gold["count"]
    )
    impact = summary.get("impact") or {}
    refusal = summary.get("refusal") or {}
    failure_breakdown = _failure_breakdown(report)
    task_complete = (
        summary.get("passed_tasks") == summary.get("total_tasks") == gold["count"]
        and failure_breakdown["unexpected_count"] == 0
        and failure_breakdown["blocked_count"] == 0
    )
    gates = {
        "identity": identity_ok,
        "manifest_hash": reproducibility.get("task_manifest_sha256") == gold["sha256"],
        "retrieval": acceptance.get("recall_gain_pt", -999) >= 10.0
        and acceptance.get("mrr_delta", -999) >= -0.02,
        "impact": impact.get("f1") == 1.0,
        "refusal": refusal.get("accuracy") == 100.0,
        "no_unexpected_failures": failure_breakdown["unexpected_count"] == 0,
        "no_review_blockers": failure_breakdown["blocked_count"] == 0,
        "task_complete": task_complete,
    }
    return {
        "ok": all(gates.values()), "gates": gates, "summary": summary,
        "acceptance": acceptance, "failure_breakdown": failure_breakdown,
        "task_complete": task_complete,
        "reproducibility": reproducibility,
        "sha256": _sha256(report_path), "generated_at": report.get("generated_at"),
    }


def _validate_current_task_report(
        report_path: Path, corpus: dict[str, Any], gold: dict[str, Any]) -> dict[str, Any]:
    """Validate five current release-card questions without abusing ablation gates."""
    if not report_path.is_file():
        return {"ok": False, "error": "current_task_report_missing"}
    report = _read_json(report_path)
    manifest = report.get("manifest") or {}
    summary = report.get("summary") or {}
    reproducibility = report.get("reproducibility") or {}
    experiences = report.get("experiences") or []
    failures = _failure_breakdown(report)
    identity_ok = (
        manifest.get("repository") == corpus["checkout_url"]
        and manifest.get("revision") == corpus["revision"]
        and len(manifest.get("tasks") or []) == gold["count"] == 5
    )
    all_experience = len(experiences) == 5 and all(
        item.get("type") == "已审核经验复用" for item in experiences
    )
    evidence_current = all_experience and all(
        item.get("passed") is True
        and item.get("blocked_by_review") is False
        and (item.get("attempts") or {}).get("完整无模型", {}).get("hit") is True
        and (item.get("attempts") or {}).get("完整无模型", {}).get(
            "evidence_complete") is True
        for item in experiences
    )
    complete = (
        summary.get("passed_tasks") == summary.get("total_tasks") == 5
        and failures["unexpected_count"] == 0
        and failures["blocked_count"] == 0
        and evidence_current
    )
    gates = {
        "identity": identity_ok,
        "manifest_hash": reproducibility.get("task_manifest_sha256") == gold["sha256"],
        "five_release_tasks": all_experience,
        "current_b_evidence": evidence_current,
        "no_unexpected_failures": failures["unexpected_count"] == 0,
        "no_review_blockers": failures["blocked_count"] == 0,
        "complete": complete,
    }
    return {
        "ok": all(gates.values()), "current_release_complete": all(gates.values()),
        "gates": gates, "summary": summary, "failure_breakdown": failures,
        "reproducibility": reproducibility, "sha256": _sha256(report_path),
        "generated_at": report.get("generated_at"),
    }


def _validate_workflow(report_path: Path, corpus: dict[str, Any]) -> dict[str, Any]:
    if not report_path.is_file():
        return {"ok": False, "error": "workflow_report_missing"}
    report = _read_json(report_path)
    metrics = report.get("metrics") or {}
    identity_ok = report.get("repository") == corpus["checkout_url"] and report.get("revision") == corpus["revision"]
    expected = {
        "tool_trace_legal_rate": 100.0,
        "wiki_first_order_rate": 100.0,
        "a_anchor_complete_rate": 100.0,
        "card_gate_correct_rate": 100.0,
        "markdown_rebuild_consistency_rate": 100.0,
        "refusal_accuracy": 100.0,
        "raw_session_indexed_count": 0,
    }
    gates = {key: metrics.get(key) == value for key, value in expected.items()}
    return {
        "ok": identity_ok and set(expected) <= set(metrics) and all(gates.values()),
        "identity": identity_ok, "gates": gates, "metrics": metrics,
        "sha256": _sha256(report_path), "generated_at": report.get("generated_at"),
        "synthetic_review": "synthetic_test_review",
    }


def model_abc_status(corpora: list[dict[str, Any]], *,
                     project_root: str | Path) -> dict[str, Any]:
    """Load only snapshot-matched A/B/C reports; absence is explicitly not_run."""
    root = Path(project_root).resolve()
    items: list[dict[str, Any]] = []
    for corpus in corpora:
        path = root / "docs" / f"MODEL-ABC-{str(corpus['id']).upper()}.json"
        if not path.is_file():
            items.append({"id": corpus["id"], "status": "not_run", "passed": False})
            continue
        try:
            report = _read_json(path)
            from . import rubric
            database = dbm.query(_resolve(root, corpus["database"]))
            try:
                current_identity = rubric.identity(database, _resolve(root, corpus["task_manifest"]))
            finally:
                database.close()
            identity_ok = (
                report.get("identity") == current_identity
                and report.get("answer_review_status") == "completed"
                and bool(report.get("raw_trials"))
                and report.get("status") == "completed"
                and report.get("repository") == corpus["checkout_url"]
                and report.get("revision") == corpus["revision"]
                and report.get("task_set_hash") == _sha256(
                    _resolve(root, corpus["task_manifest"])
                )
                and report.get("runs_per_task") == 3
            )
            if identity_ok and report.get("acceptance", {}).get("passed") is True:
                # Recompute instead of trusting a hand-edited green flag.
                from copy import deepcopy
                checked = deepcopy(report)
                rubric.finalize(checked)
                identity_ok = checked["acceptance"] == report["acceptance"] and checked["acceptance"]["passed"]
        except (AcceptanceEvalError, OSError, sqlite3.Error, KeyError, TypeError, ValueError, AttributeError):
            identity_ok = False
            report = {}
        status = "completed" if identity_ok else "stale"
        items.append({
            "id": corpus["id"], "status": status,
            "passed": identity_ok and (report.get("acceptance") or {}).get("passed") is True,
            "model": report.get("model"), "generated_at": report.get("generated_at"),
            "report": str(path.relative_to(root)),
        })
    completed = sum(item["status"] == "completed" for item in items)
    if completed == len(items):
        status = "completed"
    elif completed:
        status = "partial"
    elif any(item["status"] == "stale" for item in items):
        status = "stale"
    else:
        status = "not_run"
    return {"status": status, "passed": bool(items) and all(item["passed"] for item in items),
            "corpora": items}


def _close_number(left, right, tolerance=1e-8):
    if left is None or right is None:
        return left is right
    return (isinstance(left, (int, float)) and not isinstance(left, bool)
            and isinstance(right, (int, float)) and not isinstance(right, bool)
            and abs(float(left) - float(right)) <= tolerance)


def _review_validity_notice(spec, root, report, report_path):
    """Bind a later defect finding to its exact historical report and raw run.

    An invalidation never grants effect eligibility, nor does it edit the old
    scores. A missing/tampered notice must not silently restore their authority.
    """
    notice_path = spec.get("review_validity")
    if not notice_path:
        return None
    notice = _read_json(_resolve(root, notice_path))
    payload = {k: v for k, v in notice.items() if k != "audit_hash"}
    if (notice.get("audit_hash") != digest(payload)
            or notice.get("schema_version") != 1
            or notice.get("status") not in {"invalid_review", "pending_evidence_audit"}
            or notice.get("scope") != "review_only"
            or notice.get("historical_report_sha256") != _sha256(report_path)
            or notice.get("run_hash") != report.get("run_hash")):
        raise ValueError("review validity notice does not match historical report")
    return {"report": notice_path, "status": notice["status"],
            "reason": notice.get("reason"), "audit_hash": notice["audit_hash"],
            "mapping_broken_trials": notice.get("mapping_broken_trials"),
            "trial_count": notice.get("trial_count")}


def _verify_curated_effect_report(report: dict[str, Any]) -> dict[str, Any]:
    """Recompute public outcome metrics without committing bulky answers.

    The ignored campaign ledger remains the source for answer/evidence audits;
    this Git-safe projection must still contain every trial, semantic outcome
    and immutable source hash needed to detect edited aggregate claims.
    """
    from .task_eval import REFUSAL_TYPE
    projection = report.get("curated_projection") or {}
    rows = report.get("trial_results")
    if (projection.get("schema_version") != 1 or not isinstance(rows, list)
            or projection.get("trial_results_hash") != digest(rows)
            or projection.get("trial_count") != len(rows)
            or projection.get("run_hash") != report.get("run_hash")
            or projection.get("review_packet_hash") != report.get("review_packet_hash")
            or projection.get("review_protocol_hash") != report.get("review_protocol_hash")
            or projection.get("review_version_hash") != report.get("review_version_hash")
            or len(rows) != report.get("executed_trial_count")):
        return {"ok": False, "reason": "curated_projection_binding"}
    ids = [row.get("trial_id") for row in rows]
    if (len(set(ids)) != len(ids) or any(not value for value in ids)
            or any(not re.fullmatch(r"[0-9a-f]{64}", str(row.get("raw_trial_hash") or ""))
                   for row in rows)
            or any(not re.fullmatch(r"[0-9a-f]{64}", str(row.get("selected_review_hash") or ""))
                   for row in rows)):
        return {"ok": False, "reason": "curated_trial_identity"}
    variants = set((report.get("variants") or {}).keys())
    if variants != {row.get("variant") for row in rows}:
        return {"ok": False, "reason": "curated_variant_identity"}
    pending_total = 0
    for variant in variants:
        trials = [row for row in rows if row["variant"] == variant]
        pending = sum(row.get("review_status") == "unresolved" for row in trials)
        pending_total += pending
        metrics = report["variants"][variant].get("metrics") or {}
        accuracy = None if pending else 100 * sum(bool(row.get("correct")) for row in trials) / len(trials)
        completeness = None if pending else 100 * sum(float(row.get("complete") or 0) for row in trials) / len(trials)
        citation = 100 * sum(bool(row.get("citation_valid")) for row in trials) / len(trials)
        non_refusal = [row for row in trials if row.get("task_type") != REFUSAL_TYPE]
        planning = 100 * sum(bool(row.get("native_planning")) for row in non_refusal) / max(1, len(non_refusal))
        if (metrics.get("trial_count") != len(trials) or metrics.get("pending") != pending
                or not _close_number(metrics.get("accuracy"), accuracy)
                or not _close_number(metrics.get("completeness"), completeness)
                or not _close_number(metrics.get("effective_citation_rate"), citation)
                or not _close_number(metrics.get("native_planning_success_rate"), planning)):
            return {"ok": False, "reason": f"curated_metrics:{variant}"}
    attempt = report.get("review_attempt") or {}
    if (attempt.get("unresolved_trial_count") != pending_total
            or bool(attempt.get("terminal_semantic_unresolved")) != bool(pending_total)):
        return {"ok": False, "reason": "curated_unresolved_count"}
    expected_status = "unresolved" if pending_total else "ai_reviewed"
    if report.get("review_status") != expected_status:
        return {"ok": False, "reason": "curated_review_status"}
    return {"ok": True, "unresolved_trial_count": pending_total,
            "review_complete": pending_total == 0}


def proof_status(manifest_path: str | Path, *, project_root: str | Path) -> dict[str, Any]:
    """Validate three proof families without turning AI review into human release."""
    root = Path(project_root).resolve()
    path = _resolve(root, manifest_path)
    if not path.is_file():
        return {"status": "not_configured", "items": [], "experiments_complete": False,
                "reviews_complete": False, "claims_complete": False, "claims": [],
                "ai_review_done": False, "human_review_done": False,
                "effect_observed": False}
    manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("experiments"), list):
        raise AcceptanceEvalError("三类证明清单格式无效")
    qualification_spec = manifest.get("judge_qualification") or {}
    qualification = {"status": "not_run", "qualified": False}
    if qualification_spec:
        policy_path = _resolve(root, qualification_spec.get("policy", ""))
        qualification_path = _resolve(root, qualification_spec.get("report", ""))
        if policy_path.is_file() and qualification_path.is_file():
            try:
                qualification_report = _read_json(qualification_path)
                report_hash = qualification_report.pop("report_hash", None)
                report_hash_ok = report_hash == digest(qualification_report)
                qualification_report["report_hash"] = report_hash
                identity = qualification_report.get("identity") or {}
                policy_hash_ok = identity.get("policy_sha256") == _sha256(policy_path)
                review = qualification_report.get("review") or {}
                selected_role = qualification_report.get("selected")
                selected_qualification = qualification_report.get(selected_role) or {}
                qualified = bool(
                    report_hash_ok and policy_hash_ok
                    and selected_role in {"primary", "fallback"}
                    and selected_qualification.get("status") == "calibrated"
                    and selected_qualification.get("passed") is True
                )
                qualification = {
                    **qualification_report, "qualified": qualified,
                    "integrity_ok": report_hash_ok and policy_hash_ok,
                    "policy": qualification_spec.get("policy"),
                    "report": qualification_spec.get("report"),
                }
            except (OSError, ValueError, TypeError, KeyError):
                qualification = {"status": "invalid", "qualified": False,
                                 "integrity_ok": False}
    items = []
    wiki_reports = []
    for spec in manifest["experiments"]:
        source = _resolve(root, spec["manifest"])
        answer = _resolve(root, spec["answer_key"]) if spec.get("answer_key") else None
        generated_artifact = (_resolve(root, spec["generated_artifact"])
                              if spec.get("generated_artifact") else None)
        report_path = _resolve(root, spec["report"])
        base = {
            "id": spec["id"], "kind": spec["kind"],
            "manifest": spec["manifest"], "report": spec["report"],
            "required_for_effect": bool(spec.get("required_for_effect", True)),
            "generated_artifact": spec.get("generated_artifact"),
        }
        if (not source.is_file() or (answer is not None and not answer.is_file())
                or (generated_artifact is not None and not generated_artifact.is_file())):
            items.append({**base, "status": "materials_missing", "identity_ok": False})
            continue
        expected_manifest = _sha256(source)
        expected_answer = _sha256(answer) if answer else None
        if not report_path.is_file():
            items.append({**base, "status": "not_run", "identity_ok": False,
                          "manifest_sha256": expected_manifest})
            continue
        try:
            report = _read_json(report_path)
            actual_manifest = (report.get("manifest_hash")
                               or (report.get("identity") or {}).get("manifest_hash")
                               or report.get("manifest_sha256")
                               or report.get("task_set_hash"))
            identity_ok = actual_manifest == expected_manifest
            if answer:
                actual_answer = (report.get("answer_key_hash")
                                 or (report.get("answer_key") or {}).get("content_hash"))
                identity_ok = identity_ok and actual_answer == expected_answer
            if generated_artifact:
                identity_ok = identity_ok and (
                    (report.get("identity") or {}).get("generated_artifact_hash")
                    == _sha256(generated_artifact)
                )
            expected_runtime = _proof_runtime_identity(spec["kind"], root)
            actual_runtime = report.get("identity") or {}
            runtime_identity_ok = all(
                actual_runtime.get(key) == value for key, value in expected_runtime.items())
            detached_review_identity_ok = False
            compact_verification = None
            review_attempt = report.get("review_attempt") or {}
            curated_hash = report.get("curated_report_hash")
            if review_attempt and curated_hash:
                curated_payload = dict(report)
                curated_payload.pop("curated_report_hash", None)
                if (report.get("curated_projection") or {}).get("schema_version") == 1:
                    compact_verification = _verify_curated_effect_report(report)
                    detached_review_identity_ok = bool(
                        digest(curated_payload) == curated_hash
                        and compact_verification.get("ok") is True
                        and review_attempt.get("raw_answer_run_hash") == report.get("run_hash")
                        and review_attempt.get("judge_model")
                        and review_attempt.get("calibration_hash")
                    )
                else:
                    history = report.get("review_history") or []
                    submitted = ((history[-1].get("reviews") or {}).get("review_protocol")
                                 if history else None)
                    submitted_calibration = (submitted or {}).get("calibration_hash")
                    if not submitted_calibration:
                        submitted_calibration = (report.get("judge_calibration") or {}).get(
                            "calibration_manifest_hash")
                    collection_integrity = (submitted or {}).get("collection_integrity") or {}
                    recovery_collection_bound = bool(
                        (submitted or {}).get("version") == "format-gap-recovery-v1"
                        and (submitted or {}).get("base_batch_id")
                        and (submitted or {}).get("base_result_hash")
                        and (submitted or {}).get("run_hashes")
                        and (submitted or {}).get("packet_hashes")
                    )
                    detached_review_identity_ok = bool(
                        digest(curated_payload) == curated_hash
                        and review_attempt.get("raw_answer_run_hash") == report.get("run_hash")
                        and isinstance(submitted, dict)
                        and review_attempt.get("review_protocol_hash") == digest(submitted)
                        and review_attempt.get("judge_model") == submitted.get("judge_model")
                        and review_attempt.get("calibration_hash") == submitted_calibration
                        and (collection_integrity.get("raw_results_verified") is True
                             or recovery_collection_bound)
                    )
            # A deliberately detached review may score an immutable answer
            # collection after the implementation has moved on.  Keep that
            # drift visible, but do not invalidate the bound raw answers.
            identity_ok = identity_ok and (runtime_identity_ok or detached_review_identity_ok)
            execution_complete = bool((report.get("acceptance") or {}).get("execution_complete"))
            review_status = (report.get("review_status") or report.get("answer_review_status")
                             or ("deterministic_oracle" if spec["kind"] == "maintenance"
                                 and execution_complete
                                 and (report.get("acceptance") or {}).get(
                                     "oracle_independently_validated") is True else None))
            state = report.get("status", "invalid")
            if not identity_ok:
                state = "stale"
            acceptance = report.get("acceptance") or {}
            proof_integrity_ok = True
            execution_integrity_ok = True
            claim_supported = False
            if spec["kind"] == "maintenance" and identity_ok and state == "executed":
                from . import maintenance_eval
                verified = (
                    maintenance_eval.verify_upstream_report(report, source)
                    if report.get("evaluation") == maintenance_eval.UPSTREAM_EVALUATION
                    else maintenance_eval.verify_report(report, source)
                )
                proof_integrity_ok = verified["integrity_ok"]
                execution_integrity_ok = proof_integrity_ok
                execution_complete = verified["execution_complete"]
                acceptance = verified["acceptance"]
                review_status = verified["review_status"]
                state = "completed" if execution_complete else "invalid"
                claim_supported = bool(state == "completed" and verified["claim_supported"])
            elif spec["kind"] == "maintenance" and identity_ok and state != "not_run":
                # ``completed`` is a registry projection, never a valid raw
                # maintenance-report state.  Only an ``executed`` report that
                # survives verify_report may be promoted to it.
                state = "invalid"
                proof_integrity_ok = False
                execution_integrity_ok = False
                execution_complete = False
            elif (spec["kind"] != "maintenance" and identity_ok and state == "completed"
                  and compact_verification is not None):
                checked_acceptance = report.get("acceptance") or {}
                checked_gates = checked_acceptance.get("gates") or {}
                execution_integrity_names = {
                    "paired_trials_complete", "full_three_repeat_run",
                    "protocol_identity_bound", "raw_trials_bound", "snapshot_pinned",
                }
                review_integrity_names = {
                    "answer_review_complete", "paired_trials_complete",
                    "frozen_semantic_holdout", "review_provenance",
                    "review_packet_complete", "judge_calibration",
                    "full_three_repeat_run", "protocol_identity_bound",
                    "raw_trials_bound", "review_integrity_bound", "snapshot_pinned",
                }
                execution_integrity_ok = bool(
                    compact_verification.get("ok")
                    and all(checked_gates.get(name) is True
                            for name in execution_integrity_names)
                )
                review_complete = compact_verification.get("review_complete") is True
                proof_integrity_ok = bool(
                    review_complete
                    and all(checked_gates.get(name) is True
                            for name in review_integrity_names)
                )
                execution_complete = execution_integrity_ok
                if not review_complete:
                    state = "pending_review"
                elif not proof_integrity_ok:
                    state = "invalid"
                acceptance = checked_acceptance
                review_status = report.get("review_status", "unresolved")
                claim_supported = bool(
                    proof_integrity_ok and state == "completed"
                    and acceptance.get("effect_observed")
                )
            elif spec["kind"] != "maintenance" and identity_ok and state == "completed":
                # Never trust the stored green flag. Recompute scoring projections
                # from immutable trials/reviews and require every non-outcome proof
                # gate before the registry can call the experiment reviewed.
                from copy import deepcopy
                from . import protocol, rubric
                checked = deepcopy(report)
                rubric.finalize(checked)
                checked_acceptance = checked.get("acceptance") or {}
                execution_integrity_names = {
                    "paired_trials_complete", "full_three_repeat_run",
                    "protocol_identity_bound", "raw_trials_bound", "snapshot_pinned",
                }
                review_integrity_names = {
                    "answer_review_complete", "paired_trials_complete",
                    "frozen_semantic_holdout", "review_provenance",
                    "review_packet_complete", "judge_calibration",
                    "full_three_repeat_run", "protocol_identity_bound",
                    "raw_trials_bound", "review_integrity_bound", "snapshot_pinned",
                }
                checked_gates = checked_acceptance.get("gates") or {}
                execution_integrity_ok = all(
                    checked_gates.get(name) is True for name in execution_integrity_names)
                proof_integrity_ok = all(
                    checked_gates.get(name) is True for name in review_integrity_names)
                execution_complete = bool(
                    report.get("status") == "completed" and execution_integrity_ok
                )
                review_complete = checked_gates.get("answer_review_complete") is True
                if checked_acceptance != acceptance:
                    proof_integrity_ok = False
                if not review_complete:
                    state = "pending_review"
                elif not proof_integrity_ok:
                    state = "invalid"
                acceptance = checked_acceptance
                review_status = checked.get("review_status", "unresolved") if proof_integrity_ok else "unresolved"
                claim_supported = bool(
                    proof_integrity_ok and state == "completed"
                    and acceptance.get("effect_observed"))
            review_validity = _review_validity_notice(spec, root, report, report_path)
            if review_validity:
                state, review_status = review_validity["status"], "unresolved"
                proof_integrity_ok, claim_supported = False, False
            primary = acceptance.get("primary_contrast") or {}
            baselines = primary.get("baselines") or []
            primary_comparison = (
                (acceptance.get("paired_code_comparisons") or {}).get(baselines[0], {})
                if baselines else {}
            )
            items.append({
                **base, "status": state, "identity_ok": identity_ok,
                "review_status": review_status,
                "review_validity": review_validity,
                "invalidated_review_count": (report.get("executed_trial_count", 0)
                    if review_validity and review_validity["status"] == "invalid_review" else 0),
                "withheld_review_count": report.get("executed_trial_count", 0) if review_validity else 0,
                "effect_observed": claim_supported, "claim_supported": claim_supported,
                "proof_integrity_ok": proof_integrity_ok,
                "execution_complete": bool(execution_complete),
                "execution_integrity_ok": execution_integrity_ok,
                "claim_evidence": {} if review_validity else {
                    "primary_contrast": primary,
                    "accuracy": primary_comparison.get("accuracy"),
                    "answer_completeness": primary_comparison.get("answer_completeness"),
                    "input_token_reduction_pct": primary_comparison.get(
                        "code_input_token_reduction_pct"),
                    "current_decisions_passed": acceptance.get("current_decisions_passed"),
                    "oracle_independently_validated": acceptance.get(
                        "oracle_independently_validated"),
                    "unsafe_missed_invalidation": acceptance.get(
                        "unsafe_missed_invalidation"),
                    "current_false_invalidation": acceptance.get(
                        "current_false_invalidation"),
                    "maintenance_value_measured": acceptance.get(
                        "maintenance_value_measured"),
                },
                "human_release_eligible": bool(acceptance.get("human_release_eligible")),
                "terminal_semantic_unresolved": bool(
                    (report.get("review_attempt") or {}).get(
                        "terminal_semantic_unresolved")),
                "executed_trial_count": report.get("executed_trial_count"),
                "unresolved_trial_count": (report.get("review_attempt") or {}).get(
                    "unresolved_trial_count", 0),
                "unresolved_trials": [
                    {key: trial.get(key) for key in
                     ("task_id", "variant", "repetition", "trial_id")}
                    for trial in (report.get("trial_results") or [])
                    if trial.get("review_status") == "unresolved"
                ],
                "runtime_identity_ok": runtime_identity_ok,
                "detached_review_identity_ok": detached_review_identity_ok,
                "manifest_sha256": expected_manifest, "report_sha256": _sha256(report_path),
            })
            if spec["kind"] == "wiki_knowledge" and identity_ok and state == "completed":
                wiki_reports.append(report)
        except (OSError, ValueError, TypeError, KeyError, AcceptanceEvalError):
            items.append({**base, "status": "invalid", "identity_ok": False})
    required = [item for item in items if item["required_for_effect"]]
    experiments_complete = bool(required) and all(
        item.get("execution_complete") is True
        and item.get("execution_integrity_ok") is True
        and item.get("identity_ok") is True
        for item in required
    )
    qualification_required = bool(qualification_spec)
    reviews_complete = ((not qualification_required or qualification.get("qualified") is True)
                        and experiments_complete and all(
        item.get("proof_integrity_ok") is True
        and item.get("status") == "completed"
        and item.get("review_status") in {
            "ai_reviewed", "human_reviewed", "deterministic_oracle"
        }
        for item in required))
    ai_review_done = reviews_complete and all(
        item.get("review_status") in {"ai_reviewed", "human_reviewed", "deterministic_oracle"}
        for item in required)
    human_candidates = [item for item in required if item["kind"] != "maintenance"]
    human_review_done = reviews_complete and bool(human_candidates) and all(
        item.get("review_status") == "human_reviewed" for item in human_candidates)
    effect_observed = ai_review_done and all(item.get("claim_supported") for item in required)

    claims = []
    for item in required:
        if not item.get("execution_complete") or not item.get("execution_integrity_ok"):
            claim_state = "not_measured"
        elif item.get("terminal_semantic_unresolved"):
            # All pre-registered calls ran, but at least one qualified arbiter
            # could not resolve the semantic point.  This is a terminal
            # insufficient-evidence claim, while reviews_complete stays false.
            claim_state = "insufficient_evidence"
        elif not item.get("proof_integrity_ok") or item.get("review_status") not in {
                "ai_reviewed", "human_reviewed", "deterministic_oracle"}:
            claim_state = "unresolved"
        elif item.get("claim_supported"):
            claim_state = "supported"
        else:
            # A completed, reviewed negative result is still a completed experiment.
            # The report does not manufacture success: it records that the
            # preregistered benefit was insufficient or the system regressed.
            interval = ((item.get("claim_evidence") or {}).get("accuracy") or {}).get(
                "ci95_pt")
            reviewed_regression = bool(
                isinstance(interval, list) and len(interval) == 2
                and isinstance(interval[1], (int, float)) and interval[1] < 0
            )
            maintenance_failure = bool(
                item["kind"] == "maintenance"
                and (item.get("claim_evidence") or {}).get(
                    "oracle_independently_validated") is True
                and (
                    (item.get("claim_evidence") or {}).get(
                        "unsafe_missed_invalidation") is True
                    or (
                        (item.get("claim_evidence") or {}).get(
                            "unsafe_missed_invalidation") is None
                        and (item.get("claim_evidence") or {}).get(
                            "current_decisions_passed") is False
                    )
                )
            )
            claim_state = "regressed" if reviewed_regression or maintenance_failure \
                else "insufficient_evidence"
        claims.append({
            "id": item["id"], "kind": item["kind"], "state": claim_state,
            "report": item["report"], "claim_supported": item.get("claim_supported", False),
            "evidence": item.get("claim_evidence"),
            "unresolved_trial_count": item.get("unresolved_trial_count", 0),
        })
    claims_complete = bool(claims) and all(
        claim["state"] not in {"not_measured", "unresolved"} for claim in claims
    )
    wiki_combined = {
        "status": "not_measured", "reason": "both_reviewed_wiki_reports_required",
        "primary_contrast": {"treatment": "wiki_flat", "baseline": "source_only"},
        "accuracy": None, "answer_completeness": None,
    }
    if len(wiki_reports) == 2 and all(
            report.get("answer_review_status") == "completed" for report in wiki_reports):
        from . import protocol
        kinds = {"定位", "调用链", "语义检索", "影响分析"}
        wiki_combined = {
            "status": "measured",
            "primary_contrast": {"treatment": "wiki_flat", "baseline": "source_only"},
            "accuracy": protocol.repository_grouped_paired_effect(
                wiki_reports, "wiki_flat", "source_only", "correct", kinds=kinds),
            "answer_completeness": protocol.repository_grouped_paired_effect(
                wiki_reports, "wiki_flat", "source_only", "complete", kinds=kinds),
            "boundary": "题内重复均值 → 仓库内机制等权 → 两仓库等权；重复试次不增加独立样本数。",
        }
    return {
        "status": "completed" if experiments_complete else ("partial" if any(
            item.get("execution_complete") for item in items) else "not_run"),
        "manifest_sha256": _sha256(path), "items": items,
        "experiments_complete": experiments_complete,
        "reviews_complete": reviews_complete,
        "claims_complete": claims_complete,
        "claims": claims,
        "ai_review_done": ai_review_done, "human_review_done": human_review_done,
        "effect_observed": effect_observed, "wiki_combined_effect": wiki_combined,
        "judge_qualification": qualification,
    }


def _validate_database(corpus: dict[str, Any], root: Path) -> dict[str, Any]:
    db_path = _resolve(root, corpus["database"])
    if not db_path.is_file() or db_path.stat().st_size < 4096:
        return {"ok": False, "error": "database_missing_or_empty"}
    conn = dbm.query(db_path)
    try:
        repository = dbm.get_meta(conn, "repository_id", dbm.get_meta(conn, "repo", ""))
        revision = dbm.get_meta(conn, "revision", "")
        degraded = dbm.get_meta(conn, "degraded", "1")
        parse_stats_raw = dbm.get_meta(conn, "parse_stats", "")
        try:
            parse_stats = json.loads(parse_stats_raw) if parse_stats_raw else {}
        except json.JSONDecodeError:
            parse_stats = {}
        edge_counts = {row["kind"]: row["c"] for row in conn.execute(
            "SELECT kind,COUNT(*) c FROM edge GROUP BY kind"
        )}
        candidate = conn.execute(
            "SELECT COUNT(*) c FROM edge WHERE confidence='candidate'"
        ).fetchone()["c"]
        branches = conn.execute("SELECT COUNT(*) c FROM branch_fact").fetchone()["c"]
        wiki_dir = (Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent / "wiki"
                    if dbm.get_meta(conn, "knowledge_set_id") else _resolve(root, corpus["wiki_dir"]))
        wiki = wiki_generator.integrity(conn, out_dir=str(wiki_dir))
        gates = {
            "identity": repository == corpus["checkout_url"] and revision == corpus["revision"],
            "compile_database": degraded == "0" and bool(dbm.get_meta(conn, "build_config_hash", "")),
            "diagnostics": parse_stats.get("tu_failed") == 0 and parse_stats.get("diag_error") == 0,
            "relations": REQUIRED_EDGE_KINDS <= set(edge_counts) and candidate > 0 and branches > 0,
            "wiki": wiki.get("ok") is True and wiki.get("pages")
            and wiki.get("file_coverage") and wiki.get("module_scope_ok") is True,
        }
        return {
            "ok": all(gates.values()), "gates": gates, "repository": repository,
            "revision": revision, "build_config_hash": dbm.get_meta(conn, "build_config_hash", ""),
            "knowledge_set_id": dbm.get_meta(conn, "knowledge_set_id"),
            "source_content_hash": dbm.get_meta(conn, "source_content_hash"),
            "parse_stats": parse_stats, "edge_counts": edge_counts,
            "candidate_edges": candidate, "branch_facts": branches, "wiki": wiki,
        }
    finally:
        conn.close()


def _validate_formal_card(corpus: dict[str, Any], root: Path) -> dict[str, Any]:
    """Verify a user-confirmed card independently from legacy task gold.

    The fixed card id and review-bundle hash live in the corpus manifest.  A
    passing task report is deliberately not part of this check: old evaluation
    questions may target an earlier example card, while release of the current
    card still has its own auditable truth.
    """
    spec = corpus["formal_card"]
    release_path = _resolve(root, spec["release"])
    try:
        from ..experience.release import load_release
        package, _markdown = load_release(release_path, project_root=root)
        release_gates = {
            "release_file": _sha256(release_path) == spec["release_sha256"],
            "release_card_id": package.get("card_id") == spec["id"],
            "release_bundle": package.get("review_bundle_hash") == spec["review_bundle_hash"],
            "release_repository": package.get("repository") == corpus["checkout_url"],
            "release_revision": package.get("revision") == corpus["revision"],
        }
    except (OSError, ValueError, KeyError, TypeError):
        release_gates = {
            "release_file": False, "release_card_id": False,
            "release_bundle": False, "release_repository": False,
            "release_revision": False,
        }
    db_path = _resolve(root, corpus["database"])
    control = dbm.read_only(db_path)
    conn = dbm.query(db_path)
    try:
        row = conn.execute(
            """SELECT id,status,artifact_scope,review_bundle_hash,publication_op_id,
                      canonical_path,card_markdown
                 FROM experience WHERE id=?""",
            (spec["id"],),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "formal_card_missing", "id": spec["id"]}
        from ..experience import store
        current, current_reason = store.approved_current(conn, spec["id"])
        approval = conn.execute(
            """SELECT reviewer,reviewer_type,evaluation_scope,created_at
                 FROM experience_review
                WHERE exp_id=? AND action='approve' ORDER BY id DESC LIMIT 1""",
            (spec["id"],),
        ).fetchone()
        confirmation = conn.execute(
            """SELECT reviewer,reviewer_type,confirmed_at
                 FROM review_confirmation
                WHERE exp_id=? AND bundle_hash=? AND reviewer_type='human' LIMIT 1""",
            (spec["id"], spec["review_bundle_hash"]),
        ).fetchone()
        qa = conn.execute(
            """SELECT reviewer,reviewer_type,evaluation_scope,reviewed_at
                 FROM experience_qa
                WHERE exp_id=? AND status='passed' AND bundle_hash=?
                  AND reviewer_type='human' AND evaluation_scope='formal' LIMIT 1""",
            (spec["id"], spec["review_bundle_hash"]),
        ).fetchone()
        # Publication recovery is control-plane history.  Immutable query
        # snapshots carry the approved card projection but intentionally do
        # not duplicate the journal.
        journal = control.execute(
            "SELECT state,committed_at FROM publication_journal WHERE op_id=?",
            (row["publication_op_id"],),
        ).fetchone() if row["publication_op_id"] else None
        markdown_path = Path(row["canonical_path"] or "")
        try:
            resolved_markdown = markdown_path.resolve(strict=True)
            markdown_inside_project = resolved_markdown == root or root in resolved_markdown.parents
            markdown_matches = (
                markdown_inside_project
                and resolved_markdown.read_text(encoding="utf-8") == (row["card_markdown"] or "")
            )
        except (OSError, RuntimeError):
            markdown_inside_project = False
            markdown_matches = False
            resolved_markdown = markdown_path
        gates = {
            **release_gates,
            "approved_current": current,
            "status": row["status"] == "approved",
            "formal_scope": (row["artifact_scope"] or "formal") == "formal",
            "frozen_bundle": row["review_bundle_hash"] == spec["review_bundle_hash"],
            "human_approval": bool(
                approval and approval["reviewer_type"] == "human"
                and approval["evaluation_scope"] == "formal"
            ),
            "human_confirmation": confirmation is not None,
            "human_qa": qa is not None,
            "publication_committed": bool(journal and journal["state"] == "committed"),
            "markdown_committed": markdown_matches,
        }
        return {
            "ok": all(gates.values()), "id": spec["id"], "gates": gates,
            "review_bundle_hash": row["review_bundle_hash"],
            "expected_review_bundle_hash": spec["review_bundle_hash"],
            "approved_current_reason": current_reason,
            "reviewer": confirmation["reviewer"] if confirmation else None,
            "confirmed_at": confirmation["confirmed_at"] if confirmation else None,
            "canonical_path": str(resolved_markdown),
            "release": spec["release"],
            "release_sha256": spec["release_sha256"],
        }
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"formal_card_invalid: {exc}", "id": spec["id"]}
    finally:
        conn.close()
        control.close()


def validate_corpus(corpus: dict[str, Any], *, project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source_checkout = _source_checkout(corpus, root)
    compile_db = _compile_database(corpus, root)
    gold = _task_gold(_resolve(root, corpus["task_manifest"]), corpus)
    current_gold = _task_gold(_resolve(root, corpus["current_task_manifest"]), corpus)
    database = _validate_database(corpus, root)
    tasks = _validate_task_report(_resolve(root, corpus["task_report"]), corpus, gold)
    current_tasks = _validate_current_task_report(
        _resolve(root, corpus["current_task_report"]), corpus, current_gold)
    workflow = _validate_workflow(_resolve(root, corpus["workflow_report"]), corpus)
    formal_card = _validate_formal_card(corpus, root)
    report_build_matches = (
        tasks.get("reproducibility", {}).get("build_config_hash")
        == database.get("build_config_hash")
    )
    if database.get("knowledge_set_id"):
        raw_task = _read_json(_resolve(root, corpus["task_report"])) if _resolve(root, corpus["task_report"]).is_file() else {}
        report_build_matches = report_build_matches and all(
            raw_task.get(key) == database.get(key) for key in ("knowledge_set_id", "source_content_hash")
        )
    current_report_build_matches = (
        current_tasks.get("reproducibility", {}).get("build_config_hash")
        == database.get("build_config_hash")
    )
    if database.get("knowledge_set_id"):
        current_path = _resolve(root, corpus["current_task_report"])
        raw_current = _read_json(current_path) if current_path.is_file() else {}
        current_report_build_matches = current_report_build_matches and all(
            raw_current.get(key) == database.get(key)
            for key in ("knowledge_set_id", "source_content_hash")
        )
    gates = {
        "source_checkout": source_checkout.get("ok") is True,
        "compile_database": compile_db.get("ok") is True,
        "database": database.get("ok") is True,
        "task_gold": gold.get("ok") is True and gold.get("count") == 20,
        "task_metrics": tasks.get("ok") is True and report_build_matches,
        "knowledge_workflow": workflow.get("ok") is True,
        "formal_card": formal_card.get("ok") is True,
        "current_task_gold": current_gold.get("ok") is True and current_gold.get("count") == 5,
        "current_release_tasks": current_tasks.get("ok") is True
        and current_report_build_matches,
    }
    foundation_gates = {
        key: gates[key] for key in (
            "source_checkout", "compile_database", "database", "task_gold",
            "knowledge_workflow",
        )
    }
    return {
        "id": corpus["id"], "role": corpus["role"], "passed": all(gates.values()),
        "foundation_passed": all(foundation_gates.values()),
        "task_complete": tasks.get("task_complete") is True and report_build_matches,
        "current_release_complete": current_tasks.get("current_release_complete") is True
        and current_report_build_matches,
        "gates": gates, "source_checkout": source_checkout,
        "compile_database": compile_db, "database": database,
        "task_gold": gold, "tasks": tasks, "current_task_gold": current_gold,
        "current_tasks": current_tasks, "workflow": workflow,
        "formal_card": formal_card,
    }


def validate_release_snapshot(
        corpora_manifest: dict[str, Any], *, project_root: str | Path,
        snapshot_path: str | Path = "docs/ACCEPTANCE-CJSON-LWIP.json") -> list[dict[str, Any]]:
    """Validate the previously committed full snapshot without requiring local DBs."""
    root = Path(project_root).resolve()
    target = _resolve(root, str(snapshot_path))
    if not target.is_file():
        raise AcceptanceEvalError("release 缺少已生成的 full 双语料快照")
    snapshot = _read_json(target)
    if snapshot.get("mode") != "full" or snapshot.get("passed") is not True:
        raise AcceptanceEvalError("release 只接受已通过的 full 双语料快照")
    tested_hash = (snapshot.get("codeatlas") or {}).get("implementation_hash")
    if not tested_hash or tested_hash != implementation_hash(root):
        raise AcceptanceEvalError("release 源码/题集内容与 full 快照不一致，请重新运行 full")
    previous = {item.get("id"): item for item in snapshot.get("corpora") or []}
    results: list[dict[str, Any]] = []
    for corpus in corpora_manifest["corpora"]:
        item = previous.get(corpus["id"])
        if not item or item.get("passed") is not True:
            raise AcceptanceEvalError(f"release 快照缺少已通过语料：{corpus['id']}")
        expected = {
            "task_manifest": _sha256(_resolve(root, corpus["task_manifest"])),
            "task_report": _sha256(_resolve(root, corpus["task_report"])),
            "workflow_report": _sha256(_resolve(root, corpus["workflow_report"])),
        }
        actual = {
            "task_manifest": (item.get("task_gold") or {}).get("sha256"),
            "task_report": (item.get("tasks") or {}).get("sha256"),
            "workflow_report": (item.get("workflow") or {}).get("sha256"),
        }
        if actual != expected:
            raise AcceptanceEvalError(
                f"release 快照文件哈希失配：{corpus['id']}，请先重新运行 full 并提交"
            )
        results.append(item)
    return results


def _raw_github_url(origin: str, revision: str, path: str) -> str | None:
    value = origin.removesuffix(".git")
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value.split(":", 1)[1]
    if not value.startswith("https://github.com/"):
        return None
    slug = value.removeprefix("https://github.com/")
    return f"https://raw.githubusercontent.com/{slug}/{revision}/{path}"


def _network_ok(url: str, timeout: float = 8.0) -> bool:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "CodeAtlas-release-check/1"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return False


def publication_status(
        *, project_root: str | Path, report_paths: list[str | Path], release: bool,
        network_checker: Callable[[str], bool] = _network_ok) -> dict[str, Any]:
    root = Path(project_root).resolve()
    identity = git_identity(root)
    resolved = [_resolve(root, str(path)) for path in report_paths]
    missing = [str(path) for path in resolved if not path.is_file()]
    tracked = [path for path in resolved if path.is_file() and _tracked(root, path)]
    if missing:
        return {"status": "unverifiable", "reason": "report_missing", "missing": missing}
    if identity["dirty"] or len(tracked) != len(resolved):
        return {
            "status": "locally_verified_not_published", "reason": "dirty_or_untracked",
            "dirty": identity["dirty"], "tracked_reports": len(tracked),
            "total_reports": len(resolved), "codeatlas": identity,
        }
    if not release:
        return {"status": "verified", "reason": "tracked_clean_local", "codeatlas": identity}
    if identity["revision"] == "unversioned" or not identity["origin"]:
        return {"status": "unverifiable", "reason": "git_identity_missing", "codeatlas": identity}
    remote = _git(root, "ls-remote", identity["origin"])
    if identity["revision"] not in remote:
        return {"status": "unverifiable", "reason": "commit_not_on_remote", "codeatlas": identity}
    urls = []
    for path in resolved:
        relative = str(path.relative_to(root)).replace(os.sep, "/")
        url = _raw_github_url(identity["origin"], identity["revision"], relative)
        if not url:
            return {"status": "unverifiable", "reason": "unsupported_remote", "codeatlas": identity}
        urls.append(url)
    accessible = {url: network_checker(url) for url in urls}
    if not all(accessible.values()):
        return {"status": "unverifiable", "reason": "public_url_unavailable",
                "urls": accessible, "codeatlas": identity}
    return {"status": "publicly_verified", "reason": "remote_commit_and_reports_accessible",
            "urls": accessible, "codeatlas": identity}


def evaluate_claims(
        corpora_manifest: dict[str, Any], claims_manifest: dict[str, Any], *,
        project_root: str | Path, release: bool = False,
        network_checker: Callable[[str], bool] = _network_ok) -> list[dict[str, Any]]:
    root = Path(project_root).resolve()
    corpora = {item["id"]: item for item in corpora_manifest["corpora"]}
    reports: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for claim in claims_manifest["claims"]:
        corpus = corpora.get(claim.get("corpus"))
        kind = claim["kind"]
        result = {
            "id": claim["id"], "kind": kind, "claim": claim.get("claim", ""),
            "required": claim.get("required", True),
        }
        if kind == "publication":
            report_paths = [
                path
                for item in corpora.values()
                for path in (item["task_report"], item["workflow_report"])
            ]
            if release:
                report_paths.extend([
                    "docs/ACCEPTANCE-CJSON-LWIP.md",
                    "docs/ACCEPTANCE-CJSON-LWIP.json",
                ])
            publication = publication_status(
                project_root=root,
                report_paths=report_paths,
                release=release, network_checker=network_checker,
            )
            result.update(publication)
        elif corpus is None:
            result.update(status="unverifiable", reason="unknown_corpus")
        elif kind == "source_identity":
            relation = corpus["source_relation"]
            expected = claim.get("expected_relation")
            ok = relation == expected and bool(corpus.get("official_url")) and bool(corpus.get("checkout_url"))
            result.update(
                status="verified" if ok else "contradicted",
                actual={"official_url": corpus["official_url"], "checkout_url": corpus["checkout_url"],
                        "source_relation": relation},
            )
        else:
            report_path = _resolve(root, corpus["task_report"])
            if not report_path.is_file():
                result.update(status="unverifiable", reason="task_report_missing")
                results.append(result)
                continue
            report = reports.setdefault(corpus["id"], _read_json(report_path))
            manifest = report.get("manifest") or {}
            if manifest.get("revision") != corpus["revision"]:
                result.update(status="stale", reason="report_revision_mismatch")
            elif (report.get("reproducibility") or {}).get("task_manifest_sha256") != _sha256(
                    _resolve(root, corpus["task_manifest"])):
                result.update(status="stale", reason="task_manifest_hash_mismatch")
            elif kind == "task_count":
                actual = len(manifest.get("tasks") or [])
                result.update(status="verified" if actual == claim.get("asserted") else "contradicted",
                              asserted=claim.get("asserted"), actual=actual)
            else:
                breakdown = _failure_breakdown(report)
                # A review-blocked experience task is not a failed retrieval or
                # wrong answer.  Historical claims phrased as "N failures" are
                # therefore compared only with unexpected capability failures;
                # total not-passed tasks remain visible as a separate field.
                actual = breakdown["unexpected_count"]
                result.update(
                    status="verified" if actual == claim.get("asserted") else "contradicted",
                    asserted=claim.get("asserted"), actual=actual,
                    not_passed_total=len((report.get("summary") or {}).get("failures") or []),
                    failure_breakdown={
                        "unexpected": breakdown["unexpected_count"],
                        "blocked_by_review": breakdown["blocked_count"],
                    },
                )
        if result.get("status") not in STATUSES:
            raise AcceptanceEvalError(f"声明 {claim['id']} 产生了非法状态")
        results.append(result)
    return results


def _peak_rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(value / divisor, 1)


def _run(command: list[str], *, cwd: Path,
         env: dict[str, str] | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    result = subprocess.run(command, cwd=cwd, text=True, check=False, env=env)
    elapsed = round(time.perf_counter() - started, 3)
    stage = {"command": command, "exit_code": result.returncode,
             "duration_s": elapsed, "peak_rss_mb": _peak_rss_mb()}
    if result.returncode:
        raise AcceptanceEvalError(f"验收步骤失败（{result.returncode}）：{' '.join(command)}")
    return stage


def execute_pipelines(mode: str, *, project_root: str | Path) -> list[dict[str, Any]]:
    root = Path(project_root).resolve()
    if mode == "release":
        return []
    demo_env = os.environ.copy()
    demo_env["CODEATLAS_PYTHON"] = sys.executable
    stages = [_run([sys.executable, "-m", "pytest", "-q"], cwd=root)]
    stages.append(_run(["bash", "examples/reproductions/cjson-nesting/run.sh"], cwd=root))
    stages.append(_run(["bash", "demo.sh", "workflow"], cwd=root, env=demo_env))
    if mode == "full":
        stages.append(_run(["bash", "examples/reproductions/lwip-netif/run.sh"], cwd=root))
        stages.append(_run(["bash", "demo.sh", "lwip"], cwd=root, env=demo_env))
        stages.append(_run([
            sys.executable, "-m", "codeatlas.cli", "workflow-eval",
            "--db", "data/lwip.db", "--data-dir", "data/lw",
            "--work-dir", "data/workflow-eval-lwip",
            "--manifest", "eval/workflow_lwip.yaml",
            "--success-session", "examples/conversations/lwip-netif-review.json",
            "--insufficient-session", "examples/conversations/lwip-insufficient-review.json",
            "--unrelated-session", "examples/conversations/lwip-unrelated-request.json",
            "--out", "docs/WORKFLOW-EVAL-LWIP.md",
        ], cwd=root))
    return stages


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def render_markdown(result: dict[str, Any]) -> str:
    publication = result["publication"]
    lines = [
        "# CodeAtlas cJSON / lwIP 双语料验收", "",
        f"> 模式：`{result['mode']}`；基础能力：**{'通过' if result['foundation_passed'] else '失败'}**；"
        f"旧 40 题：**{'完成' if result['legacy_task_complete'] else '30/40 保留'}**；"
        f"当前卡 10 题：**{'完成' if result['current_release_complete'] else '未完成'}**；"
        f"作品集：**{'就绪' if result['portfolio_ready'] else '未就绪'}**；"
        f"公开状态：`{publication['status']}`。", "", "## 可复现身份", "",
        f"- CodeAtlas：`{result['codeatlas']['revision']}`；branch `{result['codeatlas']['branch']}`；"
        f"dirty={result['codeatlas']['dirty']}",
        f"- 实现内容哈希：`{result['codeatlas']['implementation_hash']}`",
        f"- 生成时间：`{result['generated_at']}`", "", "## 双语料矩阵", "",
        "| 语料 | 角色 | 基础能力 | 固定源码/compile DB | 代码事实/Wiki | 旧 20 题 | 当前卡 5 题 | 知识闭环 | 正式卡 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in result["corpora"]:
        mark = lambda value: "通过" if value else "失败"
        lines.append(
            f"| {item['id']} | {item['role']} | {mark(item['foundation_passed'])} | "
            f"{mark(item['gates'].get('source_checkout', False) and item['gates']['compile_database'])} | "
            f"{mark(item['gates']['database'])} | "
            f"{mark(item['gates']['task_metrics'])} | "
            f"{mark(item['gates'].get('current_release_tasks', False))} | "
            f"{mark(item['gates']['knowledge_workflow'])} | "
            f"{mark(item['gates'].get('formal_card', False))} |"
        )
    for item in result["corpora"]:
        task = item["tasks"]
        if task.get("summary"):
            summary = task["summary"]
            breakdown = task["failure_breakdown"]
            lines.extend([
                "", f"### {item['id']} 任务结果", "",
                f"- 总任务：{summary.get('passed_tasks')}/{summary.get('total_tasks')}；"
                f"真实能力失败 {breakdown['unexpected_count']}；审核阻塞 {breakdown['blocked_count']}。",
                f"- Recall@10：BM25 {summary.get('baseline', {}).get('recall')}% → "
                f"完整方案 {summary.get('full', {}).get('recall')}%；"
                f"MRR {summary.get('full', {}).get('mrr')}。",
            ])
        current = item.get("current_tasks") or {}
        if current.get("summary"):
            current_summary = current["summary"]
            lines.append(
                f"- 当前发布卡验收：{current_summary.get('passed_tasks')}/"
                f"{current_summary.get('total_tasks')}；该结果不回写旧 gold。"
            )
    lines.extend(["", "## 公开声明验证", "", "| 声明 | 状态 | 实际 |", "|---|---|---|"])
    for claim in result["claims"]:
        actual = claim.get("actual", claim.get("reason", "—"))
        if isinstance(actual, (dict, list)):
            actual = json.dumps(actual, ensure_ascii=False, sort_keys=True)
        lines.append(f"| {claim['id']} | `{claim['status']}` | {actual} |")
    lines.extend(["", "## 模型边界", "",
                  f"- 旧整套 A/B/C 报告：`{result['model_abc'].get('status', 'not_run')}`；"
                  "它与下方三类价值证明分开，未运行时不以规则结果替代模型数字。",
                  "- 合成测试审核只验证门禁，不作为真实经验复用效果。", ""])
    for item in result["model_abc"].get("corpora", []):
        lines.append(
            f"- `{item['id']}`：`{item['status']}`；发布门禁 "
            f"{'通过' if item.get('passed') else '未通过'}。"
        )
    proof = result.get("proof") or {}
    scored_items = [item for item in proof.get("items", [])
                    if item.get("kind") != "maintenance"
                    and isinstance(item.get("executed_trial_count"), int)]
    scored_total = sum(item["executed_trial_count"] for item in scored_items)
    scored_terminal = scored_total - sum(
        int(item.get("executed_trial_count") or 0) if item.get("review_validity")
        else int(item.get("unresolved_trial_count") or 0) for item in scored_items)
    lines.extend(["", "## 三类价值证明", "",
                  f"- 工程底座：`{'passed' if result.get('engineer_pass') else 'not_ready'}`",
                  f"- 预登记实验执行：`{'completed' if proof.get('experiments_complete') else 'not_completed'}`",
                  f"- 可用于效果汇总的复核终局：`{scored_terminal}/{scored_total}`（待证据审计不等于判错）"
                  if scored_total else "- 双审／独立真值复核：`not_run`",
                  f"- 正式知识卡人工发布：`{'completed' if result.get('human_release') else 'not_completed'}`",
                  f"- 可选人工答案复核：`{'completed' if proof.get('human_review_done') else 'not_run'}`（不冒充 AI 双审）",
                  f"- 所有主张均有终态：`{str(bool(proof.get('claims_complete'))).lower()}`",
                  f"- 所有预登记收益均得到支持：`{str(bool(proof.get('effect_observed'))).lower()}`（不作为作品集就绪的硬门槛）",
                  f"- 公开复现：`{'completed' if result.get('public_repro') else 'not_completed'}`", ""])
    for item in proof.get("items", []):
        lines.append(f"- `{item['id']}`：`{item['status']}`；复核 `{item.get('review_status') or 'not_reviewed'}`。")
        if item.get("review_validity"):
            lines.append("  评分输入存在引用映射或正文完整性问题，原语义数字暂不用于效果判断；见 "
                         f"`{item['review_validity']['report']}`。")
    for claim in proof.get("claims", []):
        lines.append(f"- 主张 `{claim['id']}`：`{claim['state']}`。")
    combined = proof.get("wiki_combined_effect") or {}
    if combined.get("status") == "measured":
        accuracy = combined.get("accuracy") or {}
        lines.append(
            f"- Wiki 主对照（两仓库等权）：正确率差值 {accuracy.get('delta_pt')}pt；"
            f"95% 区间 {accuracy.get('ci95_pt')}。"
        )
    else:
        lines.append("- Wiki 主对照（两仓库等权）：`not_measured`；模型答案与复核未完整时不显示差值。")
    lines.append("")
    return "\n".join(lines)


def _portfolio_ready(foundation_passed: bool, human_release: bool,
                     current_release_complete: bool,
                     proof: dict[str, Any]) -> bool:
    """Completion means the evidence is complete, not that every result is positive."""
    return bool(
        foundation_passed and human_release and current_release_complete
        and proof.get("experiments_complete") is True
        and proof.get("reviews_complete") is True
        and proof.get("claims_complete") is True
    )


def main(
        *, corpora_path: str = "eval/corpora.yaml",
        claims_path: str = "eval/public_verification.yaml", mode: str = "fast",
        out: str = "docs/ACCEPTANCE-CJSON-LWIP.md", project_root: str | Path = ".",
        experiments_path: str = "eval/experiments.yaml",
        execute: bool = True, network_checker: Callable[[str], bool] = _network_ok,
        console=None) -> dict[str, Any]:
    if mode not in MODES:
        raise AcceptanceEvalError("mode 只能是 fast / full / release")
    root = Path(project_root).resolve()
    corpora_manifest = load_corpora(_resolve(root, corpora_path))
    claims_manifest = load_claims(_resolve(root, claims_path))
    stages = execute_pipelines(mode, project_root=root) if execute else []
    selected = {"cjson"} if mode == "fast" else {"cjson", "lwip"}
    if mode == "release":
        corpora = validate_release_snapshot(corpora_manifest, project_root=root)
    else:
        corpora = [validate_corpus(item, project_root=root)
                   for item in corpora_manifest["corpora"] if item["id"] in selected]
    claims = evaluate_claims(
        corpora_manifest, claims_manifest, project_root=root, release=mode == "release",
        network_checker=network_checker,
    )
    publication = next((item for item in claims if item["kind"] == "publication"),
                       {"status": "unverifiable", "reason": "publication_claim_missing"})
    required_claims_ok = all(
        item["status"] in {"verified", "locally_verified_not_published", "publicly_verified"}
        for item in claims if item.get("required", True)
    )
    release_ok = mode != "release" or publication["status"] == "publicly_verified"
    model_abc = model_abc_status(
        [item for item in corpora_manifest["corpora"] if item["id"] in selected],
        project_root=root,
    )
    proof = proof_status(experiments_path, project_root=root)
    result = {
        "schema_version": 1, "mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "codeatlas": git_identity(root), "corpora_manifest_sha256": _sha256(_resolve(root, corpora_path)),
        "claims_manifest_sha256": _sha256(_resolve(root, claims_path)),
        "execution_stages": stages, "corpora": corpora, "claims": claims,
        "publication": publication, "model_abc": model_abc, "proof": proof,
    }
    result["foundation_passed"] = all(item.get("foundation_passed", False) for item in corpora)
    result["task_complete"] = all(item.get("task_complete", False) for item in corpora)
    result["legacy_task_complete"] = result["task_complete"]
    result["current_release_complete"] = bool(corpora) and all(
        item.get("current_release_complete") is True for item in corpora
    )
    result["passed"] = (
        result["foundation_passed"] and result["task_complete"]
        and required_claims_ok and release_ok
    )
    result["engineer_pass"] = result["foundation_passed"]
    # Formal card release and legacy task gold are independent evidence.  A
    # frozen task may still target an older card; that must keep task_complete
    # false without erasing a separately verified human publication event.
    result["human_release"] = bool(corpora) and all(
        item.get("formal_card", {}).get("ok") is True for item in corpora
    )
    result["ai_effect_review"] = proof["ai_review_done"]
    result["effect_observed"] = proof["effect_observed"]
    result["experiments_complete"] = proof["experiments_complete"]
    result["reviews_complete"] = proof["reviews_complete"]
    result["claims_complete"] = proof["claims_complete"]
    result["judge_qualification"] = proof.get("judge_qualification", {
        "status": "not_run", "qualified": False,
    })
    result["public_repro"] = publication["status"] == "publicly_verified"
    result["portfolio_ready"] = _portfolio_ready(
        result["foundation_passed"], result["human_release"],
        result["current_release_complete"], proof
    )
    target = _resolve(root, out)
    json_target = target.with_suffix(".json")
    _atomic_write(target, render_markdown(result))
    _atomic_write(json_target, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if console:
        console.print(
            f"[{'green' if result['passed'] else 'red'}]"
            f"{'[OK]' if result['passed'] else '[FAIL]'}[/] 双语料验收 "
            f"{mode} → {target}；公开状态={publication['status']}"
        )
    return {**result, "report": str(target), "json_report": str(json_target)}
