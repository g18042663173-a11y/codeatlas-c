"""Graph facade: same retrieve/refuse behavior, interview topology only."""
from __future__ import annotations

from codeatlas.compat.graph_spec import ATLAS_GRAPH, mermaid
from codeatlas.compat.runtime import compile_optional_langgraph, run_atlas_graph
from codeatlas.retrieve import engine


def test_graph_spec_has_interview_vocabulary():
    ids = [node["id"] for node in ATLAS_GRAPH["nodes"]]
    assert ids == ["route", "retrieve", "grade", "act", "generate", "refuse"]
    assert any(edge.get("when") == "no A/B evidence" for edge in ATLAS_GRAPH["edges"])
    chart = mermaid()
    assert "grade" in chart and "refuse" in chart
    assert ATLAS_GRAPH["checkpoint"]["codeatlas"].startswith("codeatlas.snapshots")


def test_graph_answers_with_ab_evidence(kb):
    conn, data_dir = kb
    state = run_atlas_graph(conn, "cJSON_Delete", data_dir=data_dir)
    assert state["trace"] == ["route", "retrieve", "grade", "act", "generate"]
    assert state["decision"] == "continue"
    assert state["evidence"]["refused"] is False
    assert state["evidence"]["ab_evidence"] > 0
    assert state["answer"]
    assert state["events"]


def test_graph_refuses_without_ab_evidence(kb):
    conn, data_dir = kb
    query = "如何配置 Kubernetes Ingress 的 TLS 证书轮换"
    state = run_atlas_graph(conn, query, data_dir=data_dir)
    direct = engine.ask(conn, query, data_dir=data_dir)
    assert state["trace"] == ["route", "retrieve", "grade", "refuse"]
    assert state["decision"] == "refuse"
    assert state["evidence"]["refused"] is True
    assert state["evidence"]["ab_evidence"] == 0
    assert "证据不足" in (state["answer"] or "")
    assert direct["refused"] is True
    assert "act" not in state["trace"]


def test_optional_langgraph_compile_is_facade_or_absent(kb):
    conn, data_dir = kb
    compiled = compile_optional_langgraph(conn, data_dir=data_dir)
    if compiled is None:
        return
    native = run_atlas_graph(conn, "cJSON_Delete", data_dir=data_dir)
    via_lib = compiled.invoke({"query": "cJSON_Delete", "trace": []})
    assert via_lib["decision"] == native["decision"]
    assert via_lib["evidence"]["refused"] == native["evidence"]["refused"]
    assert "grade" in via_lib["trace"]
