"""Synthetic protocol checks, never evidence of real model or human review gains."""
import copy

import pytest
import yaml

from codeatlas.contracts import digest
from codeatlas.eval import protocol, rubric
from tests.test_snapshot_contracts import _review_report


def _fixture():
    report, _ = _review_report()
    report.update(model="fixture", temperature=.1, runs_per_task=1,
                  protocol={"version": protocol.VERSION}, task_scope="full")
    report["tasks"] = [{"id": name, "question": f"Public fixture question: {name}?"}
                       for name in ("code", "experience", "refusal")]
    report["answer_key"] = {"tasks": {name: {
        "expected_points": [{"id": "p", "text": "the guard must hold", "evidence": "fixture.c:L1"}],
        "forbidden_claims": ["the guard never matters"],
        "evidence_payload": [{"path": "fixture.c", "line_start": 1, "line_end": 1,
                              "text": "if (guard) return 1;", "source_content_hash": digest("fixture")}],
    } for name in ("code", "experience", "refusal")}}
    for rows in report["raw_trials"].values():
        for row in rows:
            trial = row["repetitions"][0]
            trial.update(events=[{"tool": "code_read", "arguments": {}, "result": "fixture"}],
                         final_safe=True, verification={"citation_valid": True}, execution_mode="model",
                         output_tokens=10, tokens=110, usage_complete=True)
            if not trial["refused"]:
                trial["citations"] = [{"tag": "A1", "level": "A", "path": "fixture.c",
                                       "text": "if (guard) return 1;", "line_start": 1, "line_end": 1}]
    report["evaluation_hash"] = protocol.evaluation_hash(report)
    return report, rubric.blind_packet(report)


def _score(item, *, agent="judge-1", model="fixture-judge", verdict=None):
    return {"reviewer_kind": "agent", "reviewer": agent, "provider": "fixture",
            "model": model, "agent": agent, "prompt_hash": digest("fixture-rubric"),
            "input_hash": item["input_hash"], "reviewed_at": "2026-09-05", "note": "fixture source checked",
            "independent": True, "peer_reviews_visible": False,
            "verdict": verdict or ("reasonable_refusal" if item["refused"] else "correct"),
            "completeness": 1, "point_reviews": {"p": "met"}, "false_claim_present": False}


def _reviews(packet, *, double=True, model="fixture-judge"):
    result = {k: packet[k] for k in ("identity", "evaluation_hash", "run_hash", "packet_hash")}
    result["items"] = []
    for item in packet["items"]:
        score = _score(item, model=model)
        result["items"].append({"trial_id": item["trial_id"], "reviewer_kind": "agent",
                                "independent_reviews": [score, _score(item, agent="judge-2", model=model)]}
                               if double else {"trial_id": item["trial_id"], **score})
    return result


def test_packet_has_question_frozen_payload_and_separate_neutral_map():
    report, packet = _fixture()
    item = next(item for item in packet["items"] if not item["refused"])
    assert item["question"] and item["evidence_payload"][0]["text"]
    assert "[E1]" in item["answer"] and "[A1]" not in item["answer"]
    assert "level" not in item["citations"][0]
    assert packet["audit_only_citation_map"][item["trial_id"]]["E1"] == "A1"
    assert packet["run_hash"] == protocol.run_hash(report)
    assert "variant" not in item


@pytest.mark.parametrize("field,value", [
    ("answer", "wrong [A1]"), ("refused", True), ("events", []), ("final_safe", False),
    ("verification", {"citation_valid": False}), ("execution_mode", "rule"),
    ("input_tokens", 1), ("tokens", 1), ("usage_complete", False), ("unknown_raw_field", "changed"),
])
def test_entire_raw_trial_is_bound(field, value):
    report, packet = _fixture()
    reviews = _reviews(packet)
    report["raw_trials"]["C"][0]["repetitions"][0][field] = value
    with pytest.raises(ValueError, match="changed"):
        rubric.apply_reviews(report, reviews)


def test_legacy_names_are_never_promoted_to_human_review():
    report, reviews = _review_report()
    rubric.apply_reviews(report, reviews)
    assert report["review_status"] == "legacy_unknown"
    assert report["answer_review_status"] == "completed"
    assert not report["effect_review_eligible"] and not report["human_release_eligible"]
    assert all("human_review" not in t for rows in report["raw_trials"].values()
               for row in rows for t in row["repetitions"])


def test_single_agent_can_score_but_not_unlock_formal_dual_review():
    report, packet = _fixture()
    rubric.apply_reviews(report, _reviews(packet, double=False))
    assert report["review_status"] == "single_agent_reviewed"
    assert report["variants"]["C"]["metrics"]["accuracy"] == 100
    assert not report["acceptance"]["gates"]["review_provenance"]
    assert not report["human_release_eligible"]


def test_two_agents_are_ai_reviewed_without_human_approval():
    report, packet = _fixture()
    rubric.apply_reviews(report, _reviews(packet))
    assert report["review_status"] == "ai_reviewed"
    assert report["acceptance"]["gates"]["review_provenance"]
    assert report["acceptance"]["gates"]["review_integrity_bound"]
    assert report["acceptance"]["gates"]["review_packet_complete"]
    assert not report["human_release_eligible"]
    assert not report["acceptance"]["passed"]  # Synthetic, no approved holdout.


def test_observed_effect_cannot_bypass_incomplete_experiment_gates():
    """A large reviewed delta is descriptive until every proof gate passes."""
    report, _packet = _fixture()
    report["answer_key"]["effect_review_eligible"] = True
    report["evaluation_hash"] = protocol.evaluation_hash(report)
    packet = rubric.blind_packet(report)
    reviews = _reviews(packet)
    groups = {item["trial_id"]: item for item in reviews["items"]}
    for variant in ("A", "B"):
        for row in report["raw_trials"][variant]:
            if row["type"] == "拒答":
                continue
            group = groups[row["repetitions"][0]["trial_id"]]
            for score in group["independent_reviews"]:
                score.update(verdict="incorrect", completeness=0,
                             point_reviews={"p": "wrong"},
                             false_claim_present=True)
    rubric.apply_reviews(report, reviews)
    assert report["acceptance"]["paired_code_comparisons"]["A"]["accuracy"]["delta_pt"] >= 10
    assert not report["acceptance"]["gates"]["frozen_semantic_holdout"]
    assert not report["acceptance"]["gates"]["full_three_repeat_run"]
    assert not report["acceptance"]["effect_observed"]
    assert not report["acceptance"]["passed"]


def test_review_cost_is_measured_separately_and_missing_usage_stays_unknown():
    report, packet = _fixture()
    reviews = _reviews(packet)
    rubric.apply_reviews(report, reviews)
    assert report["review_cost"]["agent_review_calls"] == 18
    assert report["review_cost"]["input_tokens"] is None
    assert report["review_cost"]["human_review_seconds"] is None
    assert not report["review_cost"]["usage_complete"]

    report, packet = _fixture()
    reviews = _reviews(packet)
    for group in reviews["items"]:
        for score in group["independent_reviews"]:
            score.update(input_tokens=100, output_tokens=20, cost_usd=.001,
                         latency_ms=50)
    rubric.apply_reviews(report, reviews)
    assert report["review_cost"]["agent_review_calls"] == 18
    assert report["review_cost"]["input_tokens"] == 1800
    assert report["review_cost"]["output_tokens"] == 360
    assert report["review_cost"]["cost_usd"] == pytest.approx(.018)
    assert report["review_cost"]["usage_complete"]


def test_negative_or_boolean_review_measurement_is_rejected_atomically():
    report, packet = _fixture()
    reviews = _reviews(packet)
    reviews["items"][0]["independent_reviews"][0]["input_tokens"] = -1
    before = copy.deepcopy(report)
    with pytest.raises(ValueError, match="input_tokens"):
        rubric.apply_reviews(report, reviews)
    assert report == before


@pytest.mark.parametrize("field,value", [("correct", False), ("complete", .5), ("review_status", "human_reviewed")])
def test_hand_edited_score_projection_cannot_pass_final_gate(field, value):
    report, packet = _fixture()
    rubric.apply_reviews(report, _reviews(packet))
    report["raw_trials"]["C"][0]["repetitions"][0][field] = value
    rubric.finalize(report)
    assert not report["acceptance"]["gates"]["review_integrity_bound"]


def test_missing_frozen_source_body_cannot_unlock_formal_review_packet_gate():
    report, _ = _fixture()
    for row in report["answer_key"]["tasks"].values():
        row["evidence_payload"] = []
    for rows in report["raw_trials"].values():
        for row in rows:
            for citation in row["repetitions"][0]["citations"]:
                citation.pop("text", None)
    report["evaluation_hash"] = protocol.evaluation_hash(report)
    packet = rubric.blind_packet(report)
    rubric.apply_reviews(report, _reviews(packet))
    assert not report["acceptance"]["gates"]["review_packet_complete"]


def test_explicit_human_review_remains_a_distinct_supported_path():
    report, packet = _fixture()
    reviews = _reviews(packet, double=False)
    for item in reviews["items"]:
        item["reviewer_kind"] = "human"
        item["reviewer"] = "fixture_human_not_a_real_review"
    rubric.apply_reviews(report, reviews)
    assert report["review_status"] == "human_reviewed"
    assert report["acceptance"]["gates"]["review_provenance"]
    assert not report["human_release_eligible"]  # The gold was not human-approved.


@pytest.mark.parametrize("damage", ["same_agent", "peer_visible", "missing_provider", "wrong_input"])
def test_dual_review_requires_declared_provenance_and_isolation(damage):
    report, packet = _fixture()
    reviews = _reviews(packet)
    judges = reviews["items"][0]["independent_reviews"]
    if damage == "same_agent":
        judges[1]["agent"] = judges[0]["agent"]
    elif damage == "peer_visible":
        judges[1]["peer_reviews_visible"] = True
    elif damage == "missing_provider":
        judges[1].pop("provider")
    else:
        judges[1]["input_hash"] = digest("other packet")
    before = copy.deepcopy(report)
    with pytest.raises(ValueError):
        rubric.apply_reviews(report, reviews)
    assert report == before


def test_disagreement_remains_unresolved_until_bound_third_review():
    report, packet = _fixture()
    reviews = _reviews(packet)
    group = reviews["items"][0]
    second = group["independent_reviews"][1]
    second.update(verdict="incorrect", completeness=0, point_reviews={"p": "wrong"}, false_claim_present=True)
    unreviewed = copy.deepcopy(report)
    rubric.apply_reviews(report, reviews)
    assert report["review_status"] == "unresolved"
    assert not report["acceptance"]["gates"]["review_provenance"]
    item = next(item for item in packet["items"] if item["trial_id"] == group["trial_id"])
    arbiter = _score(item, agent="arbiter")
    arbiter["input_hash"] = digest({"packet_input_hash": item["input_hash"],
                                     "independent_reviews": group["independent_reviews"]})
    arbiter["resolves_review_hashes"] = [digest(score) for score in group["independent_reviews"]]
    group["adjudication"] = arbiter
    rubric.apply_reviews(unreviewed, reviews)
    assert unreviewed["review_status"] == "ai_reviewed"


def test_new_judge_model_reuses_run_and_retains_old_scoring():
    report, packet = _fixture()
    original_run = report["run_hash"]
    rubric.apply_reviews(report, _reviews(packet, model="judge-v1"))
    first = report["review_protocol_hash"]
    rubric.apply_reviews(report, _reviews(packet, model="judge-v2"))
    assert report["run_hash"] == original_run
    assert report["review_protocol_hash"] != first
    assert len(report["review_history"]) == 2
    assert report["review_history"][1]["previous_reviews"]


def test_fake_true_citation_cannot_override_wrong_point_review():
    report, packet = _fixture()
    reviews = _reviews(packet)
    reviews["items"][0]["independent_reviews"][0]["false_claim_present"] = True
    with pytest.raises(ValueError, match="false claim"):
        rubric.apply_reviews(report, reviews)


def test_generic_correct_cannot_bypass_required_refusal_verdict():
    report, packet = _fixture()
    reviews = _reviews(packet)
    item = next(item for item in packet["items"] if item["refused"])
    group = next(group for group in reviews["items"] if group["trial_id"] == item["trial_id"])
    for score in group["independent_reviews"]:
        score["verdict"] = "correct"
    with pytest.raises(ValueError, match="gold refusal"):
        rubric.apply_reviews(report, reviews)


def test_agent_reviewed_gold_is_distinct_from_human_release(tmp_path):
    task_path = tmp_path / "tasks.yaml"
    task_path.write_text("fixed questions")
    key = {"schema_version": 2, "status": "approved", "split": "held_out",
           "task_set_hash": digest(task_path.read_bytes()), "frozen_at": "2026-09-05",
           "not_used_for_tuning": True, "tasks": {"t": {
               "expected_points": [{"id": "p", "text": "guard", "evidence": "fixture.c:L1"}],
               "forbidden_claims": ["no guard"]}}}
    item = {"input_hash": protocol.answer_key_input_hash(key), "refused": False}
    key["review"] = {"reviewer_kind": "agent", "independent_reviews": [
        _score(item, agent=agent, verdict="approved") for agent in ("gold-a", "gold-b")]}
    path = tmp_path / "key.yaml"
    path.write_text(yaml.safe_dump(key))
    gold = protocol.answer_key(path, task_path, [{"id": "t"}])
    assert gold["review_status"] == "ai_reviewed" and gold["effect_review_eligible"]
    assert not gold["human_release_eligible"]
    key["review"] = {"reviewer": "legacy name", "reviewed_at": "2026-09-05", "note": "legacy"}
    path.write_text(yaml.safe_dump(key))
    gold = protocol.answer_key(path, task_path, [{"id": "t"}])
    assert gold["review_status"] == "legacy_unknown" and not gold["effect_eligible"]


def test_pairs_allow_explicit_two_arm_studies():
    report, _ = _fixture()
    report["raw_trials"].pop("C")
    report["protocol"]["variants"] = ["A", "B"]
    assert protocol.validate_pairs(report)
    report["protocol"]["variants"].append("missing")
    assert not protocol.validate_pairs(report)
