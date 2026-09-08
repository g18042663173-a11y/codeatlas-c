"""Offline protocol tests: fake labels prove plumbing, never real judge accuracy."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from codeatlas.contracts import INPUT_BUDGET, digest, estimated_tokens
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


def test_frozen_job_selection_does_not_rescore_other_jobs(tmp_path, fake_http):
    directory = tmp_path / "campaign"
    campaign.prepare(directory, project_root=ROOT,
                     jobs=[REUSE[0], {**REUSE[0], "id": "other"}], runs=1,
                     smoke_trials=3, max_requests=400)
    campaign.run(directory, mode="full", client_factory=factory)
    base = review.review(directory, attempt_id="base", client_factory=factory)
    report = campaign.report(directory)["reports"]["reuse"]
    packet = rubric.blind_packet(report)
    selection = {"jobs": ["reuse"], "base_batch_id": base["batch_id"],
                 "run_hashes": {"reuse": report["run_hash"]},
                 "packet_hashes": {"reuse": packet["packet_hash"]}}
    selection["selection_hash"] = digest(selection)
    path = tmp_path / "selection.json"
    path.write_text(json.dumps(selection), encoding="utf-8")
    before = len(fake_http["requests"])
    scope = dict(scope_id="bounded", max_requests=60, manifest_hash="f" * 64)
    with pytest.raises(campaign.CampaignError, match="budget scope"):
        review.review(directory, attempt_id="missing-scope", base_attempt_id="base",
                      job_ids=["reuse"], selection_manifest=path, client_factory=factory)
    with pytest.raises(campaign.CampaignError, match="reuse its base"):
        review.review(directory, attempt_id="base", base_attempt_id="base",
                      job_ids=["reuse"], selection_manifest=path, budget_scope=scope, client_factory=factory)
    assert len(fake_http["requests"]) == before
    result = review.review(directory, attempt_id="repair", base_attempt_id="base",
                           job_ids=["reuse"], selection_manifest=path,
                           budget_scope=scope, client_factory=factory)
    assert set(result["reviewed_reports"]) == {"reuse"}
    assert result["binding"]["selection"]["base_batch_id"] == base["batch_id"]
    assert len(fake_http["requests"]) - before == 24 + 36
    after = len(fake_http["requests"])
    review.review(directory, attempt_id="repair", base_attempt_id="base", resume=True,
                  job_ids=["reuse"], selection_manifest=path,
                  budget_scope=scope, client_factory=factory)
    assert len(fake_http["requests"]) == after
    with pytest.raises(campaign.CampaignError, match="already frozen"):
        review.review(directory, attempt_id="repair", base_attempt_id="base", resume=True,
                      job_ids=["reuse"], selection_manifest=path,
                      budget_scope={**scope, "max_requests": 61}, client_factory=factory)
    selection["purpose"] = "changed selection identity"
    selection["selection_hash"] = digest({k: v for k, v in selection.items() if k != "selection_hash"})
    path.write_text(json.dumps(selection), encoding="utf-8")
    with pytest.raises(campaign.CampaignError, match="already frozen"):
        review.review(directory, attempt_id="repair", base_attempt_id="base", resume=True,
                      job_ids=["reuse"], selection_manifest=path,
                      budget_scope=scope, client_factory=factory)
    assert len(fake_http["requests"]) == after
    selection["packet_hashes"]["reuse"] = "a" * 64
    selection["selection_hash"] = digest({k: v for k, v in selection.items() if k != "selection_hash"})
    path.write_text(json.dumps(selection), encoding="utf-8")
    with pytest.raises(campaign.CampaignError, match="packets changed"):
        review.review(directory, attempt_id="tamper", base_attempt_id="base",
                      job_ids=["reuse"], selection_manifest=path,
                      budget_scope=scope, client_factory=factory)
    assert len(fake_http["requests"]) == after


def test_selective_review_cli_requires_and_passes_explicit_scope(tmp_path, monkeypatch):
    from codeatlas.cli import app
    seen = []
    monkeypatch.setattr(review, "review", lambda *args, **kwargs: seen.append(kwargs) or {"status": "not_run"})
    args = ["eval", "proof", "review", "--campaign", str(tmp_path), "--job", "reuse",
            "--base-attempt", "base", "--attempt", "repair", "--selection-manifest", "selection.json"]
    response = CliRunner().invoke(app, args)
    assert response.exit_code != 0 and not seen
    scope = dict(scope_id="bounded", max_requests=800, manifest_hash="f" * 64)
    scope_path = tmp_path / "scope.json"
    scope_path.write_text(json.dumps(scope), encoding="utf-8")
    response = CliRunner().invoke(app, [*args, "--budget-scope", str(scope_path)])
    assert response.exit_code == 0 and seen[0]["budget_scope"] == scope


def test_review_export_is_git_sized_and_keeps_trial_hashes(tmp_path, fake_http):
    directory = collected(tmp_path)
    result = review.review(directory, client_factory=factory)
    assert result["status"] == "completed"
    output = tmp_path / "curated.json"
    manifest = tmp_path / "experiments.yaml"
    manifest.write_text(yaml.safe_dump({"experiments": [{
        "id": "reuse", "kind": "knowledge_reuse",
        "manifest": "eval/knowledge_reuse.yaml",
        "answer_key": "eval/knowledge_reuse.answers.yaml",
        "report": str(output),
    }]}), encoding="utf-8")
    review.export_reviewed_reports(directory, attempt_id=None,
                                   experiments_path=str(manifest))
    curated = json.loads(output.read_text(encoding="utf-8"))
    assert "raw_trials" not in curated and "blind_review" not in curated
    assert len(curated["trial_results"]) == curated["executed_trial_count"] == 18
    assert curated["curated_projection"]["trial_results_hash"] == digest(
        curated["trial_results"])
    assert all(row["raw_trial_hash"] and row["selected_review_hash"]
               for row in curated["trial_results"])
    assert output.stat().st_size < 100_000
    from codeatlas.eval.acceptance_eval import _verify_curated_effect_report
    assert _verify_curated_effect_report(curated)["ok"] is True
    curated["trial_results"][0]["correct"] = not curated["trial_results"][0]["correct"]
    assert _verify_curated_effect_report(curated)["ok"] is False


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


def test_sealed_model_qualification_can_score_a_new_bounded_projection(tmp_path, fake_http):
    directory = collected(tmp_path)
    qualified = review.review(directory, client_factory=factory, attempt_id="sealed-qualification",
                              calibration_only=True, judge_model=GO_MODEL)
    count = len(fake_http["requests"])
    completed = review.review(
        directory, client_factory=factory, attempt_id="sealed-qualification.semantic-review-v1",
        judge_model=GO_MODEL, projection_version=review.SEMANTIC_REVIEW_PROJECTION,
        qualification_attestation=qualified["judge_calibration"],
        qualification_batch_id=qualified["batch_id"], workers=2,
    )
    assert completed["status"] == "completed"
    assert completed["binding"]["projection_audit"]["oversize_count"] == 0
    assert completed["binding"]["qualified_by_batch_id"] == qualified["batch_id"]
    assert len([row for row in fake_http["requests"][count:]
                if row["kind"] == "calibration"]) == 0
    scored = completed["reviewed_reports"]["reuse"]
    assert scored["acceptance"]["gates"]["judge_calibration"] is True


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
    peer_view = arbiters[0]["payload"]["independent_reviews"]
    assert [row["review_hash"] for row in peer_view] == [digest(row) for row in originals]
    assert all("raw_response" not in row and "session_id" not in row for row in peer_view)
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


def test_attempt_audit_exports_format_gaps_without_claiming_effect(tmp_path, fake_http):
    directory = collected(tmp_path)
    fake_http["mode"] = "malformed"
    result = review.review(directory, attempt_id="audit-format", client_factory=factory)
    assert result["status"] == "unresolved"
    output = tmp_path / "attempt-audit.json"
    exported = review.export_attempt_audit(
        directory, attempt_id="audit-format", out=output)
    audit = json.loads(output.read_text())
    assert exported["status"] == "exported" and output.with_suffix(".md").exists()
    assert audit["claim_status"] == "insufficient_evidence"
    assert audit["counts"]["independent_scores_repairable_invalid"] == 1
    assert audit["counts"]["independent_scores_transport_or_unknown"] == 0
    assert audit["failure_types"]["independent_scores"] == {"JSONDecodeError": 1}
    assert audit["counts"]["unresolved_trials"] == 1
    assert audit["audit_hash"] == digest({k: v for k, v in audit.items() if k != "audit_hash"})


def test_attempt_audit_does_not_call_a_calibration_failure_review_complete(tmp_path, fake_http):
    directory = collected(tmp_path)
    fake_http["mode"] = "calibration_fail"
    result = review.review(
        directory, attempt_id="audit-calibration-failure", client_factory=factory)
    assert result["status"] == "unresolved" and "reviews" not in result
    output = tmp_path / "calibration-audit.json"
    review.export_attempt_audit(
        directory, attempt_id="audit-calibration-failure", out=output)
    audit = json.loads(output.read_text())
    assert audit["review_collection_status"] == "not_reviewed"
    assert audit["claim_status"] == "not_reviewed"
    assert audit["counts"]["answer_trials"] == 0


def test_terminal_review_collection_requires_two_valid_scores_and_completed_arbitration():
    score = {
        "model_generated": True, "verdict": "correct", "completeness": 1,
        "point_reviews": {"p": "met"}, "false_claim_present": False,
        "citation_relation": "supported",
    }
    other = {**score, "verdict": "incomplete", "completeness": 0,
             "point_reviews": {"p": "missing"}}
    unresolved_arbiter = {
        **score, "verdict": "unresolved", "completeness": None,
        "point_reviews": {"p": "unresolved"}, "false_claim_present": None,
        "citation_relation": "unresolved",
    }
    value = {"reviews": {"job": {"items": [{
        "trial_id": "trial", "independent_reviews": [score, other],
        "adjudication": unresolved_arbiter,
    }]}}}
    assert review._terminal_review_collection(value) is True
    value["reviews"]["job"]["items"][0]["adjudication"]["model_generated"] = False
    assert review._terminal_review_collection(value) is False


def test_curated_boundary_never_reuses_pre_run_not_configured_message():
    completed = review._curated_boundary(0)
    unresolved = review._curated_boundary(1)
    assert "完成双评" in completed and "未配置模型" not in completed
    assert "1 个语义判断未决" in unresolved and "主对照保持未测" in unresolved


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


def test_semantic_review_projection_keeps_explicit_and_oracle_evidence_but_drops_rank_noise():
    common = {
        "repository": "https://example.test/repo", "revision": "a" * 40,
        "knowledge_set_id": "ks-1", "source_content_hash": "b" * 64,
        "build_config_hash": "c" * 64,
    }
    def anchor(tag, symbol):
        return {"tag": tag, "node_id": 4, "source_ref": f"src/a.c#L1-L3",
                "score": 0.9, "rerank_score": 0.8,
                "provenance": {**common, "source_ref": "src/a.c#L1-L3",
                    "symbol": symbol, "usr": f"c:@F@{symbol}", "path": "src/a.c",
                    "line_start": 1, "line_end": 3, "definition_hash": "d" * 64,
                    "stale_status": "not_applicable"}}
    source = {"tag": "E3", "kind": "pinned_source", "path": "src/a.c",
              "lines": "1-3", "file_sha256": "e" * 64,
              "text": "/* explanation */\nint answer(void) {\n  // fixed\n  return 42;\n}\n"}
    item = {"input_hash": "f" * 64, "answer": "It returns 42 [E1].",
            "citations": [], "evidence_payload": [anchor("E1", "answer"),
                anchor("E2", "unrelated"), source],
            "answer_key": {"expected_points": [{"id": "p", "text": "returns 42"}],
                "forbidden_claims": [],
                "evidence_payload": [{key: value for key, value in source.items()
                                      if key not in {"tag"}}]}}
    before = copy.deepcopy(item)
    projected = review._judge_item(item, review.SEMANTIC_REVIEW_PROJECTION)

    assert item == before
    assert projected["projection"]["original_input_hash"] == item["input_hash"]
    assert [row[0] for row in projected["evidence_payload"]["anchor_rows"]] == ["E1"]
    assert "score" not in str(projected["evidence_payload"])
    compact_source = projected["evidence_payload"]["other_evidence"][0]
    assert "return 42;" in compact_source["text"]
    assert "explanation" not in compact_source["text"] and "fixed" not in compact_source["text"]
    assert projected["answer_key"]["evidence_payload"][0]["$ref"].endswith("/0")


def test_semantic_review_projection_enforces_the_shared_input_budget():
    provenance = {"repository": "r", "revision": "v", "knowledge_set_id": "ks",
        "source_content_hash": "s" * 64, "build_config_hash": "b" * 64,
        "source_ref": "large.c#L1-L400", "symbol": "large", "usr": "c:@F@large",
        "path": "large.c", "line_start": 1, "line_end": 400,
        "definition_hash": "d" * 64, "stale_status": "not_applicable"}
    ranked = [{"tag": f"E{number}", "source_ref": provenance["source_ref"],
               "node_id": number, "score": 1 / number,
               "provenance": {**provenance, "symbol": f"symbol_{number}"}}
              for number in range(1, 31)]
    source = {"tag": "E31", "kind": "pinned_source", "path": "large.c",
              "lines": "1-400", "file_sha256": "f" * 64,
              "text": "\n".join(f"/* comment {i} */ int v{i} = {i};" for i in range(400))}
    oracle = {key: value for key, value in source.items() if key != "tag"}
    item = {"input_hash": "i" * 64, "task_id": "large", "task_type": "语义检索",
            "question": "what happens", "answer": "The relevant anchor is [E1].",
            "citations": ranked + [source], "evidence_payload": ranked + [source],
            "refused": False, "answer_key": {"expected_points": [{"id": "p", "text": "fact"}],
                "forbidden_claims": [], "evidence_payload": [oracle]}}
    payload = {"item": review._judge_item(item, review.SEMANTIC_REVIEW_PROJECTION),
               "rubric": {**rubric.RUBRIC, "judge_guidance": review.JUDGE_GUIDANCE,
                           "response_schema": review.ANSWER_SCHEMA}}
    size = estimated_tokens(review.SYSTEM + json.dumps(
        payload, ensure_ascii=False, sort_keys=True)) + 32
    assert size <= INPUT_BUDGET


def test_single_reviewer_calibration_is_only_allowed_for_prequalified_arbiter(tmp_path):
    # Public score_calibration still defaults to two reviews.  A single review
    # is an explicit internal path used only after the two-judge attestation.
    with pytest.raises(ValueError, match="at least 2"):
        rubric.score_calibration(ROOT / "eval/review_calibration_v3.yaml", {"reviews": []})
    with pytest.raises(ValueError, match="at least 1"):
        rubric.score_calibration(ROOT / "eval/review_calibration_v3.yaml",
                                 {"reviews": []}, minimum_reviews=1)


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


def test_knowledge_review_projection_is_arm_blind_and_bounded():
    sentinel = "RAW_TREATMENT_MUST_NOT_REACH_JUDGE " * 1200
    item = {
        "trial_id": "trial-1", "task_id": "case.question", "task_type": "experience",
        "question": "What happened?", "answer": "The checked condition held. [E1][E2][E3]",
        "refused": False, "input_hash": "a" * 64, "source_verification": {"status": "verified"},
        "citations": [],
        "evidence_payload": [
            {"tag": "E1", "kind": "source", "path": "src/a.c", "line_start": 10,
             "line_end": 14, "text": "if (ready) { return 0; }"},
            {"tag": "E2", "kind": "material", "text": sentinel},
            {"tag": "E3", "kind": "source", "path": "src/a.c", "line_start": 80,
             "line_end": 82, "text": "unrelated();"},
        ],
        "answer_key": {
            "expected_points": [{"id": "P1", "text": "ready is checked", "evidence": ["S"]}],
            "forbidden_claims": ["ready is ignored"],
            "forbidden_evidence": [{"claim_index": 0, "evidence": ["S"]}],
            "evidence_payload": [
                {"id": "S", "tag": "K1", "kind": "source", "path": "src/a.c",
                 "line_start": 10, "line_end": 14, "text": "if (ready) { return 0; }"},
                {"id": "UNUSED", "kind": "source", "path": "src/unused.c",
                 "line_start": 1, "line_end": 1, "text": "unused"},
            ],
            "executable_verification": {"observation_evidence": [], "build_evidence": []},
            "reviewer_kind": "agent", "status": "ai_reviewed",
        },
    }
    value = review._judge_item(item, review.KNOWLEDGE_REVIEW_PROJECTION)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    assert sentinel not in encoded and "UNUSED" not in encoded
    assert value["answer"] == item["answer"]
    assert value["answer_key"]["expected_points"] == item["answer_key"]["expected_points"]
    candidates = {row["tag"]: row for row in value["evidence_payload"]["candidate_evidence"]}
    assert candidates["E1"]["oracle_refs"] == ["S"]
    assert candidates["E2"]["content_visibility"] == "withheld_experimental_treatment"
    assert candidates["E3"]["content_visibility"] == "withheld_experimental_treatment"
    legacy = review._judge_item(item, review.KNOWLEDGE_REVIEW_PROJECTION_V1)
    legacy_candidates = {
        row["tag"]: row for row in legacy["evidence_payload"]["candidate_evidence"]
    }
    assert legacy_candidates["E3"]["oracle_refs"] == ["S"]
    payload = {"item": value, "rubric": {**rubric.RUBRIC,
        "judge_guidance": review.JUDGE_GUIDANCE, "response_schema": review.ANSWER_SCHEMA}}
    assert estimated_tokens(review.SYSTEM + json.dumps(payload, ensure_ascii=False, sort_keys=True)) + 32 < INPUT_BUDGET


def test_format_gap_recovery_preserves_the_base_bounded_projection():
    from codeatlas.eval import review_recovery
    assert review_recovery._recovery_projection({
        "projection_version": review.SEMANTIC_REVIEW_PROJECTION,
    }) == review.SEMANTIC_REVIEW_PROJECTION
    assert review_recovery._recovery_projection({
        "projection_version": review.KNOWLEDGE_REVIEW_PROJECTION,
    }) == review.KNOWLEDGE_REVIEW_PROJECTION
    assert review_recovery._recovery_projection({
        "projection_version": review.KNOWLEDGE_REVIEW_PROJECTION_V1,
    }) == review.KNOWLEDGE_REVIEW_PROJECTION_V1
    with pytest.raises(campaign.CampaignError, match="bounded semantic projection"):
        review_recovery._recovery_projection({"projection_version": "unbounded"})


def test_format_recovery_does_not_retry_unknown_adjudication_or_dual_slots():
    from codeatlas.eval import review_recovery
    valid = {"model_generated": True, "verdict": "correct", "completeness": 1,
             "point_reviews": {"p": "met"}, "false_claim_present": False,
             "citation_relation": "supported"}
    disagree = {**valid, "verdict": "incomplete", "completeness": 0,
                "point_reviews": {"p": "missing"}}
    unknown = {"model_generated": False, "execution_status": "unresolved",
               "error_type": "TimeoutError", "verdict": "unresolved"}
    group = {"trial_id": "unknown", "independent_reviews": [valid, disagree],
             "adjudication": unknown}
    assert not review_recovery._recovery_arbitration_allowed(
        "job", group, repairable_adjudications=set(), replacement_trials=set())
    assert review_recovery._recovery_arbitration_allowed(
        "job", group,
        repairable_adjudications={("job", "unknown")}, replacement_trials=set())

    invalid = {**unknown, "error_type": "ValueError"}
    gaps = review_recovery.repairable_summary({"reviews": {"job": {"items": [{
        "trial_id": "dual", "independent_reviews": [invalid, invalid],
    }]}}})
    assert gaps["dual_independent_gap_trials"] == 1
    assert gaps["single_role_recoverable_count"] == 0
    with pytest.raises(campaign.CampaignError, match="both independent review slots"):
        review_recovery._validate_recovery_gaps(gaps)


def test_partial_smoke_reviews_are_saved_but_not_applied_as_full_proof(tmp_path, fake_http):
    directory = tmp_path / "campaign"
    campaign.prepare(directory, project_root=ROOT, jobs=REUSE, runs=1, smoke_trials=3, max_requests=100)
    campaign.run(directory, mode="smoke", client_factory=factory)
    result = review.review(directory, client_factory=factory)
    assert len(result["reviews"]["reuse"]["items"]) == 3
    assert result["reviewed_reports"] == {} and not result["human_reviewed"]


def test_trial_selection_calls_only_selected_and_preserves_proved_old_scores(tmp_path, fake_http, monkeypatch):
    from codeatlas.eval import wiki_review_projection as wiki
    from codeatlas.eval.review_inheritance import validate_inheritance
    directory = collected(tmp_path)
    base = review.review(directory, attempt_id="trial-base", client_factory=factory)
    _, _, ledger = campaign._load(directory)
    source = campaign.report(directory)["reports"]["reuse"]
    packet = rubric.blind_packet(source)
    chosen = packet["items"][0]["trial_id"]
    old, audit = validate_inheritance(ledger, base["batch_id"], base["binding"], base, {"reuse": packet}, [chosen])
    # This test covers scheduling, not source hydration (independently tested).
    original_packet = rubric.blind_packet
    def without_fixture_descriptor(report):
        spec = report.pop("review_evidence", None)
        try:
            return original_packet(report)
        finally:
            if spec is not None:
                report["review_evidence"] = spec
    monkeypatch.setattr(rubric, "blind_packet", without_fixture_descriptor)
    selection = {"base_batch_id": base["batch_id"], "jobs": ["reuse"],
        "run_hashes": {"reuse": packet["run_hash"]}, "packet_hashes": {"reuse": packet["packet_hash"]},
        "trial_ids": {"reuse": [chosen]}, "evidence_repairs": {"reuse": {"selected_trial_ids": [chosen]}},
        "projection_version": wiki.VERSION, "wiki_projection_runtime_hash": digest(Path(wiki.__file__).read_bytes()),
        "inheritance_audit_hash": audit["audit_hash"]}
    selection["selection_hash"] = digest(selection)
    path = tmp_path / "selection.json"
    path.write_text(json.dumps(selection))
    before = len(fake_http["requests"])
    result = review.review(directory, attempt_id="trial-repair", client_factory=factory,
        judge_model=GO_MODEL, qualification_attestation=base["judge_calibration"],
        qualification_batch_id=base["batch_id"], projection_version=wiki.VERSION,
        job_ids=["reuse"], base_attempt_id="trial-base", selection_manifest=path,
        budget_scope={"scope_id": "test-scope", "max_requests": 100, "manifest_hash": digest(selection)})
    calls = fake_http["requests"][before:]
    assert len(calls) == 2 and all(c["payload"]["item"]["trial_id"] == chosen for c in calls)
    assert result["status"] == "completed"
    inherited = {g["trial_id"]: g for g in old["reuse"]}
    for group in result["reviews"]["reuse"]["items"]:
        if group["trial_id"] != chosen:
            assert group == inherited[group["trial_id"]]
    assert result["inheritance_audit"]["audit_hash"] == audit["audit_hash"]
