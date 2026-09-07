"""Sealed judge qualification and one-way primary/fallback orchestration."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from ..contracts import digest
from ..publication import atomic_text
from . import campaign_review, proof_campaign, rubric


class QualificationError(ValueError):
    pass


def _qualification_metrics(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    attestation = value.get("judge_calibration") or {}
    runs = attestation.get("review_runs") or []
    return {
        "status": value.get("status"), "reason": value.get("reason"),
        "batch_id": value.get("batch_id"),
        "judge_request_count": value.get("judge_request_count"),
        "passed": attestation.get("passed"),
        "reviewer_count": attestation.get("reviewer_count"),
        "reviewers": [{
            "reviewer": (row.get("reviewer") or {}).get("agent"),
            "model": (row.get("reviewer") or {}).get("model"),
            "metrics": row.get("metrics"), "gates": row.get("gates"),
        } for row in runs],
    }


def public_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Return the trackable qualification outcome without prompts/responses."""
    review = result.get("review") or {}
    value = {
        "schema_version": 1, "status": result.get("status"),
        "identity": result.get("identity"), "selected": result.get("selected"),
        "primary": _qualification_metrics(result.get("primary")),
        "fallback": _qualification_metrics(result.get("fallback")),
        "review": ({
            "status": review.get("status"), "batch_id": review.get("batch_id"),
            "attempt_id": (review.get("binding") or {}).get("attempt_id"),
            "base_attempt_id": (review.get("binding") or {}).get("base_attempt_id"),
            "projection_version": (review.get("binding") or {}).get("projection_version"),
            "unresolved_trial_count": review.get("unresolved_trial_count"),
            "disagreement_count": review.get("disagreement_count"),
            "replacement_count": review.get("replacement_count"),
            "recovery_arbitration_count": review.get("recovery_arbitration_count"),
            "judge_request_count": review.get("judge_request_count"),
        } if review else None),
        "failed_review_attempts": result.get("failed_review_attempts") or [],
        "request_budget": result.get("request_budget"),
        "boundary": result.get("boundary"),
    }
    value["report_hash"] = digest(value)
    return value


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _inside(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if root not in path.parents or not path.is_file():
        raise QualificationError(f"qualification path escaped or is missing: {value}")
    return path


def _raw_samples(path: Path) -> list[dict[str, Any]]:
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    samples = value.get("samples")
    if not isinstance(samples, list):
        raise QualificationError(f"qualification has no samples: {path}")
    return samples


def _sample_sets(samples: list[dict[str, Any]]) -> dict[str, set[str]]:
    fields = ("id", "mechanism", "question", "candidate_response")
    result = {field: set() for field in fields}
    for sample in samples:
        for field in fields:
            value = sample.get(field)
            if isinstance(value, str) and value.strip():
                result[field].add(digest(value.strip()))
    result["input"] = {digest({
        "question": sample.get("question"),
        "candidate_response": sample.get("candidate_response"),
        "basis": sample.get("basis"),
        "expected_points": sample.get("expected_points"),
    }) for sample in samples}
    return result


def validate_policy(policy_path: str | Path, *, campaign_dir: str | Path,
                    project_root: str | Path | None = None) -> dict[str, Any]:
    root = Path(project_root or Path(__file__).resolve().parents[3]).resolve()
    path = Path(policy_path)
    if not path.is_absolute():
        path = root / path
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if raw.get("schema_version") != 1 or raw.get("sealed") is not True:
        raise QualificationError("judge qualification policy must be sealed schema_version 1")
    if raw.get("workers") != 2 or raw.get("transport_retries") != 0:
        raise QualificationError("qualification policy must freeze workers=2 and transport_retries=0")
    if raw.get("max_new_requests") != 1522:
        raise QualificationError("qualification policy must freeze the 1,522 request ceiling")
    primary = raw.get("primary") or {}
    fallback = raw.get("fallback") or {}
    if (primary.get("model") != "gpt-5.6-terra"
            or fallback.get("model") != "gpt-5.6-sol"
            or fallback.get("activate_only_when") != "judge_calibration_failed"):
        raise QualificationError("primary/fallback judge order changed")
    rows = []
    loaded = {}
    sample_sets = {}
    for role, spec in (("primary", primary), ("fallback", fallback)):
        calibration_path = _inside(root, str(spec.get("calibration", "")))
        if _sha256(calibration_path) != spec.get("sha256"):
            raise QualificationError(f"{role} calibration file hash changed")
        calibration = rubric.load_calibration(calibration_path)
        if (calibration.get("schema_version") != 4
                or calibration.get("sealed") is not True
                or calibration.get("eligible") is not True):
            raise QualificationError(f"{role} V4 calibration is not independently approved")
        point_count = sum(len(sample["expected_rubric"]["expected_point_results"])
                          for sample in calibration["samples"])
        mechanisms = [sample.get("mechanism") for sample in calibration["samples"]]
        if point_count != 66 or len(set(mechanisms)) != 12 or not all(mechanisms):
            raise QualificationError(f"{role} qualification must contain 12 mechanisms and 66 points")
        loaded[role] = calibration
        sample_sets[role] = _sample_sets(_raw_samples(calibration_path))
        rows.append({"role": role, "model": spec["model"], "attempt": spec["attempt"],
                     "calibration": spec["calibration"],
                     "calibration_hash": calibration["content_hash"],
                     "calibration_input_hash": calibration["calibration_input_hash"],
                     "points": point_count})
    for field in sample_sets["primary"]:
        if sample_sets["primary"][field] & sample_sets["fallback"][field]:
            raise QualificationError(f"V4A/V4B overlap in {field}")
    v3_path = _inside(root, str(raw.get("excluded_v3", "eval/review_calibration_v3.yaml")))
    v3_sets = _sample_sets(_raw_samples(v3_path))
    for role in ("primary", "fallback"):
        for field in ("id", "mechanism", "question", "candidate_response", "input"):
            if sample_sets[role][field] & v3_sets[field]:
                raise QualificationError(f"{role} overlaps V3 in {field}")
    _target, _plan, _ledger = proof_campaign._load(campaign_dir)
    bundle = proof_campaign.report(campaign_dir)
    if (bundle.get("status") != "completed" or bundle.get("planned_trial_count") != 510
            or bundle.get("trial_states") != {"completed": 510}
            or bundle.get("uncertain_trial_ids")):
        raise QualificationError("the frozen 510-answer collection is incomplete or changed")
    formal_hashes = set()
    formal_mechanisms = set()
    for report in bundle["reports"].values():
        if report.get("proof_scope") == "development_smoke":
            continue
        for task in report.get("task_manifest") or []:
            mechanism = task.get("mechanism_id") or task.get("case_id")
            if isinstance(mechanism, str) and mechanism.strip():
                formal_mechanisms.add(digest(mechanism.strip()))
        packet = rubric.blind_packet(report)
        formal_hashes.update(item["input_hash"] for item in packet["items"])
        formal_hashes.update(digest(item.get("question")) for item in packet["items"])
        formal_hashes.update(digest(item.get("answer")) for item in packet["items"])
    for role in ("primary", "fallback"):
        if sample_sets[role]["input"] & formal_hashes:
            raise QualificationError(f"{role} reuses a frozen formal review input")
        if sample_sets[role]["mechanism"] & formal_mechanisms:
            raise QualificationError(f"{role} reuses a frozen formal task mechanism")
    identity = {
        "policy_sha256": _sha256(path), "policy_hash": digest(raw),
        "campaign_id": bundle["campaign_id"], "answer_trials": 510,
        "formal_answer_trials": 486, "qualifications": rows,
    }
    return {"policy": raw, "identity": identity, "path": str(path), "loaded": loaded}


def _store_run(ledger, run_id: str, *, policy_hash: str, start_requests: int,
               state: str, result: dict[str, Any]) -> None:
    with ledger.connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS campaign_qualification_runs (
              id TEXT PRIMARY KEY, policy_hash TEXT NOT NULL,
              start_requests INTEGER NOT NULL, state TEXT NOT NULL,
              result_json TEXT NOT NULL, result_hash TEXT NOT NULL
            )
        """)
        existing = conn.execute(
            "SELECT policy_hash,start_requests FROM campaign_qualification_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if existing and (existing["policy_hash"] != policy_hash
                         or existing["start_requests"] != start_requests):
            raise QualificationError("qualification run binding changed")
        rendered = json.dumps(result, ensure_ascii=False, sort_keys=True)
        conn.execute(
            "INSERT OR REPLACE INTO campaign_qualification_runs VALUES(?,?,?,?,?,?)",
            (run_id, policy_hash, start_requests, state, rendered, digest(result)),
        )


def run(policy_path: str | Path, *, campaign_dir: str | Path,
        project_root: str | Path | None = None, resume: bool = False,
        calibration_only: bool = False) -> dict[str, Any]:
    """Run Terra first and activate sealed Sol only after semantic calibration failure."""
    root = Path(project_root or Path(__file__).resolve().parents[3]).resolve()
    checked = validate_policy(policy_path, campaign_dir=campaign_dir,
                              project_root=root)
    raw, identity = checked["policy"], checked["identity"]
    _target, _plan, ledger = proof_campaign._load(campaign_dir)
    run_id = "qualification-" + identity["policy_hash"][:20]
    with ledger.connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS campaign_qualification_runs (
              id TEXT PRIMARY KEY, policy_hash TEXT NOT NULL,
              start_requests INTEGER NOT NULL, state TEXT NOT NULL,
              result_json TEXT NOT NULL, result_hash TEXT NOT NULL
            )
        """)
        existing = conn.execute(
            "SELECT * FROM campaign_qualification_runs WHERE id=?", (run_id,)
        ).fetchone()
    stored_result = None
    if existing:
        if existing["policy_hash"] != identity["policy_hash"]:
            raise QualificationError("qualification policy changed")
        stored_result = json.loads(existing["result_json"])
        if digest(stored_result) != existing["result_hash"]:
            raise QualificationError("qualification checkpoint changed")
        if not resume:
            raise QualificationError("qualification run exists; use resume")
        start_requests = existing["start_requests"]
    else:
        start_requests = ledger.summary()["requests"]
    if ledger.summary()["requests"] - start_requests > raw["max_new_requests"]:
        raise QualificationError("qualification request ceiling exceeded")

    def score(role: str, *, only: bool, force_resume: bool = False):
        spec = raw[role]
        return campaign_review.review(
            campaign_dir, resume=resume or existing is not None or force_resume,
            calibration_path=root / spec["calibration"],
            attempt_id=spec["attempt"], workers=raw["workers"],
            judge_model=spec["model"], calibration_only=only,
            transport_retries=raw["transport_retries"],
            global_max_requests=start_requests + raw["max_new_requests"],
        )

    if stored_result and stored_result.get("selected") in {"primary", "fallback"}:
        primary = stored_result.get("primary")
        fallback = stored_result.get("fallback")
        selected_role = stored_result["selected"]
    else:
        primary = score("primary", only=True)
        selected_role = None
        fallback = None
        if primary.get("status") == "calibrated":
            selected_role = "primary"
        elif primary.get("reason") == "judge_calibration_failed":
            fallback = score("fallback", only=True)
            if fallback.get("status") == "calibrated":
                selected_role = "fallback"
    result = {"status": "qualified" if selected_role else "unresolved",
              "identity": identity, "primary": primary, "fallback": fallback,
              "selected": selected_role,
              "boundary": "Sol is activated only after Terra semantic calibration failure."}
    if selected_role and not calibration_only:
        from . import review_recovery
        qualified = primary if selected_role == "primary" else fallback
        if not qualified or qualified.get("status") != "calibrated":
            raise QualificationError("selected judge no longer has a calibrated checkpoint")
        prior_review = (stored_result or {}).get("review") or {}
        prior_binding = prior_review.get("binding") or {}
        continuation_attempt = raw[selected_role]["attempt"] + "." \
            + campaign_review.SEMANTIC_REVIEW_PROJECTION
        failed_attempts = list((stored_result or {}).get("failed_review_attempts") or [])
        if (prior_review and prior_binding.get("projection_version")
                != campaign_review.SEMANTIC_REVIEW_PROJECTION):
            unresolved = [row for row in prior_review.get("raw_calls") or []
                          if row.get("state") in {"unresolved", "uncertain", "running"}]
            errors = {(row.get("result") or {}).get("error_type") for row in unresolved}
            if not unresolved or errors != {"ReviewInputTooLarge"}:
                selected = prior_review
                result["review"] = selected
                result["status"] = selected.get("status", "unresolved")
                used = ledger.summary()["requests"] - start_requests
                result["request_budget"] = {"start": start_requests, "used": used,
                                            "maximum": raw["max_new_requests"]}
                _store_run(ledger, run_id, policy_hash=identity["policy_hash"],
                           start_requests=start_requests, state=result["status"], result=result)
                return result
            failed_attempts.append({
                "attempt_id": prior_binding.get("attempt_id"),
                "batch_id": prior_review.get("batch_id"),
                "status": prior_review.get("status"),
                "reason": "review_input_budget_failed",
                "unresolved_trial_count": prior_review.get("unresolved_trial_count"),
                "judge_request_count": prior_review.get("judge_request_count"),
            })
        gaps = review_recovery.repairable_summary(prior_review) if prior_review else {}
        if (prior_review.get("reason") == "recovery_arbiter_calibration_failed"
                and prior_binding.get("version") == review_recovery.RECOVERY_VERSION
                and prior_binding.get("base_attempt_id")):
            failed_attempts.append({
                "attempt_id": prior_binding.get("attempt_id"),
                "batch_id": prior_review.get("batch_id"),
                "status": prior_review.get("status"),
                "reason": prior_review.get("reason"),
                "judge_request_count": prior_review.get("judge_request_count"),
            })
            selected = review_recovery.recover(
                campaign_dir,
                base_attempt_id=prior_binding["base_attempt_id"],
                attempt_id=continuation_attempt + ".format-gap-recovery-v2",
                calibration_path=root / raw[selected_role]["calibration"],
                judge_model=raw[selected_role]["model"], workers=raw["workers"],
                global_max_requests=start_requests + raw["max_new_requests"],
                qualification_attestation=qualified["judge_calibration"],
                qualification_batch_id=qualified["batch_id"], resume=resume,
                reuse_replacements_from=prior_binding["attempt_id"],
                reuse_base_arbiter=True,
            )
        elif (prior_review.get("status") == "unresolved"
                and prior_binding.get("projection_version")
                == campaign_review.SEMANTIC_REVIEW_PROJECTION
                and gaps.get("repairable_count", 0) > 0
                and not gaps.get("nonrepairable_unresolved")):
            failed_attempts.append({
                "attempt_id": prior_binding.get("attempt_id"),
                "batch_id": prior_review.get("batch_id"),
                "status": prior_review.get("status"),
                "reason": "format_invalid_review_positions",
                "unresolved_trial_count": prior_review.get("unresolved_trial_count"),
                "judge_request_count": prior_review.get("judge_request_count"),
                "repairable_count": gaps["repairable_count"],
            })
            selected = review_recovery.recover(
                campaign_dir,
                base_attempt_id=prior_binding["attempt_id"],
                attempt_id=continuation_attempt + ".format-gap-recovery-v1",
                calibration_path=root / raw[selected_role]["calibration"],
                judge_model=raw[selected_role]["model"], workers=raw["workers"],
                global_max_requests=start_requests + raw["max_new_requests"],
                qualification_attestation=qualified["judge_calibration"],
                qualification_batch_id=qualified["batch_id"], resume=resume,
            )
        elif (prior_binding.get("version") == review_recovery.RECOVERY_VERSION
              and prior_review.get("status") in {"unresolved", "completed"}):
            # A semantically unresolved, already-adjudicated recovery is a
            # terminal observation.  Never fall back to the older batch or
            # create a new judge attempt to search for a preferred outcome.
            selected = prior_review
        else:
            selected = campaign_review.review(
                campaign_dir, resume=bool(prior_binding.get("projection_version")),
                calibration_path=root / raw[selected_role]["calibration"],
                attempt_id=continuation_attempt, workers=raw["workers"],
                judge_model=raw[selected_role]["model"], calibration_only=False,
                transport_retries=raw["transport_retries"],
                global_max_requests=start_requests + raw["max_new_requests"],
                projection_version=campaign_review.SEMANTIC_REVIEW_PROJECTION,
                qualification_attestation=qualified.get("judge_calibration"),
                qualification_batch_id=qualified.get("batch_id"),
            )
        result["failed_review_attempts"] = failed_attempts
        result["review"] = selected
        result["status"] = selected.get("status", "unresolved")
    used = ledger.summary()["requests"] - start_requests
    result["request_budget"] = {"start": start_requests, "used": used,
                                "maximum": raw["max_new_requests"]}
    if used > raw["max_new_requests"]:
        raise QualificationError("qualification request ceiling exceeded")
    _store_run(ledger, run_id, policy_hash=identity["policy_hash"],
               start_requests=start_requests, state=result["status"], result=result)
    report_path = raw.get("report")
    if report_path:
        target = _inside(root, report_path) if (root / report_path).is_file() else (root / report_path).resolve()
        if target != root and root not in target.parents:
            raise QualificationError("qualification report path escaped project root")
        atomic_text(target, json.dumps(public_summary(result), ensure_ascii=False, indent=2) + "\n")
    return result
