"""One explicitly authorized timeout supplement; old calls are never retried.

Preparation reads the ledger without changing it. A sealed authorization names
one new attempt and both failed slots; resume may finish unsent work but never
resends a completed, failed, or transport-unknown request.
"""
from __future__ import annotations

import copy
import fcntl
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from ..contracts import digest, estimated_tokens
from ..llm.client import get_client
from ..publication import atomic_text
from . import campaign_review as review, evidence_repair, proof_campaign as campaign
from . import protocol, rubric
from .review_inheritance import _Sources
from .review_recovery import _stored_batch

VERSION = "explicit-timeout-supplement-v1"
BASE_ATTEMPT = "closeout-v2.reuse-evidence-repair"
TARGET_ID = "802cb127778ba3d407a3b21574a0df1488016928ac34e07a23ddd1a329fd6f05"
JOB = "knowledge-reuse"
INPUT_BUDGET = 8000
TIMEOUT_SECONDS = 180
ROLES = ("judge-a", "judge-b")


def _require(condition, message):
    if not condition:
        raise campaign.CampaignError("authorized recovery: " + message)


class _ReadOnlyLedger:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def _payload(item, scores=None):
    value = {"item": review._judge_item(item, evidence_repair.PROJECTION),
             "rubric": {**rubric.RUBRIC, "judge_guidance": review.JUDGE_GUIDANCE,
                        "response_schema": review.ANSWER_SCHEMA}}
    if scores is not None:
        value["independent_reviews"] = [review._peer_view(s, evidence_repair.PROJECTION) for s in scores]
    return value


def _runtime_hashes():
    from ..llm import client
    from . import review_inheritance
    return {Path(module.__file__).name: digest(Path(module.__file__).read_bytes())
            for module in (review, rubric, evidence_repair, review_inheritance, client)} | {
                "authorized_review_recovery.py": digest(Path(__file__).read_bytes())}


def _inspect(ledger, plan, target_id):
    """Prove the exact base, all 53 preserved groups, and both unknown calls."""
    _require(target_id == TARGET_ID, "target is outside this explicit authorization")
    bid, binding, base = _stored_batch(ledger, BASE_ATTEMPT)
    _require(binding.get("projection_version") == evidence_repair.PROJECTION,
             "base projection differs")
    _require(binding.get("campaign_plan_hash") == plan["plan_hash"], "base campaign differs")
    _require(set(base.get("reviews", {})) == set(base.get("reviewed_reports", {})) == {JOB},
             "base must contain only the authorized reuse job")
    report = base["reviewed_reports"][JOB]
    packet = report["blind_review"]
    _require(protocol.run_hash(report) == report["run_hash"] == packet["run_hash"], "raw answers changed")
    _require(rubric.blind_packet(copy.deepcopy(report)) == packet, "frozen packet changed")
    _require(binding.get("packet_hashes") == {JOB: packet["packet_hash"]}
             and binding.get("run_hashes") == {JOB: packet["run_hash"]}, "base packet binding changed")
    items = {item["trial_id"]: item for item in packet["items"]}
    groups = {g["trial_id"]: g for g in base["reviews"][JOB]["items"]}
    _require(len(items) == len(packet["items"]) == len(groups) == 54
             and len(base["reviews"][JOB]["items"]) == 54 and set(items) == set(groups)
             and target_id in items, "base must cover exactly 54 unique answers")
    target = groups[target_id]
    _require(len(target.get("independent_reviews", [])) == 2 and not target.get("adjudication"),
             "target must contain exactly two failed independent slots")
    preserved = [g for g in base["reviews"][JOB]["items"] if g["trial_id"] != target_id]
    _require(all(protocol.review_origin(g, items[g["trial_id"]]["input_hash"])["review_status"]
                 != "unresolved" for g in preserved), "other base answers are not terminal")
    failures, proofs, original_qualification = [], [], None
    with ledger.connect() as conn:
        _require(json.loads(conn.execute("SELECT settings FROM ledger_config WHERE id=1").fetchone()[0])
                 ["max_requests"] == plan["budget"]["max_requests"] == 5000, "global cap must remain 5000")
        active = conn.execute("SELECT scope_id FROM active_request_scope WHERE singleton=1").fetchone()
        scope = binding.get("budget_scope") or {}
        _require(scope.get("max_requests") == 800 and active and active[0] == scope.get("scope_id"),
                 "original shared 800 scope is not active")
        scope_row = conn.execute("SELECT * FROM request_scopes WHERE id=?", (active[0],)).fetchone()
        _require(scope_row and scope_row["max_requests"] == 800
                 and scope_row["manifest_hash"] == scope.get("manifest_hash"), "scope binding changed")
        sources = _Sources(conn, bid)
        _require(sources.result(bid) == base, "base durable result changed")
        for identifier, group in groups.items():
            item = items[identifier]
            _require(item["input_hash"] == digest({k: v for k, v in item.items() if k != "input_hash"}),
                     "item checksum changed")
            for slot, score in enumerate(group["independent_reviews"]):
                role = score.get("reviewer")
                _require(role == ROLES[slot], "base independent role changed")
                source_id, source_binding, row = sources.locate(score, identifier)
                _require(source_id == bid, "base score belongs to a different scoring batch")
                payload = _payload(item)
                if identifier == target_id:
                    _require(row["state"] == "unresolved" and row.get("result_json"), "failed slot is not durable")
                    call = json.loads(row["result_json"])
                    _require(digest(call) == row["result_hash"], "failed call checksum changed")
                    _require(call.get("state") == "unresolved" and call.get("error_type") == "TimeoutError"
                             and all(call.get(k) is None for k in ("parsed", "raw_response", "response_hash")),
                             "only unknown TimeoutError slots are authorized")
                    expected = digest([review.SYSTEM, json.dumps(payload, ensure_ascii=False, sort_keys=True)])
                    _require(row["request_hash"] == call.get("request_hash") == expected,
                             "failed request differs from complete original input")
                    failures.append({k: row[k] for k in ("id", "batch_id", "role", "call_id", "request_hash", "result_hash")})
                else:
                    _, _, row, call = sources.call(score, identifier, payload, item)
                _require(review._score_record(source_binding, role, item["input_hash"], call) == score,
                         "base score differs from original call")
                certificate_key = sources.qualification(source_id, role, score)
                if identifier == target_id:
                    certificate = sources.certificates[certificate_key]["certificate"]
                    _require(certificate.get("reviewer_count") == 2
                             and digest(certificate) == binding.get("qualification_attestation_hash"),
                             "original two-judge qualification changed")
                    original_qualification = copy.deepcopy(certificate)
                proofs.append({"trial_id": identifier, "role": role, "result_hash": row["result_hash"],
                               "request_hash": row["request_hash"], "qualification_proof": certificate_key})
            if group.get("adjudication"):
                score = group["adjudication"]
                _require(score.get("reviewer") == "arbiter", "unsupported base arbiter")
                source_id, source_binding, row, call = sources.call(
                    score, identifier, _payload(item, group["independent_reviews"]), item)
                _require(source_id == bid, "base arbiter belongs to another batch")
                ih = digest({"packet_input_hash": item["input_hash"], "independent_reviews": group["independent_reviews"]})
                rebuilt = review._score_record(source_binding, "arbiter", ih, call)
                rebuilt.update(peer_reviews_visible=True, independent=False,
                               input_hash_scope="packet_input_hash_and_original_reviews",
                               resolves_review_hashes=[digest(s) for s in group["independent_reviews"]])
                _require(rebuilt == score, "base arbiter fields changed")
                proofs.append({"trial_id": identifier, "role": "arbiter", "result_hash": row["result_hash"],
                               "request_hash": row["request_hash"],
                               "qualification_proof": sources.qualification(source_id, "arbiter", score)})
        qualifications = copy.deepcopy(sources.certificates)
    _require([f["role"] for f in failures] == list(ROLES), "authorization must cover both timeout slots")
    item = items[target_id]
    worst = {"point_reviews": {p["id"]: "unresolved" for p in item["answer_key"]["expected_points"]},
             "false_claim_present": False, "citation_relation": "fabricated_content",
             "verdict": "reasonable_refusal", "completeness": 0.3333333333333333}
    maximum = estimated_tokens(review.SYSTEM + json.dumps(_payload(item, [worst, worst]),
                               ensure_ascii=False, sort_keys=True)) + 32
    _require(maximum <= INPUT_BUDGET, "complete arbitration exceeds frozen 8000 input cap")
    facts = {"base_attempt_id": BASE_ATTEMPT, "base_batch_id": bid, "base_result_hash": digest(base),
             "campaign_plan_hash": plan["plan_hash"], "job_id": JOB, "target_trial_id": target_id,
             "slots": [0, 1], "failed_calls": failures, "run_hashes": binding["run_hashes"],
             "packet_hashes": binding["packet_hashes"], "item_input_hash": item["input_hash"],
             "preserved_groups_hash": digest(preserved), "preserved_trial_count": len(preserved),
             "original_score_proofs_hash": digest(proofs), "qualification_proofs_hash": digest(qualifications),
             "qualified_by_batch_id": binding["qualified_by_batch_id"],
             "qualification_attestation_hash": digest(original_qualification),
             "budget_scope": scope, "global_max_requests": 5000, "input_budget": INPUT_BUDGET,
             "request_timeout_seconds": TIMEOUT_SECONDS, "transport_retries": 0, "workers": 2,
             "projection_version": evidence_repair.PROJECTION, "max_estimated_arbitration_input": maximum,
             "maximum_new_requests": 15, "runtime_hashes": _runtime_hashes()}
    return facts, base, binding, packet, original_qualification, qualifications


def prepare(directory, out, *, attempt_id, authorization_note, trial_id=None):
    """Freeze explicit user authorization offline; never changes the campaign DB."""
    _require(isinstance(attempt_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", attempt_id)
             and attempt_id != BASE_ATTEMPT, "a new stable attempt id is required")
    _require(isinstance(authorization_note, str) and authorization_note.strip(), "explicit user authorization note required")
    directory = Path(directory).resolve()
    plan = json.loads((directory / "plan.json").read_text())
    _require(plan["plan_hash"] == digest({k: v for k, v in plan.items() if k != "plan_hash"}), "plan checksum changed")
    facts, *_ = _inspect(_ReadOnlyLedger(directory / "ledger.sqlite3"), plan, trial_id or TARGET_ID)
    manifest = {"version": VERSION, "attempt_id": attempt_id,
                "authorization": {"kind": "explicit_user", "note": authorization_note}, **facts}
    manifest["authorization_hash"] = digest(manifest)
    path = Path(out).resolve()
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    _require(not path.exists() or path.read_text() == text, "authorization manifest is already frozen")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_text(path, text)
    return {"authorization_manifest": str(path), "authorization_hash": manifest["authorization_hash"],
            "attempt_id": attempt_id, "slots": 2, "preserved_trials": 53, "new_requests": 0}


def recover(directory, *, authorization_manifest, resume=False, client_factory=None):
    """Run the two explicitly authorized new sessions, preserving all old records."""
    manifest = json.loads(Path(authorization_manifest).read_text())
    _require(manifest.get("authorization_hash") == digest({k: v for k, v in manifest.items() if k != "authorization_hash"}),
             "authorization checksum changed")
    _require(manifest.get("version") == VERSION and (manifest.get("authorization") or {}).get("kind") == "explicit_user"
             and (manifest.get("authorization") or {}).get("note"), "explicit authorization is missing")
    attempt = manifest.get("attempt_id")
    _require(isinstance(attempt, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", attempt)
             and attempt != BASE_ATTEMPT, "invalid authorized attempt")
    target, plan, ledger = campaign._load(directory)
    with (target / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise campaign.CampaignError("campaign collection/review is already running") from exc
        facts, base, old_binding, packet, attestation, qualification_proofs = _inspect(
            ledger, plan, manifest.get("target_trial_id"))
        expected = {"version": VERSION, "attempt_id": attempt, "authorization": manifest["authorization"], **facts}
        _require({k: v for k, v in manifest.items() if k != "authorization_hash"} == expected,
                 "frozen authorization differs from original calls, scope, or runtime")
        core = {"version": VERSION, "attempt_id": attempt, "base_attempt_id": BASE_ATTEMPT,
                "base_batch_id": facts["base_batch_id"], "base_result_hash": facts["base_result_hash"],
                "authorization_hash": manifest["authorization_hash"],
                **{k: old_binding[k] for k in ("provider", "judge_model", "prompt_hash", "schema_hash", "judge_guidance_hash")},
                **{k: facts[k] for k in ("run_hashes", "packet_hashes", "projection_version", "budget_scope",
                    "qualified_by_batch_id", "qualification_attestation_hash", "input_budget", "request_timeout_seconds")}}
        batch = digest(core)
        binding = {**core, "agents": {r: f"{plan['campaign_id']}/review/{batch[:16]}/{r}" for r in (*ROLES, "arbiter")},
                   "sessions": {r: f"{plan['campaign_id']}-review-{batch[:16]}-{r}" for r in (*ROLES, "arbiter")}}
        with ledger.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in conn.execute("SELECT id,binding_json FROM campaign_review_batches"):
                prior = json.loads(row["binding_json"])
                if prior.get("attempt_id") == attempt:
                    _require(row["id"] == batch, "attempt already bound to different authorization")
                if prior.get("version") == VERSION and prior.get("base_batch_id") == facts["base_batch_id"]:
                    _require(row["id"] == batch, "this authorized base already has a supplement attempt")
            existing = conn.execute("SELECT * FROM campaign_review_batches WHERE id=?", (batch,)).fetchone()
            if existing:
                _require(resume, "supplement exists; use resume")
                _require(json.loads(existing["binding_json"]) == binding, "supplement binding changed")
                if existing["result_json"]:
                    result = json.loads(existing["result_json"])
                    _require(digest(result) == existing["result_hash"], "supplement result changed")
                    _require(result.get("binding") == binding and result.get("authorization") == manifest,
                             "supplement result binding changed")
                    calls = []
                    for row in conn.execute("SELECT * FROM campaign_review_calls WHERE batch_id=? ORDER BY started_at,id", (batch,)):
                        call = json.loads(row["result_json"]) if row["result_json"] else None
                        _require(call is None or digest(call) == row["result_hash"], "supplement call checksum changed")
                        calls.append({"call_id": row["call_id"], "role": row["role"], "state": row["state"],
                                      "request_hash": row["request_hash"], "result": call})
                    _require(calls == result.get("raw_calls"), "supplement calls differ from durable result")
                    return result
            conn.execute("INSERT OR IGNORE INTO campaign_review_batches(id,binding_json) VALUES(?,?)", (batch, json.dumps(binding, sort_keys=True)))
            conn.execute("UPDATE campaign_review_calls SET state='uncertain' WHERE batch_id=? AND state='running'", (batch,))
        ledger.bind_scope(**facts["budget_scope"])
        groups = copy.deepcopy(base["reviews"][JOB]["items"])
        group = next(g for g in groups if g["trial_id"] == TARGET_ID)
        item = next(i for i in packet["items"] if i["trial_id"] == TARGET_ID)
        calibrations, reason = [], None

        def client(role):
            obj = (client_factory or get_client)(ledger=ledger, session_id=binding["sessions"][role],
                base_url=plan["base_url"], model=binding["judge_model"],
                thinking_mode=plan.get("thinking_mode", "disabled"), transport_retries=0)
            _require(obj is not None and obj.model == binding["judge_model"] and obj.base_url == plan["base_url"],
                     "client differs from frozen model/endpoint or is unconfigured")
            obj.ledger, obj.session_id = ledger, binding["sessions"][role]
            obj.max_requests = obj.max_cost_usd = None
            obj.review_input_budget, obj.request_timeout_seconds = INPUT_BUDGET, TIMEOUT_SECONDS
            obj.input_usd_per_million = plan["budget"]["input_usd_per_million"]
            obj.output_usd_per_million = plan["budget"]["output_usd_per_million"]
            return obj

        def score(role):
            call = review._call(ledger, batch, role, TARGET_ID, client(role), _payload(item),
                                lambda answer: review._validate(answer, item))
            return review._score_record(binding, role, item["input_hash"], call)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                group["independent_reviews"] = list(pool.map(score, ROLES))
            scores = group["independent_reviews"]
            disputed = all(s["model_generated"] for s in scores) and (
                {k: scores[0].get(k) for k in review.OUTCOMES} != {k: scores[1].get(k) for k in review.OUTCOMES})
            if disputed:
                path = Path(__file__).resolve().parents[3] / attestation["calibration_path"]
                calibration = rubric.load_calibration(path)
                run = review._calibrate(ledger, batch, binding, "arbiter", client, calibration, workers=2)
                calibrations.append(run)
                check = rubric.score_calibration(path, {"reviews": [run]}, minimum_reviews=1) if run["complete"] else None
                if check and check["passed"]:
                    attestation = copy.deepcopy(attestation)
                    attestation["review_runs"].extend(check["review_runs"])
                    attestation["reviewer_count"] = len(attestation["review_runs"])
                    attestation["review_results_hash"] = digest(attestation["review_runs"])
                    call = review._call(ledger, batch, "arbiter", TARGET_ID, client("arbiter"), _payload(item, scores),
                                        lambda answer: review._validate(answer, item))
                    ih = digest({"packet_input_hash": item["input_hash"], "independent_reviews": scores})
                    arbiter = review._score_record(binding, "arbiter", ih, call)
                    arbiter.update(peer_reviews_visible=True, independent=False,
                        input_hash_scope="packet_input_hash_and_original_reviews", resolves_review_hashes=[digest(s) for s in scores])
                    group["adjudication"] = arbiter
                else:
                    reason = "authorized_arbiter_calibration_unresolved_or_failed"
        except review.ReviewPaused:
            reason = "persistent_ledger_ceiling"
        _require(digest([g for g in groups if g["trial_id"] != TARGET_ID]) == facts["preserved_groups_hash"],
                 "a preserved group changed")
        submission = {k: packet[k] for k in ("identity", "evaluation_hash", "run_hash", "packet_hash")}
        submission.update(items=groups, judge_calibration=attestation, review_protocol=binding,
                          authorized_supplement=manifest, original_qualification_proofs=qualification_proofs)
        updated = rubric.apply_reviews(copy.deepcopy(base["reviewed_reports"][JOB]), submission)
        _require(protocol.run_hash(updated) == packet["run_hash"], "supplement changed original answers")
        unresolved = int(protocol.review_origin(group, item["input_hash"])["review_status"] == "unresolved")
        return review._finish(ledger, batch, {"status": "unresolved" if unresolved else "completed", "reason": reason,
            "binding": binding, "authorization": manifest, "unresolved_trial_count": unresolved,
            "preserved_trial_count": 53, "supplemented_slots": 2, "calibration_runs": calibrations,
            "judge_calibration": attestation, "reviews": {JOB: submission}, "reviewed_reports": {JOB: updated},
            "boundary": "Explicitly authorized new sessions. Original 53 groups and both unknown calls remain unchanged; unknown results are never automatically resent."})
