"""Optional model benchmark records protocol metrics without storing prompts or keys."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codeatlas.eval import model_eval


class _ProtocolModel:
    model = "fixture-model"

    def __init__(self):
        self.request_count = 0
        self.usage_totals = {}

    def reset_metrics(self):
        self.request_count = 0
        self.usage_totals = {}

    def _record(self):
        self.request_count += 1
        self.usage_totals["total_tokens"] = self.usage_totals.get("total_tokens", 0) + 5

    def next_action(self, query, events):
        self._record()
        if not events:
            return {"action": "tool", "tool": "search_evidence",
                    "arguments": {"query": query}}
        return {"action": "finish"}

    def next_baseline_action(self, query, events, *, fixed_skill=False):
        self._record()
        if not events:
            return {"action": "tool", "tool": "repo_search",
                    "arguments": {"query": query}}
        return {"action": "finish"}

    def answer(self, _system, _query, _context):
        self._record()
        return "结论仅来自当前固定源码证据 [A1]。"


def _tasks(tmp_path: Path) -> Path:
    manifest = {
        "schema_version": 1,
        "name": "fixture model tasks",
        "repository": "tests/fixtures/mini_c",
        "revision": "fixture",
        "public_source": "内置公开 C fixture",
        "reviewer": "fixture-reviewer", "reviewed_at": "2026-09-04",
        "failure_policy": "未命中固定锚点或门禁不满足即失败。",
        "tasks": [
            {
                "id": "locate-delete", "type": "定位", "status": "approved",
                "question": "cJSON_Delete 在哪里定义？",
                "source_permalink": "https://example.test/fixture/mini.c#L20",
                "expected_anchors": [
                    {"symbol": "cJSON_Delete", "path": "mini.c", "line_start": 20},
                ],
                "rationale": "固定 fixture 的精确源码锚点。",
            },
            {
                "id": "refuse-unrelated", "type": "无证据拒答", "status": "approved",
                "question": "如何轮换 Kubernetes Ingress TLS 证书？",
                "source_permalink": "https://example.test/fixture/mini.c#L1",
                "rationale": "与 fixture 无关。",
            },
        ],
    }
    target = tmp_path / "model-tasks.yaml"
    target.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
                      encoding="utf-8")
    return target


def test_model_eval_records_native_planning_safety_tokens_and_sanitized_report(kb, tmp_path):
    conn, data_dir = kb
    tasks = _tasks(tmp_path)
    report = model_eval.main(
        conn, str(tasks), client=_ProtocolModel(), data_dir=data_dir,
        out=str(tmp_path / "MODEL.md"),
    )

    assert report["metrics"]["native_planning_success_rate"] == 100.0
    assert report["metrics"]["final_safety_gate_rate"] == 100.0
    assert report["metrics"]["tool_trace_legal_rate"] == 100.0
    assert report["metrics"]["requests"] == 2  # preflight is counted locally, not a hidden model request
    assert report["metrics"]["usage"]["total_tokens"] == 10
    assert report["acceptance"]["passed"] is True
    text = Path(report["report"]).read_text(encoding="utf-8")
    assert "fixture-model" in text and "原生合法规划率" in text
    assert "cJSON_Delete 在哪里定义" not in text


def test_model_eval_requires_a_configured_client(kb, tmp_path):
    conn, data_dir = kb
    with pytest.raises(model_eval.ModelEvalError, match="未配置"):
        model_eval.evaluate(conn, str(_tasks(tmp_path)), client=None, data_dir=data_dir)


def test_model_abc_uses_paired_variants_and_odd_repetitions(kb, tmp_path):
    conn, data_dir = kb
    result = model_eval.evaluate_abc(
        conn, str(_tasks(tmp_path)), client=_ProtocolModel(), data_dir=data_dir,
        runs=1,
    )
    assert result["runs_per_task"] == 1
    assert result["status"] == "completed" and result["task_scope"] == "full"
    assert set(result["variants"]) == {"A", "B", "C"}
    assert result["protocol"]["max_tool_calls"] == 6
    assert result["protocol"]["A_tools"] == ["code_read", "repo_search"]
    assert all(variant["metrics"]["native_planning_success_rate"] == 100.0
               for variant in result["variants"].values())
    assert all(variant["metrics"]["effective_citation_rate"] == 100.0
               for variant in result["variants"].values())
    rendered = model_eval.render_abc_markdown(result, tasks_path="fixture.yaml")
    assert "Agent + CodeAtlas + Code" in rendered
    assert "cJSON_Delete 在哪里定义" not in rendered
    with pytest.raises(model_eval.ModelEvalError, match="正奇数"):
        model_eval.evaluate_abc(
            conn, str(_tasks(tmp_path)), client=_ProtocolModel(), data_dir=data_dir,
            runs=2,
        )


def test_model_abc_smoke_subset_is_explicit_and_not_publishable(kb, tmp_path):
    conn, data_dir = kb
    result = model_eval.evaluate_abc(
        conn, str(_tasks(tmp_path)), client=_ProtocolModel(), data_dir=data_dir,
        runs=1, limit=1,
    )
    assert result["status"] == "smoke"
    assert result["task_scope"] == "representative_smoke"
    assert result["task_count"] == 1
    assert all(len(value["tasks"]) == 1 for value in result["variants"].values())


def test_value_smoke_profile_has_two_code_one_experience_one_refusal():
    tasks = [
        {"id": "semantic-a", "type": "语义检索"},
        {"id": "semantic-b", "type": "语义检索"},
        {"id": "locate", "type": "定位"},
        {"id": "experience", "type": "已审核经验复用"},
        {"id": "refusal", "type": "无证据拒答"},
    ]
    selected = model_eval._value_smoke_tasks(tasks)
    assert [task["id"] for task in selected] == [
        "semantic-a", "semantic-b", "experience", "refusal"]
    with pytest.raises(model_eval.ModelEvalError, match="one experience"):
        model_eval._value_smoke_tasks(tasks[:3])
