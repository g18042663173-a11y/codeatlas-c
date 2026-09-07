"""Controlled knowledge-layer ablation with a common code-only retrieval projection."""
from __future__ import annotations
import json
import sqlite3
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone

from .. import agent, db
from ..contracts import digest
from . import task_eval, rubric, protocol

ARMS = {
    "source_only": ("共享代码检索，无 Wiki", "flat"),
    # Legacy name retained; flat disables forced order, it never meant full-text injection.
    "wiki_flat": ("共享代码检索 + Wiki 自由阅读", "flat"),
    "wiki_progressive": ("共享代码检索 + Wiki 渐进阅读", "progressive"),
}


def _copy(conn):
    target = sqlite3.connect(":memory:")
    target.row_factory = sqlite3.Row
    conn.backup(target)
    return target


def code_projection(conn, directory):
    """All arms use identical FTS statistics, vector rows and code graph."""
    import numpy as np
    from ..indexer.build import LocalHashEmbedder
    conn.execute("UPDATE chunk SET visible=0 WHERE kind IN ('experience','wiki')")
    conn.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('delete-all')")
    conn.execute("INSERT INTO chunk_fts(rowid,title,text) SELECT rowid,title,text FROM chunk WHERE visible=1")
    conn.commit()
    rows = conn.execute("SELECT rowid AS id,title,text FROM chunk WHERE visible=1 ORDER BY rowid").fetchall()
    embedder = LocalHashEmbedder()
    np.save(Path(directory) / "vectors.npy", embedder.encode([r["text"] for r in rows]))
    (Path(directory) / "vec_ids.json").write_text(json.dumps([r["id"] for r in rows]))
    return embedder, digest([tuple(r) for r in rows])


def evaluate(conn, tasks_path, *, client=None, data_dir="data", embedder=None, runs=3,
             seed=17, answer_key_path=None):
    from .model_eval import _abc_grade
    protocol.schedule([], ARMS, runs, seed)  # Validate even in no-key mode.
    # Preserve the no-key short circuit for callers probing capability without
    # supplying a task bundle.  Real proof runs with existing paths still bind
    # the frozen task and answer-key hashes below before reporting ``not_run``.
    if client is None and not Path(tasks_path).is_file():
        return {"report_kind": "reading", "status": "not_run", "arms": ARMS,
                "reason": "model_not_configured", "passed": False}
    if db.get_meta(conn, "snapshot_enabled") == "1":
        from ..snapshots import pin
        with pin(conn) as context:
            return evaluate(context.conn, tasks_path, client=client, data_dir=str(context.directory),
                            runs=runs, seed=seed, answer_key_path=answer_key_path)
    manifest = task_eval.load_task_set(conn, tasks_path, require_approved=True)
    if not conn.execute("SELECT 1 FROM wiki_page WHERE status='ok' LIMIT 1").fetchone():
        return {"report_kind": "reading", "status": "not_run", "arms": ARMS,
                "reason": "wiki_unavailable", "passed": False}
    tasks = [t for t in manifest["tasks"] if t["type"] != task_eval.EXPERIENCE_TYPE]
    if not tasks:
        raise ValueError("reading ablation requires code/refusal tasks")
    gold = protocol.answer_key(answer_key_path, tasks_path, tasks)
    task_set_hash = digest(Path(tasks_path).read_bytes())
    answer_key_hash = gold.get("content_hash")
    if client is None:
        order = protocol.schedule(tasks, ARMS, runs, seed)
        return {
            "report_kind": "reading", "status": "not_run", "arms": ARMS,
            "reason": "model_not_configured", "passed": False,
            "task_count": len(tasks), "repository": manifest["repository"],
            "revision": manifest["revision"], "task_set": str(tasks_path),
            "task_scope": "full", "runs_per_task": runs,
            "planned_trial_count": len(order), "executed_trial_count": 0,
            "model": None, "temperature": .1,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "manifest_hash": task_set_hash, "task_set_hash": task_set_hash,
            "answer_key_hash": answer_key_hash, "answer_key": gold,
            "identity": rubric.identity(conn, tasks_path),
            "protocol": {
                "version": protocol.VERSION, "seed": seed,
                "schedule_hash": digest(order), "variant_count": len(ARMS),
                "max_tool_calls": 6, "max_code_read_lines": 300,
                "estimated_input_token_limit": 8000, "output_token_limit": 1200,
            },
            "review_status": gold.get("review_status", "unresolved"),
            "answer_review_status": "not_run",
            "acceptance": {"passed": False, "effect_observed": False,
                           "human_release_eligible": False,
                           "execution_complete": False},
            "boundary": "冻结题集、答案键和数据库身份已校验；未配置模型，没有效果数字。",
        }
    order = protocol.schedule(tasks, ARMS, runs, seed)
    raw = {arm: [{"id": t["id"], "type": t["type"], "question": t["question"],
                  "mechanism_id": t.get("mechanism_id", t["id"]),
                  "category": t.get("category"), "repetitions": []}
                 for t in tasks] for arm in ARMS}
    task_map = {t["id"]: t for t in tasks}
    rows = {(arm, row["id"]): row for arm in raw for row in raw[arm]}
    projection = _copy(conn)
    started = time.perf_counter()
    budget_exhausted = False
    try:
        with tempfile.TemporaryDirectory(prefix="codeatlas-reading-") as directory:
            common_embedder, projection_hash = code_projection(projection, directory)
            preparation_ms = (time.perf_counter() - started) * 1000
            for number, event in enumerate(order):
                isolated = _copy(projection)  # No run history crosses trials.
                arm = event["variant"]
                if arm == "source_only":
                    isolated.execute("UPDATE wiki_page SET status='stale'")
                    isolated.commit()
                before = protocol.usage_start(client)
                trial_start = time.perf_counter()
                try:
                    task = task_map[event["task_id"]]
                    result = agent.run(isolated, task["question"], llm_client=client, mode="model",
                                       read_mode=ARMS[arm][1], embedder=common_embedder, data_dir=directory)
                    from .model_eval import agent_trial
                    once = agent_trial(result)
                except Exception as exc:
                    once = protocol.error_trial(exc)
                finally:
                    isolated.close()
                once.update(protocol.usage_delta(client, before))
                once.update(latency_ms=round((time.perf_counter() - trial_start) * 1000, 1),
                            sequence=number, repetition=event["repetition"])
                graded = _abc_grade(task_map[event["task_id"]], once, variant=arm)
                rows[(arm, event["task_id"])]["repetitions"].append({**once, **graded})
                if protocol.budget_exhausted(once):
                    budget_exhausted = True
                    break
    finally:
        projection.close()
    config = {"version": protocol.VERSION, "seed": seed, "schedule": order, "max_tool_calls": 6,
              "max_code_read_lines": 300, "estimated_input_token_limit": 8000, "output_token_limit": 1200,
              "retrieval_projection_hash": projection_hash, "embedding": "local-hash-384",
              "wiki_in_search_results": False, "experience_in_any_arm": False,
              "wiki_flat_meaning": "free tool reading, not automatic full-page injection"}
    result = {"report_kind": "reading",
              "status": "partial_budget" if budget_exhausted else "completed",
              "task_scope": "partial_budget" if budget_exhausted else "full",
              "task_count": len(tasks), "repository": manifest["repository"], "revision": manifest["revision"],
              "task_set": str(tasks_path), "manifest_hash": task_set_hash,
              "task_set_hash": task_set_hash, "answer_key_hash": answer_key_hash,
              "model": getattr(client, "model", "openai-compatible"), "temperature": .1,
              "runs_per_task": runs, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "identity": rubric.identity(conn, tasks_path), "protocol": config, "answer_key": gold,
              "preparation_ms": preparation_ms, "raw_trials": raw,
              "variants": {arm: {"name": ARMS[arm][0]} for arm in ARMS}, "acceptance": {"gates": {}},
              "boundary": "只比较 Wiki 可用性和阅读策略；三组代码检索完全相同、均无知识卡。自由阅读不等于 Wiki 全文平铺。"}
    result["evaluation_hash"] = digest([config, gold, result["model"], result["temperature"], runs])
    result["blind_review"] = rubric.blind_packet(result)
    if budget_exhausted:
        result["answer_review_status"] = "unresolved"
        result["review_status"] = "unresolved"
        result["acceptance"].update(
            passed=False, effect_observed=False, human_release_eligible=False,
            gates={"budget_complete": False, "paired_trials_complete": False})
        return result
    rubric.finalize(result)
    if result.get("answer_review_status") == "completed":
        kinds = {"定位", "调用链", "语义检索", "影响分析"}
        grouped = protocol.grouped_paired_effect(
            raw, "wiki_flat", "source_only", "correct", kinds=kinds, seed=seed,
        )
        result["acceptance"]["primary_mechanism_grouped_effect"] = grouped
    return result
