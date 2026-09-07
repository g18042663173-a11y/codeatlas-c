"""Auditable recovery for format-invalid answer reviews.

This module never retries a valid score.  It fills only review positions whose
stored model response did not produce a score, using a new qualified session,
and composes a new immutable review batch that references the failed base
batch.  Transport-unknown and genuinely semantic ``unresolved`` decisions are
deliberately ineligible for this path.
"""
from __future__ import annotations

import copy
import fcntl
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..contracts import INPUT_BUDGET, digest, estimated_tokens
from ..llm.client import get_client
from . import campaign_review as review
from . import proof_campaign as campaign
from . import protocol, rubric


RECOVERY_VERSION = "format-gap-recovery-v1"
REPAIRABLE_ERRORS = {"ValueError", "ReviewInputTooLarge"}


def _stored_batch(ledger, attempt_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    with ledger.connect() as conn:
        rows = conn.execute(
            "SELECT id,binding_json,result_json,result_hash FROM campaign_review_batches"
        ).fetchall()
    matches = []
    for row in rows:
        binding = json.loads(row["binding_json"])
        if binding.get("attempt_id") == attempt_id:
            matches.append((row, binding))
    if len(matches) != 1:
        raise campaign.CampaignError("base review attempt must identify exactly one batch")
    row, binding = matches[0]
    if not row["result_json"]:
        raise campaign.CampaignError("base review attempt has no durable result")
    result = json.loads(row["result_json"])
    if digest(result) != row["result_hash"]:
        raise campaign.CampaignError("base review result hash changed")
    return row["id"], binding, result


def _stored_call(ledger, batch_id: str, role: str, call_id: str) -> dict[str, Any]:
    with ledger.connect() as conn:
        row = conn.execute(
            "SELECT state,result_json,result_hash FROM campaign_review_calls "
            "WHERE batch_id=? AND role=? AND call_id=?",
            (batch_id, role, call_id),
        ).fetchone()
    if not row or row["state"] != "completed" or not row["result_json"]:
        raise campaign.CampaignError("prior replacement score is not complete")
    result = json.loads(row["result_json"])
    if digest(result) != row["result_hash"] or result.get("parsed") is None:
        raise campaign.CampaignError("prior replacement score changed or is invalid")
    return result


def _invalid_score(score: Any) -> bool:
    return bool(
        isinstance(score, dict)
        and score.get("model_generated") is False
        and score.get("execution_status") == "unresolved"
        and score.get("error_type") in REPAIRABLE_ERRORS
    )


def repairable_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Classify unresolved positions without exposing prompts or responses."""
    independent, adjudication, nonrepairable = [], [], []
    for job_id, submission in (result.get("reviews") or {}).items():
        for group in submission.get("items") or []:
            bad = []
            for slot, score in enumerate(group.get("independent_reviews") or []):
                if _invalid_score(score):
                    bad.append(slot)
            if bad:
                independent.append({"job_id": job_id, "trial_id": group.get("trial_id"),
                                    "slots": bad})
            score = group.get("adjudication")
            if _invalid_score(score):
                adjudication.append({"job_id": job_id, "trial_id": group.get("trial_id")})
            if bad or _invalid_score(score):
                continue
            scores = group.get("independent_reviews") or []
            unresolved = len(scores) != 2 or not all(
                row.get("model_generated") for row in scores)
            if not unresolved:
                outcomes = [{key: row.get(key) for key in review.OUTCOMES} for row in scores]
                if outcomes[0] == outcomes[1]:
                    unresolved = scores[0].get("verdict") == "unresolved"
                else:
                    unresolved = not (
                        isinstance(score, dict)
                        and score.get("model_generated") is True
                        and score.get("verdict") != "unresolved"
                    )
            if unresolved:
                nonrepairable.append({"job_id": job_id, "trial_id": group.get("trial_id")})
    return {
        "independent_gaps": independent,
        "adjudication_gaps": adjudication,
        "nonrepairable_unresolved": nonrepairable,
        "repairable_count": sum(len(row["slots"]) for row in independent) + len(adjudication),
    }


def _compact_arbiter_view(score: dict[str, Any]) -> dict[str, Any]:
    """Bind the full score by hash while sending only decision fields."""
    return {
        "review_hash": digest(score),
        "point_reviews": copy.deepcopy(score.get("point_reviews")),
        "false_claim_present": score.get("false_claim_present"),
        "citation_relation": score.get("citation_relation"),
        "verdict": score.get("verdict"),
        "completeness": score.get("completeness"),
    }


def _finish(ledger, batch_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return review._finish(ledger, batch_id, result)


def recover(directory: str | Path, *, base_attempt_id: str, attempt_id: str,
            calibration_path: str | Path, judge_model: str,
            qualification_attestation: dict[str, Any], qualification_batch_id: str,
            workers: int = 2, global_max_requests: int | None = None,
            resume: bool = False, reuse_replacements_from: str | None = None,
            reuse_base_arbiter: bool = False) -> dict[str, Any]:
    """Fill only repairable gaps and emit a complete composite review attempt."""
    if workers != 2:
        raise campaign.CampaignError("sealed recovery requires workers=2")
    if not attempt_id or attempt_id == base_attempt_id:
        raise campaign.CampaignError("recovery requires a new stable attempt id")
    target, plan, ledger = campaign._load(directory)
    if global_max_requests is not None:
        frozen = ledger.settings.get("max_requests")
        ledger.settings["max_requests"] = min(
            global_max_requests, frozen if frozen is not None else global_max_requests)
    calibration_path = Path(calibration_path).resolve()

    with (target / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise campaign.CampaignError("campaign collection/review is already running") from exc

        source_bundle = campaign.report(directory)
        if (source_bundle.get("status") != "completed"
                or source_bundle.get("trial_states") != {
                    "completed": source_bundle.get("planned_trial_count")
                } or source_bundle.get("uncertain_trial_ids")):
            raise campaign.CampaignError("recovery requires the complete immutable answer collection")
        packets = {
            key: rubric.blind_packet(value)
            for key, value in source_bundle["reports"].items()
            if value.get("executed_trial_count")
            and value.get("proof_scope") != "development_smoke"
        }
        base_batch_id, base_binding, base = _stored_batch(ledger, base_attempt_id)
        gaps = repairable_summary(base)
        if gaps["nonrepairable_unresolved"]:
            raise campaign.CampaignError("base review has semantic or unknown unresolved items")
        if gaps["repairable_count"] == 0:
            raise campaign.CampaignError("base review has no format-invalid positions to recover")
        if base_binding.get("judge_model") != judge_model:
            raise campaign.CampaignError("recovery judge model differs from the base attempt")
        if base_binding.get("projection_version") != review.SEMANTIC_REVIEW_PROJECTION:
            raise campaign.CampaignError("recovery requires the bounded semantic projection")
        if (base_binding.get("qualified_by_batch_id") != qualification_batch_id
                or digest(qualification_attestation)
                != base_binding.get("qualification_attestation_hash")):
            raise campaign.CampaignError("recovery qualification differs from the base attempt")

        prior_replacement = None
        if reuse_replacements_from:
            prior_batch_id, prior_binding, prior_result = _stored_batch(
                ledger, reuse_replacements_from)
            if (prior_binding.get("base_batch_id") != base_batch_id
                    or prior_binding.get("judge_model") != judge_model
                    or prior_result.get("reason") != "recovery_arbiter_calibration_failed"
                    or (prior_result.get("replacement_calibration") or {}).get("passed") is not True):
                raise campaign.CampaignError("prior replacement attempt is not reusable")
            prior_replacement = (prior_batch_id, prior_binding, prior_result)

        base_arbiter_run = None
        if reuse_base_arbiter:
            expected_agent = (base_binding.get("agents") or {}).get("arbiter")
            matches = [row for row in (base.get("calibration_runs") or [])
                       if (row.get("reviewer") or {}).get("agent") == expected_agent]
            if len(matches) != 1 or not matches[0].get("complete"):
                raise campaign.CampaignError("base attempt has no reusable qualified arbiter")
            checked_arbiter = rubric.score_calibration(
                calibration_path, {"reviews": matches}, minimum_reviews=1)
            if checked_arbiter.get("passed") is not True:
                raise campaign.CampaignError("base arbiter no longer passes the frozen qualification")
            base_arbiter_run = (matches[0], checked_arbiter)

        binding_core = {
            "version": RECOVERY_VERSION,
            "attempt_id": attempt_id,
            "base_attempt_id": base_attempt_id,
            "base_batch_id": base_batch_id,
            "base_result_hash": base.get("result_hash"),
            "campaign_plan_hash": source_bundle["plan_hash"],
            "run_hashes": {key: packet["run_hash"] for key, packet in packets.items()},
            "packet_hashes": {key: packet["packet_hash"] for key, packet in packets.items()},
            "judge_model": judge_model,
            "provider": base_binding["provider"],
            "prompt_hash": digest(review.SYSTEM),
            "schema_hash": digest([review.ANSWER_SCHEMA, review.CALIBRATION_SCHEMA]),
            "judge_guidance_hash": digest(review.JUDGE_GUIDANCE),
            "projection_version": review.SEMANTIC_REVIEW_PROJECTION,
            "qualified_by_batch_id": qualification_batch_id,
            "qualification_attestation_hash": digest(qualification_attestation),
            "repairable_summary": gaps,
            "review_workers": workers,
            "review_transport_retries": 0,
            "recovery_runtime_hash": digest(Path(__file__).read_bytes()),
            "base_review_runtime_hash": base_binding.get("review_runtime_hash"),
            "reuse_replacements_from": reuse_replacements_from,
            "reuse_base_arbiter": reuse_base_arbiter,
        }
        batch_id = digest(binding_core)
        roles = ["replacement", "recovery-arbiter"]
        agents = {role: f"{plan['campaign_id']}/review/{batch_id[:16]}/{role}"
                  for role in roles}
        sessions = {role: f"{plan['campaign_id']}-review-{batch_id[:16]}-{role}"
                    for role in roles}
        if prior_replacement:
            agents["replacement"] = prior_replacement[1]["agents"]["replacement"]
            sessions["replacement"] = prior_replacement[1]["sessions"]["replacement"]
        if base_arbiter_run:
            agents["recovery-arbiter"] = base_binding["agents"]["arbiter"]
            sessions["recovery-arbiter"] = base_binding["sessions"]["arbiter"]
        binding = {
            **binding_core,
            "agents": agents,
            "sessions": sessions,
        }
        with ledger.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS campaign_review_batches (
                    id TEXT PRIMARY KEY, binding_json TEXT NOT NULL,
                    result_json TEXT, result_hash TEXT);
                CREATE TABLE IF NOT EXISTS campaign_review_calls (
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, role TEXT NOT NULL,
                    call_id TEXT NOT NULL, request_hash TEXT NOT NULL, state TEXT NOT NULL,
                    started_at REAL NOT NULL, result_json TEXT, result_hash TEXT);
            """)
            existing = conn.execute(
                "SELECT * FROM campaign_review_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if existing and not resume:
                raise campaign.CampaignError("recovery attempt exists; use --resume")
            if existing and json.loads(existing["binding_json"]) != binding:
                raise campaign.CampaignError("recovery binding changed")
            conn.execute(
                "INSERT OR IGNORE INTO campaign_review_batches(id,binding_json) VALUES(?,?)",
                (batch_id, json.dumps(binding, sort_keys=True)),
            )
            conn.execute(
                "UPDATE campaign_review_calls SET state='uncertain' "
                "WHERE batch_id=? AND state='running'", (batch_id,),
            )

        def build_client(role: str):
            obj = get_client(
                ledger=ledger, session_id=binding["sessions"][role],
                base_url=plan["base_url"], model=judge_model,
                thinking_mode=plan.get("thinking_mode", "disabled"),
                transport_retries=0,
            )
            if obj is None:
                return None
            if obj.model != judge_model or obj.base_url != plan["base_url"]:
                raise campaign.CampaignError("recovery client differs from frozen endpoint/model")
            obj.max_requests = obj.max_cost_usd = None
            obj.input_usd_per_million = plan["budget"]["input_usd_per_million"]
            obj.output_usd_per_million = plan["budget"]["output_usd_per_million"]
            return obj

        def client_for_call(role: str):
            return build_client(role)

        if not prior_replacement and client_for_call("replacement") is None:
            return _finish(ledger, batch_id, {
                "status": "not_run", "reason": "model_not_configured", "binding": binding})

        calibration = rubric.load_calibration(calibration_path)
        if prior_replacement:
            replacement_calibration = prior_replacement[2]["recovery_calibrations"][0]
            replacement_attestation = prior_replacement[2]["replacement_calibration"]
        else:
            replacement_calibration = review._calibrate(
                ledger, batch_id, binding, "replacement", client_for_call,
                calibration, workers=workers)
            if not replacement_calibration["complete"]:
                return _finish(ledger, batch_id, {
                    "status": "unresolved", "reason": "replacement_calibration_response_unresolved",
                    "binding": binding, "recovery_calibrations": [replacement_calibration]})
            replacement_attestation = rubric.score_calibration(
                calibration_path, {"reviews": [replacement_calibration]}, minimum_reviews=1)
            if not replacement_attestation["passed"]:
                return _finish(ledger, batch_id, {
                    "status": "unresolved", "reason": "replacement_calibration_failed",
                    "binding": binding, "recovery_calibrations": [replacement_calibration],
                    "replacement_calibration": replacement_attestation})

        items_by_job = {
            job_id: {item["trial_id"]: item for item in packet["items"]}
            for job_id, packet in packets.items()
        }
        groups = {
            job_id: copy.deepcopy(submission["items"])
            for job_id, submission in (base.get("reviews") or {}).items()
        }
        jobs_by_trial = {
            group["trial_id"]: (job_id, group)
            for job_id, rows in groups.items() for group in rows
        }

        replacements = []
        for row in gaps["independent_gaps"]:
            job_id, group = jobs_by_trial[row["trial_id"]]
            item = items_by_job[job_id][row["trial_id"]]
            for slot in row["slots"]:
                replacements.append((job_id, item, group, slot))

        def replace(entry):
            _job_id, item, _group, slot = entry
            call = review._call(
                ledger, batch_id, "replacement", f"replacement:{item['trial_id']}:{slot}",
                client_for_call("replacement"),
                {"item": review._judge_item(item, review.SEMANTIC_REVIEW_PROJECTION),
                 "rubric": {**rubric.RUBRIC, "judge_guidance": review.JUDGE_GUIDANCE,
                            "response_schema": review.ANSWER_SCHEMA}},
                lambda answer: review._validate(answer, item),
            )
            return entry, review._score_record(binding, "replacement", item["input_hash"], call)

        if prior_replacement:
            previous_batch_id, previous_binding, _previous_result = prior_replacement
            replacement_rows = []
            for entry in replacements:
                _job_id, item, _group, slot = entry
                call = _stored_call(
                    ledger, previous_batch_id, "replacement",
                    f"replacement:{item['trial_id']}:{slot}")
                replacement_rows.append((
                    entry,
                    review._score_record(
                        previous_binding, "replacement", item["input_hash"], call),
                ))
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                replacement_rows = list(pool.map(replace, replacements))
        for (_job_id, _item, group, slot), score in replacement_rows:
            group["independent_reviews"][slot] = score

        arbitration = []
        for job_id, rows in groups.items():
            for group in rows:
                scores = group.get("independent_reviews") or []
                if len(scores) != 2 or not all(row.get("model_generated") for row in scores):
                    continue
                outcomes = [{key: score.get(key) for key in review.OUTCOMES} for score in scores]
                if outcomes[0] == outcomes[1]:
                    group.pop("adjudication", None)
                    continue
                current = group.get("adjudication")
                if current and current.get("model_generated"):
                    continue
                arbitration.append((job_id, items_by_job[job_id][group["trial_id"]], group))

        recovery_calibrations = [replacement_calibration]
        arbiter_attestation = None
        if arbitration:
            if base_arbiter_run:
                arbiter_calibration, arbiter_attestation = base_arbiter_run
            else:
                arbiter_calibration = review._calibrate(
                    ledger, batch_id, binding, "recovery-arbiter", client_for_call,
                    calibration, workers=workers)
            recovery_calibrations.append(arbiter_calibration)
            if not base_arbiter_run and not arbiter_calibration["complete"]:
                return _finish(ledger, batch_id, {
                    "status": "unresolved", "reason": "recovery_arbiter_calibration_response_unresolved",
                    "binding": binding, "recovery_calibrations": recovery_calibrations})
            if not base_arbiter_run:
                arbiter_attestation = rubric.score_calibration(
                    calibration_path, {"reviews": [arbiter_calibration]}, minimum_reviews=1)
            if not arbiter_attestation["passed"]:
                return _finish(ledger, batch_id, {
                    "status": "unresolved", "reason": "recovery_arbiter_calibration_failed",
                    "binding": binding, "recovery_calibrations": recovery_calibrations,
                    "replacement_calibration": replacement_attestation,
                    "arbiter_calibration": arbiter_attestation})

            def arbitrate(entry):
                _job_id, item, group = entry
                original = group["independent_reviews"]
                arbiter_hash = digest({
                    "packet_input_hash": item["input_hash"],
                    "independent_reviews": original,
                })
                payload = {
                    "item": review._judge_item(item, review.SEMANTIC_REVIEW_PROJECTION),
                    "rubric": {**rubric.RUBRIC, "judge_guidance": review.JUDGE_GUIDANCE,
                               "response_schema": review.ANSWER_SCHEMA},
                    "independent_reviews": [_compact_arbiter_view(row) for row in original],
                }
                user = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                if estimated_tokens(review.SYSTEM + user) + 32 > INPUT_BUDGET:
                    raise campaign.CampaignError("recovery arbitration exceeds the 8k input budget")
                call = review._call(
                    ledger, batch_id, "recovery-arbiter",
                    f"arbitration:{item['trial_id']}", client_for_call("recovery-arbiter"),
                    payload, lambda answer: review._validate(answer, item),
                )
                score = review._score_record(
                    binding, "recovery-arbiter", arbiter_hash, call)
                score.update(
                    peer_reviews_visible=True, independent=False,
                    input_hash_scope="packet_input_hash_and_original_reviews",
                    resolves_review_hashes=[digest(row) for row in original],
                )
                return group, score

            with ThreadPoolExecutor(max_workers=workers) as pool:
                adjudications = list(pool.map(arbitrate, arbitration))
            for group, score in adjudications:
                group["adjudication"] = score

        submissions, reviewed, unresolved = {}, {}, 0
        for job_id, packet in packets.items():
            submission = {
                key: packet[key]
                for key in ("identity", "evaluation_hash", "run_hash", "packet_hash")
            }
            submission.update(
                items=groups[job_id], judge_calibration=qualification_attestation,
                review_protocol=binding,
            )
            submissions[job_id] = submission
            by_id = {item["trial_id"]: item for item in packet["items"]}
            unresolved += sum(
                protocol.review_origin(group, by_id[group["trial_id"]]["input_hash"])
                ["review_status"] == "unresolved"
                for group in groups[job_id]
            )
            source_report = source_bundle["reports"][job_id]
            if protocol.validate_pairs(source_report):
                reviewed[job_id] = rubric.apply_reviews(
                    copy.deepcopy(source_report), submission)

        return _finish(ledger, batch_id, {
            "status": "unresolved" if unresolved else "completed",
            "binding": binding,
            "unresolved_trial_count": unresolved,
            "disagreement_count": sum(
                bool(group.get("adjudication")) for rows in groups.values() for group in rows),
            "base_disagreement_count": base.get("disagreement_count"),
            "replacement_count": len(replacements),
            "recovery_arbitration_count": len(arbitration),
            "recovery_calibrations": recovery_calibrations,
            "replacement_calibration": replacement_attestation,
            "arbiter_calibration": arbiter_attestation,
            "judge_calibration": qualification_attestation,
            "reviews": submissions,
            "reviewed_reports": reviewed,
            "boundary": (
                "Only format-invalid score positions were replaced. Valid base scores, "
                "frozen Luna answers, questions, gold and thresholds were not rerun or changed."
            ),
        })
