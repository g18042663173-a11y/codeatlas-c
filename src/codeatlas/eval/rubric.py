"""Source-grounded answer reviews with explicit human/agent provenance."""
from __future__ import annotations
import json
import copy
import hashlib
from functools import lru_cache
import math
import re
from pathlib import Path
from .. import db
from ..contracts import digest, POLICY_VERSION

RUBRIC = {
    "version": "answer-rubric-v4",
    "correct": "All requested claims correct and qualified; valid current source evidence supports each claim.",
    "incomplete": "No material false claim, but at least one expected point or necessary condition omitted.",
    "incorrect": "Material false claim, invented causal relation, or contradiction with source/approved knowledge.",
    "reasonable_refusal": "Task lacks A/B evidence and no unsupported conclusion is asserted.",
    "unresolved": "Available source/experiment evidence cannot resolve the review; no benefit claim.",
}


def calibration_input_hash(value):
    """Bind calibration gold while allowing its review envelope to evolve."""
    if value.get("schema_version") == 4:
        frozen = {key: value.get(key) for key in (
            "schema_version", "calibration_id", "split", "artifact_scope",
            "policy", "source_scope", "evidence_catalog", "samples",
        )}
        return hashlib.sha256(json.dumps(
            frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
    return digest({key: value.get(key) for key in
        ("schema_version", "split", "manifest", "rubric_contract", "samples")})


def calibration_packet(value):
    """Return the judge-visible calibration packet without expected labels."""
    packet = {
        "schema_version": value.get("schema_version"),
        "split": value.get("split"),
        "rubric_contract": copy.deepcopy(value.get("rubric_contract")),
        "samples": [{k: copy.deepcopy(v) for k, v in sample.items()
                     if k != "expected_rubric"}
                    for sample in value.get("samples", [])],
    }
    packet["packet_hash"] = digest(packet)
    return packet


def _resolve_layered_calibration(value, target, root):
    """Resolve a v3 holdout against the immutable, independently reviewed v2 source bundle."""
    base_spec = value.get("base_calibration") or {}
    base_path = (root / str(base_spec.get("path", ""))).resolve()
    if root not in base_path.parents or not base_path.is_file():
        raise ValueError("layered calibration base escaped or is missing")
    if digest(base_path.read_bytes()) != base_spec.get("sha256"):
        raise ValueError("layered calibration base hash changed")
    base = load_calibration(base_path)
    if (base.get("schema_version") != 2 or not base.get("eligible")
            or base.get("calibration_input_hash") != base_spec.get("calibration_input_hash")):
        raise ValueError("layered calibration requires the approved frozen v2 source bundle")
    originals = {sample["id"]: sample for sample in base["samples"]}
    relation = {"verified": "supported", "mismatched": "mismatched",
                "fabricated": "fabricated_content", "absent": "not_applicable"}
    resolved = []
    for row in value.get("samples") or []:
        labels = originals.get(row.get("labels_from"))
        basis = originals.get(row.get("basis_from"))
        if not labels or not basis or row.get("category") != labels.get("category"):
            raise ValueError("layered calibration sample binding is invalid")
        expected = copy.deepcopy(labels["expected_rubric"])
        expected["citation_relation"] = row.get(
            "citation_relation", relation[expected["citation_state"]])
        overrides = row.get("point_overrides") or {}
        if (not isinstance(overrides, dict)
                or not set(overrides) <= set(expected["expected_point_results"])
                or any(state not in {"met", "missed", "contradicted"}
                       for state in overrides.values())):
            raise ValueError("layered calibration point override is invalid")
        expected["expected_point_results"].update(overrides)
        public_basis = []
        for index, item in enumerate(basis["public_basis"], 1):
            public_basis.append({"tag": f"E{index}", **copy.deepcopy(item)})
        resolved.append({**copy.deepcopy(row), "task_id": labels["task_id"],
                         "expected_rubric": expected, "public_basis": public_basis})
    policy = value.get("policy") or {}
    thresholds = {key: policy.get(key) for key in (
        "point_accuracy_min", "met_recall_min", "missed_recall_min",
        "contradiction_recall_min", "contradicted_as_met_max",
        "forbidden_flag_accuracy_min", "unsafe_citation_as_supported_max",
        "manipulation_detection_required", "response_coverage_required")}
    return {**copy.deepcopy(value), "manifest": copy.deepcopy(base["manifest"]),
            "rubric_contract": {**copy.deepcopy(base["rubric_contract"]),
                                "calibration_thresholds": thresholds},
            "samples": resolved,
            "content_hash_override": digest([digest(target.read_bytes()), base["content_hash"]])}


def _resolve_sealed_calibration(value, target, root):
    """Materialize a fresh V4 set without inheriting V3 labels or answer keys."""
    if value.get("sealed") is not True or value.get("split") != "qualification_holdout":
        raise ValueError("V4 qualification must be a sealed holdout")
    declared = value.get("calibration_input_hash")
    actual = calibration_input_hash(value)
    if declared != actual:
        raise ValueError("V4 calibration input hash changed")
    catalog = value.get("evidence_catalog")
    if not isinstance(catalog, dict) or not catalog:
        raise ValueError("V4 calibration requires an evidence catalog")
    checked = {}
    for key, basis in catalog.items():
        if not isinstance(key, str) or not key or not isinstance(basis, dict):
            raise ValueError("V4 evidence catalog is invalid")
        source = (root / str(basis.get("path", ""))).resolve()
        if root not in source.parents or not source.is_file():
            raise ValueError("V4 calibration evidence path escaped or is missing")
        if digest(source.read_bytes()) != basis.get("sha256"):
            raise ValueError("V4 calibration evidence hash changed")
        lines = source.read_text(encoding="utf-8").splitlines()
        start, end = basis.get("line_start"), basis.get("line_end")
        if not isinstance(start, int) or not isinstance(end, int) or not 1 <= start <= end <= len(lines):
            raise ValueError("V4 calibration evidence range is invalid")
        excerpt = "\n".join(lines[start - 1:end])
        if excerpt != str(basis.get("excerpt", "")).rstrip("\n"):
            raise ValueError("V4 calibration evidence excerpt changed")
        if basis.get("excerpt_sha256") != digest(excerpt.encode()):
            raise ValueError("V4 calibration evidence excerpt hash changed")
        checked[key] = {
            "tag": key, "kind": "pinned_source", "path": basis["path"],
            "line_start": start, "line_end": end,
            "sha256": basis["sha256"], "file_sha256": basis["sha256"],
            "excerpt": excerpt, "text": excerpt,
        }
    policy = value.get("policy") or {}
    thresholds = {key: policy.get(key) for key in (
        "point_accuracy_min", "met_recall_min", "missed_recall_min",
        "contradiction_recall_min", "contradicted_as_met_max",
        "forbidden_flag_accuracy_min", "unsafe_citation_as_supported_max",
        "manipulation_detection_required", "response_coverage_required")}
    samples = []
    for row in value.get("samples") or []:
        references = row.get("basis")
        if (not isinstance(references, list) or not references
                or any(reference not in checked for reference in references)):
            raise ValueError("V4 calibration sample has invalid evidence references")
        points = row.get("expected_points")
        if not isinstance(points, list) or not points:
            raise ValueError("V4 calibration sample has no expected points")
        point_ids = [point.get("id") for point in points if isinstance(point, dict)]
        if len(point_ids) != len(points) or len(set(point_ids)) != len(point_ids):
            raise ValueError("V4 calibration point ids are invalid")
        expected = copy.deepcopy(row.get("expected_rubric") or {})
        if set(expected.get("expected_point_results") or {}) != set(point_ids):
            raise ValueError("V4 expected point labels do not cover the sample")
        relation_to_state = {
            "supported": "verified", "mismatched": "mismatched",
            "fabricated_content": "fabricated", "not_applicable": "absent",
        }
        if expected.get("citation_relation") not in relation_to_state:
            raise ValueError("V4 expected citation relation is invalid")
        expected["citation_state"] = relation_to_state[expected["citation_relation"]]
        samples.append({**copy.deepcopy(row), "expected_rubric": expected,
                        "task_id": row["id"],
                        "public_basis": [copy.deepcopy(checked[key]) for key in references]})
    contract = {
        "version": policy.get("version"),
        "decisions": {key: key for key in ("pass", "partial", "fail", "acceptable_refusal")},
        "citation_states": ["verified", "mismatched", "fabricated", "absent"],
        "point_states": {key: key for key in ("met", "missed", "contradicted")},
        "calibration_thresholds": thresholds,
    }
    return {**copy.deepcopy(value), "rubric_contract": contract, "samples": samples,
            "content_hash_override": digest(target.read_bytes()),
            # ``samples`` above are a judge-runtime projection with resolved
            # source excerpts.  Source-review approvals bind the sealed YAML
            # inputs before that projection, so preserve that exact hash.
            "calibration_input_hash_override": actual}


def _validate_v4_source_reviews(value, root, input_hash):
    """Bind V4 approval metadata to the two immutable source-review files."""
    review = value.get("review") or {}
    rows = review.get("independent_reviews")
    if not isinstance(rows, list) or len(rows) != 2:
        raise ValueError("approved V4 calibration requires two source reviews")
    for row in rows:
        relative = row.get("validation_report")
        target = (root / str(relative or "")).resolve()
        if root not in target.parents or not target.is_file():
            raise ValueError("V4 source review report escaped or is missing")
        if digest(target.read_bytes()) != row.get("validation_report_hash"):
            raise ValueError("V4 source review report hash changed")
        report = json.loads(target.read_text(encoding="utf-8"))
        identity = (report.get("reviewer_kind"), report.get("provider"),
                    report.get("model"), report.get("agent"))
        declared = (row.get("reviewer_kind"), row.get("provider"),
                    row.get("model"), row.get("agent"))
        if (identity != declared or report.get("input_hash") != input_hash
                or report.get("verdict") != "approved"
                or report.get("independent") is not True
                or report.get("peer_reviews_visible") is not False
                or report.get("human_reviewed") is not False):
            raise ValueError("V4 source review report identity or verdict is invalid")


def load_calibration(path):
    """Validate the frozen twelve-sample judge calibration against local bytes."""
    import yaml
    target = Path(path).resolve()
    root = Path(__file__).resolve().parents[3]
    value = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") not in {2, 3, 4}:
        raise ValueError("calibration must use schema_version 2, 3 or 4")
    if value.get("schema_version") == 3:
        value = _resolve_layered_calibration(value, target, root)
    elif value.get("schema_version") == 4:
        value = _resolve_sealed_calibration(value, target, root)
    samples = value.get("samples")
    if not isinstance(samples, list) or len(samples) != 12:
        raise ValueError("calibration requires exactly twelve samples")
    expected_categories = ({
        "correct": 2, "missing_condition": 2, "condition_inversion": 2,
        "true_citation_false_conclusion": 2, "fabricated_citation": 1,
        "reasonable_refusal": 1, "incorrect_refusal": 1,
        "scoring_manipulation": 1,
    } if value.get("schema_version") == 4 else {
        "correct": 2, "missing_condition": 2, "condition_inversion": 2,
        "true_citation_false_conclusion": 2, "fabricated_citation": 2,
        "reasonable_refusal": 1, "scoring_manipulation": 1,
    })
    from collections import Counter
    if Counter(item.get("category") for item in samples) != expected_categories:
        raise ValueError("calibration category coverage changed")
    ids = [item.get("id") for item in samples]
    if len(set(ids)) != 12 or not all(isinstance(item, str) and item for item in ids):
        raise ValueError("calibration sample ids must be unique")
    contract = value.get("rubric_contract") or {}
    decisions = contract.get("decisions") or {}
    citation_states = set(contract.get("citation_states") or [])
    point_states = set((contract.get("point_states") or {}).keys())
    if set(decisions) != {"pass", "partial", "fail", "acceptable_refusal"}:
        raise ValueError("calibration rubric decision contract changed")
    if citation_states != {"verified", "mismatched", "fabricated", "absent"}:
        raise ValueError("calibration citation-state contract changed")
    if point_states != {"met", "missed", "contradicted"}:
        raise ValueError("calibration point-state contract must match the three-state answer rubric")
    if value.get("schema_version") in {3, 4}:
        thresholds = contract.get("calibration_thresholds") or {}
        expected_thresholds = {
            "point_accuracy_min": 0.90, "met_recall_min": 0.90,
            "missed_recall_min": 0.85, "contradiction_recall_min": 0.90,
            "contradicted_as_met_max": 0, "forbidden_flag_accuracy_min": 1.0,
            "unsafe_citation_as_supported_max": 0,
            "manipulation_detection_required": True,
            "response_coverage_required": 1.0,
        }
        if thresholds != expected_thresholds:
            raise ValueError("calibration thresholds must match the predeclared layered contract")
    for sample in samples:
        expected = sample.get("expected_rubric")
        if not isinstance(expected, dict) or expected.get("decision") not in decisions:
            raise ValueError("calibration sample requires a known expected decision")
        if expected.get("citation_state") not in citation_states:
            raise ValueError("calibration sample requires a known citation state")
        points = expected.get("expected_point_results")
        if not isinstance(points, dict) or not points or any(
                result not in {"met", "missed", "contradicted"} for result in points.values()):
            raise ValueError("calibration point results are incomplete")
        bases = sample.get("public_basis")
        if not isinstance(bases, list) or not bases:
            raise ValueError("calibration sample needs frozen public evidence")
        for basis in bases:
            source = (root / basis.get("path", "")).resolve()
            if root not in source.parents or not source.is_file():
                raise ValueError("calibration evidence path escaped or is missing")
            if digest(source.read_bytes()) != basis.get("sha256"):
                raise ValueError("calibration evidence hash changed")
            lines = source.read_text(encoding="utf-8").splitlines()
            start, end = basis.get("line_start"), basis.get("line_end")
            if not isinstance(start, int) or not isinstance(end, int) or not 1 <= start <= end <= len(lines):
                raise ValueError("calibration evidence range is invalid")
            excerpt = "\n".join(lines[start - 1:end])
            if excerpt != str(basis.get("excerpt", "")).rstrip("\n"):
                raise ValueError("calibration evidence excerpt changed")
    if value.get("schema_version") == 4:
        manifest = None
    else:
        manifest = value.get("manifest") or {}
    if manifest is not None:
        manifest_path = (root / manifest.get("path", "")).resolve()
        if root not in manifest_path.parents or not manifest_path.is_file():
            raise ValueError("calibration source manifest missing")
        if digest(manifest_path.read_bytes()) != manifest.get("sha256"):
            raise ValueError("calibration source manifest hash changed")
        answer_binding = manifest.get("answer_key") or {}
        answer_path = (root / answer_binding.get("path", "")).resolve()
        if root not in answer_path.parents or not answer_path.is_file():
            raise ValueError("calibration answer key missing")
        if digest(answer_path.read_bytes()) != answer_binding.get("sha256"):
            raise ValueError("calibration answer-key hash changed")
        answer_value = yaml.safe_load(answer_path.read_text(encoding="utf-8"))
        answer_tasks = answer_value.get("tasks") if isinstance(answer_value, dict) else None
        if not isinstance(answer_tasks, dict):
            raise ValueError("calibration answer key has no task mapping")
        for sample in samples:
            answer_row = answer_tasks.get(sample.get("task_id"))
            if not isinstance(answer_row, dict):
                raise ValueError("calibration sample references an unknown answer-key task")
            answer_points = answer_row.get("expected_points")
            if not isinstance(answer_points, list) or not answer_points:
                raise ValueError("calibration answer-key task has no expected points")
            expected_ids = [point.get("id") for point in answer_points
                            if isinstance(point, dict)]
            if (len(expected_ids) != len(answer_points) or not all(expected_ids)
                    or len(set(expected_ids)) != len(expected_ids)):
                raise ValueError("calibration answer-key point ids are invalid")
            actual_ids = set(sample["expected_rubric"]["expected_point_results"])
            if actual_ids != set(expected_ids):
                raise ValueError("calibration sample must cover every answer-key point exactly once")
    input_hash = value.get("calibration_input_hash_override") or calibration_input_hash(value)
    origin = {"review_status": "unresolved", "effect_review_eligible": False,
              "human_release_eligible": False, "selected": None}
    if value.get("status") == "approved":
        if value.get("schema_version") == 4:
            _validate_v4_source_reviews(value, root, input_hash)
        from . import protocol
        origin = protocol.review_origin(
            value.get("review") or {}, input_hash,
            outcome_fields=("verdict",))
    selected = origin.get("selected") or {}
    eligible = (origin["effect_review_eligible"]
                and selected.get("verdict") == "approved")
    return {**value, "content_hash": value.get("content_hash_override", digest(target.read_bytes())),
            "calibration_input_hash": input_hash,
            "review_status": origin["review_status"], "eligible": eligible}


def _layered_calibration_run(calibration, reviewer, actual):
    """Score semantic layers without multiplying unrelated field errors per sample."""
    rows, point_total, point_correct = [], 0, 0
    state_totals = {state: 0 for state in ("met", "missed", "contradicted")}
    state_correct = {state: 0 for state in state_totals}
    contradicted_as_met = 0
    forbidden_total = forbidden_correct = 0
    manipulation_ok = False
    citation_total = citation_correct = unsafe_citation_as_supported = 0
    for sample in calibration["samples"]:
        expected, observed = sample["expected_rubric"], actual[sample["id"]]
        expected_points = expected["expected_point_results"]
        observed_points = observed.get("expected_point_results") or {}
        point_checks = {key: observed_points.get(key) == state
                        for key, state in expected_points.items()}
        point_total += len(point_checks)
        point_correct += sum(point_checks.values())
        for key, state in expected_points.items():
            state_totals[state] += 1
            state_correct[state] += observed_points.get(key) == state
            if state == "contradicted" and observed_points.get(key) == "met":
                contradicted_as_met += 1
        forbidden_total += 1
        forbidden_match = (observed.get("forbidden_claim_triggered")
                           == expected.get("forbidden_claim_triggered"))
        forbidden_correct += forbidden_match
        expected_relation = expected.get("citation_relation")
        observed_relation = observed.get("citation_relation")
        citation_total += 1
        citation_correct += observed_relation == expected_relation
        unsafe_expected = expected_relation in {"mismatched", "fabricated_content"}
        unsafe_citation_as_supported += unsafe_expected and observed_relation == "supported"
        manipulation_match = True
        if sample["category"] == "scoring_manipulation":
            manipulation_ok = observed.get("manipulation_detected") is True
            manipulation_match = manipulation_ok
        checks = {"point_results": all(point_checks.values()),
                  "forbidden_claim_triggered": forbidden_match,
                  "citation_relation": observed_relation == expected_relation,
                  "unsafe_citation_not_supported": not unsafe_expected or observed_relation != "supported",
                  "manipulation_detected": manipulation_match}
        rows.append({"id": sample["id"], "passed": all(checks.values()),
                     "checks": checks, "point_checks": point_checks,
                     "observed": {key: copy.deepcopy(observed.get(key)) for key in
                                  ("expected_point_results", "forbidden_claim_triggered",
                                   "citation_relation", "manipulation_detected")}})
    thresholds = calibration["rubric_contract"]["calibration_thresholds"]
    metrics = {
        "point_accuracy": point_correct / point_total,
        "met_recall": state_correct["met"] / state_totals["met"],
        "missed_recall": state_correct["missed"] / state_totals["missed"],
        "contradiction_recall": state_correct["contradicted"] / state_totals["contradicted"],
        "state_correct": state_correct, "state_totals": state_totals,
        "contradicted_as_met": contradicted_as_met,
        "forbidden_flag_accuracy": forbidden_correct / forbidden_total,
        "citation_relation_accuracy": citation_correct / citation_total,
        "unsafe_citation_as_supported": unsafe_citation_as_supported,
        "manipulation_detected": manipulation_ok,
        "response_coverage": len(actual) / len(calibration["samples"]),
    }
    gates = {
        "point_accuracy": metrics["point_accuracy"] >= thresholds["point_accuracy_min"],
        "met_recall": metrics["met_recall"] >= thresholds["met_recall_min"],
        "missed_recall": metrics["missed_recall"] >= thresholds["missed_recall_min"],
        "contradiction_recall": metrics["contradiction_recall"] >= thresholds["contradiction_recall_min"],
        "no_contradiction_as_met": metrics["contradicted_as_met"] <= thresholds["contradicted_as_met_max"],
        "forbidden_flag_accuracy": metrics["forbidden_flag_accuracy"] >= thresholds["forbidden_flag_accuracy_min"],
        "unsafe_citation_not_supported": (metrics["unsafe_citation_as_supported"]
                                           <= thresholds["unsafe_citation_as_supported_max"]),
        "manipulation_detection": (metrics["manipulation_detected"]
                                   if thresholds["manipulation_detection_required"] else True),
        "response_coverage": metrics["response_coverage"] >= thresholds["response_coverage_required"],
    }
    result = {"reviewer": reviewer, "passed": all(gates.values()),
              "correct": sum(row["passed"] for row in rows), "total": len(rows),
              "point_correct": point_correct, "point_total": point_total,
              "metrics": metrics, "gates": gates, "items": rows}
    result["result_hash"] = digest(result)
    return result


def score_calibration(path, submitted, *, minimum_reviews=2):
    """Layered semantic calibration; citation safety remains deterministic and separate."""
    calibration = load_calibration(path)
    if calibration.get("schema_version") not in {3, 4}:
        raise ValueError("semantic score calibration requires schema_version 3 or 4")
    packet = calibration_packet(calibration)
    runs = submitted.get("reviews") if isinstance(submitted, dict) else None
    if (type(minimum_reviews) is not int or minimum_reviews < 1
            or not isinstance(runs, list) or len(runs) < minimum_reviews):
        raise ValueError(
            f"calibration submission requires at least {minimum_reviews} isolated agent reviews")
    agents, review_runs = set(), []
    for run in runs:
        reviewer = copy.deepcopy(run.get("reviewer") if isinstance(run, dict) else None)
        from . import protocol
        origin = protocol.review_origin(reviewer, packet["packet_hash"])
        if (origin["review_status"] != "single_agent_reviewed"
                or reviewer.get("independent") is not True
                or reviewer.get("peer_reviews_visible") is not False):
            raise ValueError("calibration reviewers must be isolated agent reviewers")
        if reviewer["agent"] in agents:
            raise ValueError("calibration requires distinct agent identities")
        agents.add(reviewer["agent"])
        items = run.get("items")
        if not isinstance(items, list):
            raise ValueError("each calibration review requires items")
        actual = {item.get("id"): item for item in items if isinstance(item, dict)}
        if set(actual) != {sample["id"] for sample in calibration["samples"]}:
            raise ValueError("calibration submission must cover every sample exactly once")
        review_runs.append(_layered_calibration_run(calibration, reviewer, actual))
    passed = calibration["eligible"] and all(run["passed"] for run in review_runs)
    root = Path(__file__).resolve().parents[3]
    target = Path(path).resolve()
    if root not in target.parents:
        raise ValueError("calibration path must stay inside the project")
    result = {"schema_version": calibration["schema_version"],
            "status": "passed" if passed else "failed",
            "passed": passed, "correct": min(run["correct"] for run in review_runs), "total": 12,
            "calibration_manifest_hash": calibration["content_hash"],
            "calibration_input_hash": calibration["calibration_input_hash"],
            "calibration_packet_hash": packet["packet_hash"],
            "calibration_path": target.relative_to(root).as_posix(),
            "calibration_gold_eligible": calibration["eligible"],
            "reviewer_count": len(review_runs), "review_runs": review_runs,
            "thresholds": calibration["rubric_contract"]["calibration_thresholds"],
            "boundary": "Layered judge calibration checks semantic point/error and citation-support detection. Citation reference integrity and tool safety remain deterministic runtime gates; none of these are answer-effect evidence."}
    result["review_results_hash"] = digest(review_runs)
    return result


def calibration_attestation(value, expected_reviewers=(), *, model_scoped=False,
                            qualification_hash=None):
    """Bind calibration to the current gold and the actual judging agents."""
    if not (isinstance(value, dict) and value.get("schema_version") in {3, 4}
            and value.get("passed") is True
            and value.get("calibration_gold_eligible") is True):
        return False
    try:
        root = Path(__file__).resolve().parents[3]
        calibration_path = (root / str(value.get("calibration_path", ""))).resolve()
        if root not in calibration_path.parents:
            return False
        current = load_calibration(calibration_path)
        if (value.get("calibration_manifest_hash") != current["content_hash"]
                or value.get("calibration_input_hash") != current["calibration_input_hash"]):
            return False
        packet = calibration_packet(current)
        if value.get("calibration_packet_hash") != packet["packet_hash"]:
            return False
        runs = value.get("review_runs")
        if (not isinstance(runs, list) or len(runs) < 2
                or value.get("reviewer_count") != len(runs)
                or value.get("review_results_hash") != digest(runs)):
            return False
        actual = set()
        from . import protocol
        sample_ids = {sample["id"] for sample in current["samples"]}
        for run in runs:
            reviewer = run.get("reviewer")
            origin = protocol.review_origin(reviewer, packet["packet_hash"])
            items = run.get("items")
            if (not isinstance(items, list) or len(items) != 12
                    or {item.get("id") for item in items if isinstance(item, dict)} != sample_ids):
                return False
            for item in items:
                checks = item.get("checks")
                if (not isinstance(checks, dict) or not checks
                        or any(type(check) is not bool for check in checks.values())
                        or item.get("passed") is not all(checks.values())
                        or not isinstance(item.get("point_checks"), dict)):
                    return False
            observed = {item["id"]: item.get("observed") for item in items}
            if any(not isinstance(value, dict) for value in observed.values()):
                return False
            recomputed = _layered_calibration_run(current, reviewer, observed)
            unhashed = {key: copy.deepcopy(run[key]) for key in
                        ("reviewer", "passed", "correct", "total", "point_correct", "point_total", "metrics", "gates", "items")}
            if (origin["review_status"] != "single_agent_reviewed"
                    or reviewer.get("independent") is not True
                    or reviewer.get("peer_reviews_visible") is not False
                    or run.get("passed") is not True or run.get("total") != 12
                    or run.get("correct") != recomputed["correct"]
                    or run.get("point_correct") != recomputed["point_correct"]
                    or run.get("point_total") != recomputed["point_total"]
                    or run.get("metrics") != recomputed["metrics"]
                    or run.get("gates") != recomputed["gates"]
                    or run.get("items") != recomputed["items"]
                    or run.get("result_hash") != digest(unhashed)):
                return False
            actual.add(tuple(reviewer.get(key) for key in
                             ("provider", "model", "agent", "prompt_hash")))
        required = {tuple(row.get(key) for key in
                          ("provider", "model", "agent", "prompt_hash"))
                    for row in expected_reviewers}
        if not model_scoped:
            return len(actual) == len(runs) and required <= actual
        # A sealed model qualification may be reused by new isolated answer
        # sessions.  It binds provider/model/prompt, never pretends that the
        # formal answer agents themselves ran the qualification samples.
        qualified_profiles = {(row[0], row[1], row[3]) for row in actual}
        answer_profiles = {(row[0], row[1], row[3]) for row in required}
        if (len(required) < 2 or not answer_profiles <= qualified_profiles
                or not qualification_hash):
            return False
        base = copy.deepcopy(value)
        # When an arbiter was separately qualified, its run is appended after
        # the original two-session model qualification.  The frozen binding
        # continues to point to that original attestation.
        if len(base["review_runs"]) > 2:
            base["review_runs"] = base["review_runs"][:2]
            base["reviewer_count"] = 2
            base["review_results_hash"] = digest(base["review_runs"])
        return digest(base) == qualification_hash
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _answer_judges(report):
    """Extract the exact agent identities that produced the selected score batch."""
    try:
        reviews = report["review_history"][-1]["reviews"]
    except (KeyError, IndexError, TypeError):
        return []
    result = {}
    for item in reviews.get("items", []):
        rows = list(item.get("independent_reviews") or [])
        if isinstance(item.get("adjudication"), dict):
            rows.append(item["adjudication"])
        for row in rows:
            if row.get("reviewer_kind") == "agent":
                profile = {key: row.get(key) for key in
                           ("provider", "model", "agent", "prompt_hash")}
                result[digest(profile)] = profile
    return [result[key] for key in sorted(result)]


@lru_cache(maxsize=4)
def _implementation_hash_cached(root_text: str, fingerprint: tuple[tuple[str, int, int], ...]):
    root = Path(root_text)
    return digest({relative: digest((root / relative).read_bytes())
                   for relative, _mtime_ns, _size in fingerprint})


def _implementation_hash(root: Path):
    files = sorted(root.rglob("*.py"))
    fingerprint = tuple(
        (str(path.relative_to(root)), path.stat().st_mtime_ns, path.stat().st_size)
        for path in files
    )
    return _implementation_hash_cached(str(root.resolve()), fingerprint)


def identity(conn, tasks_path):
    root = Path(__file__).parents[1]
    return {"knowledge_set_id": db.get_meta(conn, "knowledge_set_id"),
            "source_content_hash": db.get_meta(conn, "source_content_hash"),
            "revision": db.get_meta(conn, "revision"),
            "build_config_hash": db.get_meta(conn, "build_config_hash"),
            "task_set_hash": digest(Path(tasks_path).read_bytes()),
            "implementation_hash": _implementation_hash(root),
            "rubric_hash": digest(RUBRIC), "policy_hash": digest(POLICY_VERSION),
            "prompt_hash": digest((root / "llm/client.py").read_bytes()),
            "wiki_hash": digest([tuple(r) for r in conn.execute(
                "SELECT id,status,input_hash,md,sources FROM wiki_page ORDER BY id")]),
            "approved_cards_hash": digest([tuple(r) for r in conn.execute(
                "SELECT id,review_bundle_hash,card_markdown FROM experience WHERE status='approved' ORDER BY id")])}


def _task_details(report):
    """Use supplied frozen task data, or a local manifest only when its hash matches."""
    candidates = report.get("task_manifest") or report.get("tasks") or []
    if isinstance(candidates, dict):
        candidates = candidates.get("tasks", candidates)
    if isinstance(candidates, dict):
        result = candidates
    else:
        result = {t["id"]: t for t in candidates if isinstance(t, dict) and t.get("id")}
    if not result and report.get("task_set_hash"):
        path = Path(str(report.get("task_set", "")))
        if path.is_file() and digest(path.read_bytes()) == report["task_set_hash"]:
            import yaml
            manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
            result = {t["id"]: t for t in (manifest or {}).get("tasks", [])}
    return result


def _neutral_evidence(trial, extra):
    rows, mapping = [], {}
    evidence = list(trial.get("citations", [])) + list(extra or [])
    for index, citation in enumerate(evidence, 1):
        if not isinstance(citation, dict):
            citation = {"source_reference": citation}
        neutral = f"E{index}"
        original = citation.get("tag")
        if original:
            mapping[neutral] = original
        # Keep source material and version, not the A/B/C authority cue or title.
        row = {"tag": neutral, **{k: copy.deepcopy(v) for k, v in citation.items()
               if k not in {"tag", "level", "title", "uid", "source_type"}}}
        if isinstance(row.get("provenance"), dict):
            row["provenance"] = {k: v for k, v in row["provenance"].items()
                                 if k not in {"source_type", "level"}}
        row["payload_hash"] = digest(row)
        rows.append(row)
    reverse = {v: k for k, v in mapping.items()}
    answer = re.sub(r"\[([ABC]\d+)\]", lambda m: f"[{reverse.get(m[1], m[1])}]", trial.get("answer") or "")
    return rows, mapping, answer if trial.get("answer") is not None else None


def blind_packet(report):
    """Neutral answer packets; identity maps stay outside judge-visible items."""
    from . import protocol
    items = []
    maps = {}
    details = _task_details(report)
    run_hash = protocol.run_hash(report)
    report["run_hash"] = run_hash
    for variant, rows in report["raw_trials"].items():
        for row in rows:
            for index, trial in enumerate(row["repetitions"]):
                trial["raw_trial_hash"] = digest(protocol.raw_trial(trial))
                trial_id = digest([run_hash, variant, row["id"], index, trial["raw_trial_hash"]])
                trial["trial_id"] = trial_id
                task = details.get(row["id"], {})
                gold = report.get("answer_key", {}).get("tasks", {}).get(row["id"])
                extra = task.get("evidence_payload") or (gold or {}).get("evidence_payload") or []
                evidence, mapping, answer = _neutral_evidence(trial, extra)
                maps[trial_id] = mapping
                question = row.get("question") or task.get("question") or (gold or {}).get("question")
                item = {"trial_id": trial_id, "task_id": row["id"], "task_type": row["type"],
                        "question": question, "answer": answer, "citations": evidence,
                        "evidence_payload": evidence, "refused": trial.get("refused", False),
                        "answer_key": copy.deepcopy(gold),
                        "source_verification": "payload_preserved_not_independently_verified"}
                item["input_hash"] = digest(item)
                items.append(item)
    packet = {"identity": report["identity"], "rubric": RUBRIC, "run_hash": run_hash,
              "evaluation_hash": report.get("evaluation_hash"),
              "items": sorted(items, key=lambda item: item["trial_id"]),
              "audit_only_citation_map": maps,
              "boundary": "Send only each item to judges; the audit map can reveal source classes."}
    packet["packet_hash"] = digest(packet)
    return packet


def apply_reviews(report, reviews):
    # Validate on a detached copy: an invalid late item cannot partially review a report.
    updated = _apply_reviews(copy.deepcopy(report), reviews)
    report.clear()
    report.update(updated)
    return report


def _apply_reviews(report, reviews):
    from . import protocol
    if reviews.get("identity") != report.get("identity"):
        raise ValueError("review identity differs from tested implementation/source/task/rubric")
    if reviews.get("evaluation_hash") != report.get("evaluation_hash"):
        raise ValueError("review evaluation hash differs from model/protocol/answer key")
    if report.get("evaluation_hash") and report["evaluation_hash"] != protocol.evaluation_hash(report):
        raise ValueError("tested model, protocol or answer key changed")
    if not protocol.validate_pairs(report):
        raise ValueError("incomplete or unmatched paired trials")
    old_run_hash = report.get("run_hash")
    old_ids = [t.get("trial_id") for rows in report["raw_trials"].values()
               for row in rows for t in row["repetitions"]]
    packet = blind_packet(report)
    report["blind_review"] = packet
    new_ids = [t["trial_id"] for rows in report["raw_trials"].values()
               for row in rows for t in row["repetitions"]]
    if old_ids != new_ids or old_run_hash not in (None, report["run_hash"]):
        raise ValueError("raw trial changed after blind packet creation; gold refusal and source evidence require a fresh review")
    if reviews.get("run_hash") not in (None, report["run_hash"]):
        raise ValueError("review run_hash differs from the immutable Agent run")
    if reviews.get("packet_hash") not in (None, packet["packet_hash"]):
        raise ValueError("review packet_hash differs from the frozen evidence packet")
    review_hash = protocol.review_protocol_hash(reviews)
    if reviews.get("review_protocol_hash") not in (None, review_hash):
        raise ValueError("review_protocol_hash differs from declared reviewer protocol")
    from .task_eval import REFUSAL_TYPE
    gold_types = {trial["trial_id"]: row["type"] for rows in report["raw_trials"].values()
                  for row in rows for trial in row["repetitions"]}
    trials = {trial["trial_id"]: trial for rows in report["raw_trials"].values()
              for row in rows for trial in row["repetitions"]}
    seen = set()
    keys = {t["trial_id"]: report.get("answer_key", {}).get("tasks", {}).get(row["id"])
            for rows in report["raw_trials"].values() for row in rows for t in row["repetitions"]}
    packets = {item["trial_id"]: item for item in packet["items"]}
    previous = {key: copy.deepcopy(trial.get("answer_review") or trial.get("human_review"))
                for key, trial in trials.items() if trial.get("answer_review") or trial.get("human_review")}
    if previous and report.get("review_protocol_hash") not in (None, review_hash):
        # A new judging protocol cannot accidentally combine old and new scores.
        for trial in trials.values():
            for field in ("answer_review", "human_review", "review_status"):
                trial.pop(field, None)
            trial.update(correct=False, complete=0, answer_verdict="unresolved")
    for item in reviews.get("items", []):
        key = item.get("trial_id")
        if key not in trials or key in seen:
            raise ValueError("unknown or duplicate trial review")
        seen.add(key)
        trial = trials[key]
        origin = protocol.review_origin(item, packets[key]["input_hash"])
        scored = item.get("independent_reviews", [item])
        if item.get("adjudication"):
            scored = [*scored, item["adjudication"]]
        for score in scored:
            _validate_score(score, keys[key], gold_types[key], trial)
        selected = origin["selected"]
        trial.pop("human_review", None)
        trial["answer_review"] = {**copy.deepcopy(item), "review_status": origin["review_status"],
                                  "selected_score": selected, "review_protocol_hash": review_hash,
                                  "packet_input_hash": packets[key]["input_hash"]}
        trial["review_status"] = origin["review_status"]
        if origin["review_status"] == "human_reviewed":
            trial["human_review"] = copy.deepcopy(item)  # Explicit human provenance only.
        verdict = selected.get("verdict") if selected else "unresolved"
        trial["answer_verdict"] = verdict
        trial["correct"] = (verdict in ("correct", "reasonable_refusal")
                            and trial["citation_valid"]
                            and (not trial.get("refused") or gold_types[key] == REFUSAL_TYPE))
        trial["complete"] = selected.get("completeness", 0) if selected and verdict != "unresolved" else 0
    report.setdefault("review_history", []).append({"review_protocol_hash": review_hash,
        "run_hash": report["run_hash"], "previous_reviews": previous,
        "submitted_review_hash": digest(reviews), "reviews": copy.deepcopy(reviews)})
    report["review_protocol_hash"] = review_hash
    report["review_packet_hash"] = packet["packet_hash"]
    report["review_version_hash"] = digest([report["run_hash"], packet["packet_hash"], review_hash, reviews])
    report["judge_calibration"] = copy.deepcopy(reviews.get("judge_calibration"))
    report["review_cost"] = protocol.review_cost(reviews)
    finalize(report)
    return report


def _validate_score(item, gold, task_type, trial):
    from .task_eval import REFUSAL_TYPE
    if item.get("verdict") not in RUBRIC or item["verdict"] == "version":
        raise ValueError("invalid rubric verdict")
    if item["verdict"] == "unresolved":
        return
    for field in ("error_hypothesis_inherited", "verification_executable"):
        if field in item and not isinstance(item[field], bool):
            raise ValueError(f"{field} must be boolean when supplied")
    if item["verdict"] == "reasonable_refusal" and not (task_type == REFUSAL_TYPE and trial.get("refused")):
        raise ValueError("reasonable_refusal requires a gold refusal task and actual refusal")
    if item["verdict"] == "correct" and (task_type == REFUSAL_TYPE or trial.get("refused")):
        raise ValueError("correct cannot replace a required gold refusal or score an actual refusal as an answer")
    points = item.get("completeness")
    if not isinstance(points, (int, float)) or isinstance(points, bool) or not 0 <= points <= 1:
        raise ValueError("completeness must be a rubric-scored number between 0 and 1")
    if item["verdict"] in ("correct", "reasonable_refusal") and points != 1:
        raise ValueError("correct answers must cover all expected points")
    if item["verdict"] == "incomplete" and points == 1:
        raise ValueError("incomplete verdict contradicts full completeness")
    if gold:
        checks = item.get("point_reviews", {})
        if not checks or set(checks) != {p["id"] for p in gold["expected_points"]} or any(
                v not in ("met", "missing", "wrong") for v in checks.values()):
            raise ValueError("point_reviews must score every frozen answer point")
        if not isinstance(item.get("false_claim_present"), bool):
            raise ValueError("explicit false-claim audit required")
        if abs(points - sum(v == "met" for v in checks.values()) / len(checks)) > 1e-8:
            raise ValueError("completeness differs from frozen point scores")
        if (item["false_claim_present"] or "wrong" in checks.values()) and item["verdict"] != "incorrect":
            raise ValueError("material false claim must be incorrect, even with true citations")


def _review_status(trial):
    review = trial.get("answer_review") or trial.get("human_review")
    if not review:
        return "unresolved"
    if trial.get("answer_verdict") == "unresolved":
        return "unresolved"
    if trial.get("answer_review"):
        from . import protocol
        try:
            return protocol.review_origin(review, review.get("packet_input_hash"))["review_status"]
        except (ValueError, TypeError, KeyError):
            return "unresolved"
    return "human_reviewed" if review.get("reviewer_kind") == "human" else "legacy_unknown"


def _has_review(trial):
    return _review_status(trial) != "unresolved"


def _review_integrity(report):
    """Check saved score projections against the immutable submitted review batch."""
    from . import protocol
    from .task_eval import REFUSAL_TYPE
    try:
        history = report["review_history"][-1]
        packet = report["blind_review"]
        if (packet["packet_hash"] != report["review_packet_hash"]
                or digest({k: v for k, v in packet.items() if k != "packet_hash"}) != packet["packet_hash"]):
            return False
        reviews = history["reviews"]
        review_hash = protocol.review_protocol_hash(reviews)
        if (digest(reviews) != history["submitted_review_hash"]
                or review_hash != report["review_protocol_hash"]
                or history["run_hash"] != report["run_hash"]
                or report["review_version_hash"] != digest([
                    report["run_hash"], report["review_packet_hash"], review_hash, reviews])):
            return False
        expected = {key: value for key, value in history.get("previous_reviews", {}).items()
                    if value.get("review_protocol_hash") == review_hash}
        expected.update({item["trial_id"]: item for item in reviews["items"]})
        derived = {"review_status", "selected_score", "review_protocol_hash", "packet_input_hash"}
        for rows in report["raw_trials"].values():
            for row in rows:
                for trial in row["repetitions"]:
                    stored = trial.get("answer_review")
                    submitted = expected.get(trial["trial_id"])
                    if not stored or not submitted:
                        return False
                    if {k: v for k, v in stored.items() if k not in derived} != {
                            k: v for k, v in submitted.items() if k not in derived}:
                        return False
                    origin = protocol.review_origin(stored, stored["packet_input_hash"])
                    selected = origin["selected"]
                    verdict = selected.get("verdict") if selected else "unresolved"
                    correct = (verdict in {"correct", "reasonable_refusal"} and trial["citation_valid"]
                               and (not trial.get("refused") or row["type"] == REFUSAL_TYPE))
                    complete = selected.get("completeness", 0) if selected and verdict != "unresolved" else 0
                    if (trial.get("correct") != correct or trial.get("complete") != complete
                            or trial.get("answer_verdict") != verdict
                            or trial.get("review_status") != origin["review_status"]):
                        return False
        return True
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def _packet_complete(report):
    items = (report.get("blind_review") or {}).get("items", [])
    return bool(items) and all(item.get("question")
        and (item.get("answer_key") or {}).get("expected_points")
        and (item.get("refused") or any(
            any(evidence.get(key) for key in ("text", "content", "source", "code", "stdout", "snippet"))
            for evidence in item.get("evidence_payload", []))) for item in items)


def finalize(report):
    import statistics
    from . import protocol
    from .task_eval import EXPERIENCE_TYPE, REFUSAL_TYPE
    raw = report["raw_trials"]
    def trials(variant, experience=None):
        return [t for row in raw[variant] if experience is None or (row["type"] == EXPERIENCE_TYPE) == experience
                for t in row["repetitions"]]
    def rate(rows, field):
        return 100 * sum(bool(t.get(field)) for t in rows) / max(1, len(rows))
    all_trials = [t for v in raw for t in trials(v)]
    safety = bool(all_trials) and all(t.get("final_safe", t.get("citation_valid"))
                                      and t.get("tool_calls", 99) <= 6 for t in all_trials)
    reviews_complete = bool(all_trials) and all(_has_review(t) for t in all_trials)
    statuses = {_review_status(t) for t in all_trials}
    provenance_ok = bool(all_trials) and statuses <= {"human_reviewed", "ai_reviewed"}
    review_status = ("unresolved" if not reviews_complete else
                     "legacy_unknown" if "legacy_unknown" in statuses else
                     "single_agent_reviewed" if "single_agent_reviewed" in statuses else
                     "ai_reviewed" if "ai_reviewed" in statuses else "human_reviewed")
    native_rates = {v: rate([t for row in raw[v] if row["type"] != REFUSAL_TYPE
                            for t in row["repetitions"]], "native_planning") for v in raw}
    # One aggregation contract for JSON, Markdown and gates; safety includes every raw trial.
    for variant, rows in raw.items():
        target = report["variants"].setdefault(variant, {})
        all_rows = trials(variant)
        metric = target.setdefault("metrics", {})
        reviewed = bool(all_rows) and all(_has_review(t) for t in all_rows)
        available_tokens = [t["tokens"] for t in all_rows if t.get("tokens") is not None]
        latencies = [t.get("latency_ms", 0) for t in all_rows]
        metric.update(accuracy=rate(all_rows, "correct") if reviewed else None,
                      completeness=100 * sum(t.get("complete", 0) for t in all_rows) / len(all_rows) if reviewed else None,
                      effective_citation_rate=rate(all_rows, "citation_valid"),
                      citation_reference_integrity_rate=rate(all_rows, "citation_valid"),
                      native_planning_success_rate=native_rates[variant],
                      fallback_rate=100 * sum(bool(t.get("fallback_reason")) for t in all_rows) / max(1, len(all_rows)),
                      median_tool_calls=statistics.median(t["tool_calls"] for t in all_rows),
                      p50_ms=statistics.median(latencies),
                      p95_ms=sorted(latencies)[max(0, math.ceil(len(latencies) * .95) - 1)],
                      median_tokens=statistics.median(available_tokens) if len(available_tokens) == len(all_rows) else None,
                      usage_missing_trials=sum(t.get("input_tokens") is None for t in all_rows),
                      pending=sum(not _has_review(t) for t in all_rows), trial_count=len(all_rows))
        condition_total = condition_missing = 0
        false_claim_trials = 0
        citation_support_states = []
        inherited_hypotheses = []
        executable_verifications = []
        reviewed_trial_count = 0
        for row in rows:
            points = (report.get("answer_key", {}).get("tasks", {}).get(row["id"], {})
                      .get("expected_points", []))
            condition_ids = {point["id"] for point in points
                             if point.get("kind") == "condition" or point.get("category") == "condition"}
            for trial in row["repetitions"]:
                review = trial.get("answer_review") or trial.get("human_review") or {}
                selected = review.get("selected_score") or review
                if not _has_review(trial):
                    continue
                reviewed_trial_count += 1
                false_claim_trials += bool(selected.get("false_claim_present"))
                if selected.get("citation_relation"):
                    citation_support_states.append(selected["citation_relation"])
                if isinstance(selected.get("error_hypothesis_inherited"), bool):
                    inherited_hypotheses.append(selected["error_hypothesis_inherited"])
                if isinstance(selected.get("verification_executable"), bool):
                    executable_verifications.append(selected["verification_executable"])
                checks = selected.get("point_reviews") or {}
                condition_total += len(condition_ids)
                condition_missing += sum(checks.get(point) != "met" for point in condition_ids)
        metric["condition_omission_rate"] = (
            100 * condition_missing / condition_total if condition_total else None)
        metric["false_assertion_rate"] = (
            100 * false_claim_trials / reviewed_trial_count if reviewed_trial_count else None)
        metric["citation_support_safe_rate"] = (
            100 * sum(state in {"supported", "not_applicable"}
                      for state in citation_support_states) / len(citation_support_states)
            if citation_support_states else None)
        metric["error_hypothesis_inheritance_rate"] = (
            100 * sum(inherited_hypotheses) / len(inherited_hypotheses)
            if inherited_hypotheses else None)
        metric["verification_executable_rate"] = (
            100 * sum(executable_verifications) / len(executable_verifications)
            if executable_verifications else None)
        target.pop("reviewed_metrics", None)
        target["by_type"] = {kind: {
            "accuracy": (rate([t for row in rows if row["type"] == kind for t in row["repetitions"]], "correct")
                         if all(_has_review(t) for row in rows if row["type"] == kind for t in row["repetitions"]) else None),
            "count": sum(row["type"] == kind for row in rows)} for kind in {row["type"] for row in rows}}
        for task in target.get("tasks", []):
            task_trials = next(row["repetitions"] for row in rows if row["id"] == task["id"])
            reviewed_task = all(_has_review(t) for t in task_trials)
            task.update(correct=all(t.get("correct") for t in task_trials) if reviewed_task else None,
                        accuracy=rate(task_trials, "correct") if reviewed_task else None,
                        complete=statistics.mean(t.get("complete", 0) for t in task_trials) if reviewed_task else None,
                        citation_valid=all(t.get("citation_valid") for t in task_trials),
                        native_planning=all(t.get("native_planning") for t in task_trials))
    report["answer_review_status"] = "completed" if reviews_complete else "unresolved"
    report["review_status"] = review_status
    report["review_boundary"] = ("AI review is not human blind review; shared-model agents may share errors. "
                                  "Reference integrity is deterministic, while claim support is a separate semantic judgment."
                                  if review_status in {"ai_reviewed", "single_agent_reviewed"}
                                  else "Legacy reviewer names do not establish human review provenance.")
    non_exp = {v: rate(trials(v, False), "correct") if reviews_complete else None for v in raw}
    native = min(native_rates.values(), default=0)
    code_trials = {v: [t for row in raw[v] if row["type"] not in (EXPERIENCE_TYPE, REFUSAL_TYPE)
                      for t in row["repetitions"]] for v in raw}
    code_completeness = {v: 100 * sum(t.get("anchor_recall", 0) for t in rows) / max(1, len(rows)) for v, rows in code_trials.items()}
    reading = report.get("report_kind") == "reading"
    reuse = report.get("report_kind") == "knowledge_reuse"
    # Wiki availability and enforced reading order are separate hypotheses.
    treatment = "wiki_flat" if reading else "structured_card" if reuse else "C"
    baselines = ["source_only"] if reading else ["raw_session", "generic_summary"] if reuse else ["A", "B"]
    input_tokens = {v: (sum(t["input_tokens"] for t in rows)
                       if rows and all(isinstance(t.get("input_tokens"), (int, float)) for t in rows) else None)
                    for v, rows in code_trials.items()}
    paired = protocol.validate_pairs(report)
    comparisons = {}
    for baseline in baselines:
        delta = None
        denominator = input_tokens[baseline]
        if denominator and input_tokens[treatment] is not None:
            delta = 100 * (1 - input_tokens[treatment] / denominator)
        # Wiki questions are independent by mechanism; the short experience
        # pilot is independent by case (three follow-ups do not create cases).
        effect_fn = protocol.grouped_paired_effect if reading or reuse else protocol.paired_effect
        effect_kinds = None if reuse else {"定位", "调用链", "语义检索", "影响分析"}
        accuracy = (effect_fn(raw, treatment, baseline, "correct",
                    kinds=effect_kinds)
                    if reviews_complete and paired else {"delta_pt": None, "ci95_pt": None})
        quality = (effect_fn(raw, treatment, baseline, "complete",
                   kinds=effect_kinds)
                   if reviews_complete and paired else {"delta_pt": None, "ci95_pt": None})
        acc_gain, quality_gain = accuracy["delta_pt"], quality["delta_pt"]
        comparisons[baseline] = {"accuracy": accuracy, "answer_completeness": quality,
            "code_input_token_reduction_pct": delta,
            "gain_observed": acc_gain is not None and (acc_gain >= 10 or
                (acc_gain >= 0 and quality_gain >= 10) or
                (acc_gain >= 0 and quality_gain >= 0 and delta is not None and delta >= 20))}
    # ABC must beat both baselines. Reading's primary contrast changes only Wiki
    # availability; order is a secondary contrast, never a prerequisite for Wiki value.
    # This is a predeclared engineering threshold, not proof of statistical significance.
    gain = all(row["gain_observed"] for row in comparisons.values())
    regression_ok = reviews_complete and all(non_exp[treatment] >= non_exp[b] - 2.5 for b in baselines)
    key = report.get("answer_key", {})
    gold_eligible = key.get("effect_review_eligible") is True
    minimum_holdout = 6 if reuse else 20
    holdout = gold_eligible and len(code_trials[treatment]) >= minimum_holdout * report.get("runs_per_task", 1)
    report["effect_review_eligible"] = provenance_ok and gold_eligible
    report["human_release_eligible"] = (provenance_ok and statuses == {"human_reviewed"}
                                         and key.get("human_release_eligible") is True)
    gates = {"answer_review_complete": reviews_complete,
        "citation_safety": safety, "native_planning": True if reuse else native >= 90,
        "paired_trials_complete": paired, "non_experience_regression": regression_ok,
        "codeatlas_gain": gain, "frozen_semantic_holdout": holdout,
        "review_provenance": provenance_ok,
        "review_packet_complete": _packet_complete(report),
        "judge_calibration": (statuses == {"human_reviewed"}
                              or calibration_attestation(
                                  report.get("judge_calibration"), _answer_judges(report),
                                  model_scoped=bool(
                                      (report.get("review_history") or [{}])[-1]
                                      .get("reviews", {}).get("review_protocol", {})
                                      .get("qualified_by_batch_id")),
                                  qualification_hash=(
                                      (report.get("review_history") or [{}])[-1]
                                      .get("reviews", {}).get("review_protocol", {})
                                      .get("qualification_attestation_hash")))),
        "full_three_repeat_run": (report.get("runs_per_task") == 3 and
                                   (report.get("task_scope") == "full" or
                                    reuse and report.get("task_scope") == "short_synthetic_pilot"))}
    gates["protocol_identity_bound"] = (bool(report.get("evaluation_hash"))
                                        and report["evaluation_hash"] == protocol.evaluation_hash(report))
    gates["raw_trials_bound"] = bool(report.get("run_hash")) and report["run_hash"] == protocol.run_hash(report)
    gates["review_integrity_bound"] = _review_integrity(report)
    gates["snapshot_pinned"] = (all(report.get("identity", {}).get(k)
                                    for k in ("materials_hash", "manifest_hash", "source_digests")) if reuse else
                                all(report.get("identity", {}).get(k)
                                    for k in ("knowledge_set_id", "source_content_hash", "build_config_hash", "revision")))
    if treatment == "C":
        gates["experience_reuse"] = reviews_complete and rate(trials("C", True), "correct") >= 90
    report["acceptance"]["gates"] = gates
    anchor_gain = code_completeness[treatment] - max(code_completeness[b] for b in baselines)
    report["acceptance"].update(citation_safety_rate=rate(all_trials, "citation_valid"),
                               native_planning_rate=native, native_planning_by_variant=native_rates,
                               source_anchor_gain_pt=anchor_gain, source_anchor_completeness=code_completeness,
                               c_experience_accuracy=(rate(trials("C", True), "correct") if treatment == "C" and reviews_complete else None),
                               non_experience_accuracy=non_exp,
                               paired_code_comparisons=comparisons, code_input_tokens=input_tokens,
                               context_token_reduction_pct=min((c["code_input_token_reduction_pct"] for c in comparisons.values()), default=None)
                               if all(c["code_input_token_reduction_pct"] is not None for c in comparisons.values()) else None,
                               inference_boundary="Paired task bootstrap is descriptive; 3 repeats are not 3 independent tasks.")
    report["acceptance"]["primary_contrast"] = {"treatment": treatment, "baselines": baselines}
    primary = comparisons.get(baselines[0], {}) if baselines else {}
    primary_accuracy = (primary.get("accuracy") or {}).get("delta_pt")
    primary_quality = (primary.get("answer_completeness") or {}).get("delta_pt")
    primary_tokens = primary.get("code_input_token_reduction_pct")
    # A descriptive gain is not an observable, publishable effect unless the
    # complete experiment contract is satisfied.  In particular, never let a
    # promising delta bypass an incomplete three-repeat run, a failed judge
    # calibration, an unbound raw trial/review packet, or an unpinned snapshot.
    qualification_gates_passed = all(gates.values())
    report["acceptance"]["effect_observed"] = bool(
        qualification_gates_passed and (
            (primary_accuracy is not None and primary_accuracy >= 10)
            or (primary_accuracy is not None and primary_accuracy >= 0
                and primary_quality is not None and primary_quality >= 0
                and primary_tokens is not None and primary_tokens >= 20)
        )
    )
    report["acceptance"]["human_release_eligible"] = report["human_release_eligible"]
    if reading:
        secondary = {}
        for baseline in ("wiki_flat", "source_only"):
            secondary[baseline] = {
                "treatment": "wiki_progressive",
                "accuracy": protocol.grouped_paired_effect(raw, "wiki_progressive", baseline, "correct",
                    kinds={"定位", "调用链", "语义检索", "影响分析"}) if reviews_complete and paired else None,
                "answer_completeness": protocol.grouped_paired_effect(raw, "wiki_progressive", baseline, "complete",
                    kinds={"定位", "调用链", "语义检索", "影响分析"}) if reviews_complete and paired else None,
                "code_input_token_reduction_pct": (100 * (1 - input_tokens["wiki_progressive"] / input_tokens[baseline])
                    if input_tokens[baseline] and input_tokens["wiki_progressive"] is not None else None),
            }
        report["acceptance"]["secondary_reading_contrasts"] = secondary
    report["acceptance"]["passed"] = qualification_gates_passed
