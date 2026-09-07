"""Interview-sized portfolio demo built from the existing trusted retrieval core.

The demo never writes to the supplied knowledge base. It makes a SQLite backup in a
small work directory, then demonstrates code evidence, certain-only impact analysis,
the pending → approved experience gate, refusal, and an optional LLM expression layer.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from . import db as dbm
from . import agent
from . import conversation
from .experience import store
from .graph import traverse
from .indexer import build as indexer
from .retrieve import engine


CJSON_REPOSITORY = "https://github.com/DaveGamble/cJSON"
CJSON_REVISION = "fb16e5cf358798aabb049655975cde8427101056"
LOCATION_QUESTION = "cJSON_ParseWithOpts 在哪里定义，它是哪个解析入口？"
# This exact public sample title is deliberate: SQLite's unicode tokenizer treats a
# continuous Chinese phrase as one token, so shortening it would test tokenization
# rather than the pending → approved visibility boundary.
EXPERIENCE_QUESTION = "深层嵌套输入审查示例"
REFUSAL_QUESTION = "如何轮换 Kubernetes Ingress 的 TLS 证书？"
WORKFLOW_QUESTION = "深层嵌套 JSON 输入审查 parse_value 的影响范围"
_CITATION_TAG = re.compile(r"\[([AB]\d+)\]")


class PortfolioDemoError(ValueError):
    """The requested database cannot safely serve the fixed portfolio demo."""


def _backup_database(source_db: str, work_dir: str) -> str:
    source = Path(source_db).expanduser().resolve()
    if not source.is_file():
        raise PortfolioDemoError(f"知识库不存在: {source_db}")
    root = Path(work_dir).expanduser().resolve()
    if root == Path.cwd().resolve():
        raise PortfolioDemoError("work_dir 不能是项目根目录；请使用专用演示目录")
    target = root / "portfolio-demo.db"
    if source == target:
        raise PortfolioDemoError("演示数据库不能覆盖源知识库")
    root.mkdir(parents=True, exist_ok=True)
    dbm.reset(target)
    from .eval.workflow_eval import _copy_database, _reset_isolated_governance
    _copy_database(source, target)
    target_conn = dbm.connect(target)
    _reset_isolated_governance(target_conn)
    dbm.set_meta(target_conn, "knowledge_dir", str(root / "knowledge"))
    dbm.set_meta(target_conn, "evaluation_scope", "isolated_experiment")
    target_conn.commit()
    target_conn.close()
    return str(target)


def _require_cjson_snapshot(conn: sqlite3.Connection) -> None:
    repository = dbm.get_meta(conn, "repository_id", dbm.get_meta(conn, "repo", ""))
    revision = dbm.get_meta(conn, "revision", "unversioned")
    if repository != CJSON_REPOSITORY or revision != CJSON_REVISION:
        raise PortfolioDemoError(
            "portfolio-demo 只接受固定 cJSON 快照；请先运行 `bash demo.sh cjson`。"
        )


def _first_code_evidence(result: dict[str, Any]) -> dict[str, Any]:
    for citation in result.get("citations", []):
        provenance = citation.get("provenance") or {}
        if citation.get("level") == "A" and provenance.get("symbol"):
            required = ("repository", "revision", "usr", "path", "line_start", "line_end")
            if all(provenance.get(field) for field in required):
                return citation
    raise PortfolioDemoError("定位结果缺少可追溯 A 级代码证据")


def _valid_llm_answer(answer: str | None, citations: list[dict[str, Any]]) -> bool:
    if not isinstance(answer, str) or not answer.strip():
        return False
    tags = set(_CITATION_TAG.findall(answer))
    allowed = {citation.get("tag") for citation in citations if citation.get("level") in ("A", "B")}
    return bool(tags) and tags <= allowed


def _ask_with_optional_expression(conn: sqlite3.Connection, *, embedder, data_dir: str,
                                  llm_client=None) -> tuple[dict[str, Any], str]:
    """Keep a failed or uncited model response out of the interview demonstration."""
    baseline = engine.ask(conn, LOCATION_QUESTION, embedder=embedder, data_dir=data_dir,
                          llm_client=None)
    if llm_client is None:
        return baseline, "disabled"
    try:
        generated = engine.ask(conn, LOCATION_QUESTION, embedder=embedder, data_dir=data_dir,
                               llm_client=llm_client)
        if _valid_llm_answer(generated.get("answer"), generated.get("citations", [])):
            return generated, "generated"
    except Exception:
        pass
    return baseline, "fallback_to_evidence_pack"


def run(source_db: str, *, work_dir: str, experience_file: str,
        embedder_kind: str = "tfidf", llm_client=None,
        require_cjson_snapshot: bool = True, conversation_file: str | None = None) -> dict[str, Any]:
    """Run a complete, isolated portfolio demonstration and return structured results."""
    demo_db = _backup_database(source_db, work_dir)
    work = Path(work_dir).expanduser().resolve()
    demo_data = work / "index"
    conn = dbm.connect(demo_db)
    try:
        if require_cjson_snapshot:
            _require_cjson_snapshot(conn)
        repository = dbm.get_meta(conn, "repository_id", dbm.get_meta(conn, "repo", ""))
        revision = dbm.get_meta(conn, "revision", "unversioned")
        repo = dbm.get_meta(conn, "repo", "")
        if not repo:
            raise PortfolioDemoError("知识库缺少源码目录元数据，无法重建演示索引")

        # The public sample enters the copied database as pending. Rebuilding here
        # creates its hidden chunk, allowing the demo to prove visibility rather than
        # merely print a status field.
        imported = store.import_jsonl(conn, experience_file, llm_client=None)
        if imported["imported"] != 1 or imported["failed"]:
            raise PortfolioDemoError(f"公开经验样例导入失败: {imported}")
        indexer.build(conn, repo, embedder_kind=embedder_kind, out_dir=str(demo_data))
        embedder = indexer.get_embedder(embedder_kind, str(demo_data))

        locate, llm_mode = _ask_with_optional_expression(
            conn, embedder=embedder, data_dir=str(demo_data), llm_client=llm_client
        )
        evidence = _first_code_evidence(locate)

        target = traverse.resolve_symbol(conn, "parse_value")
        if not target:
            raise PortfolioDemoError("固定 cJSON 快照中未找到 parse_value")
        impact = traverse.impact(conn, target[0]["id"], max_hop=2)

        row = conn.execute(
            "SELECT id, status FROM experience WHERE source_type='public-manual-example'"
        ).fetchone()
        if row is None or row["status"] != "pending":
            raise PortfolioDemoError("公开经验样例未以 pending 状态进入演示库")
        pending = engine.ask(conn, EXPERIENCE_QUESTION, embedder=embedder,
                             data_dir=str(demo_data), llm_client=None)
        pending_visible = any(citation.get("uid") == f"exp:{row['id']}"
                              for citation in pending.get("citations", []))
        if pending_visible:
            raise PortfolioDemoError("pending 经验泄漏进检索结果")

        # This is deliberately a simulation in a copied database. The source sample
        # itself still requires a human reviewer before any real publication. The
        # reviewer must bind the card to a concrete USR; a symbol-name hint in the
        # imported JSONL is never enough to publish B-level evidence.
        store.bind(conn, row["id"], target[0]["usr"], bound_by="portfolio-demo")
        qa = store.qa_add(conn, row["id"], "深层嵌套失败路径如何由源码验证？",
                          ["固定 revision 的入口、递归函数和失败返回"], ["A1"])
        store.qa_review(conn, qa["id"], True, "portfolio demo: QA evidence checked",
                        "portfolio-demo", reviewer_type="fixture",
                        evaluation_scope="isolated_experiment")
        store.review(conn, row["id"], "approve",
                     note="portfolio demo: simulated review of public sample",
                     reviewer="portfolio-demo", reviewer_type="fixture",
                     evaluation_scope="isolated_experiment")
        approved = engine.ask(conn, EXPERIENCE_QUESTION, embedder=embedder,
                              data_dir=str(demo_data), llm_client=None)
        approved_visible = next(
            (citation for citation in approved.get("citations", [])
             if citation.get("uid") == f"exp:{row['id']}" and citation.get("level") == "B"),
            None,
        )
        if approved_visible is None:
            raise PortfolioDemoError("approved 经验未成为 B 级检索证据")

        refused = engine.ask(conn, REFUSAL_QUESTION, embedder=embedder,
                             data_dir=str(demo_data), llm_client=None)
        if not refused.get("refused") or refused.get("ab_evidence") != 0:
            raise PortfolioDemoError("无关问题没有触发无证据拒答")

        candidate_total = conn.execute(
            "SELECT COUNT(*) FROM edge WHERE kind='calls' AND confidence='candidate'"
        ).fetchone()[0]
        workflow = None
        if conversation_file:
            session = conversation.import_public_session(conn, conversation_file)
            run = agent.run(conn, WORKFLOW_QUESTION, data_dir=str(demo_data), embedder=embedder,
                            session_id=session["id"], llm_client=None, save_draft=True)
            if not run["draft_id"]:
                raise PortfolioDemoError("workflow Agent 未生成待审核知识卡")
            parse_value = traverse.resolve_symbol(conn, "parse_value")
            if not parse_value or not parse_value[0]["usr"]:
                raise PortfolioDemoError("workflow 缺少 parse_value USR")
            store.bind(conn, run["draft_id"], parse_value[0]["usr"], bound_by="portfolio-demo")
            qa = store.qa_add(conn, run["draft_id"], "该会话结论对应哪些源码事实？",
                              ["parse_value 定义和失败路径"],
                              [run["draft_preview"]["evidence_tags"][0]])
            store.qa_review(conn, qa["id"], True, "公开会话与源码证据一致", "portfolio-demo",
                            reviewer_type="fixture", evaluation_scope="isolated_experiment")
            store.review(conn, run["draft_id"], "approve", "公开合成会话与固定源码锚点复核",
                         "portfolio-demo", reviewer_type="fixture",
                         evaluation_scope="isolated_experiment")
            indexer.build(conn, repo, embedder_kind=embedder_kind, out_dir=str(demo_data))
            visible = engine.ask(conn, WORKFLOW_QUESTION, embedder=embedder,
                                 data_dir=str(demo_data), llm_client=None)
            b_visible = any(c.get("uid") == f"exp:{run['draft_id']}" for c in visible["citations"])
            # The isolated database simulates a changed definition hash. It never
            # mutates the pinned public source checkout or the source knowledge base.
            conn.execute("UPDATE node SET definition_hash='demo-changed' WHERE id=?", (parse_value[0]["id"],))
            stale = store.stale_check(conn)
            hidden = engine.ask(conn, WORKFLOW_QUESTION, embedder=embedder,
                                data_dir=str(demo_data), llm_client=None)
            b_hidden = not any(c.get("uid") == f"exp:{run['draft_id']}" for c in hidden["citations"])
            if not b_visible or not stale["stale"] or not b_hidden:
                raise PortfolioDemoError("workflow 审核/过期隔离演示未达到预期")
            workflow = {"session_id": session["id"], "run_id": run["id"], "card_id": run["draft_id"],
                        "tool_calls": len(run["events"]), "b_visible_after_review": b_visible,
                        "stale": stale["stale"], "b_hidden_after_stale": b_hidden}
        return {
            "demo_db": demo_db,
            "demo_data": str(demo_data),
            "repository": repository,
            "revision": revision,
            "locate": locate,
            "evidence": evidence,
            "impact": impact,
            "candidate_total": candidate_total,
            "experience": {
                "id": row["id"],
                "pending_visible": pending_visible,
                "approved_visible": True,
                "citation": approved_visible,
                "simulated_review": True,
            },
            "refusal": refused,
            "llm_mode": llm_mode,
            "workflow": workflow,
        }
    finally:
        conn.close()


def render_markdown(result: dict[str, Any]) -> str:
    """Write a compact leave-behind without implying the sample was production data."""
    evidence = result["evidence"]
    provenance = evidence["provenance"]
    impact = result["impact"]
    total_impact = sum(len(items) for items in impact["by_hop"].values())
    L = ["# CodeAtlas 求职演示", "",
         "> 使用公开 cJSON 固定快照和隔离演示数据库。默认未调用 LLM；"
         "经验批准步骤为**模拟人工审核**，不改变源知识库。", "",
         "## 运行快照", "",
         f"- 仓库：`{result['repository']}`", f"- revision：`{result['revision']}`",
         f"- LLM 表达层：`{result['llm_mode']}`", "",
         "## 1. 定位与 A 级证据", "",
         f"- `{provenance['symbol']}` → `{provenance['path']}:L{provenance['line_start']}`",
         f"- USR：`{provenance['usr']}`；来源：`{provenance['repository']}@{provenance['revision']}`", "",
         "## 2. certain 影响分析", "",
         f"- `parse_value` 的 2 跳确定影响：{total_impact} 个函数 / "
         f"{len(impact['affected_files'])} 个文件。",
         f"- 当前目标的 candidate 影响：{len(impact['candidate_impact'])}；"
         f"图谱中候选调用总数：{result['candidate_total']}（始终与确定结论隔离）。", "",
         "## 3. 经验审核闸门", "",
         "- 公开样例在 `pending` 时不可检索；在隔离演示库中模拟审核后，成为 B 级证据。", "",
         "## 4. 无证据拒答", "",
         f"- `{REFUSAL_QUESTION}` → `refused={result['refusal']['refused']}`，"
         f"`A/B={result['refusal']['ab_evidence']}`。", "",
         "## 边界", "",
         "规则摘要头和分层 Wiki 是由代码事实生成的结构化说明；LLM 仅可选地改写"
         "已选证据。当前未将真实 LLM-Wiki 的自动演进、质量、成本或生产稳定性作为成果。", ""]
    workflow = result.get("workflow")
    if workflow:
        insertion = ["## 5. Agent 与会话知识闭环", "",
                     f"- 会话：`{workflow['session_id']}`；受限工具调用：{workflow['tool_calls']} 次。",
                     f"- 卡片：`{workflow['card_id']}`；审核后 B 级可见={workflow['b_visible_after_review']}。",
                     f"- 隔离库模拟定义变化后 B 级隐藏={workflow['b_hidden_after_stale']}；"
                     f"stale 原因：{workflow['stale'][0]['reason'] if workflow['stale'] else '无' }。", ""]
        boundary = L.index("## 边界")
        L[boundary:boundary] = insertion
    return "\n".join(L)
