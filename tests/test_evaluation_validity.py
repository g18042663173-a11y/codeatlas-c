"""Evaluation mechanics only. Synthetic reviews never approve portfolio claims."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from codeatlas import agent
from codeatlas.contracts import digest
from codeatlas.eval import protocol, reading_eval, rubric, model_eval
from codeatlas.retrieve import engine
from codeatlas.wiki import generator
from tests.test_model_eval import _ProtocolModel, _tasks
from tests.test_snapshot_contracts import _review_report


def _refreeze_fixture_scores(report, reviews):
    """Prepare synthetic scores after fixture setup, never alter measured trials."""
    old = [t["trial_id"] for rows in report["raw_trials"].values()
           for row in rows for t in row["repetitions"]]
    rubric.blind_packet(report)
    new = [t["trial_id"] for rows in report["raw_trials"].values()
           for row in rows for t in row["repetitions"]]
    mapping = dict(zip(old, new))
    for item in reviews["items"]:
        item["trial_id"] = mapping[item["trial_id"]]


def test_schedule_is_paired_repeatable_and_not_grouped_by_arm():
    tasks = [{"id": str(i)} for i in range(8)]
    plan = protocol.schedule(tasks, ("A", "B", "C"), 3)
    assert plan == protocol.schedule(tasks, ("A", "B", "C"), 3)
    assert plan != protocol.schedule(tasks, ("A", "B", "C"), 3, 99)
    assert len(plan) == 72
    for start in range(0, len(plan), 3):
        block = plan[start:start + 3]
        assert len({e["task_id"] for e in block}) == 1
        assert {e["variant"] for e in block} == {"A", "B", "C"}


def test_unreviewed_answers_are_unknown_not_zero():
    report, _ = _review_report()
    rubric.finalize(report)
    assert report["variants"]["C"]["metrics"]["accuracy"] is None
    assert report["variants"]["C"]["metrics"]["completeness"] is None
    assert report["acceptance"]["c_experience_accuracy"] is None
    assert not report["acceptance"]["passed"]


def test_anchor_gain_cannot_substitute_for_answer_gain():
    report, reviews = _review_report()
    rubric.apply_reviews(report, reviews)
    assert report["acceptance"]["source_anchor_gain_pt"] == 50
    assert not report["acceptance"]["gates"]["codeatlas_gain"]


def test_experience_token_savings_do_not_mask_code_cost():
    report, reviews = _review_report()
    for v in ("A", "B"):
        report["raw_trials"][v][1]["repetitions"][0]["input_tokens"] = 10000
    _refreeze_fixture_scores(report, reviews)
    rubric.apply_reviews(report, reviews)
    assert report["acceptance"]["context_token_reduction_pct"] == 0
    assert not report["acceptance"]["gates"]["codeatlas_gain"]


def test_missing_provider_usage_cannot_become_token_gain():
    report, reviews = _review_report()
    report["raw_trials"]["C"][0]["repetitions"][0]["input_tokens"] = None
    _refreeze_fixture_scores(report, reviews)
    rubric.apply_reviews(report, reviews)
    assert report["acceptance"]["context_token_reduction_pct"] is None
    assert not report["acceptance"]["gates"]["codeatlas_gain"]


def test_usage_counts_failed_or_partially_reported_requests_as_unknown():
    client = SimpleNamespace(request_count=0, usage_totals={}, usage_observations={})
    before = protocol.usage_start(client)
    assert protocol.usage_delta(client, before)["input_tokens"] == 0  # No call really is zero.
    client.request_count = 2
    client.usage_totals = {"prompt_tokens": 50, "completion_tokens": 8, "total_tokens": 58}
    client.usage_observations = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 1}
    assert protocol.usage_delta(client, before)["input_tokens"] is None
    client.usage_observations = {key: 2 for key in client.usage_totals}
    assert protocol.usage_delta(client, before)["input_tokens"] == 50


def test_grouped_effect_weights_mechanisms_not_number_of_questions():
    def row(identifier, mechanism, value):
        return {"id": identifier, "type": "语义检索", "mechanism_id": mechanism,
                "repetitions": [{"correct": value}]}
    raw = {
        "source": [row("a1", "a", 0), row("a2", "a", 0), row("b1", "b", 0)],
        "wiki": [row("a1", "a", 1), row("a2", "a", 0), row("b1", "b", 1)],
    }
    effect = protocol.grouped_paired_effect(raw, "wiki", "source", "correct")
    # Mechanism a improves 50pt and mechanism b 100pt: equal mechanism mean = 75pt.
    assert effect["task_count"] == 3 and effect["mechanism_count"] == 2
    assert effect["delta_pt"] == 75


def test_run_hash_binds_mechanism_and_case_grouping():
    report, _ = _review_report()
    original = protocol.run_hash(report)
    report["raw_trials"]["A"][0]["mechanism_id"] = "different-mechanism"
    assert protocol.run_hash(report) != original
    report["raw_trials"]["A"][0].pop("mechanism_id")
    report["raw_trials"]["A"][0]["case_id"] = "different-case"
    assert protocol.run_hash(report) != original


def test_short_experience_pilot_bootstraps_two_cases_not_six_questions():
    def row(case_id, suffix, treatment):
        return {"id": f"{case_id}-{suffix}", "type": "经验同源对照",
                "case_id": case_id, "repetitions": [{"correct": treatment}]}
    raw = {
        "raw": [row(case, q, 0) for case in ("one", "two") for q in range(3)],
        "card": [row(case, q, int(case == "one"))
                 for case in ("one", "two") for q in range(3)],
    }
    effect = protocol.grouped_paired_effect(raw, "card", "raw", "correct")
    assert effect["task_count"] == 6
    assert effect["group_unit"] == "case" and effect["group_count"] == 2
    assert effect["delta_pt"] == 50
    assert effect["ci95_pt"] == [0, 100]


def test_combined_effect_weights_repositories_after_mechanisms():
    def row(identifier, mechanism, source, wiki):
        return ({"id": identifier, "type": "语义检索", "mechanism_id": mechanism,
                 "source": {"correct": source}, "wiki": {"correct": wiki}})
    # Repo one has two mechanisms averaging +50pt; repo two has one mechanism
    # at -100pt.  Equal repository weighting is therefore -25pt, rather than a
    # pooled mechanism mean of 0pt.
    repositories = []
    for name, rows in (
        ("one", [row("a", "m1", 0, 1), row("b", "m2", 0, 0)]),
        ("two", [row("c", "m3", 1, 0)]),
    ):
        repositories.append({"repository": name, "raw_trials": {
            "source": [{"id": item["id"], "type": item["type"],
                        "mechanism_id": item["mechanism_id"],
                        "repetitions": [item["source"]]} for item in rows],
            "wiki": [{"id": item["id"], "type": item["type"],
                      "mechanism_id": item["mechanism_id"],
                      "repetitions": [item["wiki"]]} for item in rows],
        }})
    effect = protocol.repository_grouped_paired_effect(
        repositories, "wiki", "source", "correct",
    )
    assert effect["repository_count"] == 2
    assert effect["mechanism_count"] == 3
    assert effect["delta_pt"] == -25


@pytest.mark.parametrize("damage", ["missing_repeat", "duplicate_task", "different_task"])
def test_unpaired_trials_fail_the_gate(damage):
    report, _ = _review_report()
    if damage == "missing_repeat":
        report["raw_trials"]["C"][0]["repetitions"] = []
    elif damage == "duplicate_task":
        report["raw_trials"]["C"].append(copy.deepcopy(report["raw_trials"]["C"][0]))
    else:
        report["raw_trials"]["C"][0]["id"] = "different"
    assert not protocol.validate_pairs(report)


def test_review_batch_is_atomic_and_bound_to_answer():
    report, reviews = _review_report()
    previous = copy.deepcopy(report)
    reviews["items"][-1]["completeness"] = -1
    with pytest.raises(ValueError, match="completeness"):
        rubric.apply_reviews(report, reviews)
    assert report == previous
    report, reviews = _review_report()
    report["raw_trials"]["C"][0]["repetitions"][0]["answer"] = "different conclusion [A1]"
    with pytest.raises(ValueError, match="changed"):
        rubric.apply_reviews(report, reviews)


def test_true_citation_with_false_conclusion_is_wrong():
    report, reviews = _review_report()
    trial = report["raw_trials"]["C"][0]["repetitions"][0]
    report["answer_key"] = {"tasks": {"code": {"expected_points": [{"id": "condition"}], "forbidden_claims": []}}}
    for item in reviews["items"]:
        item.update(point_reviews={"condition": "met"}, false_claim_present=False)
    review = next(r for r in reviews["items"] if r["trial_id"] == trial["trial_id"])
    review.update(point_reviews={"condition": "wrong"}, completeness=0, false_claim_present=True)
    with pytest.raises(ValueError):
        rubric.apply_reviews(report, reviews)
    review["verdict"] = "incorrect"
    rubric.apply_reviews(report, reviews)
    assert report["raw_trials"]["C"][0]["repetitions"][0]["citation_valid"] is True
    assert report["variants"]["C"]["tasks"][0]["correct"] is False


def test_bootstrap_unit_is_task_not_repeat():
    raw = {arm: [{"id": str(i), "type": "定位", "repetitions": [{"correct": arm == "C"}] * 3}
                 for i in range(4)] for arm in ("A", "C")}
    result = protocol.paired_effect(raw, "C", "A", "correct")
    assert result["task_count"] == 4 and result["delta_pt"] == 100


def test_smoke_and_development_gold_do_not_qualify_for_publication():
    report, reviews = _review_report()
    report.update(task_scope="representative_smoke", runs_per_task=1)
    _refreeze_fixture_scores(report, reviews)
    rubric.apply_reviews(report, reviews)
    assert not report["acceptance"]["gates"]["frozen_semantic_holdout"]
    assert not report["acceptance"]["gates"]["full_three_repeat_run"]


def test_hidden_vectors_do_not_take_visible_top_k(kb, tmp_path):
    conn = reading_eval._copy(kb[0])
    try:
        ids = [r[0] for r in conn.execute("SELECT rowid FROM chunk WHERE visible=1 ORDER BY rowid LIMIT 2")]
        conn.execute("UPDATE chunk SET visible=0 WHERE rowid=?", (ids[0],))
        np.save(tmp_path / "vectors.npy", np.array([[1.0], [.1]], dtype="float32"))
        (tmp_path / "vec_ids.json").write_text(json.dumps(ids))
        emb = SimpleNamespace(encode=lambda _: np.array([[1.0]], dtype="float32"))
        assert engine.recall_vector(conn, "delete", emb, data_dir=str(tmp_path), limit=1) == [ids[1]]
    finally:
        conn.close()


def test_reading_no_key_or_missing_wiki_makes_no_model_calls(kb, tmp_path):
    assert reading_eval.evaluate(kb[0], "missing.yaml")["reason"] == "model_not_configured"
    model = _ProtocolModel()
    empty_wiki = reading_eval._copy(kb[0])
    empty_wiki.execute("UPDATE wiki_page SET status='stale'")
    empty_wiki.commit()
    result = reading_eval.evaluate(empty_wiki, str(_tasks(tmp_path)), client=model)
    empty_wiki.close()
    assert result["reason"] == "wiki_unavailable" and model.request_count == 0


def test_reading_protocol_uses_identical_code_index_and_isolated_trials(kb, tmp_path, monkeypatch):
    source = reading_eval._copy(kb[0])
    source.execute("INSERT OR REPLACE INTO wiki_page(id,level,title,md,status,sources) VALUES('test-wiki','repo','Wiki','test','ok','[]')")
    source.commit()
    records = []
    def fake_run(conn, question, **kwargs):
        assert conn.execute("SELECT COUNT(*) FROM chunk WHERE visible=1 AND kind IN ('wiki','experience')").fetchone()[0] == 0
        fingerprint = digest([tuple(r) for r in conn.execute("SELECT rowid,text FROM chunk WHERE visible=1 ORDER BY rowid")])
        wiki = conn.execute("SELECT COUNT(*) FROM wiki_page WHERE status='ok'").fetchone()[0]
        records.append((fingerprint, wiki, kwargs["read_mode"]))
        conn.execute("UPDATE wiki_page SET status='stale'")  # Must not contaminate next trial.
        raise RuntimeError("private-header-must-not-be-recorded")
    monkeypatch.setattr(agent, "run", fake_run)
    result = reading_eval.evaluate(source, str(_tasks(tmp_path)), client=_ProtocolModel(), runs=1)
    source.close()
    assert len(records) == 6 and len({r[0] for r in records}) == 1
    assert sum(r[1] > 0 for r in records) == 4
    assert sum(r[2] == "progressive" for r in records) == 2
    assert all(len(row["repetitions"]) == 1 for rows in result["raw_trials"].values() for row in rows)
    assert "private-header" not in json.dumps(result)
    assert not result["acceptance"]["passed"]
    assert "待审" in protocol.render(result)
    assert all("variant" not in row for row in result["blind_review"]["items"])


def test_frozen_gold_requires_hash_points_and_review(tmp_path):
    tasks_path = tmp_path / "tasks.yaml"
    tasks_path.write_text("existing gold")
    key = {"schema_version": 1, "task_set_hash": digest(tasks_path.read_bytes()),
           "split": "development_regression", "status": "draft",
           "tasks": {"t": {"expected_points": [{"id": "p1", "text": "expected condition", "evidence": "fixture:L1"}],
                           "forbidden_claims": ["opposite condition"]}}}
    path = tmp_path / "answers.yaml"
    path.write_text(yaml.safe_dump(key))
    assert not protocol.answer_key(path, tasks_path, [{"id": "t"}])["effect_eligible"]
    tasks_path.write_text("changed gold")
    with pytest.raises(ValueError, match="hash"):
        protocol.answer_key(path, tasks_path, [{"id": "t"}])


def test_flat_no_wiki_is_not_forced_into_rule_fallback(kb):
    conn = reading_eval._copy(kb[0])
    conn.execute("UPDATE wiki_page SET status='stale'")
    conn.commit()
    try:
        run = agent.run(conn, "parse_value 如何工作？", mode="model", read_mode="flat",
                        llm_client=_ProtocolModel(), data_dir=kb[1])
        assert run["execution_mode"] == "model"
        assert run["fallback_reason"] != "wiki_unavailable"
    finally:
        conn.close()


def test_reading_report_round_trips_through_review_cli(kb, tmp_path):
    from typer.testing import CliRunner
    from codeatlas.cli import app
    conn = reading_eval._copy(kb[0])
    try:
        generator.generate(conn, "tests/fixtures/mini_c")
        result = reading_eval.evaluate(conn, str(_tasks(tmp_path)), client=_ProtocolModel(), runs=1)
    finally:
        conn.close()
    packet = result["blind_review"]
    scores = {"identity": packet["identity"], "evaluation_hash": packet["evaluation_hash"],
              "items": [{"trial_id": item["trial_id"], "reviewer": "synthetic_test_review",
                         "reviewed_at": "2026-09-05", "note": "fixture protocol only",
                         "verdict": "reasonable_refusal" if item["refused"] else "correct",
                         "completeness": 1} for item in packet["items"]]}
    raw = tmp_path / "raw.json"
    review = tmp_path / "scores.json"
    out = tmp_path / "reviewed.json"
    raw.write_text(json.dumps(result))
    review.write_text(json.dumps(scores))
    original = raw.read_bytes()
    response = CliRunner().invoke(app, ["eval", "review", str(raw), str(review), str(out)])
    assert response.exit_code == 0, response.output
    assert raw.read_bytes() == original
    updated = json.loads(out.read_text())
    assert updated["answer_review_status"] == "completed"
    assert not updated["acceptance"]["passed"]  # Fake model, regression data, only one repeat.
    assert "共享代码检索" in out.with_suffix(".md").read_text()


def test_existing_model_results_cannot_be_overwritten_before_requests(kb, tmp_path):
    target = tmp_path / "run.md"
    target.write_text("earlier failed run")
    model = _ProtocolModel()
    with pytest.raises(ValueError, match="output exists"):
        model_eval.main_abc(kb[0], str(_tasks(tmp_path)), client=model, data_dir=kb[1], out=str(target))
    assert model.request_count == 0 and target.read_text() == "earlier failed run"


def test_planner_is_told_when_no_wiki_exists(monkeypatch):
    from codeatlas.llm.client import OpenAICompatClient
    client = OpenAICompatClient("http://unused.test", "fixture-key", "fixture")
    messages = []
    def fake_chat(system, user, **kwargs):
        messages.append(system)
        return '{"action":"finish"}'
    monkeypatch.setattr(client, "_chat", fake_chat)
    client.next_action("public fixture", [{"wiki_available": False}], read_mode="flat")
    assert "wiki_outline(module?)" not in messages[0]
    assert "当前没有可读 Wiki" in messages[0]


def test_review_cannot_reuse_scores_after_model_or_protocol_change(kb, tmp_path):
    report = model_eval.evaluate_abc(kb[0], str(_tasks(tmp_path)), client=_ProtocolModel(), runs=1, data_dir=kb[1])
    review = {"identity": report["identity"], "evaluation_hash": report["evaluation_hash"], "items": []}
    report["protocol"]["output_token_limit"] = 9000
    with pytest.raises(ValueError, match="protocol or answer key"):
        rubric.apply_reviews(report, review)


def test_failed_experience_retrieval_is_not_always_a_review_blocker():
    from codeatlas.eval.task_eval import failure_groups, EXPERIENCE_TYPE
    rows = [{"id": "no-card", "type": EXPERIENCE_TYPE, "passed": False, "blocked_by_review": True},
            {"id": "missed-approved-card", "type": EXPERIENCE_TYPE, "passed": False, "blocked_by_review": False}]
    blocked, failures = failure_groups(rows)
    assert [r["id"] for r in blocked] == ["no-card"]
    assert [r["id"] for r in failures] == ["missed-approved-card"]


def test_wiki_value_is_not_conflated_with_progressive_order_value():
    report, _ = _review_report()
    report["report_kind"] = "reading"
    mapping = {"A": "source_only", "B": "wiki_flat", "C": "wiki_progressive"}
    report["raw_trials"] = {mapping[k]: v for k, v in report["raw_trials"].items()}
    report["variants"] = {mapping[k]: v for k, v in report["variants"].items()}
    for key, rows in report["raw_trials"].items():
        for row in rows:
            for trial in row["repetitions"]:
                trial.update(human_review={"reviewer": "synthetic_test_review"},
                             correct=key != "source_only", complete=1 if key != "source_only" else 0)
    rubric.finalize(report)
    assert report["acceptance"]["primary_contrast"]["treatment"] == "wiki_flat"
    assert report["acceptance"]["gates"]["codeatlas_gain"]
    assert report["acceptance"]["secondary_reading_contrasts"]["wiki_flat"]["accuracy"]["delta_pt"] == 0
    assert not report["acceptance"]["passed"]  # Not human-frozen holdout evidence.
