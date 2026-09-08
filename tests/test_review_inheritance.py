"""Synthetic in-memory provenance chains: no gateway and no file writes."""
import copy
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from codeatlas.contracts import digest
from codeatlas.eval import campaign_review as review, rubric
from codeatlas.eval.review_inheritance import validate_inheritance


class MemoryLedger:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE campaign_review_batches (id TEXT PRIMARY KEY, binding_json TEXT,
                result_json TEXT, result_hash TEXT);
            CREATE TABLE campaign_review_calls (id TEXT PRIMARY KEY, batch_id TEXT, role TEXT,
                call_id TEXT, request_hash TEXT, state TEXT, started_at REAL,
                result_json TEXT, result_hash TEXT);
        """)

    @contextmanager
    def connect(self):
        yield self.conn


def _call(ledger, bid, role, identifier, payload, item, answer, *, calibration=False):
    raw = json.dumps(answer, ensure_ascii=False)
    request_hash = digest([review.SYSTEM, json.dumps(payload, ensure_ascii=False, sort_keys=True)])
    call = {"state": "completed", "parsed": review._validate(answer, item, calibration=calibration),
            "raw_response": raw, "response_hash": digest(raw), "request_hash": request_hash,
            "error_type": None, "started_at": 1.0, "finished_at": 2.0,
            "usage": {"usage": {"prompt_tokens": 10, "completion_tokens": 10}, "measured_cost_usd": None}}
    ledger.conn.execute("INSERT INTO campaign_review_calls VALUES(?,?,?,?,?,?,?,?,?)",
                        (digest([bid, role, identifier]), bid, role, identifier, request_hash,
                         "completed", 1.0, json.dumps(call), digest(call)))
    return call


def _fixture(*, arbitration=False, recovery=False):
    ledger = MemoryLedger()
    report = {"identity": {}, "raw_trials": {"arm": [{"id": "question", "type": "semantic",
        "question": "Describe the source.", "repetitions": [{"answer": "The result is one.", "citations": []}]}]},
        "answer_key": {"tasks": {"question": {"expected_points": [{"id": "p", "text": "Result is one."}],
                                                  "forbidden_claims": []}}}}
    packet = rubric.blind_packet(report)
    report["blind_review"] = packet
    item = packet["items"][0]
    core = {"version": "campaign-review-v3-layered", "attempt_id": "base", "provider": "offline-fixture",
            "judge_model": "offline-fixture", "prompt_hash": digest(review.SYSTEM),
            "schema_hash": digest([review.ANSWER_SCHEMA, review.CALIBRATION_SCHEMA]),
            "judge_guidance_hash": digest(review.JUDGE_GUIDANCE),
            "projection_version": review.SEMANTIC_REVIEW_PROJECTION}
    bid = digest(core)
    roles = (["judge-a", "replacement", "recovery-arbiter"] if recovery else
             ["judge-a", "judge-b", *(["arbiter"] if arbitration else [])])
    binding = {**core, "agents": {r: bid + "/" + r for r in roles},
               "sessions": {r: bid + "-" + r for r in roles}}
    cal_path = Path(__file__).resolve().parents[1] / "eval/review_calibration_v4b.yaml"
    calibration = rubric.load_calibration(cal_path)
    cal_items = review._calibration_items(calibration)
    samples = {s["id"]: s for s in calibration["samples"]}
    cal_runs = []
    for role in roles:
        calls, items = [], []
        for cal_item in cal_items:
            answer = {k: v for k, v in samples[cal_item["id"]]["expected_rubric"].items()
                      if k in {"expected_point_results", "forbidden_claim_triggered",
                               "citation_relation", "manipulation_detected"}}
            answer["note"] = "Synthetic offline fixture."
            payload = {"item": cal_item, "rubric": {**calibration["rubric_contract"],
                "judge_guidance": review.JUDGE_GUIDANCE, "response_schema": review.CALIBRATION_SCHEMA}}
            call = _call(ledger, bid, role, "calibration:" + cal_item["id"], payload, cal_item,
                         answer, calibration=True)
            calls.append(call)
            items.append({"id": cal_item["id"], **call["parsed"]})
        profile = review._profile(binding, role, rubric.calibration_packet(calibration)["packet_hash"], timestamp=2.0)
        profile.update(verdict="approved", input_hash_scope="complete_calibration_packet",
                       request_hashes=[c["request_hash"] for c in calls])
        cal_runs.append({"reviewer": profile, "items": items})
    certificate = rubric.score_calibration(cal_path, {"reviews": cal_runs})
    payload = {"item": review._judge_item(item, review.SEMANTIC_REVIEW_PROJECTION),
               "rubric": {**rubric.RUBRIC, "judge_guidance": review.JUDGE_GUIDANCE,
                          "response_schema": review.ANSWER_SCHEMA}}
    scores = []
    for slot, role in enumerate(roles[:2]):
        answer = {"point_reviews": {"p": "missing" if (arbitration or recovery) and slot == 1 else "met"},
                  "false_claim_present": False, "citation_relation": "not_applicable", "note": "Offline score."}
        call_id = f"replacement:{item['trial_id']}:{slot}" if role == "replacement" else item["trial_id"]
        call = _call(ledger, bid, role, call_id, payload, item, answer)
        scores.append(review._score_record(binding, role, item["input_hash"], call))
    group = {"trial_id": item["trial_id"], "reviewer_kind": "agent", "independent_reviews": scores}
    if arbitration or recovery:
        answer = {"point_reviews": {"p": "met"}, "false_claim_present": False,
                  "citation_relation": "not_applicable", "note": "Offline adjudication."}
        from codeatlas.eval.review_recovery import _compact_arbiter_view
        peer_view = _compact_arbiter_view if recovery else review._arbiter_view
        role = roles[2]
        arb_payload = {**payload, "independent_reviews": [peer_view(s) for s in scores]}
        call_id = "arbitration:" + item["trial_id"] if recovery else item["trial_id"]
        call = _call(ledger, bid, role, call_id, arb_payload, item, answer)
        ih = digest({"packet_input_hash": item["input_hash"], "independent_reviews": scores})
        arbiter = review._score_record(binding, role, ih, call)
        arbiter.update(peer_reviews_visible=True, independent=False,
                       input_hash_scope="packet_input_hash_and_original_reviews",
                       resolves_review_hashes=[digest(s) for s in scores])
        group["adjudication"] = arbiter
    base = {"binding": binding, "judge_calibration": certificate, "reviewed_reports": {"wiki": report},
            "reviews": {"wiki": {"items": [group]}}}
    if recovery:
        for role, field in (("replacement", "replacement_calibration"), ("recovery-arbiter", "arbiter_calibration")):
            base[field] = rubric.score_calibration(cal_path,
                {"reviews": [r for r in cal_runs if r["reviewer"]["reviewer"] == role]}, minimum_reviews=1)
    ledger.conn.execute("INSERT INTO campaign_review_batches VALUES(?,?,?,?)",
                        (bid, json.dumps(binding), json.dumps(base), digest(base)))
    return ledger, bid, binding, base, {"wiki": copy.deepcopy(packet)}


def _save_base(fixture):
    ledger, bid, _binding, base, _packets = fixture
    ledger.conn.execute("UPDATE campaign_review_batches SET result_json=?,result_hash=? WHERE id=?",
                        (json.dumps(base), digest(base), bid))


@pytest.mark.parametrize("arbitration", [False, True])
def test_inherits_exact_groups_and_original_qualification_without_mutation(arbitration):
    f = _fixture(arbitration=arbitration)
    before = copy.deepcopy((f[3], f[4]))
    groups, audit = validate_inheritance(*f, set())
    assert groups["wiki"] == f[3]["reviews"]["wiki"]["items"]
    assert (f[3], f[4]) == before
    assert audit["inherited_trial_count"] == 1
    assert len(audit["trials"][0]["scores"]) == (3 if arbitration else 2)
    assert all(p["certificate"]["passed"] for p in audit["qualifications"].values())


def test_replacement_and_recovery_arbiter_retain_exact_original_calls():
    f = _fixture(recovery=True)
    groups, audit = validate_inheritance(*f, set())
    assert groups["wiki"] == f[3]["reviews"]["wiki"]["items"]
    proofs = audit["trials"][0]["scores"]
    assert [p["role"] for p in proofs] == ["judge-a", "replacement", "recovery-arbiter"]
    assert proofs[1]["call_id"].startswith("replacement:")
    assert proofs[2]["call_id"].startswith("arbitration:")
    assert {c["field"] for c in audit["qualifications"].values()} == {
        "judge_calibration", "replacement_calibration", "arbiter_calibration"}


def test_selected_trials_are_not_inherited_but_answers_and_gold_stay_frozen():
    f = _fixture()
    item = f[4]["wiki"]["items"][0]
    identifier = item["trial_id"]
    item["reviewer_verification"] = ["extra frozen evidence"]
    item["input_hash"] = digest({k: v for k, v in item.items() if k != "input_hash"})
    packet = f[4]["wiki"]
    packet["packet_hash"] = digest({k: v for k, v in packet.items() if k != "packet_hash"})
    with pytest.raises(ValueError, match="unselected item"):
        validate_inheritance(*f, set())
    groups, audit = validate_inheritance(*f, {identifier})
    assert groups == {"wiki": []} and audit["inherited_trial_count"] == 0
    item["answer"] = "a forged answer"
    item["input_hash"] = digest({k: v for k, v in item.items() if k != "input_hash"})
    packet["packet_hash"] = digest({k: v for k, v in packet.items() if k != "packet_hash"})
    with pytest.raises(ValueError, match="answer/gold"):
        validate_inheritance(*f, {identifier})


@pytest.mark.parametrize("change", ["request", "raw", "parsed", "result_checksum"])
def test_corrupt_source_call_fails_closed(change):
    f = _fixture()
    row = f[0].conn.execute("SELECT * FROM campaign_review_calls WHERE call_id NOT LIKE 'calibration:%' LIMIT 1").fetchone()
    call = json.loads(row["result_json"])
    if change == "request":
        f[0].conn.execute("UPDATE campaign_review_calls SET request_hash='different' WHERE id=?", (row["id"],))
    else:
        if change == "raw":
            call["raw_response"] = '{}'
            call["response_hash"] = digest('{}')
        elif change == "parsed":
            call["parsed"]["point_reviews"]["p"] = "wrong"
        f[0].conn.execute("UPDATE campaign_review_calls SET result_json=?,result_hash=? WHERE id=?",
            (json.dumps(call), "bad" if change == "result_checksum" else digest(call), row["id"]))
    with pytest.raises(ValueError):
        validate_inheritance(*f, set())


def test_score_edit_cannot_hide_behind_valid_batch_checksum():
    f = _fixture()
    f[3]["reviews"]["wiki"]["items"][0]["independent_reviews"][0]["note"] = "Changed rationale"
    _save_base(f)
    with pytest.raises(ValueError, match="score fields"):
        validate_inheritance(*f, set())


def test_qualification_must_match_actual_calibration_calls():
    f = _fixture()
    certificate = f[3]["judge_calibration"]
    certificate["review_runs"][0]["items"][0]["observed"]["manipulation_detected"] = True
    certificate["review_results_hash"] = digest(certificate["review_runs"])
    _save_base(f)
    with pytest.raises(ValueError, match="qualification"):
        validate_inheritance(*f, set())


def test_duplicate_and_unknown_trials_are_rejected():
    f = _fixture()
    with pytest.raises(ValueError, match="selected trial absent"):
        validate_inheritance(*f, {"unknown"})
    p = f[4]["wiki"]
    p["items"].append(copy.deepcopy(p["items"][0]))
    p["packet_hash"] = digest({k: v for k, v in p.items() if k != "packet_hash"})
    with pytest.raises(ValueError, match="duplicate"):
        validate_inheritance(*f, set())
