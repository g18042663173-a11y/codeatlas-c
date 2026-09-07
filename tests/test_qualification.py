from __future__ import annotations

from pathlib import Path
import json

import pytest
import yaml

from codeatlas.eval import qualification, review_recovery, rubric
from codeatlas.contracts import digest
from codeatlas.llm.ledger import RequestLedger


ROOT = Path(__file__).resolve().parents[1]


def _checked(tmp_path: Path) -> dict:
    return {
        "policy": {
            "primary": {
                "model": "gpt-5.6-terra", "calibration": "eval/v4a.yaml",
                "attempt": "v4a-terra",
            },
            "fallback": {
                "model": "gpt-5.6-sol", "calibration": "eval/v4b.yaml",
                "attempt": "v4b-sol", "activate_only_when": "judge_calibration_failed",
            },
            "workers": 2, "transport_retries": 0, "max_new_requests": 1522,
        },
        "identity": {"policy_hash": "p" * 64, "policy_sha256": "s" * 64},
        "path": str(tmp_path / "policy.yaml"),
        "loaded": {},
    }


def _run(tmp_path: Path, monkeypatch, responses: dict[str, list[dict]]) -> tuple[dict, list[dict]]:
    ledger = RequestLedger(tmp_path / "ledger.sqlite3", max_requests=5000)
    (tmp_path / "eval").mkdir()
    for name in ("v4a.yaml", "v4b.yaml"):
        (tmp_path / "eval" / name).write_text("schema_version: 4\n", encoding="utf-8")
    monkeypatch.setattr(qualification, "validate_policy", lambda *_a, **_k: _checked(tmp_path))
    monkeypatch.setattr(qualification.proof_campaign, "_load",
                        lambda _directory: (tmp_path, {}, ledger))
    calls = []

    def fake_review(_directory, **kwargs):
        calls.append(kwargs)
        return responses[kwargs["judge_model"]].pop(0)

    monkeypatch.setattr(qualification.campaign_review, "review", fake_review)
    result = qualification.run(tmp_path / "policy.yaml", campaign_dir=tmp_path,
                               project_root=tmp_path)
    return result, calls


def test_terra_passes_without_opening_sealed_sol(tmp_path: Path, monkeypatch):
    result, calls = _run(tmp_path, monkeypatch, {
        "gpt-5.6-terra": [{"status": "calibrated"}, {
            "status": "completed", "unresolved_trial_count": 0,
            "disagreement_count": 4, "judge_request_count": 1000,
        }],
        "gpt-5.6-sol": [],
    })

    assert result["selected"] == "primary" and result["status"] == "completed"
    assert [row["judge_model"] for row in calls] == ["gpt-5.6-terra"] * 2
    assert calls[0]["calibration_only"] is True
    assert calls[1]["calibration_only"] is False
    assert calls[1]["attempt_id"] == "v4a-terra.semantic-review-v1"
    assert calls[1]["projection_version"] == "semantic-review-v1"
    assert calls[1]["qualification_attestation"] is None
    assert all(row["workers"] == 2 and row["transport_retries"] == 0 for row in calls)
    assert all(row["global_max_requests"] == 1522 for row in calls)


def test_sol_opens_only_after_complete_terra_semantic_failure(tmp_path: Path, monkeypatch):
    result, calls = _run(tmp_path, monkeypatch, {
        "gpt-5.6-terra": [{"status": "unresolved", "reason": "judge_calibration_failed"}],
        "gpt-5.6-sol": [{"status": "calibrated"}, {
            "status": "completed", "unresolved_trial_count": 0,
            "disagreement_count": 0, "judge_request_count": 996,
        }],
    })

    assert result["selected"] == "fallback" and result["status"] == "completed"
    assert [row["judge_model"] for row in calls] == [
        "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-sol",
    ]
    assert calls[-1]["attempt_id"] == "v4b-sol.semantic-review-v1"


@pytest.mark.parametrize("reason", ["calibration_response_unresolved", "model_not_configured"])
def test_transport_or_configuration_failure_does_not_open_sol(
        tmp_path: Path, monkeypatch, reason: str):
    result, calls = _run(tmp_path, monkeypatch, {
        "gpt-5.6-terra": [{"status": "unresolved", "reason": reason}],
        "gpt-5.6-sol": [],
    })

    assert result["selected"] is None and result["status"] == "unresolved"
    assert [row["judge_model"] for row in calls] == ["gpt-5.6-terra"]


def test_public_summary_removes_calibration_and_review_payloads():
    raw = {
        "status": "completed", "identity": {"policy_sha256": "x"},
        "selected": "primary", "request_budget": {"used": 9},
        "primary": {
            "status": "calibrated", "reason": None, "batch_id": "b",
            "judge_request_count": 24,
            "judge_calibration": {"passed": True, "reviewer_count": 2,
                "review_runs": [{"reviewer": {"reviewer": "judge-a", "model": "terra"},
                                 "metrics": {"point_accuracy": 1}, "gates": {"x": True}}]},
            "calibration_runs": [{"raw_response": "must not leak"}],
        },
        "fallback": None,
        "review": {"status": "completed", "batch_id": "r",
                   "unresolved_trial_count": 0, "disagreement_count": 2,
                   "judge_request_count": 980,
                   "reviewed_reports": {"secret": "large payload"}},
    }
    public = qualification.public_summary(raw)

    rendered = str(public)
    assert "must not leak" not in rendered and "large payload" not in rendered
    assert public["primary"]["passed"] is True
    assert public["review"]["unresolved_trial_count"] == 0
    assert public["report_hash"]


def test_repairable_summary_separates_format_gaps_from_semantic_unresolved():
    resolved = {
        "model_generated": True, "verdict": "correct", "completeness": 1,
        "point_reviews": {"p": "met"}, "false_claim_present": False,
        "citation_relation": "supported",
    }
    invalid = {
        "model_generated": False, "execution_status": "unresolved",
        "error_type": "ValueError", "verdict": "unresolved",
    }
    semantic = {
        "model_generated": True, "verdict": "unresolved", "completeness": None,
        "point_reviews": {"p": "unresolved"}, "false_claim_present": None,
        "citation_relation": "unresolved",
    }
    result = {"reviews": {"job": {"items": [
        {"trial_id": "format", "independent_reviews": [invalid, resolved]},
        {"trial_id": "semantic", "independent_reviews": [semantic, semantic]},
        {"trial_id": "arbiter", "independent_reviews": [resolved, {
            **resolved, "verdict": "incomplete", "completeness": 0,
            "point_reviews": {"p": "missing"},
        }], "adjudication": {**invalid, "error_type": "ReviewInputTooLarge"}},
    ]}}}

    summary = review_recovery.repairable_summary(result)

    assert summary["repairable_count"] == 2
    assert summary["independent_gaps"] == [
        {"job_id": "job", "trial_id": "format", "slots": [0]}]
    assert summary["adjudication_gaps"] == [
        {"job_id": "job", "trial_id": "arbiter"}]
    assert summary["nonrepairable_unresolved"] == [
        {"job_id": "job", "trial_id": "semantic"}]


def test_semantic_unresolved_recovery_is_terminal_and_not_retried(tmp_path: Path, monkeypatch):
    ledger = RequestLedger(tmp_path / "ledger.sqlite3", max_requests=5000)
    monkeypatch.setattr(qualification, "validate_policy", lambda *_a, **_k: _checked(tmp_path))
    monkeypatch.setattr(qualification.proof_campaign, "_load",
                        lambda _directory: (tmp_path, {}, ledger))
    semantic = {
        "model_generated": True, "verdict": "unresolved", "completeness": None,
        "point_reviews": {"p": "unresolved"}, "false_claim_present": None,
        "citation_relation": "unresolved",
    }
    stored = {
        "status": "unresolved", "identity": _checked(tmp_path)["identity"],
        "selected": "fallback",
        "primary": {"status": "unresolved", "reason": "judge_calibration_failed"},
        "fallback": {"status": "calibrated", "judge_calibration": {"passed": True},
                     "batch_id": "qualified"},
        "review": {
            "status": "unresolved", "unresolved_trial_count": 1,
            "binding": {"version": review_recovery.RECOVERY_VERSION,
                        "attempt_id": "terminal-recovery"},
            "reviews": {"job": {"items": [{
                "trial_id": "semantic", "independent_reviews": [semantic, semantic],
            }]}},
        },
    }
    qualification._store_run(
        ledger, "qualification-" + ("p" * 64)[:20], policy_hash="p" * 64,
        start_requests=0, state="unresolved", result=stored)
    monkeypatch.setattr(
        qualification.campaign_review, "review",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not retry")))
    monkeypatch.setattr(
        review_recovery, "recover",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not recover")))

    result = qualification.run(
        tmp_path / "policy.yaml", campaign_dir=tmp_path,
        project_root=tmp_path, resume=True)

    assert result["status"] == "unresolved"
    assert result["review"]["binding"]["attempt_id"] == "terminal-recovery"


def test_v4_source_review_file_is_hash_and_identity_bound(tmp_path: Path):
    input_hash = "a" * 64
    report = {
        "reviewer_kind": "agent", "provider": "openai", "model": "gpt-5",
        "agent": "/root/reviewer-a", "input_hash": input_hash,
        "independent": True, "peer_reviews_visible": False,
        "verdict": "approved", "human_reviewed": False,
    }
    rows = []
    for number in (1, 2):
        value = {**report, "agent": f"/root/reviewer-{number}"}
        path = tmp_path / f"review-{number}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        rows.append({
            "reviewer_kind": "agent", "provider": "openai", "model": "gpt-5",
            "agent": value["agent"], "validation_report": path.name,
            "validation_report_hash": digest(path.read_bytes()),
        })

    rubric._validate_v4_source_reviews(
        {"review": {"independent_reviews": rows}}, tmp_path, input_hash)
    (tmp_path / "review-2.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="report hash changed"):
        rubric._validate_v4_source_reviews(
            {"review": {"independent_reviews": rows}}, tmp_path, input_hash)


def test_repository_v4_sets_are_independently_approved_and_disjoint():
    paths = [ROOT / "eval/review_calibration_v4a.yaml",
             ROOT / "eval/review_calibration_v4b.yaml"]
    loaded = [rubric.load_calibration(path) for path in paths]
    for value in loaded:
        assert value["eligible"] is True and value["review_status"] == "ai_reviewed"
        assert len(value["samples"]) == 12
        assert sum(len(row["expected_rubric"]["expected_point_results"])
                   for row in value["samples"]) == 66
    raw = [qualification._sample_sets(qualification._raw_samples(path)) for path in paths]
    assert all(not raw[0][field] & raw[1][field] for field in raw[0])
    policy = yaml.safe_load((ROOT / "eval/judge_qualification_v4.yaml").read_text())
    assert policy["primary"]["model"] == "gpt-5.6-terra"
    assert policy["fallback"]["model"] == "gpt-5.6-sol"
    assert policy["fallback"]["activate_only_when"] == "judge_calibration_failed"
    assert policy["workers"] == 2 and policy["transport_retries"] == 0
    assert policy["max_new_requests"] == 1522
