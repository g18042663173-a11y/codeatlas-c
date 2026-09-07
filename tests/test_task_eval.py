"""轻量任务评测：schema、精确锚点、certain/candidate 与拒答门禁。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codeatlas.eval import task_eval
from codeatlas.retrieve import engine


def _manifest(*, revision: str = "fixture", bad_anchor: bool = False) -> dict:
    target = "not_a_real_symbol" if bad_anchor else "cJSON_Delete"
    return {
        "schema_version": 1,
        "name": "fixture tasks",
        "repository": "tests/fixtures/mini_c",
        "revision": revision,
        "public_source": "内置公开 C fixture",
        "reviewer": "fixture-reviewer", "reviewed_at": "2026-09-04",
        "failure_policy": "未命中固定锚点或门禁不满足即失败。",
        "tasks": [
            {
                "id": "locate-delete", "type": "定位", "status": "draft",
                "question": f"{target} 在哪里定义？",
                "source_permalink": f"https://example.test/{revision}/mini.c#L20",
                "expected_anchors": [{"symbol": target, "path": "mini.c", "line_start": 20}],
                "rationale": "固定 fixture 的精确源码锚点。",
            },
            {
                "id": "call-new-item", "type": "调用链", "status": "draft",
                "question": "谁直接调用 cJSON_New_Item？",
                "source_permalink": f"https://example.test/{revision}/mini.c#L15",
                "expected_anchors": [{"symbol": "cleanup_a", "path": "mini.c", "line_start": 89}],
                "rationale": "清理路径会创建并释放新节点，是直接调用者。",
            },
            {
                "id": "impact-parse-value", "type": "影响分析", "status": "draft",
                "question": "修改 parse_value 的 2 跳 certain 影响？",
                "source_permalink": f"https://example.test/{revision}/mini.c#L44",
                "target": {"symbol": "parse_value", "path": "mini.c", "line_start": 44},
                "depth": 2,
                "expected_certain": [
                    {"symbol": "cJSON_ParseWithOpts", "path": "mini.c", "line_start": 52},
                    {"symbol": "workflow", "path": "driver.c", "line_start": 10},
                ],
                "rationale": "run_all 对 parse_value 的取地址是 candidate，不能进入 gold。",
            },
            {
                "id": "refuse-unrelated", "type": "无证据拒答", "status": "draft",
                "question": "如何轮换 Kubernetes Ingress TLS 证书？",
                "source_permalink": f"https://example.test/{revision}/mini.c#L1",
                "rationale": "与内置 C fixture 无关。",
            },
        ],
    }


def _write_manifest(tmp_path: Path, manifest: dict) -> Path:
    path = tmp_path / "tasks.yaml"
    path.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return path


def test_public_manifests_have_40_reviewed_tasks_and_balanced_categories():
    root = Path(__file__).resolve().parents[1]
    all_tasks = []
    for name in ("tasks_cjson.yaml", "tasks_lwip.yaml"):
        manifest = yaml.safe_load((root / "eval" / name).read_text(encoding="utf-8"))
        assert len(manifest["tasks"]) == 20
        assert all(task["status"] == "approved" and task.get("rationale")
                   and task.get("source_permalink") for task in manifest["tasks"])
        all_tasks.extend(manifest["tasks"])
    assert len(all_tasks) == 40
    assert sum(task["type"] == task_eval.EXPERIENCE_TYPE for task in all_tasks) == 10
    assert sum(task["type"] == "定位" for task in all_tasks) == 10
    assert sum(task["type"] in ("调用链", "语义检索") for task in all_tasks) == 10
    assert sum(task["type"] in (task_eval.IMPACT_TYPE, task_eval.REFUSAL_TYPE)
               for task in all_tasks) == 10


def test_task_eval_runs_fixture_and_keeps_candidate_separate(kb, tmp_path):
    conn, data_dir = kb
    tasks = _write_manifest(tmp_path, _manifest())
    out = tmp_path / "TASK.md"

    report = task_eval.main(conn, str(tasks), data_dir=data_dir, embedder_kind="null",
                            out=str(out), db_path="fixture.db")

    assert all(item["passed"] for item in report["retrieval"])
    impact = report["impacts"][0]
    assert impact["precision"] == impact["recall"] == impact["f1"] == 1.0
    assert impact["candidate_count"] >= 1
    assert impact["candidate_independent"] is True
    assert report["refusals"][0]["passed"] is True
    text = out.read_text(encoding="utf-8")
    assert "待人工确认，不可写入简历指标" in text
    assert "A 级 provenance" in text


def test_task_eval_rejects_mismatched_revision_and_unknown_anchor(kb, tmp_path):
    conn, _ = kb
    wrong_revision = _write_manifest(tmp_path, _manifest(revision="other"))
    with pytest.raises(task_eval.TaskEvalError, match="revision"):
        task_eval.load_task_set(conn, str(wrong_revision))

    unknown = _write_manifest(tmp_path, _manifest(bad_anchor=True))
    with pytest.raises(task_eval.TaskEvalError, match="锚点无法解析"):
        task_eval.load_task_set(conn, str(unknown))


def test_task_eval_requires_complete_provenance_and_approved_gate(kb, tmp_path):
    conn, data_dir = kb
    tasks = _write_manifest(tmp_path, _manifest())
    with pytest.raises(task_eval.TaskEvalError, match="尚未审核"):
        task_eval.load_task_set(conn, str(tasks), require_approved=True)

    # The source anchor still resolves, but the selected A-level citation can no
    # longer be published because it has lost its stable compiler identity.
    original_usr = conn.execute(
        "SELECT usr FROM node WHERE name='cJSON_Delete' AND path='mini.c'"
    ).fetchone()["usr"]
    conn.execute("UPDATE node SET usr=NULL WHERE name='cJSON_Delete' AND path='mini.c'")
    conn.commit()
    try:
        report = task_eval.main(conn, str(tasks), data_dir=data_dir, embedder_kind="null",
                                out=str(tmp_path / "TASK.md"))
    finally:
        conn.execute("UPDATE node SET usr=? WHERE name='cJSON_Delete' AND path='mini.c'",
                     (original_usr,))
        conn.commit()
    locate = next(item for item in report["retrieval"] if item["id"] == "locate-delete")
    assert locate["passed"] is False
    assert "provenance" in locate["errors"][0]


def test_vector_only_neighbour_is_not_promoted_to_evidence(kb, monkeypatch):
    """A nearest-neighbour implementation must not defeat the refusal boundary."""
    conn, data_dir = kb
    arbitrary = conn.execute("SELECT rowid FROM chunk WHERE kind='code' LIMIT 1").fetchone()["rowid"]
    monkeypatch.setattr(engine, "recall_symbol", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(engine, "recall_bm25", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(engine, "recall_vector", lambda *_args, **_kwargs: [arbitrary])

    result = engine.ask(conn, "unrelated cloud certificate rotation", embedder=object(),
                        data_dir=data_dir, llm_client=None)
    assert result["recall"]["vector"] == 0
    assert result["refused"] is True and result["ab_evidence"] == 0
