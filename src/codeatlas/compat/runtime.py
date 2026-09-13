"""Walk the Atlas graph. Optional LangGraph compile is a facade over the same nodes."""

from __future__ import annotations

from typing import Any

from .graph_spec import ATLAS_GRAPH


def _trace(state: dict[str, Any], node: str) -> dict[str, Any]:
    out = dict(state)
    out.setdefault("trace", [])
    out["trace"] = list(out["trace"]) + [node]
    return out


def node_route(state: dict[str, Any]) -> dict[str, Any]:
    from ..retrieve.engine import route

    next_state = _trace(state, "route")
    next_state["route"] = route(state["query"])
    return next_state


def node_retrieve(state: dict[str, Any], *, conn, embedder, data_dir: str,
                  hops: int = 2) -> dict[str, Any]:
    from ..retrieve.engine import ask

    next_state = _trace(state, "retrieve")
    next_state["evidence"] = ask(
        conn, state["query"], embedder=embedder, data_dir=data_dir, hops=hops, llm_client=None
    )
    return next_state


def node_grade(state: dict[str, Any]) -> dict[str, Any]:
    evidence = state.get("evidence") or {}
    next_state = _trace(state, "grade")
    next_state["decision"] = (
        "refuse" if evidence.get("refused") or not evidence.get("ab_evidence") else "continue"
    )
    return next_state


def node_act(state: dict[str, Any], *, conn, embedder, data_dir: str) -> dict[str, Any]:
    from .. import agent

    next_state = _trace(state, "act")
    events = []
    for call in agent._rule_plan(conn, state["query"])[: agent.MAX_TOOL_CALLS]:
        event = agent._run_call(conn, call, embedder=embedder, data_dir=data_dir)
        events.append({
            "phase": event["phase"],
            "tool": event["tool"],
            "ok": event["status"] == "completed",
            "status": event["status"],
        })
    next_state["events"] = events
    return next_state


def node_generate(state: dict[str, Any]) -> dict[str, Any]:
    evidence = state.get("evidence") or {}
    next_state = _trace(state, "generate")
    next_state["answer"] = (
        evidence.get("answer")
        or evidence.get("evidence_pack")
        or evidence.get("context")
    )
    return next_state


def node_refuse(state: dict[str, Any]) -> dict[str, Any]:
    evidence = state.get("evidence") or {}
    next_state = _trace(state, "refuse")
    next_state["answer"] = evidence.get("answer")
    next_state["decision"] = "refuse"
    return next_state


def run_atlas_graph(conn, query: str, *, embedder=None, data_dir: str = "data",
                    hops: int = 2) -> dict[str, Any]:
    """Deterministic walker. Same topology as the optional LangGraph compile."""
    state: dict[str, Any] = {"query": query, "trace": []}
    state = node_route(state)
    state = node_retrieve(state, conn=conn, embedder=embedder, data_dir=data_dir, hops=hops)
    state = node_grade(state)
    if state["decision"] == "refuse":
        return node_refuse(state)
    state = node_act(state, conn=conn, embedder=embedder, data_dir=data_dir)
    return node_generate(state)


def compile_optional_langgraph(conn, *, embedder=None, data_dir: str = "data", hops: int = 2):
    """Return a LangGraph compiled app if the extra is installed; otherwise None."""
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        return None

    graph = StateGraph(dict)
    graph.add_node("route", node_route)
    graph.add_node("retrieve", lambda s: node_retrieve(
        s, conn=conn, embedder=embedder, data_dir=data_dir, hops=hops
    ))
    graph.add_node("grade", node_grade)
    graph.add_node("act", lambda s: node_act(
        s, conn=conn, embedder=embedder, data_dir=data_dir
    ))
    graph.add_node("generate", node_generate)
    graph.add_node("refuse", node_refuse)
    graph.add_edge(START, "route")
    graph.add_edge("route", "retrieve")
    graph.add_edge("retrieve", "grade")
    graph.add_conditional_edges(
        "grade",
        lambda s: s["decision"],
        {"refuse": "refuse", "continue": "act"},
    )
    graph.add_edge("act", "generate")
    graph.add_edge("generate", END)
    graph.add_edge("refuse", END)
    compiled = graph.compile()
    compiled.atlas_spec = ATLAS_GRAPH  # type: ignore[attr-defined]
    return compiled
