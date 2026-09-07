"""Offline protocol tests: fake labels prove plumbing, never real judge accuracy."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from codeatlas.contracts import digest
from codeatlas.eval import campaign_review as review, proof_campaign as campaign, protocol, rubric
from codeatlas.eval.task_eval import REFUSAL_TYPE
from codeatlas.llm.client import OpenAICompatClient, GO_MODEL

ROOT = Path(__file__).resolve().parents[1]
REUSE = [{"id": "reuse", "kind": "knowledge_reuse", "manifest": "eval/knowledge_reuse.yaml",
          "answer_key": "eval/knowledge_reuse.answers.yaml"}]


@pytest.fixture(autouse=True)
def no_user_configuration(monkeypatch):
    for name in ("LLM_API_KEY", "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_MAX_REQUESTS",
                 "LLM_MAX_COST_USD", "LLM_INPUT_USD_PER_MILLION", "LLM_OUTPUT_USD_PER_MILLION",
                 "LLM_USER_AGENT", "LLM_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)


def factory(**kwargs):
    return OpenAICompatClient(kwargs.pop("base_url"), "fixture-only-key", kwargs.pop("model"), **kwargs)


@pytest.fixture
def fake_http(monkeypatch):
    golden = rubric.load_calibration(ROOT / "eval/review_calibration_v3.yaml")["samples"]
    expected = {sample["id"]: {
                "expected_point_results": sample["expected_rubric"]["expected_point_results"],
                "forbidden_claim_triggered": sample["expected_rubric"]["forbidden_claim_triggered"],
                "citation_relation": sample["expected_rubric"]["citation_relation"],
                "manipulation_detected": sample["category"] == "scoring_manipulation",
                "note": "Synthetic fixture labels; not a real source review."} for sample in golden}
    state = {"requests": [], "mode": "agree", "changed": False,
             "change_events": set(), "interrupt_after": None}

    class Response:
        def __init__(self, content):
            self.content = content

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps({"choices": [{"message": {"content": self.content}}],
                "usage": {"prompt_tokens": 23, "completion_tokens": 11, "total_tokens": 34}}).encode()

    def send(request, **kwargs):
        body = json.loads(request.data)
        if body["messages"][0]["content"] != review.SYSTEM:
            state["requests"].append({"kind": "answer", "session": request.get_header("X-opencode-session")})
            return Response("Synthetic answer [M1].")
        payload = json.loads(body["messages"][1]["content"])
        item = payload["item"]
        session = request.get_header("X-opencode-session")
        role = "arbiter" if session.endswith("arbiter") else "judge-a" if session.endswith("judge-a") else "judge-b"
        kind = "calibration" if "id" in item else "arbitration" if "independent_reviews" in payload else "review"
        state["requests"].append({"kind": kind, "role": role, "session": session,
                                  "payload": payload, "model": body["model"]})
        assert payload["rubric"]["judge_guidance"] == review.JUDGE_GUIDANCE
        total = sum(r["kind"] != "answer" for r in state["requests"])
        if state["interrupt_after"] == total:
            state["interrupt_after"] = None
            raise KeyboardInterrupt()
        if kind == "calibration":
            if (state["mode"] == "calibration_transport_once"
                    and "calibration_transport_once" not in state["change_events"]):
                from urllib.error import URLError
                state["change_events"].add("calibration_transport_once")
                state["changed"] = True
                raise URLError("synthetic one-batch transport failure")
            score = copy.deepcopy(expected[item["id"]])
            if state["mode"] == "calibration_fail" and role == "judge-b" and item["id"] == "v3-cal-01":
                score["forbidden_claim_triggered"] = True
            if state["mode"] == "arbiter_calibration_fail" and role == "arbiter" and item["id"] == "v3-cal-01":
                score["forbidden_claim_triggered"] = True
            return Response(json.dumps(score))
        score = {"point_reviews": {point["id"]: "met" for point in item["answer_key"]["expected_points"]},
                 "false_claim_present": False, "citation_relation": "supported",
                 "note": "Synthetic scoring fixture; does not prove this answer is correct."}
        if (state["mode"] == "malformed" and role == "judge-a"
                and "malformed" not in state["change_events"]):
            state["change_events"].add("malformed")
            state["changed"] = True
            return Response("explanation before JSON " + json.dumps(score))
        if (state["mode"] in {"disagree", "arbiter_calibration_fail", "arbiter_unresolved"}
                and role == "judge-b" and state["mode"] not in state["change_events"]):
            state["change_events"].add(state["mode"])
            state["changed"] = True
            score.update(false_claim_present=True,
                         point_reviews={key: "wrong" for key in score["point_reviews"]})
        if (state["mode"] == "citation_disagree" and role == "judge-b"
                and "citation_disagree" not in state["change_events"]):
            state["change_events"].add("citation_disagree")
            state["changed"] = True
            score["citation_relation"] = "mismatched"
        if state["mode"] == "arbiter_unresolved" and role == "arbiter":
            score.update(point_reviews={key: "unresolved" for key in score["point_reviews"]},
                         false_claim_present=None, citation_relation="unresolved",
                         unresolved_reason="Synthetic arbiter cannot resolve the source.")
        return Response(json.dumps(score))
    monkeypatch.setattr("urllib.request.urlopen", send)
    return state


def collected(tmp_path, *, max_requests=200):
    directory = tmp_path / "campaign"
    campaign.prepare(directory, project_root=ROOT, jobs=REUSE, runs=1, smoke_trials=3,
                     max_requests=max_requests)
    campaign.run(directory, mode="full", client_factory=factory)
    return directory


def test_calibrated_dual_reviews_use_isolated_sessions_and_preserve_raw_trials(tmp_path, fake_http):
    directory = collected(tmp_path)
    before = campaign.report(directory)["reports"]["reuse"]["run_hash"]
    result = review.review(directory, client_factory=factory)
    assert result["status"] == "completed" and not result["human_reviewed"]
    assert result["judge_calibration"]["passed"] and result["disagreement_count"] == 0
    requests = fake_http["requests"]
    assert len([r for r in requests if r["kind"] == "calibration"]) == 24
    assert len([r for r in requests if r["kind"] == "review"]) == 36
    assert len({r["session"] for r in requests if r["kind"] != "answer"}) == 2
    for request in requests:
        if request["kind"] == "answer":
            continue
        assert request["model"] == GO_MODEL and set(request["payload"]) == {"item", "rubric"}
        item = request["payload"]["item"]
        assert not set(item) & {"category", "expected_rubric", "variant", "arm", "audit_only_citation_map"}
    scored = result["reviewed_reports"]["reuse"]
    assert scored["review_status"] == "ai_reviewed" and not scored["human_release_eligible"]
    assert scored["acceptance"]["gates"]["judge_calibration"]
    assert scored["run_hash"] == before == campaign.report(directory)["reports"]["reuse"]["run_hash"]
    assert result["ledger"]["requests"] == 18 + 24 + 36
    size = len(requests)
    resumed = review.review(directory, resume=True, client_factory=factory)
    assert len(requests) == size and resumed["reviews"] == result["reviews"]
    with pytest.raises(campaign.CampaignError, match="already exists"):
        review.review(directory, client_factory=factory)


def test_failed_calibration_stops_before_answer_scoring(tmp_path, fake_http):
    directory = collected(tmp_path)
    fake_http["mode"] = "calibration_fail"
    result = review.review(directory, client_factory=factory)
    assert result["status"] == "unresolved" and result["reason"] == "judge_calibration_failed"
    assert not any(r["kind"] in {"review", "arbitration"} for r in fake_http["requests"])
    assert result["ledger"]["requests"] == 18 + 24


def test_calibration_only_can_resume_same_batch_without_repeating_calls(tmp_path, fake_http):
    directory = collected(tmp_path)
    calibrated = review.review(directory, client_factory=factory, attempt_id="layered-check",
                               calibration_only=True)
    assert calibrated["status"] == "calibrated"
    assert not any(row["kind"] == "review" for row in fake_http["requests"])
    count = len(fake_http["requests"])
    completed = review.review(directory, client_factory=factory, attempt_id="layered-check",
                              resume=True)
    assert completed["status"] == "completed"
    assert len([row for row in fake_http["requests"][count:] if row["kind"] == "calibration"]) == 0


def test_new_versioned_review_attempt_preserves_failed_batch_and_raw_answers(tmp_path, fake_http):
    directory = collected(tmp_path)
    before = campaign.report(directory)["reports"]["reuse"]["run_hash"]
    fake_http["mode"] = "calibration_transport_once"
    failed = review.review(directory, client_factory=factory)
    assert failed["status"] == "unresolved"
    completed = review.review(directory, client_factory=factory,
                              attempt_id="transport-recovery-1", workers=2,
                              judge_model="fixture-judge")
    assert completed["status"] == "completed"
    assert completed["binding"]["version"] == "campaign-review-v3-layered"
    assert completed["binding"]["attempt_id"] == "transport-recovery-1"
    assert completed["binding"]["review_workers"] == 2
    assert completed["binding"]["judge_model"] == "fixture-judge"
    assert campaign.report(directory)["reports"]["reuse"]["run_hash"] == before
    _, _, ledger = campaign._load(directory)
    with ledger.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM campaign_review_batches").fetchone()[0] == 2


def test_disagreement_uses_calibrated_third_agent_and_binds_original_scores(tmp_path, fake_http):
    directory = collected(tmp_path)
    fake_http["mode"] = "disagree"
    result = review.review(directory, client_factory=factory)
    assert result["status"] == "completed" and result["disagreement_count"] == 1
    assert result["judge_calibration"]["reviewer_count"] == 3
    assert sum(r["kind"] == "calibration" for r in fake_http["requests"]) == 36
    arbiters = [r for r in fake_http["requests"] if r["kind"] == "arbitration"]
    assert len(arbiters) == 1 and set(arbiters[0]["payload"]) == {"item", "rubric", "independent_reviews"}
    group = next(item for item in result["reviews"]["reuse"]["items"] if item.get("adjudication"))
    originals = group["independent_reviews"]
    item = arbiters[0]["payload"]["item"]
    assert group["adjudication"]["input_hash"] == digest({"packet_input_hash": item["input_hash"], "independent_reviews": originals})
    assert group["adjudication"]["resolves_review_hashes"] == [digest(score) for score in originals]
    assert protocol.review_origin(group, item["input_hash"])["review_status"] == "ai_reviewed"
    assert not result["human_reviewed"]


@pytest.mark.parametrize("mode", ["malformed", "arbiter_calibration_fail"])
def test_unresolved_is_retained_without_majority_or_json_repair(tmp_path, fake_http, mode):
    directory = collected(tmp_path)
    fake_http["mode"] = mode
    result = review.review(directory, client_factory=factory)
    assert result["status"] == "unresolved" and result["unresolved_trial_count"] == 1
    assert not any(r["kind"] == "arbitration" for r in fake_http["requests"])
    assert not result["reviewed_reports"]["reuse"]["acceptance"]["effect_observed"]
    requests = len(fake_http["requests"])
    review.review(directory, resume=True, client_factory=factory)
    assert len(fake_http["requests"]) == requests


def test_review_budget_and_interruption_survive_resume(tmp_path, fake_http):
    directory = collected(tmp_path, max_requests=20)
    result = review.review(directory, client_factory=factory)
    assert result["status"] == "partial_budget" and result["ledger"]["requests"] == 20
    requests = len(fake_http["requests"])
    assert review.review(directory, resume=True, client_factory=factory)["status"] == "partial_budget"
    assert len(fake_http["requests"]) == requests


def test_interrupted_calibration_stays_uncertain_and_is_not_retried(tmp_path, fake_http):
    directory = collected(tmp_path)
    fake_http["interrupt_after"] = 3
    with pytest.raises(KeyboardInterrupt):
        review.review(directory, client_factory=factory)
    result = review.review(directory, resume=True, client_factory=factory)
    assert result["status"] == "unresolved" and result["reason"] == "calibration_response_unresolved"
    assert result["ledger"]["requests"] == 42
    _, _, ledger = campaign._load(directory)
    with ledger.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM campaign_review_calls WHERE state='uncertain'").fetchone()[0] == 1
    assert not any(r["kind"] == "review" for r in fake_http["requests"])


@pytest.mark.parametrize("raw", ['[]', '{"x":1,"x":2}', '{"x":NaN}', 'prefix {"x":1}'])
def test_review_strict_json_rejects_repairs_duplicates_and_nonfinite_values(raw):
    with pytest.raises(ValueError):
        review._strict_json(raw)


def test_review_strict_json_accepts_one_lossless_fence():
    assert review._strict_json('```json\n{"verdict":"correct"}\n```') == {
        "verdict": "correct"
    }
    assert review._strict_json('{"verdict":"correct"}{"verdict":"correct"}') == {
        "verdict": "correct"
    }
    with pytest.raises(ValueError):
        review._strict_json('{"verdict":"correct"}{"verdict":"incorrect"}')


def test_answer_outcome_is_derived_from_points_not_model_summary_label():
    item = {"task_type": "语义检索", "refused": False,
            "answer_key": {"expected_points": [{"id": "p1"}, {"id": "p2"}]}}
    raw = {"point_reviews": {"p1": "met", "p2": "wrong"},
           "false_claim_present": False, "citation_relation": "supported", "note": "fixture"}
    scored = review._validate(raw, item)
    assert scored["verdict"] == "incorrect" and scored["completeness"] == 0.5
    assert scored["outcome_source"] == "deterministic_point_projection"

    raw.update(point_reviews={"p1": "met", "p2": "met"})
    assert review._validate(raw, item)["verdict"] == "correct"
    raw["citation_relation"] = "mismatched"
    assert review._validate(raw, item)["verdict"] == "incorrect"
    with pytest.raises(ValueError, match="schema keys differ"):
        review._validate({**raw, "verdict": "unresolved", "completeness": None}, item)


def test_refusal_mode_mismatch_is_deterministically_incorrect():
    item = {"task_type": REFUSAL_TYPE, "refused": False,
            "answer_key": {"expected_points": [{"id": "boundary"}]}}
    raw = {"point_reviews": {"boundary": "met"}, "false_claim_present": False,
           "citation_relation": "not_applicable", "note": "fixture"}
    assert review._validate(raw, item)["verdict"] == "incorrect"


def test_calibration_rejects_unknown_citation_relation():
    calibration = rubric.load_calibration(ROOT / "eval/review_calibration_v3.yaml")
    item = calibration["samples"][0]
    raw = {"expected_point_results": item["expected_rubric"]["expected_point_results"],
           "forbidden_claim_triggered": item["expected_rubric"]["forbidden_claim_triggered"],
           "citation_relation": "looks_supported", "manipulation_detected": False,
           "note": "fixture"}
    with pytest.raises(ValueError, match="citation relation"):
        review._validate(raw, item, calibration=True)


def test_citation_support_disagreement_reaches_arbiter(tmp_path, fake_http):
    directory = collected(tmp_path)
    fake_http["mode"] = "citation_disagree"
    result = review.review(directory, client_factory=factory)
    assert result["status"] == "completed" and result["disagreement_count"] == 1
    assert sum(row["kind"] == "arbitration" for row in fake_http["requests"]) == 1


def test_keyless_review_and_cli_entry_do_not_call_model(tmp_path, fake_http):
    from codeatlas.cli import app
    directory = collected(tmp_path)
    result = review.review(directory)
    assert result["status"] == "not_run" and result["reason"] == "model_not_configured"
    assert len(fake_http["requests"]) == 18
    response = CliRunner().invoke(app, ["eval", "campaign", "review", "--help"])
    assert response.exit_code == 0 and "--resume" in response.stdout and "--out" in response.stdout


def test_unresolved_arbiter_does_not_become_a_majority_vote(tmp_path, fake_http):
    directory = collected(tmp_path)
    fake_http["mode"] = "arbiter_unresolved"
    result = review.review(directory, client_factory=factory)
    assert result["status"] == "unresolved" and result["unresolved_trial_count"] == 1
    assert sum(r["kind"] == "arbitration" for r in fake_http["requests"]) == 1
    group = next(item for item in result["reviews"]["reuse"]["items"] if item.get("adjudication"))
    assert group["adjudication"]["verdict"] == "unresolved"


def test_lossless_evidence_reference_and_oversized_item_fail_closed(tmp_path, fake_http):
    item = {"citations": [{"text": "same source"}], "evidence_payload": [{"text": "same source"}]}
    value = review._judge_item(item)
    assert value["citations"] == {"$ref": "#/evidence_payload"}
    assert value["evidence_payload"] == item["citations"] == item["evidence_payload"]
    directory = collected(tmp_path)
    result = review.review(directory, client_factory=factory)
    _, plan, ledger = campaign._load(directory)
    model = factory(ledger=ledger, session_id="separate-oversize-test", base_url=plan["base_url"], model=GO_MODEL)
    before = ledger.summary()["requests"]
    call = review._call(ledger, result["batch_id"], "judge-a", "oversized-fixture", model,
                       {"item": {"evidence_payload": "x" * 50000}, "rubric": {}}, lambda obj: obj)
    assert call["state"] == "unresolved" and call["error_type"] == "ReviewInputTooLarge"
    assert ledger.summary()["requests"] == before


def test_partial_smoke_reviews_are_saved_but_not_applied_as_full_proof(tmp_path, fake_http):
    directory = tmp_path / "campaign"
    campaign.prepare(directory, project_root=ROOT, jobs=REUSE, runs=1, smoke_trials=3, max_requests=100)
    campaign.run(directory, mode="smoke", client_factory=factory)
    result = review.review(directory, client_factory=factory)
    assert len(result["reviews"]["reuse"]["items"]) == 3
    assert result["reviewed_reports"] == {} and not result["human_reviewed"]
