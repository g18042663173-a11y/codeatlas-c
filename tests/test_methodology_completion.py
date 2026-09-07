"""Methodology completion: Wiki-first Agent and Markdown-source curation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeatlas import agent, conversation, db as dbm
from codeatlas.experience import store
from codeatlas.indexer import build as indexer
from codeatlas.parser import ast_walker, compile_db
from codeatlas.retrieve import engine
from codeatlas.summary import head
from codeatlas.wiki import generator

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "mini_c"


def _fresh(tmp_path: Path):
    conn = dbm.connect(tmp_path / "kb.db")
    units, _ = compile_db.load(str(CORPUS), None, [str(CORPUS)], [])
    walker = ast_walker.AstWalker(str(CORPUS))
    for unit in units:
        walker.walk_unit(unit.source, unit.args, unit.directory)
    ast_walker.persist(conn, walker.result)
    dbm.set_meta(conn, "repo", str(CORPUS))
    dbm.set_meta(conn, "repository_id", "tests/fixtures/mini_c")
    dbm.set_meta(conn, "revision", "fixture")
    dbm.set_meta(conn, "build_config_hash", "fixture-build")
    conn.commit()
    head.build(conn, str(CORPUS))
    indexer.build(conn, str(CORPUS), out_dir=str(tmp_path / "index"))
    return conn


def test_wiki_covers_header_and_passes_backlink_integrity(tmp_path):
    conn = _fresh(tmp_path)
    out = tmp_path / "wiki"
    result = generator.generate(conn, str(CORPUS), out_dir=str(out), resume=False)
    assert result["integrity"]["ok"] is True
    assert conn.execute("SELECT 1 FROM wiki_page WHERE id='files/mini.h'").fetchone()
    header = conn.execute("SELECT md FROM wiki_page WHERE id='files/mini.h'").fetchone()[0]
    assert "没有函数定义" in header
    assert result["integrity"]["file_coverage"] == conn.execute(
        "SELECT COUNT(*) FROM node WHERE kind='file' AND path IS NOT NULL"
    ).fetchone()[0]


def test_module_call_rows_are_scoped_to_module_files(tmp_path):
    conn = _fresh(tmp_path)
    # Add two artificial modules to make a repository-level leakage obvious.
    for suffix, path in (("a", "alpha/a.c"), ("b", "beta/b.c")):
        file_node = {"id": f"file-{suffix}", "kind": "file", "name": f"{suffix}.c",
                     "usr": None, "repo": str(CORPUS), "path": path,
                     "line_start": None, "line_end": None, "signature": None,
                     "definition_hash": None, "is_definition": 1, "is_static": 0,
                     "extra": None}
        fn_node = {**file_node, "id": f"fn-{suffix}", "kind": "function",
                   "name": f"fn_{suffix}", "usr": f"usr-{suffix}",
                   "line_start": 1, "line_end": 2, "signature": "void ()",
                   "definition_hash": suffix * 64}
        conn.execute(
            """INSERT INTO node(id,kind,name,usr,repo,path,line_start,line_end,signature,
               definition_hash,is_definition,is_static,extra) VALUES(:id,:kind,:name,:usr,:repo,
               :path,:line_start,:line_end,:signature,:definition_hash,:is_definition,:is_static,:extra)""",
            file_node,
        )
        conn.execute(
            """INSERT INTO node(id,kind,name,usr,repo,path,line_start,line_end,signature,
               definition_hash,is_definition,is_static,extra) VALUES(:id,:kind,:name,:usr,:repo,
               :path,:line_start,:line_end,:signature,:definition_hash,:is_definition,:is_static,:extra)""",
            fn_node,
        )
        conn.execute("INSERT INTO edge(src,dst,kind,confidence) VALUES(?,?,'contains','certain')",
                     (file_node["id"], fn_node["id"]))
    conn.execute("INSERT INTO edge(src,dst,kind,confidence) VALUES('fn-b','fn-a','calls','certain')")
    conn.commit()
    generator.generate(conn, str(CORPUS), resume=False)
    alpha = conn.execute("SELECT md FROM wiki_page WHERE id='modules/alpha'").fetchone()[0]
    beta = conn.execute("SELECT md FROM wiki_page WHERE id='modules/beta'").fetchone()[0]
    assert "`beta/b.c` → `alpha/a.c`" not in alpha
    assert "`beta/b.c` → `alpha/a.c`" in beta


def test_agent_obeys_wiki_first_and_blocks_path_escape(tmp_path):
    conn = _fresh(tmp_path)
    generator.generate(conn, str(CORPUS), resume=False)
    result = agent.run(conn, "parse_value 的失败路径如何处理？",
                       data_dir=str(tmp_path / "index"), mode="rule")
    tools = [event["tool"] for event in result["events"]]
    assert tools[:3] == ["search_evidence", "wiki_outline", "wiki_section"]
    assert [event["phase"] for event in result["events"][:3]] == [
        "discover", "discover", "understand",
    ]
    assert "code_read" in tools and len(tools) <= 6
    assert any(citation["level"] == "C" for citation in result["evidence_registry"])
    with pytest.raises(agent.AgentError, match="当前仓库"):
        agent._code_read(conn, {"path": "../secret.txt", "line_start": 1, "line_end": 1})


def test_generic_query_normalization_finds_identifier_actions_without_gold_map(tmp_path):
    conn = _fresh(tmp_path)
    names = []
    for rowid in engine.recall_symbol(conn, "JSON parser 如何处理 object values？"):
        row = conn.execute(
            "SELECT n.name FROM chunk c JOIN node n ON n.id=c.node_id WHERE c.rowid=?",
            (rowid,),
        ).fetchone()
        if row:
            names.append(row["name"])
    assert {"parse_value", "parse_object"} <= set(names)

    conn.execute(
        """INSERT INTO node(id,kind,name,usr,repo,path,line_start,line_end,signature,
           definition_hash,is_definition,is_static,extra)
           VALUES('netif-up','function','netif_set_up','usr-netif-up',?,'mini.c',1,2,
                  'void ()','fixture-netif-hash',1,0,NULL)""", (str(CORPUS),),
    )
    cursor = conn.execute(
        """INSERT INTO chunk(uid,node_id,kind,title,text,source_ref,evidence_level,visible)
           VALUES('code:netif-up','netif-up','code','netif_set_up','void netif_set_up(void)',
                  'mini.c','A',1)"""
    )
    conn.commit()
    assert cursor.lastrowid in engine.recall_symbol(conn, "网络接口设置为 up 的函数在哪里？")
    assert engine.recall_symbol(conn, "Kubernetes Ingress TLS 证书轮换") == []
    serialized_vocab = json.dumps(engine.QUERY_VOCAB, ensure_ascii=False)
    assert "netif_set_up" not in serialized_vocab and "print_value" not in serialized_vocab


def test_direct_caller_query_prioritizes_compiler_graph_neighbours(tmp_path):
    conn = _fresh(tmp_path)
    result = engine.ask(
        conn, "哪些函数直接调用 parse_value？", data_dir=str(tmp_path / "index"),
        embedder=None,
    )
    top_names = {
        (citation.get("provenance") or {}).get("symbol")
        for citation in result["citations"][:10]
    }
    assert "cJSON_ParseWithOpts" in top_names


def test_candidate_qa_gate_and_markdown_rebuild(tmp_path):
    conn = _fresh(tmp_path)
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({
        "schema_version": 1, "id": "curation-session", "source_type": "public_synthetic",
        "source_ref": "tests/curation-session.json", "license": "CC0-1.0",
        "repository": "tests/fixtures/mini_c", "revision": "fixture",
        "messages": [
            {"role": "user", "knowledge_role": "symptom",
             "content": "复现 parse_value 失败返回并检查固定函数定义"},
            {"role": "tool", "knowledge_role": "root_cause",
             "content": "固定边界判断触发了失败返回。"},
            {"role": "reviewer", "knowledge_role": "fix",
             "content": "调用方处理失败返回并保留边界检查。"},
            {"role": "reviewer", "knowledge_role": "verification",
             "content": "使用固定输入验证调用路径、返回值和源码范围。"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    session_id = conversation.import_public_session(conn, session_file)["id"]
    conversation.set_curation_goal(conn, session_id, "沉淀 parse_value 失败路径审查")
    candidate = conversation.propose_candidates(conn, session_id)[0]
    corrected = dict(candidate["payload"])
    corrected["root_cause"] += "（人工纠正：确认由边界条件触发。）"
    decision = conversation.decide_candidate(
        conn, candidate["id"], "correct", payload=corrected,
    )
    assert decision["correction_diff"] and decision["card_id"]
    card_id = decision["card_id"]
    node = conn.execute(
        "SELECT * FROM node WHERE name='parse_value' AND is_definition=1"
    ).fetchone()
    store.bind(conn, card_id, node["usr"], relation="warns")
    store.attach_experiments(
        conn, card_id, ["examples/reproductions/cjson-nesting/result.txt"]
    )
    assert json.loads(store.get(conn, card_id)["evidence_tags"]) == ["A1"]
    with pytest.raises(ValueError, match="QA"):
        store.review(conn, card_id, "approve", "尚未 QA")
    qa = store.qa_add(conn, card_id, "失败路径如何验证？", ["固定返回范围"], ["A1"])
    store.qa_review(conn, qa["id"], True, "源码与复现步骤一致")
    store.confirm_review(conn, card_id, store.review_bundle(conn, card_id), "local-reviewer")
    assert store.review(conn, card_id, "approve", "锚点与 QA 已复核")["status"] == "approved"
    assert store.consistency(conn)["ok"] is True
    rebuilt = store.rebuild(conn, rebuild_index=False)
    assert rebuilt["ok"] is True and rebuilt["cards"] == 1
    card = store.get(conn, card_id)
    assert card["status"] == "approved"
    assert card["anchors"][0]["relation"] == "warns"
    assert card["qas"][0]["status"] == "passed"


def test_unstructured_last_message_is_not_promoted_to_root_cause(tmp_path):
    conn = _fresh(tmp_path)
    session_file = tmp_path / "unstructured-session.json"
    session_file.write_text(json.dumps({
        "schema_version": 1, "id": "unstructured-session",
        "source_type": "public_synthetic",
        "source_ref": "tests/unstructured-session.json", "license": "CC0-1.0",
        "repository": "tests/fixtures/mini_c", "revision": "fixture",
        "messages": [
            {"role": "user", "content": "复现 parse_value 失败返回"},
            {"role": "reviewer", "content": "最后复核源码和测试输入。"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    session_id = conversation.import_public_session(conn, session_file)["id"]
    conversation.set_curation_goal(conn, session_id, "沉淀失败路径")
    candidate = conversation.propose_candidates(conn, session_id)[0]
    assert candidate["payload"]["root_cause"] == ""
    assert set(candidate["payload"]["missing_fields"]) == {
        "root_cause", "fix_steps", "verification",
    }
    with pytest.raises(conversation.ConversationError, match="缺少必填字段"):
        conversation.decide_candidate(conn, candidate["id"], "accept")


def test_forced_fact_reset_preserves_governed_assets_and_rebinds_current_card(tmp_path):
    conn = _fresh(tmp_path)
    session_file = tmp_path / "force-session.json"
    session_file.write_text(json.dumps({
        "schema_version": 1, "id": "force-session", "source_type": "public_synthetic",
        "source_ref": "tests/force-session.json", "license": "CC0-1.0",
        "repository": "tests/fixtures/mini_c", "revision": "fixture",
        "messages": [
            {"role": "user", "knowledge_role": "symptom", "content": "固定失败现象"},
            {"role": "reviewer", "knowledge_role": "root_cause", "content": "固定根因"},
            {"role": "reviewer", "knowledge_role": "fix", "content": "固定处理步骤"},
            {"role": "reviewer", "knowledge_role": "verification", "content": "固定验证方式"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    conversation.import_public_session(conn, session_file)
    card_id = store.add(conn, {
        "title": "全量解析保留审核资产", "symptom": "固定失败现象",
        "root_cause": "固定根因", "fix_steps": "固定处理步骤",
        "verification": "固定验证方式", "evidence_tags": ["A1"],
        "evidence_turns": [1, 2, 3, 4],
    }, source_type="public_synthetic", source_ref="tests/force-session.json",
       session_id="force-session")
    node = conn.execute(
        "SELECT * FROM node WHERE name='parse_value' AND is_definition=1"
    ).fetchone()
    store.bind(conn, card_id, node["usr"], relation="warns")
    qa = store.qa_add(conn, card_id, "如何验证？", ["固定源码锚点"], ["A1"])
    store.qa_review(conn, qa["id"], True, "fixture QA")
    store.attach_experiments(
        conn, card_id, ["examples/reproductions/cjson-nesting/result.txt"]
    )
    store.qa_review(conn, qa["id"], True, "fixture QA with reproduction")
    store.confirm_review(conn, card_id, store.review_bundle(conn, card_id), "local-reviewer")
    store.review(conn, card_id, "approve", "fixture review")
    original_node = dict(node)

    dbm.reset_code_facts(conn)
    assert conn.execute("SELECT COUNT(*) FROM node").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM experience WHERE id=?", (card_id,)).fetchone()[0] == "approved"
    assert conn.execute("SELECT COUNT(*) FROM conversation_session").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM experience_qa").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM experience_review").fetchone()[0] == 1

    columns = [
        "id", "kind", "name", "usr", "repo", "path", "line_start", "line_end",
        "signature", "definition_hash", "is_definition", "is_static", "extra",
    ]
    conn.execute(
        f"INSERT INTO node({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        [original_node[column] for column in columns],
    )
    conn.commit()
    # Restoring only the main function is no longer sufficient: consumed
    # dependencies disappeared during the reset, so the card must be isolated.
    reason = store.stale_check(conn)["stale"][0]["reason"]
    assert reason.startswith("dependency_missing:")
    assert conn.execute(
        "SELECT COUNT(*) FROM experience_link WHERE exp_id=?", (card_id,)
    ).fetchone()[0] == 0
