"""Public Agent / conversation / knowledge-card loop tests; no network or API key."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from codeatlas import agent, conversation, db as dbm
from codeatlas.experience import store
from codeatlas.eval import workflow_eval
from codeatlas.indexer import build as indexer
from codeatlas.retrieve import engine


def _session_file(tmp_path: Path, *, content: str = "审查 parse_value 的调用路径",
                  session_id: str = "fixture-public-session") -> Path:
    payload = {
        "schema_version": 1, "id": session_id, "source_type": "public_synthetic",
        "source_ref": "tests/public-session.json", "license": "CC0-1.0",
        "repository": "tests/fixtures/mini_c", "revision": "fixture",
        "messages": [
            {"role": "user", "content": content},
            {"role": "developer", "content": "先查看函数定义和确定调用链。"},
        ],
    }
    path = tmp_path / "session.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _card_data() -> dict:
    return {
        "title": "parse_value 公开审查", "symptom": "需要审查 parse_value 的确定调用路径",
        "hypotheses": ["调用关系应来自 AST"], "dead_ends": ["只做文本猜测"],
        "root_cause": "候选结论，待绑定源码锚点", "fix_steps": "定位函数并核对调用关系",
        "verification": "检查固定快照的定义和调用者", "related_symbols": ["parse_value"],
        "confidence": "medium", "evidence_tags": ["A1"], "evidence_turns": [1, 2],
    }


def _pass_qa(conn, card_id: str) -> None:
    store.attach_experiments(
        conn, card_id, ["examples/reproductions/cjson-nesting/result.txt"]
    )
    qa = store.qa_add(conn, card_id, "结论对应哪个源码事实？", ["函数定义与调用链"], ["A1"])
    store.qa_review(conn, qa["id"], True, "已用固定源码锚点核对")
    store.confirm_review(conn, card_id, store.review_bundle(conn, card_id), "local-reviewer")


def test_public_session_is_deduplicated_and_never_indexed(kb, tmp_path):
    conn, data_dir = kb
    raw_marker = "RAW-CONVERSATION-MUST-NOT-ENTER-RETRIEVAL-9273"
    result = conversation.import_public_session(
        conn, _session_file(tmp_path, content=f"{raw_marker} 审查 parse_value",
                            session_id="fixture-raw-session")
    )
    assert result["imported"] is True
    assert conversation.import_public_session(
        conn, _session_file(tmp_path, content=f"{raw_marker} 审查 parse_value",
                            session_id="fixture-raw-session")
    )["duplicate"] is True
    assessment = conversation.assess_session(conn, result["id"])
    assert assessment["eligible"] is True and all(assessment["checks"].values())
    assert conversation.get_session(conn, result["id"])["assessment"]["eligible"] is True
    indexer.build(conn, "tests/fixtures/mini_c", out_dir=data_dir)
    assert conn.execute(
        "SELECT COUNT(*) FROM chunk WHERE text LIKE ?", (f"%{raw_marker}%",)
    ).fetchone()[0] == 0


def test_sensitive_public_session_is_rejected(kb, tmp_path):
    conn, _ = kb
    with pytest.raises(conversation.ConversationError, match="敏感"):
        conversation.import_public_session(conn, _session_file(tmp_path, content="token=secret-value"))


def test_session_assessment_rejects_non_reproducible_input(kb, tmp_path):
    conn, _ = kb
    session_id = conversation.import_public_session(
        conn, _session_file(tmp_path, content="偶尔出错，但没有提供任何定位信息",
                            session_id="fixture-insufficient-session")
    )["id"]
    assessment = conversation.assess_session(conn, session_id)
    assert assessment["eligible"] is False
    assert assessment["checks"]["reproducible_input"] is False
    assert assessment["reasons"]


def test_card_requires_exact_anchor_and_becomes_stale(kb, tmp_path):
    conn, data_dir = kb
    session_id = conversation.import_public_session(conn, _session_file(tmp_path))["id"]
    card_id = store.add(conn, _card_data(), source_type="public_synthetic",
                        source_ref="tests/public-session.json", session_id=session_id)
    with pytest.raises(ValueError, match="锚点"):
        store.review(conn, card_id, "approve", "没有绑定不能通过")
    node = conn.execute(
        "SELECT * FROM node WHERE name='parse_value' AND is_definition=1"
    ).fetchone()
    assert node["usr"] and node["definition_hash"]
    original_start, original_end, original_hash = node["line_start"], node["line_end"], node["definition_hash"]
    store.bind(conn, card_id, node["usr"])
    _pass_qa(conn, card_id)
    assert store.review(conn, card_id, "approve", "已核对公开源码") == {"id": card_id, "status": "approved"}
    assert store.get(conn, card_id)["reviews"][0]["action"] == "approve"
    indexer.build(conn, "tests/fixtures/mini_c", out_dir=data_dir)
    visible = engine.ask(conn, "parse_value 公开审查", data_dir=data_dir)
    assert any(c["uid"] == f"exp:{card_id}" and c["level"] == "B" for c in visible["citations"])
    # Moving a byte-identical definition does not stale a card, but the B citation
    # must point to the current code range rather than its historical location.
    conn.execute("UPDATE node SET line_start=777, line_end=790 WHERE id=?", (node["id"],))
    assert store.stale_check(conn)["stale"] == []
    relocated = engine.ask(conn, "parse_value 公开审查", data_dir=data_dir)
    b_citation = next(c for c in relocated["citations"] if c["uid"] == f"exp:{card_id}")
    assert b_citation["provenance"]["line_start"] == 777
    conn.execute("UPDATE node SET definition_hash='changed' WHERE id=?", (node["id"],))
    stale = store.stale_check(conn)
    assert any(item["id"] == card_id for item in stale["stale"])
    assert store.get(conn, card_id)["reviews"][0]["action"] == "stale"
    hidden = engine.ask(conn, "parse_value 公开审查", data_dir=data_dir)
    assert not any(c["uid"] == f"exp:{card_id}" for c in hidden["citations"])
    # kb is session-scoped; restore source facts so unrelated task-eval tests retain
    # their fixed fixture anchors. The knowledge card remains stale, as intended.
    conn.execute("UPDATE node SET line_start=?, line_end=?, definition_hash=? WHERE id=?",
                 (original_start, original_end, original_hash, node["id"]))
    conn.commit()


def test_approved_replacement_supersedes_prior_card_only_after_review(kb, tmp_path):
    conn, data_dir = kb
    session_id = conversation.import_public_session(conn, _session_file(tmp_path))[
        "id"
    ]
    node = conn.execute(
        "SELECT * FROM node WHERE name='parse_value' AND is_definition=1"
    ).fetchone()
    first_data = _card_data() | {"title": "parse_value 原始排障结论"}
    first_id = store.add(conn, first_data, source_type="public_synthetic",
                         source_ref="tests/prior-card.json", session_id=session_id)
    store.bind(conn, first_id, node["usr"])
    _pass_qa(conn, first_id)
    store.review(conn, first_id, "approve", "原始结论已核对")

    replacement_data = _card_data() | {
        "title": "parse_value 更新后的排障结论",
        "root_cause": "补充验证后的公开结论，仍需人工审核",
    }
    replacement_id = store.add(
        conn, replacement_data, source_type="public_synthetic",
        source_ref="tests/replacement-card.json", session_id=session_id,
    )
    store.bind(conn, replacement_id, node["usr"])
    _pass_qa(conn, replacement_id)
    lineage = store.link_supersedes(conn, replacement_id, first_id)
    assert lineage["effective"] is False
    assert store.get(conn, first_id)["status"] == "approved"

    with pytest.raises(ValueError, match="QA"):
        store.review(conn, replacement_id, "approve", "旧 QA 不得覆盖新的替代关系")
    qa_id = store.get(conn, replacement_id)["qas"][0]["id"]
    store.qa_review(conn, qa_id, True, "重新核对含替代关系的完整材料")
    store.confirm_review(
        conn, replacement_id, store.review_bundle(conn, replacement_id), "local-reviewer"
    )
    approved = store.review(conn, replacement_id, "approve", "更新结论已复核")
    assert approved["superseded_id"] == first_id
    assert store.get(conn, first_id)["status"] == "superseded"
    assert store.get(conn, first_id)["reviews"][0]["action"] == "supersede"
    assert store.get(conn, replacement_id)["status"] == "approved"
    indexer.build(conn, "tests/fixtures/mini_c", out_dir=data_dir)
    assert conn.execute(
        "SELECT visible FROM chunk WHERE uid=?", (f"exp:{first_id}",)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT visible FROM chunk WHERE uid=?", (f"exp:{replacement_id}",)
    ).fetchone()[0] == 1


def test_rule_agent_is_read_only_and_explicitly_saves_drafts(kb, tmp_path):
    conn, data_dir = kb
    session_id = conversation.import_public_session(conn, _session_file(tmp_path))["id"]
    result = agent.run(conn, "parse_value 的影响范围", data_dir=data_dir, session_id=session_id,
                       embedder=None, save_draft=False)
    assert result["mode"] == "rule" and result["status"] == "completed"
    assert 1 <= len(result["events"]) <= agent.MAX_TOOL_CALLS
    assert {event["tool"] for event in result["events"]} <= agent.ALLOWED_TOOLS
    assert result["draft_preview"] and result["draft_preview"]["evidence_tags"] and result["draft_id"] is None
    # Saving persists this exact preview; it must not rerun the Agent or change its
    # evidence trace merely because the user clicked the save control.
    run_count = conn.execute("SELECT COUNT(*) FROM agent_run").fetchone()[0]
    saved = agent.save_draft(conn, result["id"])
    assert saved["saved"] is True
    assert conn.execute("SELECT COUNT(*) FROM agent_run").fetchone()[0] == run_count
    assert agent.save_draft(conn, result["id"])["saved"] is False
    stored = agent.get_run(conn, result["id"])
    assert stored["draft_id"] == saved["id"] and stored["draft_preview"]["session_id"] == session_id
    row = conn.execute("SELECT status,evidence_tags FROM experience WHERE id=?", (saved["id"],)).fetchone()
    assert row["status"] == "pending" and json.loads(row["evidence_tags"])


def test_unrelated_agent_refuses_without_draft(kb, tmp_path):
    conn, data_dir = kb
    session_id = conversation.import_public_session(conn, _session_file(tmp_path))["id"]
    result = agent.run(conn, "如何轮换 Kubernetes Ingress TLS 证书", data_dir=data_dir,
                       session_id=session_id, embedder=None, save_draft=True)
    assert result["status"] == "refused"
    assert result["draft_preview"] is None and result["draft_id"] is None


def test_invalid_model_tool_is_never_executed_and_falls_back_to_rule_agent(kb, tmp_path):
    conn, data_dir = kb
    session_id = conversation.import_public_session(conn, _session_file(tmp_path))["id"]

    class UnsafePlanner:
        def plan_tool(self, _query, _events):
            return {"tool": "external_http", "arguments": {"url": "https://example.invalid"}}

    result = agent.run(conn, "parse_value 的影响范围", data_dir=data_dir, session_id=session_id,
                       llm_client=UnsafePlanner(), save_draft=False)
    assert result["mode"] == "rule"
    assert result["fallback_reason"].startswith("model_plan_invalid")
    assert all(event["tool"] in agent.ALLOWED_TOOLS for event in result["events"] if event["status"] != "rejected")
    assert any(event["status"] == "rejected" for event in result["events"])


def test_model_iterates_tools_then_finishes_with_valid_citations(kb, tmp_path):
    conn, data_dir = kb
    session_id = conversation.import_public_session(conn, _session_file(tmp_path))["id"]

    class IterativePlanner:
        model = "fixture-model"

        def __init__(self):
            self.actions = [
                {"action": "tool", "tool": "analyze_impact",
                 "arguments": {"symbol": "parse_value", "depth": 2}},
                {"action": "finish"},
            ]

        def next_action(self, _query, events):
            assert len(events) <= 2
            return self.actions.pop(0)

        def answer(self, _system, _query, _context):
            return "parse_value 的影响范围由当前 certain 调用图给出 [A1]。"

        def extract_knowledge_card(self, _messages, _schema):
            return _card_data() | {"evidence_tags": ["A1"]}

    result = agent.run(
        conn, "parse_value 的影响范围", data_dir=data_dir, session_id=session_id,
        llm_client=IterativePlanner(), mode="auto",
    )
    assert result["requested_mode"] == "auto" and result["execution_mode"] == "model"
    assert result["fallback_reason"] is None and result["answer_source"] == "model"
    assert [event["tool"] for event in result["events"]] == [
        "search_evidence", "analyze_impact",
    ]
    assert result["answer"] and result["evidence_registry"]
    assert result["draft_preview"]["evidence_tags"] == ["A1"]
    stored = agent.get_run(conn, result["id"])
    assert stored["requested_mode"] == "auto" and stored["execution_mode"] == "model"


def test_invalid_model_answer_is_visible_evidence_fallback(kb):
    conn, data_dir = kb

    class UncitedAnswer:
        model = "fixture-model"

        def __init__(self):
            self.actions = [
                {"action": "tool", "tool": "resolve_symbol",
                 "arguments": {"symbol": "parse_value"}},
                {"action": "finish"},
            ]

        def next_action(self, _query, _events):
            return self.actions.pop(0)

        def answer(self, *_args):
            return "这是一个没有任何引用的自由回答。"

    result = agent.run(conn, "parse_value 做什么", data_dir=data_dir,
                       llm_client=UncitedAnswer(), mode="model", read_mode="flat")
    assert result["execution_mode"] == "rule" and result["answer"] is None
    assert result["answer_source"] == "evidence_pack"
    assert result["fallback_reason"].startswith("model_answer_invalid")


def test_no_evidence_refuses_before_any_model_call(kb):
    conn, data_dir = kb

    class CountingModel:
        model = "must-not-run"

        def __init__(self):
            self.calls = 0

        def next_action(self, *_args):
            self.calls += 1
            return {"action": "finish"}

    model = CountingModel()
    result = agent.run(conn, "Kubernetes Ingress TLS 证书轮换", data_dir=data_dir,
                       llm_client=model, mode="model")
    assert result["status"] == "refused" and model.calls == 0
    assert result["fallback_reason"] == "no_ab_evidence"
    assert len(result["events"]) == 1 and result["events"][0]["tool"] == "search_evidence"


def test_agent_feedback_is_local_only_and_cannot_mutate_knowledge(kb):
    conn, data_dir = kb
    result = agent.run(conn, "parse_value 的影响范围", data_dir=data_dir, mode="rule")
    evidence_tag = result["evidence_registry"][0]["tag"]
    before = {
        "cards": conn.execute("SELECT COUNT(*) FROM experience").fetchone()[0],
        "chunks": conn.execute("SELECT COUNT(*) FROM chunk").fetchone()[0],
    }
    feedback = agent.add_feedback(
        conn, result["id"], "incomplete", evidence_tag=evidence_tag,
        comment="还需要解释失败分支。",
    )
    assert feedback["verdict"] == "incomplete"
    assert agent.get_run(conn, result["id"])["feedback"][0]["evidence_tag"] == evidence_tag
    assert agent.list_feedback(conn)[0]["run_id"] == result["id"]
    assert conn.execute("SELECT COUNT(*) FROM experience").fetchone()[0] == before["cards"]
    assert conn.execute("SELECT COUNT(*) FROM chunk").fetchone()[0] == before["chunks"]
    with pytest.raises(agent.AgentError, match="证据标签"):
        agent.add_feedback(conn, result["id"], "incorrect", evidence_tag="A999")


def test_public_workflow_manifest_has_exactly_six_fixed_cases():
    manifest = Path(__file__).resolve().parents[1] / "eval" / "workflow_cjson.yaml"
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert {case["id"] for case in raw["cases"]} == {
        "session-import", "agent-trace", "pending-gate", "approval-anchor",
        "stale-isolation", "no-evidence-refusal",
    }


def test_workflow_reset_clears_only_governance_in_isolated_copy(tmp_path):
    conn = dbm.connect(tmp_path / "isolated.db")
    try:
        conn.execute("INSERT INTO node(id,kind,name) VALUES('n1','function','kept')")
        conn.execute(
            """INSERT INTO conversation_session
               (id,source_type,source_ref,license,repository,revision,content_hash,status,created_at)
               VALUES('s1','public_synthetic','fixture','CC0-1.0','repo','rev','hash','imported','now')"""
        )
        conn.execute(
            """INSERT INTO experience(id,title,symptom,status,created_at)
               VALUES('e1','draft','symptom','pending','now')"""
        )
        conn.execute(
            """INSERT INTO agent_run
               (id,query,repository,revision,mode,status,refused,created_at)
               VALUES('r1','query','repo','rev','rule','completed',0,'now')"""
        )
        conn.commit()

        workflow_eval._reset_isolated_governance(conn)

        assert conn.execute("SELECT COUNT(*) FROM node").fetchone()[0] == 1
        for table in ("conversation_session", "experience", "agent_run"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        conn.close()


def test_legacy_approved_card_is_downgraded_during_migration(tmp_path):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.execute("""CREATE TABLE experience (
      id TEXT PRIMARY KEY,title TEXT,symptom TEXT,hypotheses TEXT,dead_ends TEXT,
      root_cause TEXT,fix_steps TEXT,verification TEXT,source_type TEXT,source_ref TEXT,
      status TEXT CHECK(status IN ('draft','pending','approved','rejected')),reviewer_note TEXT,
      model TEXT,prompt_ver TEXT,created_at TEXT,reviewed_at TEXT)""")
    raw.execute("INSERT INTO experience VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("legacy","old","old","[]","[]",None,None,None,"manual","", "approved",None,None,None,None,None))
    raw.commit(); raw.close()
    conn = dbm.connect(path)
    row = conn.execute("SELECT status,reviewer_note FROM experience WHERE id='legacy'").fetchone()
    assert row["status"] == "pending" and "缺少精确源码锚点" in row["reviewer_note"]


def test_legacy_approved_anchor_without_passed_qa_is_downgraded(tmp_path):
    path = tmp_path / "legacy-anchor.db"
    conn = dbm.connect(path)
    conn.execute(
        """INSERT INTO experience(id,title,symptom,status,created_at)
           VALUES('legacy-qa','old','old','approved','old')"""
    )
    conn.execute(
        """INSERT INTO experience_anchor
           (exp_id,repository,revision,usr,path,line_start,line_end,definition_hash,
            relation,active,bound_at,bound_by)
           VALUES('legacy-qa','repo','rev','usr','a.c',1,2,'hash','explains',1,'old','legacy')"""
    )
    conn.commit(); conn.close()
    migrated = dbm.connect(path)
    row = migrated.execute(
        "SELECT status,reviewer_note FROM experience WHERE id='legacy-qa'"
    ).fetchone()
    assert row["status"] == "pending"
    assert "缺少已通过 QA" in row["reviewer_note"]
