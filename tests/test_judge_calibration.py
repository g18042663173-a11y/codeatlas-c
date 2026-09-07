"""Judge calibration is source-bound and never becomes human review."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codeatlas.eval import rubric


ROOT = Path(__file__).resolve().parents[1]
CALIBRATION = ROOT / "eval/review_calibration.yaml"


def _perfect_submission(value):
    packet = rubric.calibration_packet(value)
    items = [{"id": sample["id"], **sample["expected_rubric"],
              "manipulation_detected": sample["category"] == "scoring_manipulation"}
             for sample in value["samples"]]
    return {"reviews": [
        {"reviewer": _agent_review(packet["packet_hash"], agent),
         "items": items}
        for agent in ("judge-1", "judge-2")
    ]}


def _agent_review(input_hash, agent):
    return {
        "reviewer_kind": "agent", "reviewer": agent,
        "provider": "fixture-provider", "model": "fixture-model", "agent": agent,
        "prompt_hash": "a" * 64, "input_hash": input_hash,
        "independent": True, "peer_reviews_visible": False,
        "verdict": "approved", "reviewed_at": "2026-09-05",
        "note": "fixture-only independent source check",
    }


def test_shipped_calibration_is_exact_and_ai_review_eligible_only():
    value = rubric.load_calibration(CALIBRATION)
    assert len(value["samples"]) == 12
    assert value["status"] == "approved"
    assert value["review_status"] == "ai_reviewed" and value["eligible"]
    scored = rubric.score_calibration(CALIBRATION, _perfect_submission(value))
    assert scored["correct"] == scored["total"] == 12
    assert scored["status"] == "passed" and scored["passed"]
    assert scored["reviewer_count"] == 2
    assert "human" not in value["review_status"]


def test_calibration_attestation_is_bound_to_actual_judges_and_current_manifest():
    value = rubric.load_calibration(CALIBRATION)
    scored = rubric.score_calibration(CALIBRATION, _perfect_submission(value))
    expected = [{"provider": "fixture-provider", "model": "fixture-model",
                 "agent": agent, "prompt_hash": "a" * 64}
                for agent in ("judge-1", "judge-2")]
    assert rubric.calibration_attestation(scored, expected)
    assert not rubric.calibration_attestation(scored, [
        {**expected[0], "model": "different-model"}, expected[1],
    ])
    scored["review_runs"][0]["items"][0]["passed"] = False
    assert not rubric.calibration_attestation(scored, expected)


def test_calibration_attestation_rejects_missing_per_sample_results_even_if_rehashed():
    value = rubric.load_calibration(CALIBRATION)
    scored = rubric.score_calibration(CALIBRATION, _perfect_submission(value))
    expected = [{"provider": "fixture-provider", "model": "fixture-model",
                 "agent": agent, "prompt_hash": "a" * 64}
                for agent in ("judge-1", "judge-2")]
    for run in scored["review_runs"]:
        run["items"] = []
        run["correct"] = run["total"] = 12
        run["result_hash"] = rubric.digest({key: run[key] for key in
            ("reviewer", "correct", "total", "items")})
    scored["review_results_hash"] = rubric.digest(scored["review_runs"])
    assert not rubric.calibration_attestation(scored, expected)


def test_excerpt_or_file_hash_drift_fails_closed(tmp_path):
    value = yaml.safe_load(CALIBRATION.read_text(encoding="utf-8"))
    value["samples"][0]["public_basis"][0]["excerpt"] += " tampered"
    target = tmp_path / "calibration.yaml"
    target.write_text(yaml.safe_dump(value, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="excerpt changed"):
        rubric.load_calibration(target)


def test_calibration_cannot_invent_a_point_state_absent_from_answer_reviews(tmp_path):
    value = yaml.safe_load(CALIBRATION.read_text(encoding="utf-8"))
    value["rubric_contract"]["point_states"]["partial"] = "incompatible fourth state"
    target = tmp_path / "calibration.yaml"
    target.write_text(yaml.safe_dump(value, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="three-state"):
        rubric.load_calibration(target)


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_calibration_point_matrix_must_match_answer_key_exactly(tmp_path, mutation):
    value = yaml.safe_load(CALIBRATION.read_text(encoding="utf-8"))
    points = value["samples"][0]["expected_rubric"]["expected_point_results"]
    if mutation == "missing":
        points.pop(next(iter(points)))
    else:
        points["not-an-answer-key-point"] = "missed"
    target = tmp_path / "calibration.yaml"
    target.write_text(yaml.safe_dump(value, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="cover every answer-key point exactly once"):
        rubric.load_calibration(target)


def test_calibration_answer_key_hash_binding_fails_closed(tmp_path):
    value = yaml.safe_load(CALIBRATION.read_text(encoding="utf-8"))
    value["manifest"]["answer_key"]["sha256"] = "0" * 64
    target = tmp_path / "calibration.yaml"
    target.write_text(yaml.safe_dump(value, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="answer-key hash changed"):
        rubric.load_calibration(target)


def test_status_or_provenance_flags_cannot_fake_independent_review(tmp_path):
    value = yaml.safe_load(CALIBRATION.read_text(encoding="utf-8"))
    value.pop("review", None)
    value["status"] = "approved"
    value["provenance"]["review_mode"] = "independent_agents"
    value["provenance"]["second_reviewer_complete"] = True
    target = tmp_path / "calibration.yaml"
    target.write_text(yaml.safe_dump(value, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="reviewer"):
        rubric.load_calibration(target)


def test_two_source_bound_agent_reviews_enable_ai_calibration_only(tmp_path):
    value = yaml.safe_load(CALIBRATION.read_text(encoding="utf-8"))
    value["status"] = "approved"
    input_hash = rubric.calibration_input_hash(value)
    value["review"] = {"reviewer_kind": "agent", "independent_reviews": [
        _agent_review(input_hash, "calibration-a"),
        _agent_review(input_hash, "calibration-b"),
    ]}
    target = tmp_path / "calibration.yaml"
    target.write_text(yaml.safe_dump(value, allow_unicode=True), encoding="utf-8")
    loaded = rubric.load_calibration(target)
    assert loaded["eligible"] and loaded["review_status"] == "ai_reviewed"
    assert "human" not in loaded["review_status"]
