"""Architecture regressions: independent from the unchanged 40-task gold set."""
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from codeatlas import db, dependencies, publication, snapshots
from codeatlas.contracts import answer_verification, budget_messages, estimated_tokens, validate_fact_selection
from codeatlas.experience import store
from codeatlas.eval.model_eval import _abc_grade


@pytest.fixture
def snapshot_repo(tmp_path):
    source = tmp_path / "corpus"
    shutil.copytree(Path(__file__).parent / "fixtures/mini_c", source)
    commands = [{"directory": str(source), "file": str(p), "arguments": ["clang", "-I" + str(source), "-c", str(p)]}
                for p in sorted(source.glob("*.c"))]
    (source / "compile_commands.json").write_text(json.dumps(commands))
    control = db.connect(tmp_path / "control.db")
    db.set_meta(control, "revision", "fixture")
    control.commit()
    return source, control


def test_snapshot_freezes_sources_pins_reads_and_fails_closed(snapshot_repo):
    source, control = snapshot_repo
    with pytest.raises(snapshots.SnapshotError, match="not_ready"):
        with snapshots.pin(control):
            pass
    snapshots.build(control, str(source), str(source / "compile_commands.json"), strict=False, activate_after=True)
    active = snapshots.status(control)["active"]
    with snapshots.pin(control) as old:
        old_bytes = (Path(db.get_meta(old.conn, "repo")) / "mini.c").read_bytes()
        with pytest.raises(sqlite3.OperationalError):
            old.conn.execute("DELETE FROM node")
        (source / "mini.c").write_text((source / "mini.c").read_text() + "\nint new_version(void) {return 42;}\n")
        snapshots.build(control, str(source), str(source / "compile_commands.json"), strict=False, activate_after=True)
        assert snapshots.status(control)["active"] != active
        assert old.conn.execute("SELECT 1 FROM node WHERE name='new_version'").fetchone() is None
        assert (Path(db.get_meta(old.conn, "repo")) / "mini.c").read_bytes() == old_bytes
    with snapshots.pin(control) as new:
        assert new.conn.execute("SELECT 1 FROM node WHERE name='new_version'").fetchone()
        ids = {r[0] for r in new.conn.execute("SELECT id FROM node WHERE kind='file'")}
    previous = snapshots.status(control)["active"]
    (source / "mini.c").write_text("#include \"nonexistent_header_567.h\"\n")
    with pytest.raises(snapshots.SnapshotError, match="diagnostics"):
        snapshots.build(control, str(source), str(source / "compile_commands.json"), strict=False, activate_after=True)
    assert snapshots.status(control)["active"] == previous
    snapshots.activate(control, active, rollback=True)
    with snapshots.pin(control) as rolled:
        assert {r[0] for r in rolled.conn.execute("SELECT id FROM node WHERE kind='file'")} == ids
        assert rolled.conn.execute("SELECT 1 FROM node WHERE name='new_version'").fetchone() is None


def test_durable_publication_recovers_prepared_and_replays_committed(tmp_path):
    conn = db.connect(tmp_path / "journal.db")
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text("old")
    op = publication.prepare(conn, [(a, "new"), (b, "new")])
    assert a.read_text() == "new"
    assert publication.recover(conn) == [op]
    assert a.read_text() == "old" and not b.exists()
    op = publication.prepare(conn, [(a, "approved"), (b, "superseded")])
    publication.commit(conn, op)
    a.write_text("half-crash")
    publication.recover(conn)
    assert a.read_text() == "approved" and b.read_text() == "superseded"


def test_real_reference_does_not_prove_false_answer(kb):
    conn, _ = kb
    from codeatlas.agent import _node_citation
    node = conn.execute("SELECT * FROM node WHERE name='cJSON_Delete' AND is_definition=1").fetchone()
    citation = _node_citation(conn, dict(node), "A1")
    answer = "cJSON_Delete 总会释放 reference 节点的 child，完全不检查类型标志 [A1]。"
    verdict = answer_verification(answer, [citation], conn)
    assert verdict["citation_valid"] and not verdict["trusted"]
    assert verdict["answer_correctness"] == "pending_human_review"
    task = {"type": "定位", "expected_anchors": [{"path": node["path"], "line_start": node["line_start"]}]}
    scored = _abc_grade(task, {"answer": answer, "citations": [citation]}, variant="C")
    assert scored["retrieval_hit"] and not scored["correct"]
    citation["provenance"]["usr"] = "fake-usr"
    assert not answer_verification(answer, [citation], conn)["citation_valid"]


def test_exact_predicate_and_normalization(kb):
    conn, _ = kb
    row = conn.execute("SELECT * FROM semantic_fact LIMIT 1").fetchone()
    claim = {"fact_id": row["id"], "payload": json.loads(row["payload_json"])}
    assert validate_fact_selection(conn, [claim])
    claim["payload"]["invented_condition"] = "always releases child"
    assert not validate_fact_selection(conn, [claim])
    assert dependencies.normalized("return 0;") != dependencies.normalized("return 1;")
    assert dependencies.normalized("x < 9") != dependencies.normalized("x <= 9")
    assert dependencies.normalized("return /*comment*/ 0;") == dependencies.normalized("return 0;")


def test_card_dependencies_ignore_unconsumed_entities_but_track_anchor(kb):
    conn, _ = kb
    anchor = conn.execute(
        "SELECT * FROM node WHERE name='parse_value' AND is_definition=1"
    ).fetchone()
    card = store.add(conn, {
        "title": "parse_value dependency scope",
        "symptom": "parse_value rejects a fixture input",
        "root_cause": "the parse_value definition controls this fixture claim",
        "fix_steps": "inspect parse_value",
        "verification": "rerun the fixture",
        "evidence_tags": ["A1"],
    })
    store.bind(conn, card, anchor["usr"])
    result = store.set_dependencies(conn, card, entity_names=["parse_value"])
    assert 1 < len(result["dependencies"]) < conn.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    conn.execute(
        "INSERT INTO node(id,kind,name,usr,repo,path,line_start,line_end,signature,definition_hash,is_definition,is_static,extra) "
        "VALUES('unconsumed-node','function','unconsumed','usr:unconsumed','fixture','mini.c',1,1,'','new',1,0,'{}')"
    )
    conn.commit()
    assert dependencies.check(conn, card) == (True, "")
    conn.execute("UPDATE node SET definition_hash='changed' WHERE id=?", (anchor["id"],))
    conn.commit()
    valid, reason = dependencies.check(conn, card)
    assert not valid and reason == f"dependency_changed:{anchor['id']}"


def test_dependency_curation_reprojects_the_active_snapshot(snapshot_repo):
    source, control = snapshot_repo
    snapshots.build(
        control, str(source), str(source / "compile_commands.json"),
        strict=False, activate_after=True,
    )
    with snapshots.pin(control) as current:
        node = current.conn.execute(
            "SELECT * FROM node WHERE name='parse_value' AND is_definition=1"
        ).fetchone()
    card = store.add(control, {
        "title": "snapshot dependency curation",
        "symptom": "parse_value fixture dependency",
        "root_cause": "the parser definition controls this fixture claim",
        "fix_steps": "inspect the reviewed parser inputs",
        "verification": "rerun the fixture",
        "evidence_tags": ["A1"],
    })
    store.bind(control, card, node["usr"])
    before = snapshots.status(control)["active"]
    result = store.set_dependencies(
        control, card, entity_names=["parse_value"], call_depth=0,
    )
    after = snapshots.status(control)["active"]
    assert after != before
    assert result["dependencies"] == json.loads(
        store.get(control, card)["dependency_manifest"]
    )
    with snapshots.pin(control) as current:
        projected = store.get(current.conn, card)
        assert projected["status"] == "pending"
        assert json.loads(projected["dependency_manifest"]) == result["dependencies"]
        assert snapshots.verify(current.directory)["knowledge_set_id"] == after


def test_source_dependency_fingerprint_selects_definition_not_calls(tmp_path):
    source = tmp_path / "sample.c"
    source.write_text("""
        #define LIMIT 3
        int target(int value);
        int caller(int value) { if (target(value)) { return 1; } return 0; }
        int target(int value) { return value < LIMIT; }
    """, encoding="utf-8")
    spec = [{"path": "sample.c", "name": "target"}]
    macro = [{"name": "LIMIT", "paths": ["sample.c"]}]
    before = dependencies.source_dependency_fingerprints(tmp_path, spec, macro)
    source.write_text(source.read_text().replace("return 1;", "return 2;"), encoding="utf-8")
    assert dependencies.source_dependency_fingerprints(tmp_path, spec, macro) == before
    source.write_text(source.read_text().replace("value < LIMIT", "value <= LIMIT"), encoding="utf-8")
    assert dependencies.source_dependency_fingerprints(tmp_path, spec, macro) != before


def test_cycle_threshold_never_means_safe():
    result = dependencies.propagate({"a"}, {"a": {"b"}, "b": {"c"}, "c": {"a", "d"}}, 4)
    assert result["affected"] == ["a", "b", "c", "d"]
    assert result["mode"] == "full_revalidation"


def test_shared_budget_counts_complete_input_and_preserves_limit():
    system, user, usage = budget_messages("系统" * 200, "源码事实\n" * 20000)
    assert usage["truncated"]
    assert usage["estimated_input_tokens"] <= 8000
    assert estimated_tokens(system + user) <= 8000


def _approved_card(conn):
    node = conn.execute("SELECT * FROM node WHERE name='parse_value' AND is_definition=1").fetchone()
    card = store.add(conn, {"title": "synthetic governance case", "symptom": "fixture symptom",
                            "root_cause": "fixture root cause", "fix_steps": "fixture next step",
                            "verification": "fixture check", "evidence_tags": ["A1"]})
    store.bind(conn, card, node["usr"], bound_by="synthetic_test_review")
    store.attach_experiments(
        conn, card, ["examples/reproductions/cjson-nesting/result.txt"]
    )
    qa = store.qa_add(conn, card, "fixture QA", ["fixture expected point"], ["A1"])
    store.qa_review(conn, qa["id"], True, "synthetic test only", "synthetic_test_review")
    store.confirm_review(
        conn, card, store.review_bundle(conn, card), "synthetic_test_review"
    )
    store.review(conn, card, "approve", "synthetic test only", "synthetic_test_review")
    return card, qa["id"]


def test_qa_content_changes_and_historical_card_replay_are_rejected(snapshot_repo):
    source, conn = snapshot_repo
    snapshots.build(conn, str(source), str(source / "compile_commands.json"), strict=False, activate_after=True)
    card, qa = _approved_card(conn)
    with snapshots.pin(conn) as context:
        assert context.conn.execute("SELECT visible FROM chunk WHERE uid=?", ("exp:" + card,)).fetchone()[0] == 1
    published = store.get(conn, card)
    old_md = Path(published["canonical_path"]).read_text()
    old_set = snapshots.status(conn)["active"]
    store.qa_review(conn, qa, False, "counterexample found", "synthetic_test_review")
    assert store.get(conn, card)["status"] == "stale"
    snapshots.activate(conn, old_set, rollback=True)
    with snapshots.pin(conn) as context:
        assert context.conn.execute("SELECT visible FROM chunk WHERE uid=?", ("exp:" + card,)).fetchone()[0] == 0
    duplicate = Path(published["canonical_path"]).with_name("zz-replay.md")
    duplicate.write_text(old_md)
    with pytest.raises(ValueError, match="journal|duplicate"):
        store.rebuild(conn, rebuild_index=False)
    assert store.get(conn, card)["status"] == "stale"


def test_publication_retry_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "control.db")
    files = [(tmp_path / "card.md", "approved")]
    op = publication.prepare(conn, files, "same-op")
    publication.commit(conn, op)
    revision = db.get_meta(conn, "governance_revision")
    assert publication.prepare(conn, files, "same-op") == op
    publication.commit(conn, op)
    assert db.get_meta(conn, "governance_revision") == revision
    with pytest.raises(ValueError, match="conflicts"):
        publication.prepare(conn, [(files[0][0], "changed")], "same-op")


def test_external_macro_is_frozen_and_revalidated(snapshot_repo, tmp_path):
    source, conn = snapshot_repo
    external = tmp_path / "external-header"
    external.mkdir()
    (external / "limit.h").write_text("#define EXTERNAL_LIMIT 9\n")
    (source / "external.c").write_text('#include "limit.h"\nint external_limit(void) { return EXTERNAL_LIMIT; }\n')
    path = source / "compile_commands.json"
    commands = json.loads(path.read_text())
    commands.append({"directory": str(source), "file": str(source / "external.c"),
                     "arguments": ["clang", "-I" + str(external), "-c", str(source / "external.c")]})
    path.write_text(json.dumps(commands))
    snapshots.build(conn, str(source), str(path), strict=False, activate_after=True)
    with snapshots.pin(conn) as first:
        config = first.manifest["build_config_hash"]
        assert str(external / "limit.h") in first.manifest["external_dependencies"]
        (external / "limit.h").write_text("#define EXTERNAL_LIMIT 10\n")
        snapshots.build(conn, str(source), str(path), strict=False, activate_after=True)
        assert snapshots.verify(first.directory)["build_config_hash"] == config
    with snapshots.pin(conn) as second:
        assert second.manifest["build_config_hash"] != config


def _review_report():
    from codeatlas.eval import rubric, task_eval
    report = {"identity": {"fixture": "v1"}, "acceptance": {"gates": {}}, "variants": {}, "raw_trials": {}}
    for variant in ("A", "B", "C"):
        rows = []
        for name, kind in (("code", "定位"), ("experience", task_eval.EXPERIENCE_TYPE), ("refusal", task_eval.REFUSAL_TYPE)):
            rows.append({"id": name, "type": kind, "repetitions": [{"answer": "fixture evidence [A1]",
                "citations": [], "citation_valid": True, "tool_calls": 1, "native_planning": kind != task_eval.REFUSAL_TYPE,
                "refused": kind == task_eval.REFUSAL_TYPE, "correct": False, "complete": 0,
                "anchor_recall": 1 if variant == "C" else .5, "input_tokens": 100}]})
        report["raw_trials"][variant] = rows
        report["variants"][variant] = {"metrics": {"accuracy": 0}, "tasks": [{"id": r["id"]} for r in rows]}
    packet = rubric.blind_packet(report)
    reviews = {"identity": report["identity"], "items": [{"trial_id": item["trial_id"],
        "verdict": "reasonable_refusal" if item["refused"] else "correct", "completeness": 1,
        "reviewer": "synthetic_test_review", "note": "fixture only", "reviewed_at": "2026-09-05"} for item in packet["items"]]}
    return report, reviews


def test_review_aggregation_refusal_gold_and_fixed_native_denominator():
    from codeatlas.eval import rubric, task_eval
    report, reviews = _review_report()
    rubric.apply_reviews(report, reviews)
    # More anchor hits with identical answers are not a knowledge-quality gain.
    assert not report["acceptance"]["passed"]
    assert not report["acceptance"]["gates"]["codeatlas_gain"]
    assert report["variants"]["C"]["metrics"]["accuracy"] == 100
    assert report["variants"]["C"]["by_type"][task_eval.EXPERIENCE_TYPE]["accuracy"] == 100
    assert report["acceptance"]["c_experience_accuracy"] == 100
    assert all(t["correct"] for t in report["variants"]["C"]["tasks"])
    failed = report["raw_trials"]["C"][0]["repetitions"][0]
    failed.update(native_planning=False, refused=True)
    failed["fallback_reason"] = "invalid_tool"
    rubric.finalize(report)
    assert report["acceptance"]["native_planning_by_variant"]["C"] == 50
    assert report["variants"]["C"]["metrics"]["fallback_rate"] == pytest.approx(100 / 3)
    assert not report["acceptance"]["passed"]
    bad_review = next(r for r in reviews["items"] if r["trial_id"] == failed["trial_id"])
    bad_review["verdict"] = "reasonable_refusal"
    with pytest.raises(ValueError, match="gold refusal"):
        rubric.apply_reviews(report, reviews)


def test_report_identity_uses_active_facts_and_changes_on_switch(snapshot_repo, tmp_path):
    from codeatlas.eval import rubric, acceptance_eval
    source, control = snapshot_repo
    snapshots.build(control, str(source), str(source / "compile_commands.json"), strict=False, activate_after=True)
    task_path = tmp_path / "tasks.yaml"
    task_path.write_text("fixture identity")
    corpus = {"id": "cjson", "database": str(snapshots.database_path(control)), "task_manifest": str(task_path),
              "checkout_url": "fixture", "revision": "fixture"}
    with snapshots.pin(control) as context:
        ident = rubric.identity(context.conn, task_path)
    report = {"identity": ident, "answer_review_status": "completed", "raw_trials": {"A": ["fixture"]},
              "status": "completed", "repository": "fixture", "revision": "fixture",
              "task_set_hash": acceptance_eval._sha256(task_path), "runs_per_task": 3, "acceptance": {"passed": False}}
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/MODEL-ABC-CJSON.json").write_text(json.dumps(report))
    assert acceptance_eval.model_abc_status([corpus], project_root=tmp_path)["status"] == "completed"
    snapshots.activate(control, snapshots.status(control)["active"], rollback=True)
    assert acceptance_eval.model_abc_status([corpus], project_root=tmp_path)["status"] == "stale"


def test_delete_guard_is_not_whole_function_no_free_claim(tmp_path):
    from codeatlas.parser import ast_walker
    source = Path(__file__).parent / "fixtures/control_flow"
    walker = ast_walker.AstWalker(str(source))
    assert walker.walk_unit(str(source / "lifetime.c"), [], str(source)) == 0
    conn = db.connect(tmp_path / "flow.db")
    ast_walker.persist(conn, walker.result)
    node = conn.execute("SELECT id FROM node WHERE name='cJSON_Delete' AND is_definition=1").fetchone()
    facts = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM semantic_fact WHERE function_id=?", (node[0],))]
    guards = [f for f in facts if "condition" in f]
    assert any("cJSON_IsReference" in f["condition"] and "child" in f["condition"] for f in guards)
    assert any("valuestring" in f["condition"] for f in guards)
    all_facts = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM semantic_fact")]
    assert any(f.get("implicit_else") for f in all_facts)
    assert any(len(f.get("enclosing_conditions", [])) == 2 for f in all_facts)
    assert any(f.get("case_labels") and len(f["case_labels"]) == 3 for f in all_facts)
    assert sum(bool(f.get("components")) for f in all_facts) == 3
    assert all(f.get("error_classification") == "unknown" for f in all_facts if "expression" in f)


def test_wiki_dependencies_and_change_audit_are_persisted(snapshot_repo):
    source, conn = snapshot_repo
    snapshots.build(conn, str(source), str(source / "compile_commands.json"), strict=False, activate_after=True)
    with snapshots.pin(conn) as first:
        assert first.conn.execute("SELECT count(*) FROM knowledge_dependency WHERE owner_type='wiki'").fetchone()[0] > 0
    (source / "mini.c").write_text((source / "mini.c").read_text().replace("return 0;", "return 1;"))
    snapshots.build(conn, str(source), str(source / "compile_commands.json"), strict=False, activate_after=True)
    with snapshots.pin(conn) as second:
        assert second.manifest["invalidation"]["changed_count"] > 0
        assert second.conn.execute("SELECT 1 FROM invalidation_event WHERE before_hash<>after_hash").fetchone()


def test_baseline_six_legal_tools_summarize_without_seventh_plan(kb):
    from codeatlas.eval.model_eval import _baseline_once
    class Client:
        count = 0
        def next_baseline_action(self, query, events, *, fixed_skill):
            self.count += 1
            return {"action": "tool", "tool": "repo_search", "arguments": {"query": "parse_value"}}
        def answer(self, system, query, context):
            assert "parse_value" in context
            return "解析入口 [A1]"
    client = Client()
    run = _baseline_once(kb[0], {"question": "parse_value"}, client=client, fixed_skill=False)
    assert client.count == 5 and run["tool_calls"] == 6
    assert run["events"][0]["preflight"] is True
    assert run["native_planning"] and not run["fallback_reason"]


def test_review_ui_does_not_override_versioned_review_with_legacy_auto_approval():
    html = (Path(__file__).parents[1] / "src/codeatlas/server/static/index.html").read_text()
    assert "reviewHtml=function" not in html and "approveCard=async function" not in html
    assert "绑定、QA 并批准" not in html
    assert "data-bundle" in html and "expected_bundle_hash" in html


def test_registry_canonical_tags_and_snapshot_propagate_to_tool_results(kb):
    from codeatlas import agent
    conn, _ = kb
    node = dict(conn.execute("SELECT * FROM node WHERE name='parse_value' AND is_definition=1").fetchone())
    a = agent._node_citation(conn, node, "A1")
    b = dict(a, uid="code-range:fixture", tag="A1")
    event = {"tool": "code_read", "phase": "verify", "arguments": {}, "result": {"citations": [b], "text": "return important_source;"}}
    registry = agent._build_registry(conn, {"citations": [a]}, [event])
    assert registry[1]["tag"] == event["result"]["citations"][0]["tag"] == "A2"
    assert "knowledge_set_id" in registry[1]["provenance"]
    assert "return important_source;" in agent._compact_events([event])[0]["result"]["text"]


def test_evaluation_api_pins_current_set_and_hides_stale_reports(snapshot_repo, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import codeatlas.server.app as srv
    from codeatlas.eval.acceptance_eval import implementation_hash
    source, control = snapshot_repo
    snapshots.build(control, str(source), str(source / "compile_commands.json"), strict=False, activate_after=True)
    monkeypatch.setattr(srv, "DB", str(snapshots.database_path(control)))
    monkeypatch.setattr(srv, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "PROJECT_ROOT", tmp_path)
    with snapshots.pin(control) as context:
        current = context.knowledge_set_id
        repository = db.get_meta(context.conn, "repository_id")
        revision = db.get_meta(context.conn, "revision")
    (tmp_path / "docs").mkdir()
    report = {"schema_version": 1, "claims": [], "codeatlas": {"implementation_hash": implementation_hash(tmp_path)},
              "corpora": [{"database": {"repository": repository, "revision": revision, "knowledge_set_id": "old-set"}}]}
    path = tmp_path / "docs/ACCEPTANCE-CJSON-LWIP.json"
    path.write_text(json.dumps(report))
    client = TestClient(srv.app)
    response = client.get("/api/evaluation/latest")
    assert response.headers["X-CodeAtlas-Knowledge-Set"] == current
    assert not response.json()["available"]
    assert "acceptance:knowledge_set_changed" in response.json()["stale_reports"]
    report["corpora"][0]["database"]["knowledge_set_id"] = current
    path.write_text(json.dumps(report))
    assert client.get("/api/evaluation/latest").json()["available"]
    report["codeatlas"]["implementation_hash"] = "old-implementation"
    path.write_text(json.dumps(report))
    assert "acceptance:implementation_changed" in client.get("/api/evaluation/latest").json()["stale_reports"]


def test_evaluation_api_exposes_only_hash_valid_value_proof_coverage(
        snapshot_repo, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import codeatlas.server.app as srv
    from codeatlas.contracts import digest
    source, control = snapshot_repo
    snapshots.build(control, str(source), str(source / "compile_commands.json"),
                    strict=False, activate_after=True)
    monkeypatch.setattr(srv, "DB", str(snapshots.database_path(control)))
    monkeypatch.setattr(srv, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "PROJECT_ROOT", tmp_path)
    (tmp_path / "docs").mkdir()
    with snapshots.pin(control) as context:
        repository = db.get_meta(context.conn, "repository_id")
        revision = db.get_meta(context.conn, "revision")
    report = {
        "schema_version": 1, "kind": "value_proof_coverage_inventory",
        "target_mechanisms_per_repository": 40,
        "corpora": [{"id": "cjson", "repository": repository,
                      "revision": revision, "automatic_candidate_count": 38,
                      "target_count": 40, "selected_count": 38,
                      "status": "coverage_blocked"}],
        "boundary": "frozen coverage only",
    }
    report["inventory_hash"] = digest(report)
    report.update(status="coverage_blocked", coverage_ready=False)
    path = tmp_path / "docs/VALUE-PROOF-COVERAGE.json"
    path.write_text(json.dumps(report))
    client = TestClient(srv.app)
    payload = client.get("/api/evaluation/latest").json()
    assert payload["available"] is True
    assert payload["coverage"]["corpora"][0]["automatic_candidate_count"] == 38
    green = dict(report)
    green.update(status="approved", coverage_ready=True)
    path.write_text(json.dumps(green))
    stale = client.get("/api/evaluation/latest").json()
    assert stale["available"] is False
    assert "value-proof-coverage:hash_mismatch" in stale["stale_reports"]
    changed = dict(report)
    changed["corpora"] = [{**report["corpora"][0], "automatic_candidate_count": 39}]
    path.write_text(json.dumps(changed))
    stale = client.get("/api/evaluation/latest").json()
    assert stale["available"] is False
    assert "value-proof-coverage:hash_mismatch" in stale["stale_reports"]
