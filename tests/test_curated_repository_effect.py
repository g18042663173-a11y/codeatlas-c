"""Compact reporting must preserve the original paired/bootstrap statistics."""
from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from codeatlas.contracts import digest
from codeatlas.eval import acceptance_eval as acceptance, campaign_review, protocol

KINDS = {"定位", "调用链", "语义检索", "影响分析"}
ROOT = Path(__file__).resolve().parents[1]
GATES = {name: True for name in (
    "answer_review_complete", "paired_trials_complete", "frozen_semantic_holdout", "review_provenance",
    "review_packet_complete", "judge_calibration", "full_three_repeat_run", "protocol_identity_bound",
    "raw_trials_bound", "review_integrity_bound", "snapshot_pinned")}


def _metrics(rows):
    pending = sum(r["review_status"] == "unresolved" for r in rows)
    return {"trial_count": len(rows), "pending": pending,
            "accuracy": None if pending else 100 * sum(r["correct"] for r in rows) / len(rows),
            "completeness": None if pending else 100 * sum(r["complete"] for r in rows) / len(rows),
            "effective_citation_rate": 100.0, "native_planning_success_rate": 100.0}


def _seal(report):
    report["curated_projection"]["trial_results_hash"] = digest(report["trial_results"])
    report.pop("curated_report_hash", None)
    report["curated_report_hash"] = digest(report)
    return report


def _report(repository, count):
    raw = {}
    for arm, variant in enumerate(("source_only", "wiki_flat")):
        rows = []
        for index in range(count):
            task = {"id": f"task-{index}", "type": "语义检索", "mechanism_id": f"mechanism-{index // 2}",
                    "case_id": None, "repetitions": []}
            for repetition in range(3):
                trial = {"repetition": repetition, "correct": bool((index + repetition + arm) % 3),
                         "complete": ((index + repetition + arm) % 5) / 4,
                         "answer_verdict": "incomplete", "review_status": "ai_reviewed",
                         "citation_valid": True, "native_planning": True,
                         "trial_id": digest([repository, variant, index, repetition]),
                         "raw_trial_hash": digest(["raw", repository, variant, index, repetition]),
                         "answer_review": {"selected_score": {"note": "Synthetic aggregate fixture."}}}
                task["repetitions"].append(trial)
            rows.append(task)
        raw[variant] = rows
    report = {"repository": repository, "report_kind": "wiki_knowledge", "status": "completed",
              "answer_review_status": "completed", "review_status": "ai_reviewed", "runs_per_task": 3,
              "raw_trials": raw, "executed_trial_count": count * 6,
              "identity": {}, "protocol": {"variants": list(raw)},
              "review_packet_hash": digest("packet"), "review_protocol_hash": digest("review"),
              "review_version_hash": digest("version"),
              "acceptance": {"gates": GATES, "effect_observed": False, "primary_contrast": {"baselines": []}},
              "variants": {v: {"metrics": _metrics([t for r in rows for t in r["repetitions"]])} for v, rows in raw.items()}}
    report["run_hash"] = protocol.run_hash(report)
    report["review_attempt"] = {"raw_answer_run_hash": report["run_hash"], "judge_model": "offline-fixture",
                                "calibration_hash": digest("calibration"), "unresolved_trial_count": 0,
                                "terminal_semantic_unresolved": False}
    return report


def _effect(reports, field, seed=17):
    return protocol.repository_grouped_paired_effect(reports, "wiki_flat", "source_only", field, kinds=KINDS, seed=seed)


@pytest.mark.parametrize("field", ["correct", "complete"])
@pytest.mark.parametrize("seed", [0, 17, 29])
def test_raw_and_compact_preserve_exact_task_mechanism_deltas_and_bootstrap(field, seed):
    reports = [_report("repo-a", 5), _report("repo-b", 3)]
    compact = [_seal(campaign_review._compact_curated_report(r)) for r in reports]
    before = copy.deepcopy(compact)
    views = [acceptance._curated_repository_effect_view(r) for r in compact]
    assert _effect(reports, field, seed) == _effect(views, field, seed)
    assert compact == before
    views[0]["raw_trials"]["source_only"][0]["repetitions"][0]["correct"] = False
    assert compact == before


@pytest.fixture
def installed(tmp_path, monkeypatch):
    source = tmp_path / "tasks.yaml"
    source.write_text("schema_version: 1\n")
    answer = tmp_path / "answers.yaml"
    answer.write_text("schema_version: 1\n")
    reports, entries = [], []
    for repository, count in (("repo-a", 5), ("repo-b", 3)):
        raw = _report(repository, count)
        raw.update(manifest_hash=acceptance._sha256(source), answer_key_hash=acceptance._sha256(answer))
        compact = _seal(campaign_review._compact_curated_report(raw))
        path = tmp_path / f"{repository}.json"
        path.write_text(json.dumps(compact))
        entries.append({"id": repository, "kind": "wiki_knowledge", "manifest": "tasks.yaml",
                        "answer_key": "answers.yaml", "report": path.name})
        reports.append(raw)
    manifest = tmp_path / "experiments.yaml"
    manifest.write_text(yaml.safe_dump({"schema_version": 1, "experiments": entries}))
    monkeypatch.setattr(acceptance, "_proof_runtime_identity", lambda *_: {"moved_runtime": "new"})
    return tmp_path, manifest, reports


def test_proof_status_adapts_verified_compact_reports_before_combining(installed):
    root, manifest, reports = installed
    result = acceptance.proof_status(manifest, project_root=root)
    combined = result["wiki_combined_effect"]
    assert combined["status"] == "measured"
    assert combined["accuracy"] == _effect(reports, "correct")
    assert combined["answer_completeness"] == _effect(reports, "complete")
    assert len(result["items"]) == 2


def test_tampered_compact_cannot_enter_combined_statistics(installed):
    root, manifest, _ = installed
    path = root / "repo-a.json"
    report = json.loads(path.read_text())
    report["trial_results"][0]["correct"] = not report["trial_results"][0]["correct"]
    path.write_text(json.dumps(report))
    with pytest.raises(acceptance.AcceptanceEvalError):
        acceptance._curated_repository_effect_view(report)
    assert acceptance.proof_status(manifest, project_root=root)["wiki_combined_effect"]["status"] == "not_measured"


def test_valid_but_unresolved_compact_cannot_enter_combined_statistics(installed):
    root, manifest, _ = installed
    path = root / "repo-a.json"
    report = json.loads(path.read_text())
    report["trial_results"][0].update(review_status="unresolved", correct=False, complete=0)
    report["review_status"] = "unresolved"
    report["review_attempt"].update(unresolved_trial_count=1, terminal_semantic_unresolved=True)
    for variant in report["variants"]:
        report["variants"][variant]["metrics"] = _metrics([r for r in report["trial_results"] if r["variant"] == variant])
    _seal(report)
    assert acceptance._verify_curated_effect_report(report) == {
        "ok": True, "unresolved_trial_count": 1, "review_complete": False}
    with pytest.raises(acceptance.AcceptanceEvalError):
        acceptance._curated_repository_effect_view(report)
    path.write_text(json.dumps(report))
    result = acceptance.proof_status(manifest, project_root=root)
    assert len(result["items"]) == 2 and result["wiki_combined_effect"]["status"] == "not_measured"


@pytest.mark.parametrize("change", [
    lambda rows: rows[0].update(mechanism_id="different-only-for-this-repeat"),
    lambda rows: rows[0].update(repetition=1),
    lambda rows: rows[0].update(task_type="different-only-for-this-repeat"),
])
def test_resealed_inconsistent_grouping_or_repetition_is_rejected(change):
    compact = _seal(campaign_review._compact_curated_report(_report("repo-a", 5)))
    change(compact["trial_results"])
    _seal(compact)
    assert acceptance._verify_curated_effect_report(compact)["ok"]
    with pytest.raises(acceptance.AcceptanceEvalError):
        acceptance._curated_repository_effect_view(compact)


def test_real_v5_reports_reconstruct_two_repositories_and_match_private_raw_statistics():
    paths = [ROOT / f"docs/runs/curated/PROOF-WIKI-{name}-V5.json" for name in ("CJSON", "LWIP")]
    assert all(p.is_file() for p in paths), "Both tracked V5 proof outputs are required"
    reports = [json.loads(p.read_text()) for p in paths]
    views = [acceptance._curated_repository_effect_view(r) for r in reports]
    for field in ("correct", "complete"):
        result = _effect(views, field)
        assert (result["repository_count"], result["task_count"], result["mechanism_count"]) == (2, 40, 35)
    ledger = ROOT / "data/proof/campaign-v10/ledger.sqlite3"
    if not ledger.is_file():
        return  # Public CI still checks the complete V5 repository/task counts.
    originals = []
    with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True) as conn:
        for report, job in zip(reports, ("wiki-cjson", "wiki-lwip"), strict=True):
            value = conn.execute("SELECT json_extract(result_json, ?) FROM campaign_review_batches WHERE id=?",
                (f'$.reviewed_reports."{job}"', report["review_attempt"]["batch_id"])).fetchone()
            original = json.loads(value[0])
            assert protocol.run_hash(original) == report["run_hash"]
            originals.append(original)
    for field in ("correct", "complete"):
        assert _effect(originals, field) == _effect(views, field)
