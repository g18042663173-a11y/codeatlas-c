from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from codeatlas import db
from codeatlas.eval import value_proof


def _manifest(tmp_path: Path, kb, *, target: int = 1) -> Path:
    source, _ = kb
    database = tmp_path / "fixture.db"
    target_conn = db.connect(database)
    source.backup(target_conn)
    db.set_meta(target_conn, "repository_id", "tests/fixtures/mini_c")
    db.set_meta(target_conn, "revision", "fixture")
    target_conn.commit()
    target_conn.close()
    exposure = tmp_path / "exposure.yaml"
    exposure.write_text(yaml.safe_dump({
        "schema_version": 1,
        "tasks": [{"question": "where is cJSON_Delete", "symbol": "cJSON_Delete"}],
    }))
    manifest = tmp_path / "inventory.yaml"
    manifest.write_text(yaml.safe_dump({
        "schema_version": 1,
        "target_mechanisms_per_repository": target,
        "exposure_inputs": [exposure.name],
        "corpora": [{
            "id": "fixture", "repository": "tests/fixtures/mini_c", "revision": "fixture",
            "database": database.name, "production_paths": ["mini.c"],
        }],
    }))
    return manifest


def test_inventory_excludes_exposed_symbols_and_freezes_source_identity(tmp_path, kb):
    manifest = _manifest(tmp_path, kb)
    report = value_proof.inventory(manifest, project_root=tmp_path)
    assert report["status"] == "pending_ai_source_review"
    assert report["coverage_ready"] is False
    assert report["corpora"][0]["selected_count"] == 1
    selected = report["corpora"][0]["selected_candidates"]
    assert selected[0]["entry"] != "cJSON_Delete"
    assert selected[0]["source_validation"] == "pending_ai_review"
    assert "shared_exposed_dependencies" in selected[0]
    assert report["inventory_hash"]


def test_inventory_fails_closed_when_fixed_corpus_has_insufficient_coverage(tmp_path, kb):
    report = value_proof.inventory(_manifest(tmp_path, kb, target=999), project_root=tmp_path)
    assert report["status"] == "coverage_blocked"
    assert report["coverage_ready"] is False
    assert report["corpora"][0]["selected_count"] < 999
    rendered = value_proof.render_inventory(report)
    assert "`coverage_blocked`" in rendered
    assert "不会拆分函数、复用旧题或增加语料凑数" in rendered


def test_two_complete_independent_ai_reviews_are_required(tmp_path, kb):
    report = value_proof.inventory(_manifest(tmp_path, kb), project_root=tmp_path)
    ids = report["selected_candidate_ids"]
    reviews = [{
        "schema_version": 1, "reviewer_type": "ai", "reviewer_id": reviewer,
        "inventory_hash": report["inventory_hash"], "status": "approved",
        "checked_candidate_ids": ids,
    } for reviewer in ("source-a", "source-b")]
    approved = value_proof.validate_reviews(report, reviews)
    assert approved["coverage_ready"] is True
    assert approved["status"] == "approved"
    bad = copy.deepcopy(reviews)
    bad[1]["reviewer_id"] = "source-a"
    with pytest.raises(value_proof.ValueProofError, match="distinct"):
        value_proof.validate_reviews(report, bad)
    bad = copy.deepcopy(reviews)
    bad[0]["checked_candidate_ids"] = []
    with pytest.raises(value_proof.ValueProofError, match="complete"):
        value_proof.validate_reviews(report, bad)


def test_coverage_status_is_derived_and_review_projection_has_its_own_seal(tmp_path, kb):
    report = value_proof.inventory(_manifest(tmp_path, kb), project_root=tmp_path)
    green = copy.deepcopy(report)
    green.update(status="approved", coverage_ready=True)
    with pytest.raises(value_proof.ValueProofError, match="gate conflicts"):
        value_proof.validate_inventory_integrity(green)
    reviews = [{
        "schema_version": 1, "reviewer_type": "ai", "reviewer_id": reviewer,
        "inventory_hash": report["inventory_hash"], "status": "approved",
        "checked_candidate_ids": report["selected_candidate_ids"],
    } for reviewer in ("source-a", "source-b")]
    approved = value_proof.validate_reviews(report, reviews)
    assert value_proof.validate_inventory_integrity(approved) is approved
    changed = copy.deepcopy(approved)
    changed["source_reviews"][0]["reviewer_id"] = "tampered"
    with pytest.raises(value_proof.ValueProofError, match="review seal changed"):
        value_proof.validate_inventory_integrity(changed)


def test_source_reviews_cannot_override_insufficient_coverage(tmp_path, kb):
    report = value_proof.inventory(_manifest(tmp_path, kb, target=999), project_root=tmp_path)
    reviews = [{
        "schema_version": 1, "reviewer_type": "ai", "reviewer_id": reviewer,
        "inventory_hash": report["inventory_hash"], "status": "approved",
        "checked_candidate_ids": report["selected_candidate_ids"],
    } for reviewer in ("source-a", "source-b")]
    reviewed = value_proof.validate_reviews(report, reviews)
    assert reviewed["status"] == "coverage_blocked"
    assert reviewed["coverage_ready"] is False


def test_inventory_hash_and_review_hash_are_tamper_evident(tmp_path, kb):
    report = value_proof.inventory(_manifest(tmp_path, kb), project_root=tmp_path)
    changed = copy.deepcopy(report)
    changed["corpora"][0]["selected_candidates"][0]["entry"] = "tampered"
    review = {"schema_version": 1, "reviewer_type": "ai", "reviewer_id": "a",
              "inventory_hash": report["inventory_hash"], "status": "approved",
              "checked_candidate_ids": report["selected_candidate_ids"]}
    with pytest.raises(value_proof.ValueProofError, match="content changed"):
        value_proof.validate_reviews(changed, [review, {**review, "reviewer_id": "b"}])


def test_request_budget_distinguishes_answers_from_requests():
    budget = value_proof.request_budget()
    assert budget["formal"]["wiki_answer_trials"] == 528
    assert budget["formal"]["reuse_answer_trials"] == 324
    assert budget["formal"]["request_upper_bound"] == 6612
    assert budget["development"]["per_round"]["request_upper_bound"] == 456
    assert budget["development"]["request_upper_bound"] == 920
    assert budget["knowledge_production"]["request_upper_bound"] is None
    assert budget["authorization"] == "not_granted_by_dry_run"
    for invalid in (-1, True, 1.5):
        with pytest.raises(value_proof.ValueProofError, match="product_count"):
            value_proof.request_budget(product_count=invalid)


def test_wiki_claim_requires_ten_points_and_positive_97_5_interval():
    gates = {name: True for name in value_proof._REQUIRED_EFFECT_GATES}
    def report(repository):
        rows = [{"id": f"mechanism-{index}", "type": "语义检索",
                 "mechanism_id": f"mechanism-{index}",
                 "repetitions": [{"correct": True, "complete": 1.0,
                                  "false_claim_present": False,
                                  "review_status": "ai_reviewed"} for _ in range(3)]}
                for index in range(40)]
        rows += [{"id": f"refusal-{index}", "type": "拒答", "mechanism_id": None,
                  "repetitions": [{"correct": True, "complete": 1.0,
                                   "false_claim_present": False,
                                   "review_status": "ai_reviewed"} for _ in range(3)]}
                 for index in range(4)]
        wiki_rows = copy.deepcopy(rows)
        for row in rows[:40]:
            for trial in row["repetitions"]:
                trial["correct"] = False
        return {"repository": repository, "status": "completed", "task_scope": "full",
                "answer_review_status": "completed", "effect_review_eligible": True,
                "answer_key": {"effect_eligible": True, "effect_review_eligible": True},
                "acceptance": {"gates": gates}, "runs_per_task": 3,
                "raw_trials": {"source_only": copy.deepcopy(rows),
                               "wiki_flat": wiki_rows}}
    reports = [report("cjson"), report("lwip")]
    result = value_proof.wiki_claim(reports)
    assert result["benefit_supported"] is True
    assert result["confidence_level"] == .975
    mixed = copy.deepcopy(reports)
    for report in mixed:
        for index in range(40):
            source_value = index >= 20
            wiki_value = not source_value
            for trial in report["raw_trials"]["source_only"][index]["repetitions"]:
                trial["correct"] = source_value
            for trial in report["raw_trials"]["wiki_flat"][index]["repetitions"]:
                trial["correct"] = wiki_value
    assert value_proof.wiki_claim(mixed)["benefit_supported"] is False
    unqualified = copy.deepcopy(reports)
    unqualified[0]["answer_key"]["effect_eligible"] = False
    assert value_proof.wiki_claim(unqualified)["benefit_supported"] is False
    duplicated = [copy.deepcopy(reports[0]), copy.deepcopy(reports[0])]
    assert value_proof.wiki_claim(duplicated)["benefit_supported"] is False
    undersized = copy.deepcopy(reports)
    for arm in undersized[0]["raw_trials"].values():
        arm.pop()
    assert value_proof.wiki_claim(undersized)["benefit_supported"] is False
    incomplete = copy.deepcopy(reports)
    incomplete[0]["raw_trials"]["source_only"][0]["repetitions"][0]["correct"] = None
    assert value_proof.wiki_claim(incomplete)["benefit_supported"] is False


def test_fixed_case_card_claim_checks_each_case_and_never_claims_population_effect(monkeypatch):
    raw = {}
    for arm, tokens in (("raw_session", 100), ("generic_summary", 80), ("structured_card", 70)):
        raw[arm] = [{"id": f"{case}-q{question}", "type": "experience", "case_id": case,
                     "repetitions": [{"correct": True, "complete": 1,
                                      "false_claim_present": False, "input_tokens": tokens}
                                     for _ in range(3)]}
                    for case in (f"case-{number}" for number in range(12)) for question in range(3)]
    gates = {name: True for name in value_proof._REQUIRED_EFFECT_GATES}
    for rows in raw.values():
        for row in rows:
            for trial in row["repetitions"]:
                trial["review_status"] = "ai_reviewed"
    report = {"status": "completed", "answer_review_status": "completed",
              "task_scope": "fixed_public_cases", "runs_per_task": 3,
              "effect_review_eligible": True,
              "answer_key": {"effect_eligible": True, "effect_review_eligible": True},
              "acceptance": {"gates": gates}, "raw_trials": raw}
    from codeatlas.eval import protocol
    monkeypatch.setattr(protocol, "validate_pairs", lambda report: True)
    claim = value_proof.fixed_case_card_claim(report)
    assert claim["benefit_supported"] is True
    assert claim["input_token_reduction_pct"] == pytest.approx(30)
    assert "no population" in claim["boundary"]
    raw["structured_card"][0]["repetitions"][0]["correct"] = False
    assert value_proof.fixed_case_card_claim(report)["benefit_supported"] is False
    raw["structured_card"][0]["repetitions"][0]["correct"] = True
    report["answer_key"]["effect_eligible"] = False
    assert value_proof.fixed_case_card_claim(report)["benefit_supported"] is False
    report["answer_key"]["effect_eligible"] = True
    for arm in raw.values():
        del arm[1:3]
    assert value_proof.fixed_case_card_claim(report)["benefit_supported"] is False
