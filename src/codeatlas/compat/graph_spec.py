"""LangGraph-shaped spec for CodeAtlas. Nodes call existing functions; they are not a rewrite."""

from __future__ import annotations

from typing import Any

# Same vocabulary interviewers use. Implementation still lives in retrieve/agent/snapshots.
ATLAS_GRAPH: dict[str, Any] = {
    "name": "codeatlas.compat.atlas_graph",
    "state": [
        "query",
        "route",
        "evidence",
        "decision",
        "events",
        "answer",
        "trace",
    ],
    "nodes": [
        {
            "id": "route",
            "impl": "codeatlas.retrieve.engine.route",
            "langchain": "Query router / RunnableLambda",
            "says": "先分 symbol / call-graph / lexical，再决定召回路权重。",
        },
        {
            "id": "retrieve",
            "impl": "codeatlas.retrieve.engine.ask",
            "langchain": "EnsembleRetriever + RRF + graph expand + rerank",
            "says": "符号 / BM25 / 向量 / 已审经验融合，再沿 certain 边扩 2 跳。",
        },
        {
            "id": "grade",
            "impl": "engine.ask ab_evidence / refused",
            "langchain": "Document grader / hallucination gate",
            "says": "没有 A/B 证据就拒答，不把 C 级 Wiki 升成结论。",
        },
        {
            "id": "act",
            "impl": "codeatlas.agent._rule_plan + _execute",
            "langchain": "ToolNode / ReAct loop",
            "says": "discover / understand / verify，最多 6 次只读工具。",
        },
        {
            "id": "generate",
            "impl": "engine.ask evidence_pack or llm.answer",
            "langchain": "RAG generate",
            "says": "有模型就带引用生成；没模型也返回证据包。",
        },
        {
            "id": "refuse",
            "impl": "engine.ask refused=True",
            "langchain": "END with structured refusal",
            "says": "证据不足时给线索，不编造调用关系。",
        },
    ],
    "edges": [
        {"from": "START", "to": "route"},
        {"from": "route", "to": "retrieve"},
        {"from": "retrieve", "to": "grade"},
        {"from": "grade", "to": "refuse", "when": "no A/B evidence"},
        {"from": "grade", "to": "act", "when": "has A/B or structural follow-up"},
        {"from": "act", "to": "generate"},
        {"from": "generate", "to": "END"},
        {"from": "refuse", "to": "END"},
    ],
    "checkpoint": {
        "langgraph": "PostgresSaver / MemorySaver",
        "codeatlas": "codeatlas.snapshots.pin + knowledge_set_id",
        "says": "图状态检查点对应冻结知识快照，不是对话 transcript。",
    },
}


def mermaid(spec: dict[str, Any] | None = None) -> str:
    graph = spec or ATLAS_GRAPH
    lines = ["flowchart TD"]
    for edge in graph["edges"]:
        label = f'|"{edge["when"]}"|' if edge.get("when") else ""
        lines.append(f'    {edge["from"]} -->{label} {edge["to"]}')
    return "\n".join(lines)
