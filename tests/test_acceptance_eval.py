"""Dual-corpus acceptance and public-verification tests."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from codeatlas.eval import acceptance_eval


ROOT = Path(__file__).resolve().parents[1]


def test_public_corpora_and_claim_manifests_are_explicit():
    corpora = acceptance_eval.load_corpora(ROOT / "eval/corpora.yaml")
    by_id = {item["id"]: item for item in corpora["corpora"]}
    assert by_id["cjson"]["role"] == "correctness"
    assert by_id["cjson"]["source_relation"] == "official"
    assert by_id["lwip"]["role"] == "scale"
    assert by_id["lwip"]["source_relation"] == "mirror"
    assert "savannah" in by_id["lwip"]["official_url"]
    assert by_id["cjson"]["formal_card"]["id"] == "e-0f1743530a0e"
    assert by_id["lwip"]["formal_card"]["id"] == "e-13d209f2a9b2"

    claims = acceptance_eval.load_claims(ROOT / "eval/public_verification.yaml")
    kinds = {item["kind"] for item in claims["claims"]}
    assert kinds == {"source_identity", "task_count", "failure_count", "publication"}


def test_public_reproduction_artifact_hashes_match_documented_values():
    expected = {
        "examples/reproductions/cjson-nesting/reproduce.c":
            "cda8b6b0fef4cf4721d1086778637731b1337a56ae5fbe398afea849f30ad784",
        "examples/reproductions/cjson-nesting/run.sh":
            "529274dbbd63556db765061836196cc91af288bcff2d449fc03c3bf9843cf481",
        "examples/reproductions/cjson-nesting/result.txt":
            "ed76b1069221053876dd4995aaee6e228f4983ad8f97717d51bf1ff02be0b133",
        "examples/reproductions/lwip-netif/reproduce.c":
            "cafe3b447e59500c9dbb677a92866bda14b6a78f35594362dfba6ffe5af9e32a",
        "examples/reproductions/lwip-netif/run.sh":
            "82f26ccf13e73a3e8e19b12629a466c3b74045a3659203a0d414044f1fd34528",
        "examples/reproductions/lwip-netif/include/lwipopts.h":
            "65426d6f6d1c1a97a74392bfe626d19e68faf00ee585f5990231edd5edf2ecf2",
        "examples/reproductions/lwip-netif/result.txt":
            "8102733501276cd10303a81dc15877f7e5386b1dacb48c535f2960562f95414f",
    }
    assert {path: acceptance_eval._sha256(ROOT / path) for path in expected} == expected


def test_claims_use_current_reports_and_contradict_stale_number():
    corpora = acceptance_eval.load_corpora(ROOT / "eval/corpora.yaml")
    claims = acceptance_eval.load_claims(ROOT / "eval/public_verification.yaml")
    results = acceptance_eval.evaluate_claims(corpora, claims, project_root=ROOT)
    by_id = {item["id"]: item for item in results}

    assert by_id["cjson-official-source"]["status"] == "verified"
    assert by_id["lwip-upstream-and-mirror"]["status"] == "verified"
    assert by_id["cjson-task-count"]["status"] == "verified"
    assert by_id["lwip-task-count"]["status"] == "verified"
    conflict = by_id["lwip-two-failures"]
    assert conflict["status"] == "contradicted"
    assert conflict["actual"] == 0
    assert conflict["not_passed_total"] == 5
    assert conflict["failure_breakdown"] == {"unexpected": 0, "blocked_by_review": 5}
    assert by_id["codeatlas-report-publication"]["status"] in {
        "verified", "locally_verified_not_published",
    }


@pytest.mark.parametrize("raw_status", ["executed", "completed"])
def test_proof_registry_distinguishes_completion_from_positive_effect(
        tmp_path: Path, raw_status: str):
    (tmp_path / "eval").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "src/codeatlas").mkdir(parents=True)
    (tmp_path / "src/codeatlas/dependencies.py").write_bytes(
        (ROOT / "src/codeatlas/dependencies.py").read_bytes())
    source = tmp_path / "eval/maintenance.yaml"
    source.write_text("schema_version: 1\n", encoding="utf-8")
    manifest = {"schema_version": 1, "experiments": [{
        "id": "maintenance", "kind": "maintenance", "corpus": "combined",
        "manifest": "eval/maintenance.yaml", "report": "docs/maintenance.json",
        "required_for_effect": True,
    }]}
    (tmp_path / "eval/experiments.yaml").write_text(
        yaml.safe_dump(manifest), encoding="utf-8")
    # This unit isolates registry aggregation; the actual 24-case report
    # re-derivation is exercised by test_maintenance_eval.py.
    report = {
        "status": raw_status, "manifest_sha256": acceptance_eval._sha256(source),
        "identity": acceptance_eval._proof_runtime_identity("maintenance", tmp_path),
        "acceptance": {"execution_complete": True, "current_decisions_passed": False,
                       "oracle_independently_validated": True},
    }
    (tmp_path / "docs/maintenance.json").write_text(json.dumps(report), encoding="utf-8")
    result = acceptance_eval.proof_status("eval/experiments.yaml", project_root=tmp_path)
    assert result["status"] == "not_run"
    assert result["ai_review_done"] is False
    assert result["effect_observed"] is False
    assert result["items"][0]["status"] == "invalid"


def test_proof_registry_recomputes_effect_instead_of_trusting_green_flag(
        tmp_path: Path, monkeypatch):
    from codeatlas.eval import protocol, rubric
    from tests.test_review_provenance import _fixture, _reviews

    (tmp_path / "eval").mkdir()
    (tmp_path / "docs").mkdir()
    source = tmp_path / "eval/tasks.yaml"
    answer = tmp_path / "eval/answers.yaml"
    source.write_text("schema_version: 1\n", encoding="utf-8")
    answer.write_text("schema_version: 1\n", encoding="utf-8")
    manifest = {"schema_version": 1, "experiments": [{
        "id": "forged", "kind": "knowledge_reuse", "corpus": "combined",
        "manifest": "eval/tasks.yaml", "answer_key": "eval/answers.yaml",
        "report": "docs/forged.json", "required_for_effect": True,
    }]}
    manifest_path = tmp_path / "eval/experiments.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    report, packet = _fixture()
    rubric.apply_reviews(report, _reviews(packet))
    report.update(status="completed", manifest_hash=acceptance_eval._sha256(source),
                  answer_key_hash=acceptance_eval._sha256(answer))
    assert report["run_hash"] == protocol.run_hash(report)
    report["acceptance"]["effect_observed"] = True  # Stored flag is not evidence.
    (tmp_path / "docs/forged.json").write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(acceptance_eval, "_proof_runtime_identity",
                        lambda _kind, _root: {"fixture": "v1"})

    result = acceptance_eval.proof_status(manifest_path, project_root=tmp_path)
    item = result["items"][0]
    assert item["status"] == "invalid"
    assert item["proof_integrity_ok"] is False
    assert item["claim_supported"] is False
    assert result["effect_observed"] is False


def test_reviewed_negative_result_completes_experiment_without_claiming_gain(
        tmp_path: Path, monkeypatch):
    from codeatlas.eval import rubric

    (tmp_path / "eval").mkdir()
    (tmp_path / "docs").mkdir()
    source = tmp_path / "eval/tasks.yaml"
    answer = tmp_path / "eval/answers.yaml"
    source.write_text("schema_version: 1\n", encoding="utf-8")
    answer.write_text("schema_version: 1\n", encoding="utf-8")
    manifest = {"schema_version": 1, "experiments": [{
        "id": "negative", "kind": "knowledge_reuse", "corpus": "combined",
        "manifest": "eval/tasks.yaml", "answer_key": "eval/answers.yaml",
        "report": "docs/negative.json", "required_for_effect": True,
    }]}
    manifest_path = tmp_path / "eval/experiments.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    gates = {name: True for name in {
        "answer_review_complete", "paired_trials_complete",
        "frozen_semantic_holdout", "review_provenance",
        "review_packet_complete", "judge_calibration", "full_three_repeat_run",
        "protocol_identity_bound", "raw_trials_bound", "review_integrity_bound",
        "snapshot_pinned",
    }}
    acceptance = {
        "gates": gates, "effect_observed": False,
        "primary_contrast": {"treatment": "structured_card", "baselines": ["raw_session"]},
        "paired_code_comparisons": {"raw_session": {
            "accuracy": {"delta_pt": -2, "ci95_pt": [-8, 3]},
            "answer_completeness": {"delta_pt": 0, "ci95_pt": [-4, 4]},
            "code_input_token_reduction_pct": 5,
        }},
    }
    report = {
        "status": "completed", "manifest_hash": acceptance_eval._sha256(source),
        "answer_key_hash": acceptance_eval._sha256(answer), "identity": {"fixture": "v1"},
        "review_status": "ai_reviewed", "answer_review_status": "completed",
        "acceptance": acceptance,
    }
    (tmp_path / "docs/negative.json").write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(acceptance_eval, "_proof_runtime_identity",
                        lambda _kind, _root: {"fixture": "v1"})
    monkeypatch.setattr(rubric, "finalize", lambda _report: None)

    result = acceptance_eval.proof_status(manifest_path, project_root=tmp_path)
    assert result["experiments_complete"] is True
    assert result["reviews_complete"] is True
    assert result["claims_complete"] is True
    assert result["claims"][0]["state"] == "insufficient_evidence"
    assert result["effect_observed"] is False

    report["acceptance"]["paired_code_comparisons"]["raw_session"]["accuracy"][
        "ci95_pt"
    ] = [-12, -1]
    (tmp_path / "docs/negative.json").write_text(json.dumps(report), encoding="utf-8")
    regressed = acceptance_eval.proof_status(manifest_path, project_root=tmp_path)
    assert regressed["experiments_complete"] is True
    assert regressed["claims"][0]["state"] == "regressed"


def test_portfolio_readiness_requires_complete_evidence_not_positive_gain():
    proof = {
        "experiments_complete": True,
        "reviews_complete": True,
        "claims_complete": True,
        "effect_observed": False,
    }
    assert acceptance_eval._portfolio_ready(True, True, proof) is True
    for field in ("experiments_complete", "reviews_complete", "claims_complete"):
        incomplete = {**proof, field: False}
        assert acceptance_eval._portfolio_ready(True, True, incomplete) is False


def test_formal_card_release_is_verified_independently_from_legacy_task_gold(
        tmp_path: Path, monkeypatch):
    import sqlite3
    from codeatlas.experience import store

    markdown = tmp_path / "knowledge/cards/card-1.md"
    markdown.parent.mkdir(parents=True)
    markdown.write_text("# reviewed card\n", encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE experience (
          id TEXT, status TEXT, artifact_scope TEXT, review_bundle_hash TEXT,
          publication_op_id TEXT, canonical_path TEXT, card_markdown TEXT
        );
        CREATE TABLE experience_review (
          id INTEGER PRIMARY KEY, exp_id TEXT, action TEXT, reviewer TEXT,
          reviewer_type TEXT, evaluation_scope TEXT, created_at TEXT
        );
        CREATE TABLE review_confirmation (
          exp_id TEXT, bundle_hash TEXT, reviewer TEXT, reviewer_type TEXT,
          confirmed_at TEXT
        );
        CREATE TABLE experience_qa (
          exp_id TEXT, status TEXT, bundle_hash TEXT, reviewer TEXT,
          reviewer_type TEXT, evaluation_scope TEXT, reviewed_at TEXT
        );
        CREATE TABLE publication_journal (
          op_id TEXT, state TEXT, committed_at TEXT
        );
    """)
    bundle = "b" * 64
    conn.execute(
        "INSERT INTO experience VALUES(?,?,?,?,?,?,?)",
        ("card-1", "approved", "formal", bundle, "pub-1", str(markdown), "# reviewed card\n"),
    )
    conn.execute(
        "INSERT INTO experience_review VALUES(1,?,?,?,?,?,?)",
        ("card-1", "approve", "Reviewer", "human", "formal", "now"),
    )
    conn.execute(
        "INSERT INTO review_confirmation VALUES(?,?,?,?,?)",
        ("card-1", bundle, "Reviewer", "human", "now"),
    )
    conn.execute(
        "INSERT INTO experience_qa VALUES(?,?,?,?,?,?,?)",
        ("card-1", "passed", bundle, "Reviewer", "human", "formal", "now"),
    )
    conn.execute("INSERT INTO publication_journal VALUES(?,?,?)", ("pub-1", "committed", "now"))
    conn.commit()
    monkeypatch.setattr(acceptance_eval.dbm, "query", lambda _path: conn)
    monkeypatch.setattr(acceptance_eval.dbm, "read_only", lambda _path: conn)
    monkeypatch.setattr(store, "approved_current", lambda _conn, _id: (True, ""))

    result = acceptance_eval._validate_formal_card(
        {"database": "ignored.db", "formal_card": {
            "id": "card-1", "review_bundle_hash": bundle,
        }},
        tmp_path,
    )
    assert result["ok"] is True
    assert result["reviewer"] == "Reviewer"
    assert all(result["gates"].values())


def test_old_report_is_stale_instead_of_being_accepted(tmp_path: Path):
    report = {
        "manifest": {"revision": "old", "tasks": [1, 2]},
        "summary": {"failures": []},
    }
    (tmp_path / "task.json").write_text(json.dumps(report), encoding="utf-8")
    corpora = {
        "corpora": [{
            "id": "cjson", "revision": "current", "task_report": "task.json",
            "official_url": "https://example.test/upstream",
            "checkout_url": "https://example.test/upstream", "source_relation": "official",
        }],
    }
    claims = {"claims": [{
        "id": "count", "kind": "task_count", "corpus": "cjson", "asserted": 2,
    }]}
    result = acceptance_eval.evaluate_claims(corpora, claims, project_root=tmp_path)
    assert result[0]["status"] == "stale"


def _git(tmp_path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True)


def test_publication_distinguishes_clean_dirty_and_network_failure(tmp_path: Path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "fixture@example.test")
    _git(tmp_path, "config", "user.name", "Fixture")
    report = tmp_path / "report.json"
    report.write_text("{}\n", encoding="utf-8")
    _git(tmp_path, "add", "report.json")
    _git(tmp_path, "commit", "-qm", "fixture")

    clean = acceptance_eval.publication_status(
        project_root=tmp_path, report_paths=[report], release=False,
    )
    assert clean["status"] == "verified"

    (tmp_path / "untracked.txt").write_text("dirty", encoding="utf-8")
    dirty = acceptance_eval.publication_status(
        project_root=tmp_path, report_paths=[report], release=False,
    )
    assert dirty["status"] == "locally_verified_not_published"
    (tmp_path / "untracked.txt").unlink()

    identity = acceptance_eval.git_identity(tmp_path)

    def fake_git(_root, *args, **_kwargs):
        if args[:2] == ("remote", "get-url"):
            return "https://github.com/example/codeatlas-c.git"
        if args and args[0] == "ls-remote":
            return identity["revision"] + "\trefs/heads/main"
        return acceptance_eval._git_original(_root, *args, **_kwargs)

    monkeypatch.setattr(acceptance_eval, "_git_original", acceptance_eval._git, raising=False)
    monkeypatch.setattr(acceptance_eval, "_git", fake_git)
    unavailable = acceptance_eval.publication_status(
        project_root=tmp_path, report_paths=[report], release=True,
        network_checker=lambda _url: False,
    )
    assert unavailable["status"] == "unverifiable"
    assert unavailable["reason"] == "public_url_unavailable"


def test_source_checkout_must_match_pinned_revision_and_origin(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "fixture@example.test")
    _git(source, "config", "user.name", "Fixture")
    (source / "fixture.c").write_text("int value;\n", encoding="utf-8")
    _git(source, "add", "fixture.c")
    _git(source, "commit", "-qm", "fixture")
    _git(source, "remote", "add", "origin", "https://github.com/example/corpus.git")
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    corpus = {
        "source_root": "source", "revision": revision,
        "checkout_url": "https://github.com/example/corpus",
    }
    assert acceptance_eval._source_checkout(corpus, tmp_path)["ok"] is True
    corpus["revision"] = "0" * 40
    result = acceptance_eval._source_checkout(corpus, tmp_path)
    assert result["ok"] is False and result["revision_ok"] is False


def test_aggregate_report_is_atomic_and_consistent(tmp_path: Path, monkeypatch):
    corpora_path = tmp_path / "corpora.yaml"
    claims_path = tmp_path / "claims.yaml"
    corpora_data = yaml.safe_load((ROOT / "eval/corpora.yaml").read_text(encoding="utf-8"))
    claims_data = yaml.safe_load((ROOT / "eval/public_verification.yaml").read_text(encoding="utf-8"))
    corpora_path.write_text(yaml.safe_dump(corpora_data, allow_unicode=True), encoding="utf-8")
    claims_path.write_text(yaml.safe_dump(claims_data, allow_unicode=True), encoding="utf-8")

    fake_corpus = {
        "id": "cjson", "role": "correctness", "passed": True,
        "foundation_passed": True, "task_complete": False,
        "gates": {"compile_database": True, "database": True, "task_gold": True,
                  "task_metrics": True, "knowledge_workflow": True,
                  "source_checkout": True},
        "tasks": {
            "summary": {"passed_tasks": 14, "total_tasks": 20,
                        "baseline": {"recall": 60}, "full": {"recall": 90, "mrr": .5}},
            "failure_breakdown": {"unexpected_count": 1, "blocked_count": 5},
        },
    }
    monkeypatch.setattr(acceptance_eval, "validate_corpus",
                        lambda *_args, **_kwargs: fake_corpus)
    monkeypatch.setattr(acceptance_eval, "evaluate_claims", lambda *_args, **_kwargs: [
        {"id": "publication", "kind": "publication", "claim": "", "status": "verified"},
    ])
    monkeypatch.setattr(acceptance_eval, "git_identity", lambda *_args: {
        "revision": "fixture", "branch": "main", "dirty": False,
        "dirty_entries": 0, "origin": "https://example.test/repo.git",
        "implementation_hash": "fixture-hash",
    })

    out = tmp_path / "ACCEPTANCE.md"
    result = acceptance_eval.main(
        corpora_path=str(corpora_path), claims_path=str(claims_path), mode="fast",
        out=str(out), project_root=tmp_path, execute=False,
    )
    json_result = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert result["foundation_passed"] is True
    assert result["task_complete"] is False
    assert result["portfolio_ready"] is False
    assert result["passed"] is False
    assert json_result["passed"] is False
    assert "双语料验收" in out.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".*.tmp"))


def test_release_snapshot_rejects_changed_implementation_or_report(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/example.py").write_text("VALUE = 1\n", encoding="utf-8")
    for name in ("tasks.yaml", "task.json", "workflow.json"):
        (tmp_path / name).write_text(f"{name}\n", encoding="utf-8")
    corpus = {
        "id": "cjson", "task_manifest": "tasks.yaml", "task_report": "task.json",
        "workflow_report": "workflow.json",
    }
    item = {
        "id": "cjson", "passed": True,
        "task_gold": {"sha256": acceptance_eval._sha256(tmp_path / "tasks.yaml")},
        "tasks": {"sha256": acceptance_eval._sha256(tmp_path / "task.json")},
        "workflow": {"sha256": acceptance_eval._sha256(tmp_path / "workflow.json")},
    }
    snapshot = {
        "mode": "full", "passed": True,
        "codeatlas": {"implementation_hash": acceptance_eval.implementation_hash(tmp_path)},
        "corpora": [item],
    }
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ACCEPTANCE-CJSON-LWIP.json").write_text(
        json.dumps(snapshot), encoding="utf-8",
    )
    manifest = {"corpora": [corpus]}
    assert acceptance_eval.validate_release_snapshot(manifest, project_root=tmp_path) == [item]

    (tmp_path / "src/example.py").write_text("VALUE = 2\n", encoding="utf-8")
    try:
        acceptance_eval.validate_release_snapshot(manifest, project_root=tmp_path)
    except acceptance_eval.AcceptanceEvalError as exc:
        assert "源码/题集内容" in str(exc)
    else:  # pragma: no cover - explicit guard for a security-sensitive gate
        raise AssertionError("changed implementation was accepted")


def test_model_abc_status_requires_three_runs_and_matching_snapshot(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    task_manifest = tmp_path / "tasks.yaml"
    task_manifest.write_text("schema_version: 1\n", encoding="utf-8")
    corpus = {
        "id": "cjson", "checkout_url": "https://example.test/cjson",
        "revision": "fixed", "task_manifest": "tasks.yaml",
    }
    assert acceptance_eval.model_abc_status(
        [corpus], project_root=tmp_path,
    )["status"] == "not_run"
    report = {
        "status": "completed", "repository": corpus["checkout_url"],
        "revision": "fixed", "task_set_hash": acceptance_eval._sha256(task_manifest),
        "runs_per_task": 3, "model": "fixture-model",
        "acceptance": {"passed": True},
    }
    (tmp_path / "docs/MODEL-ABC-CJSON.json").write_text(
        json.dumps(report), encoding="utf-8",
    )
    matched = acceptance_eval.model_abc_status([corpus], project_root=tmp_path)
    # A legacy self-declared pass without implementation/rubric/cards identity
    # or per-trial human reviews must no longer be presented as model evidence.
    assert matched["status"] == "stale" and matched["passed"] is False
    report["runs_per_task"] = 1
    (tmp_path / "docs/MODEL-ABC-CJSON.json").write_text(
        json.dumps(report), encoding="utf-8",
    )
    stale = acceptance_eval.model_abc_status([corpus], project_root=tmp_path)
    assert stale["status"] == "stale" and stale["corpora"][0]["status"] == "stale"
