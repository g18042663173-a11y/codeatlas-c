"""Pre-registered maintenance trials, independent oracles, and guarded costs.

The default path validates specifications only. It never modifies a checkout,
applies a patch, runs a shell, or calls a model. An explicitly supplied executor
owns its isolated build; its CodeAtlas decisions are observations, never gold.
"""
from __future__ import annotations

from collections import Counter
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from typing import Any, Callable

import yaml

SCHEMA_VERSION = 1
ADAPTERS = ("current", "main_anchor_hash", "invalidate_all")
CLASSIFICATIONS = ("effective", "unrelated_to_claim", "failure")
ARTIFACTS = ("source", "patch", "compile_config", "external_oracle")
OUTCOMES = {"completed", "compile_failed", "run_failed", "assertion_failed", "adapter_failed"}
DECISIONS = {"keep", "revalidate", "unavailable"}
HASH = re.compile(r"[0-9a-f]{64}\Z")


class MaintenanceError(ValueError):
    """Invalid or unbound maintenance evidence."""


def file_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MaintenanceError(message)


def _inside(root: Path, relative: Any) -> Path:
    _require(isinstance(relative, str) and bool(relative), "relative path required")
    value = Path(relative)
    _require(not value.is_absolute() and ".." not in value.parts, "path must stay inside case directory")
    resolved = (root / value).resolve()
    _require(resolved.is_relative_to(root) and resolved != root, "path escapes case directory")
    return resolved


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _optional_number(value: Any, field: str) -> float | None:
    _require(value is None or _number(value), f"{field} must be a finite nonnegative measurement or null")
    return value


def validate_manifest(value: dict, *, root: str | Path) -> dict:
    """Validate all 24 cases and bind every artifact before an executor can run.

    Independence is a declared provenance contract, not proof a human was blind.
    An oracle may not reference CodeAtlas dependency output as its truth source.
    """
    root = Path(root).resolve()
    _require(isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION, "invalid manifest schema")
    corpora = value.get("corpora", {})
    _require(isinstance(corpora, dict) and set(corpora) == {"cjson", "lwip"}, "both cjson and lwip required")
    for corpus in corpora.values():
        _require(isinstance(corpus, dict) and bool(re.fullmatch(r"[0-9a-f]{40}", str(corpus.get("revision", "")))), "fixed 40-character corpus revision required")
    cases = value.get("cases")
    _require(isinstance(cases, list) and len(cases) == 24, "exactly 24 pre-registered cases required")
    ids, directories, checked, counts = set(), [], [], Counter()
    for case in cases:
        _require(isinstance(case, dict), "case must be an object")
        identifier = case.get("id")
        _require(isinstance(identifier, str) and bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]+", identifier)) and identifier not in ids, "unique safe case id required")
        ids.add(identifier)
        corpus, classification = case.get("corpus"), case.get("classification")
        _require(corpus in corpora and classification in CLASSIFICATIONS, "invalid corpus or classification")
        _require(case.get("claim") and case.get("mutation"), "claim and mutation specification required")
        _require(case.get("material_kind") in {"synthetic_fixture", "upstream_mutation"}, "explicit material_kind required")
        directory = _inside(root, case.get("case_dir"))
        _require(directory.is_dir(), f"missing case directory: {identifier}")
        _require(all(not directory.is_relative_to(other) and not other.is_relative_to(directory) for other in directories), "cases require independent non-overlapping directories")
        directories.append(directory)
        artifacts = case.get("artifacts")
        _require(isinstance(artifacts, dict) and set(artifacts) == set(ARTIFACTS), "source/patch/compile_config/external_oracle artifacts required")
        paths = {}
        for kind, artifact in artifacts.items():
            _require(isinstance(artifact, dict) and HASH.fullmatch(str(artifact.get("sha256", ""))) is not None, "artifact SHA-256 required")
            path = _inside(directory, artifact.get("path"))
            _require(path.is_file() and file_hash(path) == artifact["sha256"], f"artifact hash mismatch: {identifier}/{kind}")
            paths[kind] = path
        _require(len(set(paths.values())) == len(ARTIFACTS), "artifact roles must use distinct files")
        try:
            oracle = json.loads(paths["external_oracle"].read_text(encoding="utf-8"))
            config = json.loads(paths["compile_config"].read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise MaintenanceError("oracle and compile config must be JSON") from exc
        _require(isinstance(config, dict) and config.get("schema_version") == 1, "invalid compile config")
        for command in (config.get("compile_argv"), config.get("run_argv")):
            _require(isinstance(command, list) and bool(command) and all(isinstance(x, str) and x for x in command), "compile/run commands must be argument arrays (never shell strings)")
        _require(isinstance(oracle, dict) and oracle.get("schema_version") == 1, "invalid external oracle")
        _require(oracle.get("independence") == "predeclared_external" and oracle.get("source_kind") in {"public_source_and_experiment", "synthetic_executable_specification"}, "oracle must be independently predeclared, not CodeAtlas output")
        _require((case["material_kind"] == "synthetic_fixture") == (oracle["source_kind"] == "synthetic_executable_specification"), "oracle source kind must match material kind")
        _require(oracle.get("derived_from_codeatlas") is False, "CodeAtlas outputs cannot define the external oracle")
        expected = oracle.get("expected_revalidation")
        _require(expected is (True if classification == "effective" else False if classification == "unrelated_to_claim" else None), "oracle expected_revalidation conflicts with pre-registration")
        outcome = oracle.get("expected_outcome")
        _require(outcome in OUTCOMES and ((outcome == "completed") == (classification != "failure")), "failure cases must predeclare a failure outcome")
        assertions = oracle.get("assertions")
        _require(isinstance(assertions, list) and bool(assertions), "explicit external assertions required")
        assertion_ids = [x.get("id") if isinstance(x, dict) else None for x in assertions]
        _require(all(isinstance(x, str) and x for x in assertion_ids) and len(set(assertion_ids)) == len(assertion_ids), "unique assertion ids required")
        _require(all("expected" in x and x.get("description") for x in assertions), "assertions require expected values and descriptions")
        counts[(corpus, classification)] += 1
        checked.append({"case": case, "directory": directory, "paths": paths, "oracle": oracle, "compile_config": config})
    _require(all(counts[(c, k)] == 4 for c in corpora for k in CLASSIFICATIONS), "each corpus requires 4 effective + 4 unrelated_to_claim + 4 failure cases")
    return {"manifest": value, "root": root, "cases": checked,
            "counts": {c: {k: counts[(c, k)] for k in CLASSIFICATIONS} for c in corpora}}


def load_manifest(path: str | Path) -> dict:
    path = Path(path).resolve()
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeError) as exc:
        raise MaintenanceError("invalid YAML manifest") from exc
    result = validate_manifest(value, root=path.parent)
    result["manifest_sha256"] = file_hash(path)
    result["manifest_path"] = str(path)
    return result


def main_anchor_hash_decision(before: str | None, after: str | None) -> str:
    """Minimal baseline: only an exact main-anchor byte hash is considered."""
    _require(HASH.fullmatch(str(before or "")) is not None, "baseline main-anchor before hash required")
    _require(after is None or HASH.fullmatch(str(after)) is not None, "invalid baseline main-anchor after hash")
    return "keep" if before == after else "revalidate"


def _normalized_hash(text: str) -> str:
    """Hash C tokens while ignoring comments/whitespace but preserving semantics."""
    from ..dependencies import normalized
    return hashlib.sha256(json.dumps(normalized(text), ensure_ascii=False).encode()).hexdigest()


def _claim_anchor_hash(text: str) -> str:
    """Fixture-only main-anchor baseline; never used as an external oracle."""
    matches = [line.strip() for line in text.splitlines()
               if line.lstrip().startswith("static int claim_probe(")]
    return hashlib.sha256("\n".join(matches).encode()).hexdigest()


def synthetic_executor(case: dict, inputs: dict) -> dict:
    """Execute one CC0 fixture in an isolated temporary directory.

    This is intentionally opt-in and scoped to ``material_kind=synthetic_fixture``.
    It observes compiler/program behavior without receiving the case classification
    or expected oracle result.  The current adapter mirrors the repository's
    conservative normalized-content dependency policy; it is not an upstream
    cJSON/lwIP mutation result.
    """
    _require(case.get("material_kind") == "synthetic_fixture",
             "built-in executor only supports declared synthetic fixtures")
    config = inputs.get("compile_config")
    _require(isinstance(config, dict), "validated compile config required")
    compile_argv, run_argv = config.get("compile_argv"), config.get("run_argv")
    _require(compile_argv and compile_argv[0] in {"cc", "clang", "gcc"},
             "synthetic compiler must be cc, clang or gcc")
    _require(run_argv and run_argv[0] == "./probe", "synthetic run target must be ./probe")
    source_path = Path(inputs["paths"]["source"])
    patch_path = Path(inputs["paths"]["patch"])
    before = source_path.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="codeatlas-maint-") as temporary:
        work = Path(temporary)
        shutil.copyfile(source_path, work / "source.c")
        patch_start = time.perf_counter()
        applied = subprocess.run(
            ["patch", "-p1", "--batch", "--input", str(patch_path)], cwd=work,
            capture_output=True, text=True, timeout=10,
        )
        _require(applied.returncode == 0, "pre-registered patch did not apply to isolated source")
        after = (work / "source.c").read_text(encoding="utf-8")
        compile_start = time.perf_counter()
        compiled = subprocess.run(
            compile_argv, cwd=work, capture_output=True, text=True, timeout=30,
            env={**os.environ, **config.get("environment", {})},
        )
        compile_ms = round((time.perf_counter() - compile_start) * 1000, 3)
        ran = None
        run_ms = None
        if compiled.returncode == 0:
            run_start = time.perf_counter()
            ran = subprocess.run(
                run_argv, cwd=work, capture_output=True, text=True, timeout=10,
                env={**os.environ, **config.get("environment", {})},
            )
            run_ms = round((time.perf_counter() - run_start) * 1000, 3)
        semantic_changed = _normalized_hash(before) != _normalized_hash(after)
        line_before = next((i for i, line in enumerate(before.splitlines(), 1)
                            if line.lstrip().startswith("static int claim_probe(")), None)
        line_after = next((i for i, line in enumerate(after.splitlines(), 1)
                           if line.lstrip().startswith("static int claim_probe(")), None)
        adapter_ms = round((time.perf_counter() - patch_start) * 1000, 3)
        return {
            "compile_exit": compiled.returncode,
            "run_exit": ran.returncode if ran is not None else None,
            "assertions": ([{"id": "probe_stdout", "actual": ran.stdout}]
                           if ran is not None else []),
            "compile_ms": compile_ms,
            "run_ms": run_ms,
            "main_anchor_before": _claim_anchor_hash(before),
            "main_anchor_after": _claim_anchor_hash(after),
            "adapters": {
                "current": {
                    "decision": "revalidate" if semantic_changed else "keep",
                    "citation_refreshed": line_before != line_after,
                    "wiki_updated": semantic_changed,
                    "elapsed_ms": adapter_ms,
                    "cost": {"value": adapter_ms, "unit": "ms"},
                }
            },
        }


def amortization(build_cost: dict | None, baseline_query_cost: dict | None,
                 candidate_query_cost: dict | None, *, quality_comparable: bool) -> dict:
    """Break-even queries; never mix time, tokens or money, or ignore quality."""
    result = {"break_even_queries": None, "saving_per_query": None, "unit": None,
              "quality_comparable": quality_comparable, "reason": "missing_measurements"}
    if quality_comparable is not True:
        return {**result, "reason": "quality_not_comparable"}
    costs = (build_cost, baseline_query_cost, candidate_query_cost)
    if any(not isinstance(c, dict) or not _number(c.get("value")) or not isinstance(c.get("unit"), str) or not c["unit"] for c in costs):
        return result
    if len({c["unit"] for c in costs}) != 1:
        return {**result, "reason": "unit_mismatch"}
    saving = baseline_query_cost["value"] - candidate_query_cost["value"]
    result.update(unit=build_cost["unit"], saving_per_query=saving)
    if saving <= 0:
        return {**result, "reason": "no_positive_saving"}
    return {**result, "break_even_queries": math.ceil(build_cost["value"] / saving), "reason": None}


def _cost(value: Any) -> dict | None:
    if value is None:
        return None
    _require(isinstance(value, dict) and _number(value.get("value")) and isinstance(value.get("unit"), str) and bool(value["unit"]), "cost requires a finite nonnegative value and explicit unit")
    return {"value": value["value"], "unit": value["unit"]}


def _adapter(raw: dict | None, expected: bool | None, *, eligible: bool) -> dict:
    raw = raw or {}
    _require(isinstance(raw, dict), "adapter observation must be an object")
    decision = raw.get("decision")
    _require(decision is None or decision in DECISIONS, "invalid adapter decision")
    for field in ("citation_refreshed", "wiki_updated"):
        _require(raw.get(field) is None or type(raw[field]) is bool, f"{field} must be boolean or null")
    measured = eligible and decision in {"keep", "revalidate"}
    return {"actual_decision": decision, "measured": measured,
            "missed_invalidation": decision == "keep" if measured and expected is True else None,
            "false_invalidation": decision == "revalidate" if measured and expected is False else None,
            "decision_correct": (decision == "revalidate") == expected if measured else None,
            "citation_refreshed": raw.get("citation_refreshed"), "wiki_updated": raw.get("wiki_updated"),
            "elapsed_ms": _optional_number(raw.get("elapsed_ms"), "elapsed_ms"),
            "cost": _cost(raw.get("cost"))}


def _observe(case: dict, oracle: dict, raw: dict) -> dict:
    _require(isinstance(raw, dict), "executor must return an observation object")
    for name in ("compile_exit", "run_exit"):
        _require(raw.get(name) is None or type(raw[name]) is int, f"{name} must be integer or null")
    results = raw.get("assertions", [])
    _require(isinstance(results, list) and all(isinstance(x, dict) and isinstance(x.get("id"), str) and "actual" in x for x in results), "invalid assertion observations")
    _require(len({x["id"] for x in results}) == len(results), "duplicate assertion observations")
    expected_ids = {x["id"] for x in oracle["assertions"]}
    _require(all(x["id"] in expected_ids for x in results), "unexpected assertion; oracle cannot be extended after execution")
    observed = {x["id"]: x["actual"] for x in results}
    assertions = [{**item, "actual": observed.get(item["id"]),
                   "passed": (type(observed[item["id"]]) is type(item["expected"]) and observed[item["id"]] == item["expected"]) if item["id"] in observed else None}
                  for item in oracle["assertions"]]
    actual = raw.get("adapters", {})
    _require(isinstance(actual, dict) and not set(actual) - set(ADAPTERS), "unknown adapter observation")
    _require(all(isinstance(v, dict) for v in actual.values()), "adapter observations must be objects")
    actual = {key: dict(value) for key, value in actual.items()}
    compile_exit, run_exit = raw.get("compile_exit"), raw.get("run_exit")
    if compile_exit is None:
        outcome = "not_run"
    elif compile_exit != 0:
        outcome = "compile_failed"
    elif run_exit is None:
        outcome = "not_run"
    elif run_exit != 0:
        outcome = "run_failed"
    elif not all(a["passed"] is True for a in assertions):
        outcome = "assertion_failed"
    elif actual.get("current", {}).get("decision") == "unavailable":
        outcome = "adapter_failed"
    else:
        outcome = "completed"
    eligible = outcome == "completed" and case["classification"] != "failure"
    if "main_anchor_hash" not in actual and "main_anchor_before" in raw:
        _require("main_anchor_after" in raw, "main-anchor after hash must be explicitly observed; null means deleted")
        actual["main_anchor_hash"] = {"decision": main_anchor_hash_decision(raw["main_anchor_before"], raw.get("main_anchor_after"))}
    if "invalidate_all" not in actual and outcome == "completed":
        actual["invalidate_all"] = {"decision": "revalidate"}
    return {"outcome": outcome, "semantic_eligible": eligible,
            "expected_outcome_matched": outcome == oracle["expected_outcome"] if outcome != "not_run" else None,
            "compile_exit": compile_exit, "run_exit": run_exit, "assertions": assertions,
            "compile_ms": _optional_number(raw.get("compile_ms"), "compile_ms"),
            "run_ms": _optional_number(raw.get("run_ms"), "run_ms"),
            "adapters": {key: _adapter(actual.get(key), oracle["expected_revalidation"], eligible=eligible) for key in ADAPTERS}}


def summarize(rows: list[dict]) -> dict:
    """Failures and unmeasured fields never enter semantic-rate denominators."""
    summary = {"total": len(rows), "not_run": sum(r["outcome"] == "not_run" for r in rows),
               "compile_failed": sum(r["outcome"] == "compile_failed" for r in rows),
               "semantic_eligible": sum(r["semantic_eligible"] for r in rows),
               "outcomes": dict(Counter(r["outcome"] for r in rows)), "adapters": {}}
    for key in ADAPTERS:
        measured = [r["adapters"][key] for r in rows if r["adapters"][key]["measured"]]
        metrics = {"decision_count": len(measured), "unmeasured": summary["semantic_eligible"] - len(measured)}
        for name in ("missed_invalidation", "false_invalidation", "decision_correct", "citation_refreshed", "wiki_updated"):
            values = [r[name] for r in measured if r[name] is not None]
            metrics[name] = {"count": sum(values), "denominator": len(values),
                             "rate": sum(values) / len(values) if values else None}
        summary["adapters"][key] = metrics
    return summary


def verify_report(report: dict, manifest_path: str | Path) -> dict:
    """Re-derive a maintenance report from bound cases instead of green flags."""
    validated = load_manifest(manifest_path)
    rows = report.get("cases")
    _require(isinstance(rows, list) and len(rows) == 24, "maintenance report requires 24 case observations")
    observed_by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
    _require(len(observed_by_id) == 24, "maintenance report case ids must be unique")
    rebuilt = []
    for item in validated["cases"]:
        case, oracle = item["case"], item["oracle"]
        row = observed_by_id.get(case["id"])
        _require(isinstance(row, dict), "maintenance report is missing a pre-registered case")
        expected_identity = {
            "corpus": case["corpus"], "classification": case["classification"],
            "material_kind": case["material_kind"], "claim": case["claim"],
            "case_dir": case["case_dir"], "artifacts": case["artifacts"],
            "expected_revalidation": oracle["expected_revalidation"],
            "expected_outcome": oracle["expected_outcome"],
        }
        _require(all(row.get(key) == value for key, value in expected_identity.items()),
                 "maintenance report case identity differs from pre-registration")
        raw = {
            "compile_exit": row.get("compile_exit"), "run_exit": row.get("run_exit"),
            # A null ``passed`` means the assertion was never observed (for
            # example because compilation failed).  Omitting it preserves the
            # distinction between "not observed" and an observed null value.
            "assertions": [{"id": assertion.get("id"), "actual": assertion.get("actual")}
                           for assertion in row.get("assertions", [])
                           if assertion.get("passed") is not None],
            "compile_ms": row.get("compile_ms"), "run_ms": row.get("run_ms"),
            "adapters": {
                key: {"decision": value.get("actual_decision"),
                      "citation_refreshed": value.get("citation_refreshed"),
                      "wiki_updated": value.get("wiki_updated"),
                      "elapsed_ms": value.get("elapsed_ms"), "cost": value.get("cost")}
                for key, value in (row.get("adapters") or {}).items()
            },
        }
        recomputed = _observe(case, oracle, raw)
        comparable = {key: row.get(key) for key in recomputed}
        _require(comparable == recomputed, "maintenance case outcome or adapter projection was modified")
        rebuilt.append({"id": case["id"], **expected_identity,
                        "error_type": row.get("error_type"), **recomputed})
    summary = summarize(rebuilt)
    by_corpus = {corpus: summarize([row for row in rebuilt if row["corpus"] == corpus])
                 for corpus in ("cjson", "lwip")}
    _require(report.get("summary") == summary and report.get("by_corpus") == by_corpus,
             "maintenance aggregate differs from case observations")
    execution_complete = (report.get("mode") == "execute" and report.get("status") == "executed"
                          and all(row["outcome"] != "not_run" for row in rebuilt))
    expected_outcomes = execution_complete and all(
        row["expected_outcome_matched"] is True for row in rebuilt)
    current_correct = expected_outcomes and all(
        row["adapters"]["current"]["decision_correct"] is True
        for row in rebuilt if row["classification"] != "failure")
    # A boolean is not an independent oracle review. Future reviewed reports must
    # carry a source-bound review envelope before this can become true.
    oracle_validated = False
    review_status = "unresolved"
    review = report.get("oracle_review")
    if isinstance(review, dict):
        from . import protocol
        review_input = hashlib.sha256(json.dumps({
            "manifest_sha256": validated["manifest_sha256"],
            "cases": rebuilt,
        }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        origin = protocol.review_origin(review, review_input, outcome_fields=("verdict",))
        oracle_validated = (origin["effect_review_eligible"]
                            and (origin["selected"] or {}).get("verdict") == "approved")
        review_status = origin["review_status"] if oracle_validated else "unresolved"
    derived_acceptance = {
        "manifest_valid": True, "execution_complete": execution_complete,
        "all_expected_outcomes": expected_outcomes,
        "current_decisions_passed": current_correct,
        "oracle_independently_validated": oracle_validated,
    }
    for key, value in derived_acceptance.items():
        _require((report.get("acceptance") or {}).get(key) == value,
                 "maintenance acceptance flag differs from raw observations or review")
    return {"integrity_ok": True, "execution_complete": execution_complete,
            "review_status": review_status, "claim_supported": current_correct and oracle_validated,
            "acceptance": {**(report.get("acceptance") or {}), **derived_acceptance}}


def evaluate(manifest_path: str | Path, *, mode: str = "plan",
             executor: Callable[[dict, dict], dict] | None = None) -> dict:
    """Explicit execute mode passes a case and frozen, validated input paths.

    Executors must create their own isolated work copies. Original artifact hashes
    are checked again after every trial; mutated inputs fail the entire evaluation.
    A synthetic fixture execution is not evidence of upstream corpus behavior.
    """
    _require(mode in {"plan", "validate", "execute"}, "mode must be plan, validate or execute")
    _require((mode == "execute") == (executor is not None), "execute requires an explicit executor; plan/validate never execute")
    validated = load_manifest(manifest_path)
    rows = []
    for item in validated["cases"]:
        case, oracle = item["case"], item["oracle"]
        raw, error_type = {}, None
        if executor is not None:
            for kind, path in item["paths"].items():
                _require(path.is_file() and file_hash(path) == case["artifacts"][kind]["sha256"], "pre-registered artifact changed before trial")
            try:
                # No oracle/classification is passed as a decision input. This is
                # separation of responsibilities, not a sandbox for arbitrary Python.
                execution_case = json.loads(json.dumps(case))
                execution_case.pop("classification")
                execution_case["artifacts"].pop("external_oracle")
                raw = executor(execution_case, {"case_dir": item["directory"],
                               "paths": {k: v for k, v in item["paths"].items() if k != "external_oracle"},
                               "compile_config": json.loads(json.dumps(item["compile_config"]))})
            except Exception as exc:
                error_type = type(exc).__name__
            for kind, path in item["paths"].items():
                _require(path.is_file() and file_hash(path) == case["artifacts"][kind]["sha256"], "executor modified a pre-registered artifact")
            _require(file_hash(manifest_path) == validated["manifest_sha256"], "executor modified pre-registration")
        observed = _observe(case, oracle, raw)
        if error_type:
            observed.update(outcome="executor_failed", expected_outcome_matched=False, semantic_eligible=False)
        rows.append({"id": case["id"], "corpus": case["corpus"], "classification": case["classification"],
                     "material_kind": case["material_kind"], "claim": case["claim"], "case_dir": case["case_dir"],
                     "artifacts": case["artifacts"], "expected_revalidation": oracle["expected_revalidation"],
                     "expected_outcome": oracle["expected_outcome"], "error_type": error_type, **observed})
    if executor is not None:
        # Also catch an executor modifying an earlier case while running a later one.
        load_manifest(manifest_path)
    summary = summarize(rows)
    measured = mode == "execute" and all(r["outcome"] != "not_run" for r in rows)
    expected_outcomes = measured and all(r["expected_outcome_matched"] is True for r in rows)
    current_correct = expected_outcomes and all(r["adapters"]["current"]["decision_correct"] is True
                                              for r in rows if r["classification"] != "failure")
    project_root = Path(__file__).resolve().parents[3]
    return {"schema_version": SCHEMA_VERSION, "evaluation": "maintenance", "mode": mode,
            "status": "executed" if mode == "execute" else "not_run", "manifest_sha256": validated["manifest_sha256"],
            "identity": {"manifest_hash": validated["manifest_sha256"],
                         "evaluator_hash": file_hash(Path(__file__)),
                         "dependency_policy_hash": file_hash(project_root / "src/codeatlas/dependencies.py")},
            "generated_at": datetime.now(timezone.utc).isoformat(), "corpora": validated["manifest"]["corpora"],
            "boundary": "Predeclared external oracle; synthetic fixtures do not verify upstream behavior. No automatic shell/model invocation.",
            "oracle_provenance": "declared_not_independently_audited",
            "review_status": "unresolved", "cases": rows, "summary": summary,
            "by_corpus": {c: summarize([r for r in rows if r["corpus"] == c]) for c in ("cjson", "lwip")},
            "acceptance": {"manifest_valid": True, "execution_complete": measured,
                           "all_expected_outcomes": expected_outcomes, "current_decisions_passed": current_correct,
                           "oracle_independently_validated": False,
                           "upstream_material_declared": all(r["material_kind"] == "upstream_mutation" for r in rows),
                           "upstream_validated": False,
                           "upstream_validation_reason": "This runner binds artifacts but does not verify their Git-checkout provenance."},
            "amortization": amortization(None, None, None, quality_comparable=False)}


def render(report: dict) -> str:
    lines = ["# CodeAtlas 维护变异评测", "", report["boundary"], "",
             f"状态：`{report['status']}`；清单 SHA-256：`{report['manifest_sha256']}`。",
             "未执行和编译失败不计作有效语义变异；未实测时间与成本保持 null。", "",
             "| 场景 | 分类 | 预期复审 | 实际结果 | current / 主锚点 / 全失效 |",
             "|---|---|---|---|---|"]
    for row in report["cases"]:
        decisions = " / ".join(row["adapters"][a]["actual_decision"] or "未测" for a in ADAPTERS)
        lines.append(f"| {row['id']} | {row['classification']} | {row['expected_revalidation']} | {row['outcome']} | {decisions} |")
    def count(adapter, metric):
        value = report["summary"]["adapters"][adapter][metric]
        return f"{value['count']}/{value['denominator']}" if value["denominator"] else "未测"
    lines += [
        "", "## 结果证据卡", "",
        f"- 独立场景：{report['summary']['total']}；有效语义场景：{report['summary']['semantic_eligible']}；"
        f"故障/编译失败：{report['summary']['compile_failed']}。",
        f"- 当前保守策略：漏放有效变更 {count('current', 'missed_invalidation')}；"
        f"无关变更误失效 {count('current', 'false_invalidation')}；"
        f"决策正确 {count('current', 'decision_correct')}。",
        f"- 仅主锚点哈希：漏放有效变更 {count('main_anchor_hash', 'missed_invalidation')}；"
        f"无关变更误失效 {count('main_anchor_hash', 'false_invalidation')}。",
        f"- 全部失效：漏放有效变更 {count('invalidate_all', 'missed_invalidation')}；"
        f"无关变更误失效 {count('invalidate_all', 'false_invalidation')}。",
        f"- 真值复核：`{report.get('review_status', 'unresolved')}`；"
        "当前是 CC0 合成契约，不是上游源码变异或人工专家审核。",
        f"- 摊销：`{report['amortization']['break_even_queries']}`；原因："
        f"`{report['amortization']['reason']}`。",
        "",
        "摊销只有在质量可比、计量单位一致且每次查询节省为正时才计算。",
        "当前结果只能说明策略偏安全但误失效仍高，不能证明维护收益或构建提速。", "",
    ]
    return "\n".join(lines)


run = evaluate


# The upstream experiment is intentionally additive.  The schema above remains
# the historical synthetic-fixture contract used by existing reports.
UPSTREAM_SCHEMA_VERSION = 1
UPSTREAM_EVALUATION = "upstream_maintenance"
UPSTREAM_STAGES = ("compile", "run", "assertion", "snapshot", "card", "citation", "wiki")
UPSTREAM_FAILURE_MODES = (
    "compile_failure",
    "artifact_tamper",
    "publication_interruption",
    "governance_rollback",
)
UPSTREAM_FAILURE_OUTCOMES = {
    "compile_failure": "compile_failed",
    "artifact_tamper": "artifact_tamper",
    "publication_interruption": "publication_interrupted",
    "governance_rollback": "governance_rolled_back",
}
PINNED_UPSTREAMS = {
    "cjson": {
        "repository": "corpus/cJSON",
        "revision": "fb16e5cf358798aabb049655975cde8427101056",
    },
    "lwip": {
        "repository": "corpus/lwip",
        "revision": "3d896ba0a37ff3ce73270ca5e230707fe47f60e3",
    },
}


def _project_entry(project_root: Path, relative: Any, *, file: bool) -> Path:
    _require(isinstance(relative, str) and bool(relative), "project-relative path required")
    value = Path(relative)
    _require(not value.is_absolute() and ".." not in value.parts,
             "upstream path must stay inside the project")
    resolved = (project_root / value).resolve()
    _require(resolved.is_relative_to(project_root) and resolved != project_root,
             "upstream path escapes project")
    _require(resolved.is_file() if file else resolved.is_dir(),
             f"missing upstream {'file' if file else 'directory'}: {relative}")
    return resolved


def _argv_list(value: Any, field: str) -> list[list[str]]:
    _require(isinstance(value, list) and bool(value), f"{field} requires at least one argv array")
    commands = value if isinstance(value[0], list) else [value]
    _require(all(isinstance(command, list) and bool(command)
                 and all(isinstance(part, str) and part for part in command)
                 for command in commands), f"{field} must contain non-empty argv arrays")
    return commands


def validate_upstream_manifest(value: dict, *, project_root: str | Path) -> dict:
    """Validate a pinned, decision-complete upstream experiment manifest.

    Ready cases bind every local fixture by hash.  Blocked cases remain explicit
    protocol records and cannot silently become successful observations.
    """
    project_root = Path(project_root).resolve()
    _require(isinstance(value, dict)
             and value.get("schema_version") == UPSTREAM_SCHEMA_VERSION
             and value.get("evaluation") == UPSTREAM_EVALUATION,
             "invalid upstream maintenance schema")
    corpora = value.get("corpora")
    _require(isinstance(corpora, dict) and set(corpora) == set(PINNED_UPSTREAMS),
             "upstream manifest requires cjson and lwip")
    corpus_inputs: dict[str, dict] = {}
    for name, expected_identity in PINNED_UPSTREAMS.items():
        corpus = corpora[name]
        _require(isinstance(corpus, dict), "corpus record must be an object")
        _require({key: corpus.get(key) for key in expected_identity} == expected_identity,
                 f"{name} must use the pinned repository and revision")
        repository = _project_entry(project_root, corpus["repository"], file=False)
        anchor = corpus.get("claim_anchor")
        _require(isinstance(anchor, dict) and set(anchor) == {"path", "start_marker", "end_marker"},
                 "claim_anchor requires path/start_marker/end_marker")
        _require(all(isinstance(anchor[key], str) and anchor[key] for key in anchor),
                 "claim anchor values must be non-empty strings")
        anchor_relative = Path(anchor["path"])
        _require(not anchor_relative.is_absolute() and ".." not in anchor_relative.parts,
                 "claim anchor path must stay inside the repository")
        pinned_source = subprocess.run(
            ["git", "-C", str(repository), "show", f"{corpus['revision']}:{anchor['path']}"],
            capture_output=True, timeout=15,
        )
        _require(pinned_source.returncode == 0,
                 f"missing claim anchor in pinned {name} source")
        try:
            source_text = pinned_source.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MaintenanceError("claim anchor source must be UTF-8") from exc
        _anchor_slice(source_text, anchor)
        claim_dependencies = corpus.get("claim_dependencies")
        _require(isinstance(claim_dependencies, dict)
                 and set(claim_dependencies) == {"functions", "macros"},
                 "claim_dependencies requires functions and macros")
        functions = claim_dependencies["functions"]
        macros = claim_dependencies["macros"]
        _require(isinstance(functions, list) and bool(functions),
                 "at least one reviewed function dependency required")
        for dependency in functions:
            _require(isinstance(dependency, dict) and set(dependency) == {"path", "name"}
                     and all(isinstance(dependency[key], str) and dependency[key]
                             for key in ("path", "name")),
                     "function dependencies require path and name")
            relative = Path(dependency["path"])
            _require(not relative.is_absolute() and ".." not in relative.parts,
                     "function dependency path must stay inside the repository")
        _require(isinstance(macros, list), "macro dependencies must be a list")
        for macro in macros:
            _require(isinstance(macro, dict) and set(macro) == {"name", "paths"}
                     and isinstance(macro["name"], str)
                     and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", macro["name"])
                     and isinstance(macro["paths"], list) and bool(macro["paths"]),
                     "macro dependencies require a C name and source paths")
            for raw_path in macro["paths"]:
                relative = Path(raw_path)
                _require(isinstance(raw_path, str) and bool(raw_path)
                         and not relative.is_absolute() and ".." not in relative.parts,
                         "macro dependency path must stay inside the repository")
        build = corpus.get("build")
        _require(isinstance(build, dict), "corpus build profile required")
        compile_steps = _argv_list(build.get("compile_steps"), "compile_steps")
        run_commands = _argv_list(build.get("run_argv"), "run_argv")
        _require(len(run_commands) == 1, "run_argv must be one argv array")
        _require(all(command[0] in {"cc", "clang", "gcc"} for command in compile_steps),
                 "upstream compilation must use a declared C compiler")
        corpus_inputs[name] = {
            "repository": repository,
            "anchor": anchor,
            "claim_dependencies": json.loads(json.dumps(claim_dependencies)),
            "build": {"compile_steps": compile_steps, "run_argv": run_commands[0]},
        }

    cases = value.get("cases")
    _require(isinstance(cases, list) and len(cases) == 24,
             "exactly 24 upstream maintenance cases required")
    ids: set[str] = set()
    counts: Counter = Counter()
    modes: Counter = Counter()
    checked = []
    for case in cases:
        _require(isinstance(case, dict), "upstream case must be an object")
        identifier = case.get("id")
        _require(isinstance(identifier, str)
                 and re.fullmatch(r"[a-z0-9][a-z0-9_-]+", identifier)
                 and identifier not in ids, "unique safe upstream case id required")
        ids.add(identifier)
        corpus = case.get("corpus")
        classification = case.get("classification")
        _require(corpus in corpora and classification in CLASSIFICATIONS,
                 "invalid upstream corpus or classification")
        _require(case.get("material_kind") == "upstream_mutation",
                 "upstream cases cannot use synthetic source material")
        _require(isinstance(case.get("claim"), str) and case["claim"], "claim required")
        _require(isinstance(case.get("mutation"), str) and case["mutation"], "mutation required")
        status = case.get("execution_status")
        _require(status in {"ready", "blocked"}, "execution_status must be ready or blocked")
        failure_mode = case.get("failure_mode")
        if classification == "failure":
            _require(status == "ready", "all four upstream failure controls must be executable")
            _require(failure_mode in UPSTREAM_FAILURE_MODES, "invalid failure mode")
            modes[(corpus, failure_mode)] += 1
        else:
            _require(failure_mode is None, "non-failure cases cannot inject a failure")

        oracle = case.get("oracle")
        _require(isinstance(oracle, dict)
                 and oracle.get("source_kind") == "pinned_public_source_and_experiment"
                 and oracle.get("derived_from_codeatlas") is False,
                 "upstream oracle must be independently predeclared")
        expected_decision = oracle.get("expected_decision")
        expected_outcome = oracle.get("expected_outcome")
        if status == "blocked":
            _require(classification != "failure" and expected_outcome == "blocked",
                     "blocked cases must predeclare blocked and cannot stand in for failure controls")
            _require(expected_decision == ("revalidate" if classification == "effective" else "keep"),
                     "blocked case still requires a class-consistent planned decision")
            _require(isinstance(case.get("blocked_reason"), str) and case["blocked_reason"],
                     "blocked case requires a concrete reason")
            _require(case.get("artifacts") in (None, {}),
                     "blocked cases cannot carry unexecuted fixture claims")
            paths: dict[str, Any] = {}
        else:
            _require(case.get("blocked_reason") is None, "ready case cannot have blocked_reason")
            if classification == "failure":
                _require(expected_decision == "unavailable"
                         and expected_outcome == UPSTREAM_FAILURE_OUTCOMES[failure_mode],
                         "failure oracle conflicts with injected mode")
            else:
                _require(expected_outcome == "completed"
                         and expected_decision == ("revalidate" if classification == "effective" else "keep"),
                         "semantic oracle conflicts with classification")
            assertions = oracle.get("assertions")
            _require(isinstance(assertions, list) and bool(assertions),
                     "ready case requires independent assertions")
            assertion_ids = [item.get("id") if isinstance(item, dict) else None for item in assertions]
            _require(all(isinstance(item, str) and item for item in assertion_ids)
                     and len(set(assertion_ids)) == len(assertion_ids),
                     "ready assertion ids must be unique")
            _require(all(item.get("stream") in {"stdout", "stderr"}
                         and item.get("operator", "equals") in {"equals", "contains", "not_contains"}
                         and isinstance(item.get("expected"), str)
                         and isinstance(item.get("description"), str) and item["description"]
                         for item in assertions), "invalid independent assertion")
            if classification != "failure":
                _require(any(identifier.startswith("target_") for identifier in assertion_ids),
                         "semantic case requires a target-specific assertion")
            if classification == "unrelated_to_claim":
                _require(any(identifier.startswith("guard_") for identifier in assertion_ids),
                         "unrelated case requires an unaffected claim guard")
            artifacts = case.get("artifacts")
            _require(isinstance(artifacts, dict) and set(artifacts) == {"patch", "harness", "support"},
                     "ready case requires patch/harness/support artifacts")
            paths = {}
            for role in ("patch", "harness"):
                artifact = artifacts[role]
                _require(isinstance(artifact, dict) and HASH.fullmatch(str(artifact.get("sha256", ""))),
                         f"{role} artifact hash required")
                path = _project_entry(project_root, artifact.get("path"), file=True)
                _require(file_hash(path) == artifact["sha256"], f"artifact hash mismatch: {identifier}/{role}")
                paths[role] = path
            support = artifacts["support"]
            _require(isinstance(support, list), "support artifacts must be a list")
            support_paths = []
            for artifact in support:
                _require(isinstance(artifact, dict) and HASH.fullmatch(str(artifact.get("sha256", ""))),
                         "support artifact hash required")
                path = _project_entry(project_root, artifact.get("path"), file=True)
                _require(file_hash(path) == artifact["sha256"], f"support hash mismatch: {identifier}")
                support_paths.append(path)
            _require(len({path.name for path in support_paths}) == len(support_paths),
                     "support fixture basenames must be unique")
            paths["support"] = support_paths
            publication = case.get("publication")
            _require(isinstance(publication, dict)
                     and all(isinstance(publication.get(key), str) and publication[key]
                             for key in ("card_id", "citation_path", "wiki_page")),
                     "ready case requires card/citation/wiki publication targets")
            _require(publication["citation_path"] == corpus_inputs[corpus]["anchor"]["path"],
                     "citation target must be the predeclared claim anchor")
        counts[(corpus, classification)] += 1
        checked.append({"case": case, "paths": paths, **corpus_inputs[corpus]})
    _require(all(counts[(corpus, classification)] == 4
                 for corpus in corpora for classification in CLASSIFICATIONS),
             "each upstream corpus requires 4 effective + 4 unrelated + 4 failure cases")
    _require(all(modes[(corpus, mode)] == 1
                 for corpus in corpora for mode in UPSTREAM_FAILURE_MODES),
             "each corpus requires one control for each failure mode")
    semantic_patch_hashes = [
        item["case"]["artifacts"]["patch"]["sha256"]
        for item in checked
        if item["case"]["classification"] != "failure"
        and item["case"]["execution_status"] == "ready"
    ]
    _require(len(set(semantic_patch_hashes)) == len(semantic_patch_hashes),
             "effective/unrelated scenarios require distinct patch bytes")
    _require(all(any(item["case"]["corpus"] == corpus
                     and item["case"]["classification"] == classification
                     and item["case"]["execution_status"] == "ready"
                     for item in checked)
                 for corpus in corpora for classification in CLASSIFICATIONS),
             "each corpus/class requires at least one real executable representative")
    return {
        "manifest": value,
        "project_root": project_root,
        "cases": checked,
        "counts": {corpus: {classification: counts[(corpus, classification)]
                            for classification in CLASSIFICATIONS}
                   for corpus in corpora},
    }


def load_upstream_manifest(path: str | Path) -> dict:
    path = Path(path).resolve()
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeError) as exc:
        raise MaintenanceError("invalid upstream YAML manifest") from exc
    validated = validate_upstream_manifest(value, project_root=path.parent.parent)
    validated["manifest_path"] = str(path)
    validated["manifest_sha256"] = file_hash(path)
    return validated


def _anchor_slice(source: str, anchor: dict) -> tuple[str, int]:
    start = source.find(anchor["start_marker"])
    _require(start >= 0, "claim anchor start marker not found")
    end = source.find(anchor["end_marker"], start + len(anchor["start_marker"]))
    _require(end > start, "claim anchor end marker not found")
    return source[start:end], source.count("\n", 0, start) + 1


def _stage(status: str, **evidence: Any) -> dict:
    return {"status": status, **evidence}


def _assertion_passes(assertion: dict, actual: str) -> bool:
    operator = assertion.get("operator", "equals")
    if operator == "contains":
        return assertion["expected"] in actual
    if operator == "not_contains":
        return assertion["expected"] not in actual
    return actual == assertion["expected"]


def _skipped_stages(reason: str) -> dict:
    return {name: _stage("skipped", reason=reason) for name in UPSTREAM_STAGES}


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _upstream_adapter_decisions(before_dependencies: dict[str, str],
                                after_dependencies: dict[str, str],
                                before_anchor: str, after_anchor: str,
                                *, line_refreshed: bool,
                                dependency_hash_ms: float = 0.0) -> dict[str, dict]:
    """Apply the three preregistered maintenance policies to real source bytes.

    ``current`` mirrors a reviewed card manifest over exact function definitions
    and macro definitions. The claim-anchor baseline observes only the bound
    claim slice, and invalidate-all is the upper-bound policy. None defines
    truth; the manifest's executable oracle does that independently.
    """
    started = time.perf_counter()
    current = "revalidate" if before_dependencies != after_dependencies else "keep"
    current_ms = round(dependency_hash_ms + (time.perf_counter() - started) * 1000, 3)
    started = time.perf_counter()
    anchor = "revalidate" if hashlib.sha256(before_anchor.encode()).hexdigest() != \
        hashlib.sha256(after_anchor.encode()).hexdigest() else "keep"
    anchor_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "current": {"decision": current, "citation_refreshed": line_refreshed,
                    "wiki_updated": current == "revalidate", "elapsed_ms": current_ms},
        "main_anchor_hash": {"decision": anchor, "citation_refreshed": line_refreshed,
                             "wiki_updated": anchor == "revalidate", "elapsed_ms": anchor_ms},
        "invalidate_all": {"decision": "revalidate", "citation_refreshed": line_refreshed,
                           "wiki_updated": True, "elapsed_ms": 0.0},
    }


def _expand_argv(argv: list[str], values: dict[str, str]) -> list[str]:
    try:
        return [part.format_map(values) for part in argv]
    except (KeyError, ValueError) as exc:
        raise MaintenanceError("unknown or malformed build placeholder") from exc


def _run_command(argv: list[str], *, cwd: Path, timeout: int, env: dict | None = None) -> tuple[subprocess.CompletedProcess, float]:
    started = time.perf_counter()
    completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                               timeout=timeout, env={**os.environ, **(env or {})})
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    return completed, elapsed


def _archive_upstream(repository: Path, revision: str, destination: Path) -> str:
    resolved = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", f"{revision}^{{commit}}"],
        capture_output=True, text=True, timeout=15,
    )
    _require(resolved.returncode == 0 and resolved.stdout.strip() == revision,
             "pinned upstream commit is not locally available")
    archived = subprocess.run(
        ["git", "-C", str(repository), "archive", "--format=tar", revision],
        capture_output=True, timeout=60,
    )
    _require(archived.returncode == 0 and archived.stdout, "could not archive pinned upstream")
    with tarfile.open(fileobj=io.BytesIO(archived.stdout), mode="r:") as bundle:
        members = bundle.getmembers()
        _require(all(not Path(member.name).is_absolute() and ".." not in Path(member.name).parts
                     for member in members), "unsafe path in trusted upstream archive")
        bundle.extractall(destination, filter="data")
    return resolved.stdout.strip()


def upstream_executor(case: dict, inputs: dict) -> dict:
    """Run one ready scenario against an archived pinned upstream commit.

    Failure injection is confined to the temporary experiment.  In particular,
    the artifact-tamper control mutates only a copied fixture and validates that
    the copied bytes are rejected before patching or compilation.
    """
    _require(case.get("execution_status") == "ready", "blocked cases cannot execute")
    repository = Path(inputs["repository"])
    anchor = inputs["anchor"]
    paths = inputs["paths"]
    failure_mode = case.get("failure_mode")
    with tempfile.TemporaryDirectory(prefix=f"codeatlas-upstream-{case['id']}-") as temporary:
        root = Path(temporary)
        source_root = root / "source"
        build_root = root / "build"
        fixture_root = root / "fixtures"
        source_root.mkdir()
        build_root.mkdir()
        fixture_root.mkdir()
        archived_revision = _archive_upstream(repository, case["revision"], source_root)
        from ..dependencies import source_dependency_fingerprints
        dependency_started = time.perf_counter()
        before_dependencies = source_dependency_fingerprints(
            source_root, inputs["claim_dependencies"]["functions"],
            inputs["claim_dependencies"]["macros"],
        )
        dependency_hash_ms = (time.perf_counter() - dependency_started) * 1000

        patch_copy = fixture_root / "change.patch"
        harness_copy = fixture_root / paths["harness"].name
        shutil.copyfile(paths["patch"], patch_copy)
        shutil.copyfile(paths["harness"], harness_copy)
        support_copies = []
        for support in paths["support"]:
            copied = fixture_root / support.name
            shutil.copyfile(support, copied)
            support_copies.append(copied)

        stages = _skipped_stages("not reached")
        provenance = {
            "repository": str(repository),
            "revision": archived_revision,
            "archive_tree_sha256_before": _tree_hash(source_root),
            "isolated_temporary_copy": True,
        }
        if failure_mode == "artifact_tamper":
            patch_copy.write_bytes(patch_copy.read_bytes() + b"\n# temporary tamper control\n")
            expected_hash = case["artifacts"]["patch"]["sha256"]
            actual_hash = file_hash(patch_copy)
            _require(actual_hash != expected_hash, "artifact tamper control did not alter copied bytes")
            stages["compile"] = _stage("skipped", reason="copied artifact integrity failed before patch")
            return {
                "outcome": "artifact_tamper",
                "decision": None,
                "stages": stages,
                "provenance": provenance,
                "artifact_integrity": _stage("failed", artifact="patch", expected_sha256=expected_hash,
                                             actual_sha256=actual_hash, original_untouched=True),
            }

        target = source_root / anchor["path"]
        before_text = target.read_text(encoding="utf-8")
        before_anchor, before_line = _anchor_slice(before_text, anchor)
        applied, patch_ms = _run_command(
            ["patch", "-p1", "--batch", "--input", str(patch_copy)],
            cwd=source_root, timeout=15,
        )
        _require(applied.returncode == 0, "pre-registered upstream patch did not apply")
        after_text = target.read_text(encoding="utf-8")
        dependency_started = time.perf_counter()
        after_dependencies = source_dependency_fingerprints(
            source_root, inputs["claim_dependencies"]["functions"],
            inputs["claim_dependencies"]["macros"],
        )
        dependency_hash_ms += (time.perf_counter() - dependency_started) * 1000
        after_anchor, after_line = _anchor_slice(after_text, anchor)
        anchor_before = hashlib.sha256(before_anchor.encode()).hexdigest()
        anchor_after = hashlib.sha256(after_anchor.encode()).hexdigest()
        adapters = _upstream_adapter_decisions(
            before_dependencies, after_dependencies, before_anchor, after_anchor,
            line_refreshed=before_line != after_line,
            dependency_hash_ms=dependency_hash_ms,
        )
        observed_decision = adapters["current"]["decision"]

        values = {
            "source": str(source_root),
            "build": str(build_root),
            "fixture_root": str(fixture_root),
            "harness": str(harness_copy),
            **{f"support_{index}": str(path) for index, path in enumerate(support_copies)},
        }
        compile_records = []
        compile_exit = 0
        compile_total_ms = 0.0
        for command in inputs["build"]["compile_steps"]:
            argv = _expand_argv(command, values)
            completed, elapsed = _run_command(argv, cwd=source_root, timeout=60)
            compile_total_ms += elapsed
            compile_records.append({"argv": argv, "exit": completed.returncode,
                                    "stdout": completed.stdout[-4000:], "stderr": completed.stderr[-4000:],
                                    "elapsed_ms": elapsed})
            compile_exit = completed.returncode
            if compile_exit != 0:
                break
        stages["compile"] = _stage("passed" if compile_exit == 0 else "failed",
                                   commands=compile_records, elapsed_ms=round(compile_total_ms, 3))
        if compile_exit != 0:
            return {
                "outcome": "compile_failed",
                "decision": None,
                "adapters": adapters,
                "stages": stages,
                "provenance": {**provenance, "patch_elapsed_ms": patch_ms,
                               "claim_anchor_before_sha256": anchor_before,
                               "claim_anchor_after_sha256": anchor_after},
                "artifact_integrity": _stage("passed", original_untouched=True),
            }

        run_argv = _expand_argv(inputs["build"]["run_argv"], values)
        ran, run_ms = _run_command(run_argv, cwd=source_root, timeout=20)
        stages["run"] = _stage("passed" if ran.returncode == 0 else "failed", argv=run_argv,
                               exit=ran.returncode, stdout=ran.stdout[-4000:], stderr=ran.stderr[-4000:],
                               elapsed_ms=run_ms)
        if ran.returncode != 0:
            return {"outcome": "run_failed", "decision": None, "stages": stages,
                    "adapters": adapters,
                    "provenance": provenance,
                    "artifact_integrity": _stage("passed", original_untouched=True)}

        actual_assertions = []
        for assertion in case["assertions"]:
            actual = ran.stdout if assertion["stream"] == "stdout" else ran.stderr
            actual_assertions.append({"id": assertion["id"], "actual": actual,
                                      "passed": _assertion_passes(assertion, actual)})
        assertions_passed = all(item["passed"] for item in actual_assertions)
        stages["assertion"] = _stage("passed" if assertions_passed else "failed",
                                     observations=actual_assertions)
        if not assertions_passed:
            return {"outcome": "assertion_failed", "decision": None, "stages": stages,
                    "adapters": adapters,
                    "provenance": provenance,
                    "artifact_integrity": _stage("passed", original_untouched=True)}

        snapshot_hash = _tree_hash(source_root)
        stages["snapshot"] = _stage("passed", tree_sha256=snapshot_hash,
                                    claim_anchor_before_sha256=anchor_before,
                                    claim_anchor_after_sha256=anchor_after,
                                    dependency_manifest_before=before_dependencies,
                                    dependency_manifest_after=after_dependencies,
                                    dependency_manifest_changed=before_dependencies != after_dependencies)
        publication = case["publication"]
        card_payload = {
            "card_id": publication["card_id"], "case_id": case["id"],
            "revision": archived_revision, "decision": observed_decision,
            "snapshot_sha256": snapshot_hash,
        }
        publication_root = root / "publication"
        publication_root.mkdir()
        card_path = publication_root / "card.json"
        card_text = json.dumps(card_payload, sort_keys=True)
        card_hash = hashlib.sha256(card_text.encode()).hexdigest()
        citation_payload = {
            "path": publication["citation_path"], "line": after_line,
            "anchor_sha256": anchor_after,
            "excerpt": after_anchor.splitlines()[0],
            "line_refreshed": before_line != after_line,
        }
        citation_path = publication_root / "citation.json"
        citation_text = json.dumps(citation_payload, sort_keys=True)
        citation_hash = hashlib.sha256(citation_text.encode()).hexdigest()

        wiki_path = publication_root / "wiki.json"
        wiki_path.write_text(json.dumps({"page": publication["wiki_page"], "state": "before"},
                                        sort_keys=True), encoding="utf-8")
        wiki_before = file_hash(wiki_path)
        wiki_payload = {
            "page": publication["wiki_page"], "card_sha256": card_hash,
            "citation_sha256": citation_hash,
            "action": "update" if observed_decision == "revalidate" else "keep",
        }
        wiki_text = json.dumps(wiki_payload, sort_keys=True)

        # Exercise the same durable prepare/commit/recover journal used by card
        # publication.  A prepared operation may have already replaced several
        # files when a process dies; recovery must return all of them to the
        # authoritative pre-operation state before any index can consume them.
        from .. import db as dbm
        from .. import publication as publication_mod
        publication_db = dbm.connect(publication_root / "publication.sqlite3")
        operation_id = f"maintenance-{case['id']}"
        publication_mod.prepare(publication_db, [
            (card_path, card_text),
            (citation_path, citation_text),
            (wiki_path, wiki_text),
        ], op_id=operation_id)
        staged_wiki_hash = file_hash(wiki_path)
        stages["card"] = _stage("passed", artifact="card.json", payload=card_payload,
                                sha256=file_hash(card_path), journal_op=operation_id)
        stages["citation"] = _stage("passed", artifact="citation.json", payload=citation_payload,
                                    sha256=file_hash(citation_path), journal_op=operation_id)
        if failure_mode == "publication_interruption":
            recovered = publication_mod.recover(publication_db)
            journal_state = publication_db.execute(
                "SELECT state FROM publication_journal WHERE op_id=?", (operation_id,)
            ).fetchone()[0]
            stages["wiki"] = _stage("interrupted", committed=False,
                                    staged_sha256=staged_wiki_hash, before_sha256=wiki_before,
                                    after_sha256=file_hash(wiki_path), recovered=True,
                                    journal_state=journal_state,
                                    recovered_operation=operation_id in recovered,
                                    card_removed=not card_path.exists(),
                                    citation_removed=not citation_path.exists())
            outcome = "publication_interrupted"
            decision = None
        elif failure_mode == "governance_rollback":
            recovered = publication_mod.recover(publication_db)
            journal_state = publication_db.execute(
                "SELECT state FROM publication_journal WHERE op_id=?", (operation_id,)
            ).fetchone()[0]
            stages["card"] = {**stages["card"], "status": "rolled_back",
                              "attempt_status": "passed", "artifact_removed": not card_path.exists()}
            stages["citation"] = {**stages["citation"], "status": "rolled_back",
                                  "attempt_status": "passed", "artifact_removed": not citation_path.exists()}
            stages["wiki"] = _stage("rolled_back", committed=False, governance_approved=False,
                                    staged_sha256=staged_wiki_hash, before_sha256=wiki_before,
                                    after_sha256=file_hash(wiki_path), journal_state=journal_state,
                                    recovered_operation=operation_id in recovered)
            outcome = "governance_rolled_back"
            decision = None
        else:
            publication_mod.commit(publication_db, operation_id)
            journal_state = publication_db.execute(
                "SELECT state FROM publication_journal WHERE op_id=?", (operation_id,)
            ).fetchone()[0]
            stages["wiki"] = _stage("passed", committed=True, before_sha256=wiki_before,
                                    after_sha256=file_hash(wiki_path), payload=wiki_payload,
                                    journal_state=journal_state)
            outcome = "completed"
            decision = observed_decision
        publication_db.close()
        return {
            "outcome": outcome,
            "decision": decision,
            "adapters": adapters,
            "observed_anchor_decision": observed_decision,
            "stages": stages,
            "provenance": {**provenance, "patch_elapsed_ms": patch_ms,
                           "claim_anchor_before_sha256": anchor_before,
                           "claim_anchor_after_sha256": anchor_after},
            "artifact_integrity": _stage("passed", original_untouched=True),
        }


def _observe_upstream(case: dict, raw: dict | None) -> dict:
    if case["execution_status"] == "blocked":
        return {
            "outcome": "blocked", "decision": None, "observed_anchor_decision": None,
            "expected_outcome_matched": True, "decision_correct": None,
            "assertions": [], "stages": {name: _stage("blocked", reason=case["blocked_reason"])
                                          for name in UPSTREAM_STAGES},
            "provenance": None, "artifact_integrity": None, "adapters": {},
        }
    _require(isinstance(raw, dict), "upstream executor must return an observation")
    _require(raw.get("outcome") in {"completed", "compile_failed", "run_failed", "assertion_failed",
                                    "artifact_tamper", "publication_interrupted", "governance_rolled_back"},
             "invalid upstream outcome")
    _require(raw.get("decision") is None or raw["decision"] in {"keep", "revalidate"},
             "invalid upstream decision")
    stages = raw.get("stages")
    _require(isinstance(stages, dict) and set(stages) == set(UPSTREAM_STAGES)
             and all(isinstance(stage, dict) and isinstance(stage.get("status"), str)
                     for stage in stages.values()), "all upstream evidence stages are required")
    observations = stages["assertion"].get("observations", [])
    observed = {item.get("id"): item.get("actual") for item in observations if isinstance(item, dict)}
    _require(len(observed) == len(observations), "duplicate or invalid upstream assertion observation")
    assertions = [{**item, "actual": observed.get(item["id"]),
                   "passed": (_assertion_passes(item, observed[item["id"]])
                              if item["id"] in observed else None)}
                  for item in case["oracle"]["assertions"]]
    outcome = raw["outcome"]
    if stages["assertion"]["status"] == "passed" and not all(item["passed"] is True for item in assertions):
        outcome = "assertion_failed"
        stages = {**stages, "assertion": {**stages["assertion"], "status": "failed"}}
    adapter_rows = {}
    for name in ADAPTERS:
        item = (raw.get("adapters") or {}).get(name)
        if outcome == "completed":
            _require(isinstance(item, dict) and item.get("decision") in {"keep", "revalidate"},
                     "completed upstream case requires all adapter decisions")
            adapter_rows[name] = {
                "decision": item["decision"],
                "decision_correct": item["decision"] == case["oracle"]["expected_decision"],
                "citation_refreshed": item.get("citation_refreshed"),
                "wiki_updated": item.get("wiki_updated"),
                "elapsed_ms": _optional_number(item.get("elapsed_ms"), "upstream adapter elapsed_ms"),
            }
        else:
            adapter_rows[name] = {
                "decision": item.get("decision") if isinstance(item, dict) else None,
                "decision_correct": None, "citation_refreshed": None,
                "wiki_updated": None, "elapsed_ms": None,
            }
    return {
        "outcome": outcome,
        "decision": raw.get("decision"),
        "observed_anchor_decision": raw.get("observed_anchor_decision"),
        "expected_outcome_matched": outcome == case["oracle"]["expected_outcome"],
        "decision_correct": (raw.get("decision") == case["oracle"]["expected_decision"]
                             if outcome == "completed" else None),
        "assertions": assertions,
        "stages": stages,
        "provenance": raw.get("provenance"),
        "artifact_integrity": raw.get("artifact_integrity"),
        "adapters": adapter_rows,
    }


def _summarize_upstream(rows: list[dict]) -> dict:
    ready = [row for row in rows if row["execution_status"] == "ready"]
    blocked = [row for row in rows if row["execution_status"] == "blocked"]
    result = {
        "total": len(rows), "ready": len(ready), "blocked": len(blocked),
        "outcomes": dict(Counter(row["outcome"] for row in rows)),
        "ready_expected_outcomes": sum(row["expected_outcome_matched"] is True for row in ready),
        "semantic_decisions_correct": sum(row["decision_correct"] is True for row in ready),
        "stage_statuses": {stage: dict(Counter(row["stages"][stage]["status"] for row in rows))
                           for stage in UPSTREAM_STAGES},
        "adapters": {},
    }
    semantic = [row for row in ready if row["classification"] != "failure"
                and row["outcome"] == "completed"]
    for name in ADAPTERS:
        records = [row["adapters"][name] for row in semantic]
        effective = [row for row in semantic if row["classification"] == "effective"]
        unrelated = [row for row in semantic if row["classification"] == "unrelated_to_claim"]
        result["adapters"][name] = {
            "decision_correct": sum(row["adapters"][name]["decision_correct"] is True for row in semantic),
            "decision_denominator": len(records),
            "missed_invalidation": sum(row["adapters"][name]["decision"] == "keep" for row in effective),
            "effective_denominator": len(effective),
            "false_invalidation": sum(row["adapters"][name]["decision"] == "revalidate" for row in unrelated),
            "unrelated_denominator": len(unrelated),
            "elapsed_ms": round(sum(row["adapters"][name]["elapsed_ms"] or 0 for row in semantic), 3),
        }
    return result


def evaluate_upstream(manifest_path: str | Path, *, mode: str = "validate",
                      executor: Callable[[dict, dict], dict] | None = None) -> dict:
    """Validate or execute the real-upstream maintenance foundation."""
    _require(mode in {"validate", "execute"}, "upstream mode must be validate or execute")
    if mode == "execute" and executor is None:
        executor = upstream_executor
    _require(mode == "execute" or executor is None, "validate mode cannot invoke an executor")
    validated = load_upstream_manifest(manifest_path)
    rows = []
    for item in validated["cases"]:
        case = item["case"]
        raw = None
        error_type = None
        if mode == "execute" and case["execution_status"] == "ready":
            bound = [item["paths"]["patch"], item["paths"]["harness"], *item["paths"]["support"]]
            hashes_before = {str(path): file_hash(path) for path in bound}
            try:
                execution_case = {
                    "id": case["id"], "execution_status": "ready",
                    "failure_mode": case["failure_mode"],
                    "revision": validated["manifest"]["corpora"][case["corpus"]]["revision"],
                    "artifacts": json.loads(json.dumps(case["artifacts"])),
                    "assertions": json.loads(json.dumps(case["oracle"]["assertions"])),
                    "publication": json.loads(json.dumps(case["publication"])),
                }
                raw = executor(execution_case, {
                    "repository": item["repository"], "anchor": item["anchor"],
                    "claim_dependencies": json.loads(json.dumps(item["claim_dependencies"])),
                    "build": json.loads(json.dumps(item["build"])), "paths": item["paths"],
                })
            except Exception as exc:
                error_type = type(exc).__name__
            _require(all(path.is_file() and file_hash(path) == hashes_before[str(path)] for path in bound),
                     "upstream executor modified a bound fixture")
            _require(file_hash(manifest_path) == validated["manifest_sha256"],
                     "upstream executor modified the manifest")
        if mode == "validate" and case["execution_status"] == "ready":
            observed = {
                "outcome": "not_run", "decision": None, "observed_anchor_decision": None,
                "expected_outcome_matched": None, "decision_correct": None, "assertions": [],
                "stages": _skipped_stages("validate mode does not execute"), "provenance": None,
                "artifact_integrity": None,
            }
        elif error_type is None:
            observed = _observe_upstream(case, raw)
        else:
            observed = {
                "outcome": "executor_failed", "decision": None, "observed_anchor_decision": None,
                "expected_outcome_matched": False, "decision_correct": None, "assertions": [],
                "stages": _skipped_stages("executor raised"), "provenance": None,
                "artifact_integrity": None,
            }
        rows.append({
            "id": case["id"], "corpus": case["corpus"],
            "classification": case["classification"], "material_kind": case["material_kind"],
            "execution_status": case["execution_status"], "failure_mode": case["failure_mode"],
            "claim": case["claim"], "mutation": case["mutation"],
            "expected_decision": case["oracle"]["expected_decision"],
            "expected_outcome": case["oracle"]["expected_outcome"],
            "blocked_reason": case.get("blocked_reason"), "error_type": error_type, **observed,
        })
    if mode == "execute":
        load_upstream_manifest(manifest_path)
    ready = [row for row in rows if row["execution_status"] == "ready"]
    blocked = [row for row in rows if row["execution_status"] == "blocked"]
    expected_ready = (mode == "execute" and all(row["expected_outcome_matched"] is True for row in ready))
    decisions_correct = expected_ready and all(row["decision_correct"] is True
                                               for row in ready if row["classification"] != "failure")
    summary = _summarize_upstream(rows)
    project_root = Path(__file__).resolve().parents[3]
    return {
        "schema_version": UPSTREAM_SCHEMA_VERSION,
        "evaluation": UPSTREAM_EVALUATION,
        "mode": mode,
        "status": ("partially_executed_blocked" if mode == "execute" and blocked else
                   "executed" if mode == "execute" else "validated_not_run"),
        "manifest_sha256": validated["manifest_sha256"],
        "identity": {
            "manifest_hash": validated["manifest_sha256"],
            "evaluator_hash": file_hash(Path(__file__)),
            "dependency_policy_hash": file_hash(project_root / "src/codeatlas/dependencies.py"),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpora": validated["manifest"]["corpora"],
        "boundary": ("Real patches run only in git-archived temporary copies of pinned public upstream commits. "
                     "The dependency adapter evaluates the reviewed card function/macro manifest; publication faults "
                     "execute the production durable prepare/commit/recover journal, but this is not a timed full "
                     "parse/index rebuild. "
                     "No production Wiki, database, or network is mutated."),
        "cases": rows,
        "summary": summary,
        "acceptance": {
            "manifest_valid": True,
            "all_24_executed": mode == "execute" and not blocked and len(ready) == 24,
            "all_ready_expected_outcomes": expected_ready,
            "ready_semantic_decisions_correct": decisions_correct,
            "blocked_not_counted_as_complete": all(row["outcome"] == "blocked" for row in blocked),
            "pinned_upstream_archives": mode == "execute" and all(
                row["provenance"] and row["provenance"].get("revision") ==
                validated["manifest"]["corpora"][row["corpus"]]["revision"]
                for row in ready),
            "current_decisions_passed": decisions_correct,
            "current_missed_invalidation": summary["adapters"]["current"]["missed_invalidation"],
            "current_false_invalidation": summary["adapters"]["current"]["false_invalidation"],
            "oracle_independently_validated": mode == "execute" and expected_ready,
        },
    }


def verify_upstream_report(report: dict, manifest_path: str | Path) -> dict:
    """Rebuild every derived upstream result from the frozen manifest and rows.

    The verifier intentionally does not treat the report's green booleans as
    evidence.  It validates stage boundaries, executable oracle observations,
    provenance, adapter decisions and aggregates before the registry can call
    the experiment complete.
    """
    validated = load_upstream_manifest(manifest_path)
    _require(report.get("evaluation") == UPSTREAM_EVALUATION,
             "not an upstream maintenance report")
    _require(report.get("manifest_sha256") == validated["manifest_sha256"],
             "upstream report manifest hash differs")
    _require(report.get("mode") == "execute" and report.get("status") == "executed",
             "only raw executed upstream reports are verifiable")
    rows = report.get("cases")
    _require(isinstance(rows, list) and len(rows) == 24,
             "upstream report requires exactly 24 observations")
    by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
    _require(len(by_id) == 24, "upstream report case ids must be unique")
    rebuilt = []
    for item in validated["cases"]:
        case = item["case"]
        row = by_id.get(case["id"])
        _require(isinstance(row, dict), "upstream report is missing a registered case")
        identity = {
            "corpus": case["corpus"], "classification": case["classification"],
            "material_kind": case["material_kind"],
            "execution_status": case["execution_status"],
            "failure_mode": case["failure_mode"], "claim": case["claim"],
            "mutation": case["mutation"],
            "expected_decision": case["oracle"]["expected_decision"],
            "expected_outcome": case["oracle"]["expected_outcome"],
            "blocked_reason": case.get("blocked_reason"),
        }
        _require(all(row.get(key) == value for key, value in identity.items()),
                 "upstream report case identity differs from pre-registration")
        raw = {
            "outcome": row.get("outcome"), "decision": row.get("decision"),
            "observed_anchor_decision": row.get("observed_anchor_decision"),
            "stages": copy.deepcopy(row.get("stages")),
            "provenance": copy.deepcopy(row.get("provenance")),
            "artifact_integrity": copy.deepcopy(row.get("artifact_integrity")),
            "adapters": {
                name: {
                    "decision": (row.get("adapters") or {}).get(name, {}).get("decision"),
                    "citation_refreshed": (row.get("adapters") or {}).get(name, {}).get("citation_refreshed"),
                    "wiki_updated": (row.get("adapters") or {}).get(name, {}).get("wiki_updated"),
                    "elapsed_ms": (row.get("adapters") or {}).get(name, {}).get("elapsed_ms"),
                }
                for name in ADAPTERS
            },
        }
        derived = _observe_upstream(case, raw)
        _require(all(row.get(key) == value for key, value in derived.items()),
                 "upstream report observation or adapter projection was modified")
        revision = validated["manifest"]["corpora"][case["corpus"]]["revision"]
        _require(isinstance(row.get("provenance"), dict)
                 and row["provenance"].get("revision") == revision
                 and row["provenance"].get("isolated_temporary_copy") is True,
                 "upstream case lacks pinned isolated provenance")
        mode = case.get("failure_mode")
        stages = row["stages"]
        if case["classification"] != "failure":
            _require(row["outcome"] == "completed"
                     and all(stages[name]["status"] == "passed" for name in UPSTREAM_STAGES)
                     and all(assertion["passed"] is True for assertion in row["assertions"]),
                     "semantic upstream case did not compile, run, assert and publish")
        elif mode == "compile_failure":
            _require(stages["compile"]["status"] == "failed"
                     and stages["run"]["status"] == "skipped",
                     "compile-failure control did not stop at compilation")
        elif mode == "artifact_tamper":
            _require(row["artifact_integrity"].get("status") == "failed"
                     and row["artifact_integrity"].get("original_untouched") is True
                     and stages["compile"]["status"] == "skipped",
                     "artifact-tamper control did not fail closed before compilation")
        elif mode == "publication_interruption":
            _require(stages["wiki"]["status"] == "interrupted"
                     and stages["wiki"].get("committed") is False
                     and stages["wiki"].get("recovered") is True
                     and stages["wiki"].get("journal_state") == "rolled_back"
                     and stages["wiki"].get("card_removed") is True
                     and stages["wiki"].get("citation_removed") is True
                     and stages["wiki"].get("before_sha256") == stages["wiki"].get("after_sha256"),
                     "publication interruption exposed a half-published Wiki")
        elif mode == "governance_rollback":
            _require(all(stages[name]["status"] == "rolled_back"
                         for name in ("card", "citation", "wiki"))
                     and stages["wiki"].get("journal_state") == "rolled_back"
                     and stages["wiki"].get("before_sha256") == stages["wiki"].get("after_sha256"),
                     "governance rollback did not restore the previous publication")
        rebuilt.append({"id": case["id"], **identity,
                        "error_type": row.get("error_type"), **derived})
    summary = _summarize_upstream(rebuilt)
    _require(report.get("summary") == summary,
             "upstream report aggregate differs from raw observations")
    expected = {
        "manifest_valid": True, "all_24_executed": True,
        "all_ready_expected_outcomes": summary["ready_expected_outcomes"] == 24,
        "ready_semantic_decisions_correct":
            summary["adapters"]["current"]["decision_correct"] == 16,
        "blocked_not_counted_as_complete": True,
        "pinned_upstream_archives": True,
        "current_decisions_passed":
            summary["adapters"]["current"]["decision_correct"] == 16,
        "current_missed_invalidation":
            summary["adapters"]["current"]["missed_invalidation"],
        "current_false_invalidation":
            summary["adapters"]["current"]["false_invalidation"],
        "oracle_independently_validated": summary["ready_expected_outcomes"] == 24,
    }
    _require(all((report.get("acceptance") or {}).get(key) == value
                 for key, value in expected.items()),
             "upstream acceptance flags differ from recomputed evidence")
    unsafe = expected["current_missed_invalidation"] > 0
    # Execution is complete even when the current policy over-invalidates.  The
    # broad value claim remains unsupported until comparable maintenance/query
    # cost is measured; this is not converted into a positive result.
    return {
        "integrity_ok": True, "execution_complete": True,
        "review_status": "deterministic_oracle", "claim_supported": False,
        "acceptance": {
            **(report.get("acceptance") or {}),
            "execution_complete": True,
            "current_decisions_passed": expected["current_decisions_passed"],
            "oracle_independently_validated": expected["oracle_independently_validated"],
            "unsafe_missed_invalidation": unsafe,
            "maintenance_value_measured": False,
        },
    }


def render_upstream(report: dict) -> str:
    """Render the actual upstream run without folding blocked cases into passes."""
    summary = report["summary"]
    lines = [
        "# CodeAtlas cJSON / lwIP 真实上游维护实验", "",
        f"> 状态：`{report['status']}`；执行 {summary['ready']}/{summary['total']}；"
        f"阻塞 {summary['blocked']}；固定上游归档="
        f"`{str(report['acceptance']['pinned_upstream_archives']).lower()}`。", "",
        "## 结果", "",
        f"- 与预登记 outcome 一致：{summary['ready_expected_outcomes']}/{summary['ready']}。",
        f"- 当前保守依赖决策正确：{summary['adapters']['current']['decision_correct']}/"
        f"{summary['adapters']['current']['decision_denominator']}；有效变更漏放 "
        f"{summary['adapters']['current']['missed_invalidation']}/"
        f"{summary['adapters']['current']['effective_denominator']}；无关变更误失效 "
        f"{summary['adapters']['current']['false_invalidation']}/"
        f"{summary['adapters']['current']['unrelated_denominator']}；依赖指纹耗时 "
        f"{summary['adapters']['current']['elapsed_ms']:.3f} ms。",
        f"- 主锚点基线决策正确：{summary['adapters']['main_anchor_hash']['decision_correct']}/"
        f"{summary['adapters']['main_anchor_hash']['decision_denominator']}；全部失效基线："
        f"{summary['adapters']['invalidate_all']['decision_correct']}/"
        f"{summary['adapters']['invalidate_all']['decision_denominator']}。",
        f"- 24 场景全部实际执行：`{str(report['acceptance']['all_24_executed']).lower()}`。",
        f"- outcome：`{json.dumps(summary['outcomes'], ensure_ascii=False, sort_keys=True)}`。",
        "", "| 场景 | 语料 | 分类 | 执行状态 | outcome | 决策 |", "|---|---|---|---|---|---|",
    ]
    for row in report["cases"]:
        lines.append(
            f"| {row['id']} | {row['corpus']} | {row['classification']} | "
            f"{row['execution_status']} | {row['outcome']} | {row.get('decision') or '—'} |"
        )
        if row.get("blocked_reason"):
            lines.append(f"\n> `{row['id']}` blocked：{row['blocked_reason']}\n")
    lines.extend([
        "", "## 边界", "", report["boundary"], "",
        "这里的当前策略按仓库现有保守依赖边界重算；主锚点与全部失效只是对照。"
        "源码补丁、编译、运行断言与四类故障控制都是真实执行；快照/卡片/Wiki 阶段验证的是"
        "本地事务证据，不冒充完整解析和索引重建计时。24 个场景执行完成不等于维护收益已经成立："
        "没有同单位的知识生产、真人复审与查询节省，因此摊销价值仍为未测。", "",
    ])
    return "\n".join(lines)
