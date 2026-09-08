"""Explicit timeout supplements exercised only with local HTTP substitutes."""
from __future__ import annotations

import copy
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from codeatlas.contracts import digest
from codeatlas.eval import authorized_review_recovery as recovery
from codeatlas.eval import campaign_review as review, evidence_repair, proof_campaign as campaign, protocol, rubric
from codeatlas.llm.client import OpenAICompatClient

ROOT = Path(__file__).resolve().parents[1]
CALIBRATION = ROOT / "eval/review_calibration_v4b.yaml"
JOBS = [{"id": "knowledge-reuse", "kind": "knowledge_reuse", "manifest": "eval/knowledge_reuse.yaml",
         "answer_key": "eval/knowledge_reuse.answers.yaml"}]


def factory(**kwargs):
    return OpenAICompatClient(kwargs.pop("base_url"), "offline-fixture-only", kwargs.pop("model"), **kwargs)


class FakeHTTP:
    def __init__(self):
        samples = rubric.load_calibration(CALIBRATION)["samples"]
        self.expected = {s["id"]: {k: v for k, v in s["expected_rubric"].items() if k in {
            "expected_point_results", "forbidden_claim_triggered", "citation_relation", "manipulation_detected"}}
            for s in samples}
        self.mode, self.target, self.calls = "agree", None, []

    def send(self, request, **kwargs):
        body = json.loads(request.data)
        payload = None
        if body["messages"][0]["content"] != review.SYSTEM:
            raw = "Synthetic offline answer [M1]."
        else:
            payload = json.loads(body["messages"][1]["content"])
            item = payload["item"]
            role = request.get_header("X-opencode-session").rsplit("-", 1)[-1]
            role = "judge-" + role if role in {"a", "b"} else role
            calibration = "id" in item
            self.calls.append({"role": role, "item": item, "calibration": calibration,
                               "session": request.get_header("X-opencode-session"), "timeout": kwargs.get("timeout")})
            if calibration:
                answer = copy.deepcopy(self.expected[item["id"]])
                if self.mode == "arbiter_fail" and role == "arbiter":
                    answer["forbidden_claim_triggered"] = not answer["forbidden_claim_triggered"]
            else:
                if item["trial_id"] == self.target and self.mode in {"base_timeouts", "timeout"}:
                    raise TimeoutError("offline synthetic timeout, outcome unknown")
                if self.mode == "interrupt":
                    self.mode = "agree"
                    raise KeyboardInterrupt()
                answer = {"point_reviews": {p["id"]: "met" for p in item["answer_key"]["expected_points"]},
                          "false_claim_present": False, "citation_relation": "supported"}
                if self.mode in {"disagree", "arbiter_fail"} and role == "judge-b":
                    first = next(iter(answer["point_reviews"]))
                    answer["point_reviews"][first] = "missing"
                if self.mode == "semantic_unknown":
                    answer["point_reviews"] = {k: "unresolved" for k in answer["point_reviews"]}
                    answer["false_claim_present"] = None
                    answer["citation_relation"] = "unresolved"
                    answer["unresolved_reason"] = "Synthetic source uncertainty."
            answer["note"] = "Synthetic offline protocol fixture; not evidence of answer quality."
            raw = json.dumps(answer)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return json.dumps({"choices": [{"message": {"content": raw}}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}).encode()
        return Response()


@pytest.fixture(scope="module")
def prototype(tmp_path_factory):
    patch = pytest.MonkeyPatch()
    http = FakeHTTP()
    patch.setattr("urllib.request.urlopen", http.send)
    directory = tmp_path_factory.mktemp("authorized-recovery-base") / "campaign"
    try:
        campaign.prepare(directory, project_root=ROOT, jobs=JOBS, runs=3, smoke_trials=3, max_requests=5000)
        campaign.run(directory, mode="full", client_factory=factory)
        report = campaign.report(directory)["reports"]["knowledge-reuse"]
        http.target = rubric.blind_packet(report)["items"][0]["trial_id"]
        qualified = review.review(directory, attempt_id="offline-qualification", client_factory=factory,
            judge_model="gpt-5.6-sol", calibration_path=CALIBRATION, calibration_only=True)
        assert qualified["status"] == "calibrated"
        scope = {"scope_id": "closeout-evidence-repair-v1", "max_requests": 800, "manifest_hash": "a" * 64}
        http.mode = "base_timeouts"
        base = review.review(directory, attempt_id=recovery.BASE_ATTEMPT, client_factory=factory,
            judge_model="gpt-5.6-sol", calibration_path=CALIBRATION,
            qualification_attestation=qualified["judge_calibration"], qualification_batch_id=qualified["batch_id"],
            projection_version=evidence_repair.PROJECTION, budget_scope=scope)
        assert base["unresolved_trial_count"] == 1
        assert len(base["reviews"]["knowledge-reuse"]["items"]) == 54
        yield directory, http.target, base
    finally:
        patch.undo()


@pytest.fixture
def case(tmp_path, prototype, monkeypatch):
    original, target_id, base = prototype
    directory = tmp_path / "campaign"
    shutil.copytree(original, directory)
    http = FakeHTTP()
    http.target = target_id
    monkeypatch.setattr("urllib.request.urlopen", http.send)
    # The production module permits one exact user-named target. Bind the
    # synthetic counterpart here; no test accesses the real campaign ledger.
    monkeypatch.setattr(recovery, "TARGET_ID", target_id)
    manifest = tmp_path / "authorization.json"
    recovery.prepare(directory, manifest, attempt_id="offline-authorized-supplement",
                     authorization_note="Explicit synthetic user authorization for the two unknown slots.")
    return directory, manifest, http, base


def snapshot(directory):
    with sqlite3.connect(directory / "ledger.sqlite3") as db:
        return {
            "batches": {bid: (binding, result, sha) for bid, binding, result, sha in db.execute(
                "SELECT id,binding_json,result_json,result_hash FROM campaign_review_batches")},
            "calls": {identifier: row for identifier, *row in db.execute("SELECT * FROM campaign_review_calls")},
            "requests": db.execute("SELECT count(*) FROM requests").fetchone()[0],
        }


def run(case, **kwargs):
    directory, manifest, _http, _base = case
    return recovery.recover(directory, authorization_manifest=manifest, client_factory=factory, **kwargs)


def reseal(path, mutate):
    value = json.loads(path.read_text())
    mutate(value)
    value["authorization_hash"] = digest({k: v for k, v in value.items() if k != "authorization_hash"})
    path.write_text(json.dumps(value))


def test_prepare_is_offline_idempotent_and_does_not_touch_original_ledger(case):
    directory, manifest, http, _base = case
    before = snapshot(directory)
    result = recovery.prepare(directory, manifest, attempt_id="offline-authorized-supplement",
        authorization_note="Explicit synthetic user authorization for the two unknown slots.")
    assert result["new_requests"] == 0 and not http.calls
    assert snapshot(directory) == before
    assert json.loads(manifest.read_text())["preserved_trial_count"] == 53
    with pytest.raises(campaign.CampaignError, match="already frozen"):
        recovery.prepare(directory, manifest, attempt_id="different", authorization_note="another attempt")


def test_two_new_sessions_preserve_all_53_scores_and_old_timeout_calls(case):
    directory, _manifest, http, base = case
    before = snapshot(directory)
    result = run(case)
    after = snapshot(directory)
    assert result["status"] == "completed" and result["judge_request_count"] == 2
    assert len(http.calls) == 2 and len({r["session"] for r in http.calls}) == 2
    assert all(r["timeout"] == 180 and r["item"]["trial_id"] == http.target for r in http.calls)
    assert after["requests"] == before["requests"] + 2
    assert all(after["batches"][k] == v for k, v in before["batches"].items())
    assert all(after["calls"][k] == v for k, v in before["calls"].items())
    old = [g for g in base["reviews"][recovery.JOB]["items"] if g["trial_id"] != http.target]
    new = [g for g in result["reviews"][recovery.JOB]["items"] if g["trial_id"] != http.target]
    assert old == new and len(new) == 53
    assert protocol.run_hash(result["reviewed_reports"][recovery.JOB]) == base["binding"]["run_hashes"][recovery.JOB]
    assert result["binding"]["input_budget"] == 8000
    assert run(case, resume=True) == result and snapshot(directory) == after and len(http.calls) == 2


@pytest.mark.parametrize("mode,expected_requests", [("disagree", 15), ("arbiter_fail", 14)])
def test_only_disputed_target_gets_current_12_question_arbiter_calibration(case, mode, expected_requests):
    case[2].mode = mode
    result = run(case)
    assert result["judge_request_count"] == expected_requests
    assert len(case[2].calls) == expected_requests
    assert sum(r["calibration"] for r in case[2].calls) == 12
    assert all(r["role"] == "arbiter" for r in case[2].calls if r["calibration"])
    assert result["status"] == ("completed" if mode == "disagree" else "unresolved")
    assert run(case, resume=True) == result and len(case[2].calls) == expected_requests


@pytest.mark.parametrize("mode", ["timeout", "semantic_unknown"])
def test_unknown_supplement_is_retained_and_never_automatically_retried(case, mode):
    case[2].mode = mode
    result = run(case)
    assert result["status"] == "unresolved" and result["unresolved_trial_count"] == 1
    assert result["judge_request_count"] == len(case[2].calls) == 2
    case[2].mode = "agree"
    assert run(case, resume=True) == result and len(case[2].calls) == 2
    reseal(case[1], lambda m: m.update(attempt_id="unauthorized-second-supplement"))
    with pytest.raises(campaign.CampaignError, match="already has a supplement"):
        run(case)
    assert len(case[2].calls) == 2


@pytest.mark.parametrize("change", [
    lambda m: m.update(target_trial_id="0" * 64),
    lambda m: m.update(slots=[0]),
    lambda m: m.update(base_result_hash="0" * 64),
    lambda m: m["failed_calls"][0].update(request_hash="0" * 64),
    lambda m: m["failed_calls"][1].update(result_hash="0" * 64),
    lambda m: m["packet_hashes"].update({"knowledge-reuse": "0" * 64}),
    lambda m: m.update(input_budget=16000),
    lambda m: m.update(request_timeout_seconds=90),
    lambda m: m.update(global_max_requests=9000),
    lambda m: m["budget_scope"].update(max_requests=801),
    lambda m: m.update(authorization={"kind": "automatic_retry", "note": "not authorized"}),
])
def test_resealed_manifest_tampering_fails_before_any_dispatch(case, change):
    before = snapshot(case[0])
    reseal(case[1], change)
    with pytest.raises(campaign.CampaignError):
        run(case)
    assert not case[2].calls and snapshot(case[0]) == before


def test_original_timeout_cannot_be_reclassified_as_an_authorized_semantic_gap(case):
    directory, manifest, http, base = case
    failed = json.loads(manifest.read_text())["failed_calls"][0]
    with sqlite3.connect(directory / "ledger.sqlite3") as db:
        call = json.loads(db.execute("SELECT result_json FROM campaign_review_calls WHERE id=?", (failed["id"],)).fetchone()[0])
        call["error_type"] = "ValueError"
        db.execute("UPDATE campaign_review_calls SET result_json=?,result_hash=? WHERE id=?",
                   (json.dumps(call), digest(call), failed["id"]))
    with pytest.raises(campaign.CampaignError, match="only unknown TimeoutError"):
        run(case)
    assert not http.calls


def test_original_raw_result_corruption_is_not_hidden_by_rehashing_its_call(case):
    directory, _manifest, http, base = case
    with sqlite3.connect(directory / "ledger.sqlite3") as db:
        row = db.execute("SELECT id,result_json FROM campaign_review_calls WHERE batch_id=? AND state='completed' LIMIT 1",
                         (base["batch_id"],)).fetchone()
        call = json.loads(row[1])
        call["raw_response"] = '{}'
        call["response_hash"] = digest('{}')
        db.execute("UPDATE campaign_review_calls SET result_json=?,result_hash=? WHERE id=?", (json.dumps(call), digest(call), row[0]))
    with pytest.raises((ValueError, campaign.CampaignError)):
        run(case)
    assert not http.calls


def test_interrupted_request_is_not_resent_on_resume(case):
    case[2].mode = "interrupt"
    with pytest.raises(KeyboardInterrupt):
        run(case)
    before = len(case[2].calls)
    result = run(case, resume=True)
    assert result["status"] == "unresolved"
    assert len(case[2].calls) == before == 2
    assert run(case, resume=True) == result and len(case[2].calls) == 2


def test_resume_rejects_changed_supplement_checkpoint_without_sending(case):
    result = run(case)
    with sqlite3.connect(case[0] / "ledger.sqlite3") as db:
        db.execute("UPDATE campaign_review_calls SET request_hash='changed' WHERE batch_id=?", (result["batch_id"],))
    with pytest.raises(campaign.CampaignError, match="calls differ"):
        run(case, resume=True)
    assert len(case[2].calls) == 2


def test_original_qualification_calls_are_revalidated_before_dispatch(case):
    with sqlite3.connect(case[0] / "ledger.sqlite3") as db:
        row = db.execute("SELECT id,result_json FROM campaign_review_calls WHERE call_id LIKE 'calibration:%' LIMIT 1").fetchone()
        call = json.loads(row[1])
        call["parsed"]["forbidden_claim_triggered"] = not call["parsed"]["forbidden_claim_triggered"]
        db.execute("UPDATE campaign_review_calls SET result_json=?,result_hash=? WHERE id=?", (json.dumps(call), digest(call), row[0]))
    with pytest.raises(ValueError, match="parsed result"):
        run(case)
    assert not case[2].calls
