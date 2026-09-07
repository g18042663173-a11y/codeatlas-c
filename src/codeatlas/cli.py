"""CodeAtlas 命令行入口。每个阶段一条命令，全部幂等、可中断续跑。"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import db as dbm

app = typer.Typer(add_completion=False, help="大型 C 工程 AI 知识库")
exp_app = typer.Typer(help="专家经验库")
session_app = typer.Typer(help="公开会话资产（仅审核，不进入检索）")
card_app = typer.Typer(help="带精确源码锚点的知识卡")
agent_app = typer.Typer(help="受约束的只读诊断 Agent")
wiki_app = typer.Typer(help="分层代码 Wiki", invoke_without_command=True)
eval_app = typer.Typer(help="离线消融与可选真实模型评测", invoke_without_command=True)
campaign_app = typer.Typer(help="冻结证明试次与持久额度账本；断点续跑不覆盖结果")
proof_app = typer.Typer(help="三类价值证明的统一入口（campaign 的稳定别名）")
eval_app.add_typer(campaign_app, name="campaign")
eval_app.add_typer(proof_app, name="proof")
snapshot_app = typer.Typer(help="冻结源码、构建并原子切换知识快照")
app.add_typer(snapshot_app, name="snapshot")
app.add_typer(exp_app, name="exp")
app.add_typer(session_app, name="session")
app.add_typer(card_app, name="card")
app.add_typer(agent_app, name="agent")
app.add_typer(wiki_app, name="wiki")
app.add_typer(eval_app, name="eval")
console = Console()

DEFAULT_DB = "data/kb.db"


@campaign_app.command("prepare")
def campaign_prepare(directory: str, project_root: str = ".", include_abc: bool = False,
                     runs: int = 3, seed: int = 17, smoke_trials: int = 24,
                     max_requests: int | None = None, max_cost_usd: float | None = None,
                     input_usd_per_million: float | None = None,
                     output_usd_per_million: float | None = None,
                     base_url: str | None = None, model: str | None = None,
                     thinking_mode: str = "disabled", workers: int = 1,
                     transport_retries: int = 0):
    """准备隔离实验目录；默认 OpenCode Go，密钥不写入计划。"""
    from .eval.proof_campaign import prepare
    console.print_json(data=prepare(directory, project_root=project_root, include_abc=include_abc,
        runs=runs, seed=seed, smoke_trials=smoke_trials, max_requests=max_requests,
        max_cost_usd=max_cost_usd, input_usd_per_million=input_usd_per_million,
        output_usd_per_million=output_usd_per_million, base_url=base_url, model=model,
        thinking_mode=thinking_mode, workers=workers,
        transport_retries=transport_retries))


def _campaign_run(directory, mode, resume):
    from .eval.proof_campaign import run
    result = run(directory, mode=mode, resume=resume)
    console.print_json(data={key: value for key, value in result.items()
                            if key not in {"reports", "raw_trials", "blind_review",
                                           "task_manifest", "answer_key"}})


def _campaign_preflight(directory):
    from .eval.proof_campaign import preflight
    result = preflight(directory)
    console.print_json(data=result)
    if result.get("passed") is not True:
        raise typer.Exit(1)


@campaign_app.command("preflight")
def campaign_preflight(directory: str):
    """用不含题目的一次请求验证冻结的模型、JSON 与思考模式协议。"""
    _campaign_preflight(directory)


@campaign_app.command("run")
def campaign_run(directory: str, mode: str = "smoke"):
    """显式执行 smoke/full；LLM_API_KEY 只从环境读取。"""
    _campaign_run(directory, mode, False)


@campaign_app.command("resume")
def campaign_resume(directory: str, mode: str = "full"):
    """跳过已完成和中断不确定试次；绝不自动重试挑选结果。"""
    _campaign_run(directory, mode, True)


@campaign_app.command("report")
def campaign_report(directory: str, out: str | None = None, job: str | None = None):
    """读取完整原始试次与额度账本；可导出到新的 JSON 文件。"""
    from .eval.proof_campaign import report
    result = report(directory)
    if job:
        if job not in result["reports"]:
            raise typer.BadParameter("unknown campaign job id")
        result = result["reports"][job]
    if out:
        from .eval.protocol import ensure_new_outputs
        from .publication import atomic_text
        ensure_new_outputs(out)
        atomic_text(Path(out), json.dumps(result, ensure_ascii=False, indent=2))
    console.print_json(data={key: value for key, value in result.items() if key != "reports"})


@campaign_app.command("review")
def campaign_review(directory: str, resume: bool = False, out: str | None = None,
                    attempt: str | None = None, workers: int = 1,
                    judge_model: str | None = None, calibration: str | None = None,
                    calibration_only: bool = False,
                    transport_retries: int | None = typer.Option(None, "--transport-retries")):
    """先校准两个 V4 Flash 独立 judge，再盲审；分歧由已校准第三 agent 仲裁。"""
    from .eval.campaign_review import review
    if out:
        from .eval.protocol import ensure_new_outputs
        ensure_new_outputs(out)
    result = review(directory, resume=resume, attempt_id=attempt, workers=workers,
                    judge_model=judge_model, calibration_path=calibration,
                    calibration_only=calibration_only,
                    transport_retries=transport_retries)
    if out:
        from .publication import atomic_text
        atomic_text(Path(out), json.dumps(result, ensure_ascii=False, indent=2))
    console.print_json(data={key: value for key, value in result.items()
                            if key not in {"reviews", "reviewed_reports", "calibration_runs", "raw_calls"}})


@proof_app.command("prepare")
def proof_prepare(directory: str, project_root: str = ".", include_abc: bool = False,
                  runs: int = 3, seed: int = 17, smoke_trials: int = 24,
                  max_requests: int | None = None, max_cost_usd: float | None = None,
                  input_usd_per_million: float | None = None,
                  output_usd_per_million: float | None = None,
                  base_url: str | None = None, model: str | None = None,
                  thinking_mode: str = "disabled", workers: int = 1,
                  transport_retries: int = 0):
    """冻结题集、模型、预算和试次顺序；不会读取或保存 API Key。"""
    return campaign_prepare(
        directory, project_root=project_root, include_abc=include_abc,
        runs=runs, seed=seed, smoke_trials=smoke_trials,
        max_requests=max_requests, max_cost_usd=max_cost_usd,
        input_usd_per_million=input_usd_per_million,
        output_usd_per_million=output_usd_per_million,
        base_url=base_url, model=model, thinking_mode=thinking_mode, workers=workers,
        transport_retries=transport_retries,
    )


@proof_app.command("run")
def proof_run(campaign_dir: str = typer.Option(..., "--campaign"),
              profile: str = typer.Option("smoke", "--profile"),
              resume: bool = typer.Option(False, "--resume")):
    """运行固定 smoke/full 前缀；续跑不覆盖已完成或不确定试次。"""
    if profile not in {"smoke", "full"}:
        raise typer.BadParameter("profile must be smoke or full")
    _campaign_run(campaign_dir, profile, resume)


@proof_app.command("preflight")
def proof_preflight(campaign_dir: str = typer.Option(..., "--campaign")):
    """正式冒烟前验证服务协议；失败批次不会自动重试。"""
    _campaign_preflight(campaign_dir)


@proof_app.command("review")
def proof_review(campaign_dir: str = typer.Option(..., "--campaign"),
                 resume: bool = typer.Option(False, "--resume"),
                 out: str | None = None,
                 attempt: str | None = typer.Option(None, "--attempt"),
                 workers: int = typer.Option(1, "--workers"),
                 judge_model: str | None = typer.Option(None, "--judge-model"),
                 calibration: str | None = typer.Option(None, "--calibration"),
                 calibration_only: bool = typer.Option(False, "--calibration-only"),
                 transport_retries: int | None = typer.Option(None, "--transport-retries")):
    """校准并运行双 Agent 盲审；结果始终标记为 AI review。"""
    return campaign_review(campaign_dir, resume, out, attempt, workers, judge_model,
                           calibration, calibration_only, transport_retries)


@proof_app.command("export-review")
def proof_export_review(campaign_dir: str = typer.Option(..., "--campaign"),
                        attempt: str = typer.Option(..., "--attempt"),
                        experiments: str = typer.Option(
                            "eval/experiments.yaml", "--experiments"),
                        allow_terminal_unresolved: bool = typer.Option(
                            False, "--allow-terminal-unresolved")):
    """导出精简评审；语义未决必须显式允许且技术调用已完整。"""
    from .eval.campaign_review import export_reviewed_reports
    result = export_reviewed_reports(campaign_dir, attempt_id=attempt,
                                     experiments_path=experiments,
                                     allow_terminal_unresolved=allow_terminal_unresolved)
    console.print_json(data=result)


@proof_app.command("qualified-review")
def proof_qualified_review(
        campaign_dir: str = typer.Option(..., "--campaign"),
        policy: str = typer.Option("eval/judge_qualification_v4.yaml", "--policy"),
        resume: bool = typer.Option(False, "--resume"),
        calibration_only: bool = typer.Option(False, "--calibration-only")):
    """按密封策略执行 Terra 资格门禁，并仅在语义失败时启用 Sol。"""
    from .eval.qualification import QualificationError, run
    try:
        result = run(policy, campaign_dir=campaign_dir, resume=resume,
                     calibration_only=calibration_only)
    except (OSError, ValueError, QualificationError) as exc:
        console.print(f"[red]评分资格执行失败：{exc}[/]")
        raise typer.Exit(2) from exc
    console.print_json(data={
        key: value for key, value in result.items()
        if key not in {"primary", "fallback", "review"}
    })


@proof_app.command("report")
def proof_report(campaign_dir: str = typer.Option(..., "--campaign"),
                 out: str | None = None, job: str | None = None):
    """读取或导出不可变试次；不会重新执行模型请求。"""
    return campaign_report(campaign_dir, out, job)


@snapshot_app.command("status")
def snapshot_status(db: str = DEFAULT_DB):
    from . import snapshots
    console.print_json(data=snapshots.status(dbm.connect(db)))


@snapshot_app.command("build")
def snapshot_build(repo: str, compile_db: str, db: str = DEFAULT_DB,
                   activate: bool = False, allow_dirty: bool = False):
    from . import snapshots
    console.print_json(data=snapshots.build(dbm.connect(db), repo, compile_db,
                                          activate_after=activate, strict=not allow_dirty))


@snapshot_app.command("activate")
def snapshot_activate(identifier: str, db: str = DEFAULT_DB):
    from . import snapshots
    console.print_json(data=snapshots.activate(dbm.connect(db), identifier))


@snapshot_app.command("rollback")
def snapshot_rollback(identifier: str, db: str = DEFAULT_DB):
    from . import snapshots
    console.print_json(data=snapshots.activate(dbm.connect(db), identifier, rollback=True))


@eval_app.command("review")
def eval_review(report: str, reviews: str, out: str,
                calibration_report: str | None = None):
    """导入带来源的逐试次 rubric，保留原报告并生成新版评分。"""
    from .eval.rubric import apply_reviews
    from .publication import atomic_text
    from .eval.protocol import ensure_new_outputs
    ensure_new_outputs(out, Path(out).with_suffix(".md"))
    review_payload = json.loads(Path(reviews).read_text())
    if calibration_report:
        review_payload["judge_calibration"] = json.loads(Path(calibration_report).read_text())
    result = apply_reviews(json.loads(Path(report).read_text()), review_payload)
    if Path(out).resolve() in {Path(report).resolve(), Path(reviews).resolve()}:
        raise typer.BadParameter("必须使用新的 out 文件，保留原始试次报告")
    atomic_text(Path(out), json.dumps(result, ensure_ascii=False, indent=2))
    from .eval.model_eval import render_abc_markdown
    atomic_text(Path(out).with_suffix(".md"), render_abc_markdown(result, tasks_path=result.get("task_set", "see task_set_hash")))
    console.print(f"已保存重新评分报告：{out}；{result['answer_review_status']}")


@eval_app.command("calibrate-reviewer")
def eval_calibrate_reviewer(samples: str = "eval/review_calibration.yaml",
                             submission: str = typer.Option(..., "--submission"),
                             out: str = "docs/REVIEW-CALIBRATION.json"):
    """核对评分 agent 在 12 个冻结真值样例上的输出。"""
    from .eval.rubric import score_calibration
    from .eval.protocol import ensure_new_outputs
    from .publication import atomic_text
    ensure_new_outputs(out)
    try:
        result = score_calibration(samples, json.loads(Path(submission).read_text()))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        console.print(f"[red]评分校准无法完成：{exc}[/]")
        raise typer.Exit(2) from exc
    atomic_text(Path(out), json.dumps(result, ensure_ascii=False, indent=2))
    console.print(f"校准 {result['correct']}/{result['total']}；{result['status']} → {out}")
    if not result["passed"]:
        raise typer.Exit(1)


@eval_app.command("reading")
def eval_reading(db: str = DEFAULT_DB, tasks: str = "eval/tasks_cjson.yaml",
                 data_dir: str = "data", out: str = "docs/MODEL-READING.json", runs: int = 3,
                 seed: int = 17, answer_key: str | None = None):
    from .eval.reading_eval import evaluate
    from .llm.client import get_client
    from .publication import atomic_text
    from .eval.protocol import render, ensure_new_outputs
    ensure_new_outputs(out, Path(out).with_suffix(".md"), Path(out).with_suffix(".review.json"))
    connection = dbm.read_only(db)
    try:
        result = evaluate(connection, tasks, client=get_client(), data_dir=data_dir, runs=runs,
                          seed=seed, answer_key_path=answer_key)
    finally:
        connection.close()
    atomic_text(Path(out), json.dumps(result, ensure_ascii=False, indent=2))
    atomic_text(Path(out).with_suffix(".md"), render(result))
    if "blind_review" in result:
        atomic_text(Path(out).with_suffix(".review.json"), json.dumps(result["blind_review"], ensure_ascii=False, indent=2))
    console.print(result["status"])


@eval_app.command("knowledge-reuse")
def eval_knowledge_reuse(
        manifest: str = "eval/knowledge_reuse.yaml",
        answer_key: str | None = "eval/knowledge_reuse.answers.yaml",
        generated_artifact: str | None = None,
        out: str = "docs/PROOF-KNOWLEDGE-REUSE.json",
        runs: int = typer.Option(3, min=1), seed: int = 17):
    """运行两案例同源材料对照；无 Key 时只冻结并校验实验。"""
    from .eval import knowledge_reuse_eval
    from .eval.protocol import ensure_new_outputs
    from .llm.client import get_client
    from .publication import atomic_text

    markdown = Path(out).with_suffix(".md")
    ensure_new_outputs(out, markdown)
    try:
        result = knowledge_reuse_eval.evaluate(
            manifest, client=get_client(), project_root=Path.cwd(), runs=runs, seed=seed,
            answer_key_path=answer_key if answer_key and Path(answer_key).is_file() else None,
            generated_artifact=generated_artifact,
        )
    except ValueError as exc:
        console.print(f"[red]经验同源对照无法运行：{exc}[/]")
        raise typer.Exit(2) from exc
    atomic_text(Path(out), json.dumps(result, ensure_ascii=False, indent=2))
    atomic_text(markdown, knowledge_reuse_eval.render(result))
    console.print(
        f"{result['status']}：{result['case_count']} 个独立案例 / "
        f"{result.get('executed_trial_count', 0)} 个已执行答案试次 → {out}"
    )


@eval_app.command("knowledge-produce")
def eval_knowledge_produce(
        manifest: str = "eval/knowledge_reuse.yaml",
        out: str = "eval/frozen/knowledge_reuse-v4.pending.json",
        ledger_path: str = "data/proof/knowledge-production.sqlite3"):
    """用隔离模型会话生成摘要/知识卡材料；产物双审前不能进入实验。"""
    from .eval import knowledge_production
    from .eval.protocol import ensure_new_outputs
    from .llm.client import GO_BASE_URL, GO_MODEL, get_client
    from .llm.ledger import RequestLedger

    ensure_new_outputs(out)
    ledger = RequestLedger(ledger_path, max_requests=4)
    campaign_id = "knowledge-production-" + hashlib.sha256(
        (str(Path(manifest).resolve()) + str(time.time_ns())).encode()
    ).hexdigest()[:16]
    try:
        result = knowledge_production.produce(
            manifest, out=out, project_root=Path.cwd(), ledger=ledger,
            campaign_id=campaign_id,
            client_factory=lambda role, case_id: get_client(
                ledger=ledger, session_id=f"{campaign_id}:{role}:{case_id}",
                base_url=GO_BASE_URL, model=GO_MODEL,
            ),
        )
    except (ValueError, json.JSONDecodeError) as exc:
        console.print(f"[red]模型材料生产失败：{exc}；失败请求已保留在账本中。[/]")
        raise typer.Exit(2) from exc
    if result["status"] == "not_run":
        console.print("[yellow]未配置 LLM_API_KEY；没有写出伪造的模型知识材料。[/]")
        raise typer.Exit(1)
    console.print(f"[green][OK][/] 已生成 {len(result['products'])} 份待双审材料 → {out}")


@eval_app.command("knowledge-material-review")
def eval_knowledge_material_review(
        artifact: str, reviews: str,
        out: str = "eval/frozen/knowledge_reuse-v4.json"):
    """应用两名隔离 reviewer（及必要仲裁）的逐 section 复核结果。"""
    from .eval import knowledge_production
    from .eval.protocol import ensure_new_outputs
    from .publication import atomic_text

    ensure_new_outputs(out)
    produced = json.loads(Path(artifact).read_text(encoding="utf-8"))
    submission = json.loads(Path(reviews).read_text(encoding="utf-8"))
    result = knowledge_production.apply_reviews(produced, submission)
    atomic_text(Path(out), json.dumps(result, ensure_ascii=False, indent=2))
    console.print(f"材料复核：{result['status']} → {out}")
    if result["status"] != "frozen":
        raise typer.Exit(1)


@eval_app.command("maintenance")
def eval_maintenance(
        manifest: str = "eval/maintenance.yaml",
        out: str = "docs/PROOF-MAINTENANCE.json",
        executor_name: str = typer.Option("none", "--executor", help="none / synthetic / upstream"),
        execute_synthetic: bool = typer.Option(
            False, "--execute-synthetic",
            help="在临时目录编译并运行 24 个 CC0 合成契约；不改上游语料。")):
    """校验或显式执行知识维护变异实验。"""
    from .eval import maintenance_eval
    from .eval.protocol import ensure_new_outputs
    from .publication import atomic_text

    markdown = Path(out).with_suffix(".md")
    ensure_new_outputs(out, markdown)
    selected = "synthetic" if execute_synthetic else executor_name
    if selected not in {"none", "synthetic", "upstream"}:
        raise typer.BadParameter("--executor 只能是 none / synthetic / upstream")
    try:
        if selected == "upstream":
            if manifest == "eval/maintenance.yaml":
                manifest = "eval/upstream_maintenance.yaml"
            result = maintenance_eval.evaluate_upstream(manifest, mode="execute")
        else:
            result = maintenance_eval.evaluate(
                manifest,
                mode="execute" if selected == "synthetic" else "validate",
                executor=maintenance_eval.synthetic_executor if selected == "synthetic" else None,
            )
    except maintenance_eval.MaintenanceError as exc:
        console.print(f"[red]维护变异实验无法运行：{exc}[/]")
        raise typer.Exit(2) from exc
    atomic_text(Path(out), json.dumps(result, ensure_ascii=False, indent=2))
    atomic_text(markdown, maintenance_eval.render_upstream(result)
                if result.get("evaluation") == "upstream_maintenance"
                else maintenance_eval.render(result))
    current = (result["summary"].get("adapters") or {}).get("current", {})
    correct = current.get("decision_correct")
    if isinstance(correct, dict):
        correct, denominator = correct.get("count"), correct.get("denominator")
    else:
        denominator = current.get("decision_denominator")
    console.print(
        f"{result['status']}：24 个预登记场景；"
        f"当前策略决策正确 {correct if correct is not None else '未测'}/"
        f"{denominator if denominator is not None else '未测'} → {out}"
    )


def _setup_log(verbose: bool) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")


def _git_revision(repo: str) -> str:
    """Return the checked-out Git commit without making parsing depend on Git."""
    try:
        return subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip() or "unversioned"
    except (OSError, subprocess.CalledProcessError):
        return "unversioned"


def _repository_id(repo: str) -> str:
    """Prefer a sanitized Git remote so public evidence never exposes credentials."""
    try:
        remote = subprocess.run(
            ["git", "-C", repo, "config", "--get", "remote.origin.url"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return repo
    if not remote:
        return repo
    # Covers https://token@host/org/repo and ssh://user@host/org/repo while keeping
    # ordinary https and git@host:org/repo identifiers readable.
    return re.sub(r"(?<=//)[^/@]+@", "", remote)


# --------------------------------------------------------------------- init

@app.command()
def init(db: str = DEFAULT_DB, force: bool = False):
    """建库。"""
    if force:
        dbm.reset(db)
    conn = dbm.connect(db)
    n = conn.execute("SELECT COUNT(*) c FROM node").fetchone()["c"]
    console.print(f"[green][OK][/] 知识库就绪: {db}  (现有节点 {n})")


# -------------------------------------------------------------------- parse

@app.command()
def parse(repo: str, db: str = DEFAULT_DB, compile_db: str | None = None,
          include: list[str] = typer.Option([], "--include", "-I"),
          define: list[str] = typer.Option([], "--define", "-D"),
          limit: int = 0, force: bool = False, incremental: bool = True,
          verbose: bool = False):
    """解析 C 工程 → 节点 / 边（含 certain / candidate 双置信度）。

    默认增量：只重解析内容或编译参数发生变化的翻译单元。
    --force 强制全量重建。
    """
    _setup_log(verbose)
    from .parser import ast_walker, compile_db as cdb, incremental as inc

    database_exists = Path(db).exists()
    full_rebuild = force or not database_exists
    conn = dbm.connect(db)
    if dbm.get_meta(conn, "snapshot_enabled") == "1":
        from . import snapshots
        console.print_json(data=snapshots.build(conn, repo, compile_db or str(Path(repo) / "compile_commands.json")))
        return
    if force and database_exists:
        dbm.reset_code_facts(conn)
    inc.ensure_schema(conn)
    repo_abs = os.path.abspath(repo)
    dbm.set_meta(conn, "repo", repo_abs)
    dbm.set_meta(conn, "repository_id", _repository_id(repo_abs))
    dbm.set_meta(conn, "revision", _git_revision(repo_abs))
    conn.commit()

    units, degraded = cdb.load(repo, compile_db, include, define)
    if limit:
        units = units[:limit]
    build_payload = [
        {
            "source": os.path.relpath(unit.source, repo_abs).replace(os.sep, "/"),
            "args": unit.args,
        }
        for unit in sorted(units, key=lambda item: item.source)
    ]
    build_hash = hashlib.sha256(
        json.dumps(build_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    flag_summary = {
        "translation_units": len(units),
        "defines": sum(1 for unit in units for arg in unit.args if arg.startswith("-D")),
        "include_flags": sum(
            1 for unit in units for arg in unit.args
            if arg == "-I" or arg.startswith(("-I", "-isystem"))
        ),
        "degraded": bool(degraded),
    }
    dbm.set_meta(conn, "build_config_hash", build_hash)
    dbm.set_meta(conn, "build_config_summary", json.dumps(flag_summary, sort_keys=True))
    conn.commit()
    if degraded:
        console.print("[yellow][WARN] 降级模式[/]：无 compile_commands.json，解析质量下降")

    t0 = time.time()
    plan_stats = {}
    if incremental and not full_rebuild:
        todo, removed, plan_stats = inc.plan(conn, units, repo)
        purged = inc.purge_tu(conn, [u.source for u in todo] + removed)
        if plan_stats["reuse"] == plan_stats["total"] and not removed:
            console.print(f"[green][OK][/] 无变更，全部 {plan_stats['total']} 个翻译单元复用 "
                          f"({time.time() - t0:.2f}s)")
            return
        console.print(f"[cyan]增量[/] 复用{plan_stats['reuse']} 变更{plan_stats['changed']} "
                      f"新增{plan_stats['new']} 删除{plan_stats['removed']} "
                      f"撤销旧边{purged}")
        units = todo

    walker = ast_walker.AstWalker(repo)
    walker.build_config_override = dbm.get_meta(conn, "snapshot_config_hash")
    with console.status(f"解析 {len(units)} 个翻译单元…") as st:
        for i, u in enumerate(units, 1):
            diag = walker.walk_unit(u.source, u.args, u.directory)
            # ★ 即使是全量重建也要记录 TU 状态，否则下一次增量无从比对
            #   （之前这里挂在 incremental 上，导致 --force 之后增量永远启动不了）
            if walker.last_tu is not None:
                inc.record(conn, u.source, inc.collect_deps(walker.last_tu, repo),
                           u.args, max(diag, 0))
            if i % 10 == 0 or i == len(units):
                st.update(f"解析中 {i}/{len(units)}  "
                          f"节点{len(walker.result.nodes)} 边{len(walker.result.edges)}")
    ast_walker.persist(conn, walker.result)
    if not full_rebuild:
        inc.gc_orphan_nodes(conn)
        inc.stale_summary_and_chunks(conn)
    # Orphan cleanup must precede this check: otherwise a deleted function could
    # survive in ``node`` for one extra parse and leave an obsolete B card visible.
    from .experience import store as card_store
    stale = card_store.stale_check(conn)
    conn.commit()
    dt = time.time() - t0

    s = walker.result.stats
    dbm.set_meta(conn, "degraded", "1" if degraded else "0")
    dbm.set_meta(conn, "parse_stats", json.dumps({
        "translation_units": len(build_payload),
        "tu_ok": int(s["tu_ok"]),
        "tu_failed": int(s["tu_failed"]),
        "diag_error": int(s["diag_error"]),
    }, sort_keys=True))
    conn.commit()

    cert = conn.execute("SELECT COUNT(*) c FROM edge WHERE confidence='certain'").fetchone()["c"]
    cand = conn.execute("SELECT COUNT(*) c FROM edge WHERE confidence='candidate'").fetchone()["c"]

    t = Table(title=f"解析完成 ({dt:.1f}s)")
    t.add_column("指标"); t.add_column("值", justify="right")
    t.add_row("翻译单元 成功/失败", f"{s['tu_ok']} / {s['tu_failed']}")
    t.add_row("编译诊断错误", str(s["diag_error"]))
    t.add_row("节点总数", str(conn.execute("SELECT COUNT(*) c FROM node").fetchone()["c"]))
    qn = lambda k: conn.execute("SELECT COUNT(*) c FROM node WHERE kind=?", (k,)).fetchone()["c"]
    t.add_row("  函数(定义)", str(conn.execute(
        "SELECT COUNT(*) c FROM node WHERE kind='function' AND is_definition=1").fetchone()["c"]))
    t.add_row("  结构体/枚举/宏", f"{qn('struct')}/{qn('enum')}/{qn('macro')}")
    t.add_row("USR 去重命中", str(s["node_dedup"]))
    t.add_row("[green]确定边 certain[/]", str(cert))
    t.add_row("[yellow]候选边 candidate[/]", str(cand))
    t.add_row("  ├ 函数指针", str(s["call_candidate_fnptr"]))
    t.add_row("  ├ 取地址", str(s["call_candidate_addrtaken"]))
    t.add_row("  └ 未解析", str(s["call_candidate_unresolved"]))
    if stale["stale"]:
        t.add_row("[yellow]过期知识卡[/]", str(len(stale["stale"])))
    console.print(t)
    conn.close()


# ---------------------------------------------------------------- summary

@app.command(name="summary")
def summary_cmd(db: str = DEFAULT_DB, use_llm: bool = False, verbose: bool = False):
    """生成检索摘要头（≤200 token/函数）。"""
    _setup_log(verbose)
    from .summary import head
    conn = dbm.connect(db)
    if dbm.get_meta(conn, "snapshot_enabled") == "1":
        console.print("摘要随 snapshot build 在 staging 中生成；active 未改变。")
        return
    repo = dbm.get_meta(conn, "repo", ".")
    s = head.build(conn, repo, use_llm=use_llm)
    console.print(f"[green][OK][/] 摘要头 {s['total']} 个 "
                  f"(跳过{s['skipped']} 截断{s['truncated']} LLM增强{s['llm']}) "
                  f"最大 token {s['max_tokens']}")


# ------------------------------------------------------------------ index

@app.command(name="index")
def index_cmd(db: str = DEFAULT_DB, embedder: str = "null", data_dir: str = "data",
              verbose: bool = False):
    """构建 BM25 / 向量索引。embedder=null 时退化为纯 BM25+图。"""
    _setup_log(verbose)
    from .indexer import build
    conn = dbm.connect(db)
    if dbm.get_meta(conn, "snapshot_enabled") == "1":
        console.print("索引随 snapshot build 在 staging 中完成；使用 snapshot activate 切换。")
        return
    repo = dbm.get_meta(conn, "repo", ".")
    s = build.build(conn, repo, embedder_kind=embedder, out_dir=data_dir)
    console.print(f"[green][OK][/] chunk {s['chunks']} (可见{s['visible']} "
                  f"[yellow]待审核隐藏{s['hidden_pending']}[/]) "
                  f"向量{s['vectors']} embedder={s['embedder']}")


# -------------------------------------------------------------------- ask

@app.command()
def ask(question: str, db: str = DEFAULT_DB, budget: int = 8000, hops: int = 2,
        embedder: str = "null", data_dir: str = "data",
        no_graph: bool = False, no_vector: bool = False, no_bm25: bool = False,
        show_context: bool = False):
    """提问。无 LLM key 时返回证据包，全流程照样可用。"""
    from .retrieve import engine
    from .indexer.build import get_embedder
    from .llm.client import get_client

    conn = dbm.query(db)
    emb = None if (no_vector or embedder == "null") else get_embedder(embedder)
    r = engine.ask(conn, question, embedder=emb, budget_tokens=budget, hops=hops,
                   data_dir=data_dir, use_graph=not no_graph,
                   use_vector=not no_vector, use_bm25=not no_bm25,
                   llm_client=get_client())

    console.print(f"\n[bold]Q:[/] {question}")
    console.print(f"[dim]模式={r['mode']}  召回 符号{r['recall']['symbol']}/"
                  f"BM25 {r['recall']['bm25']}/向量{r['recall']['vector']}"
                  f"→融合{r['recall']['fused']}  图扩展摘要头{r['recall']['graph_heads']}"
                  f"  上下文{r['context_tokens']}token  A/B级证据{r['ab_evidence']}[/]\n")

    if r["refused"]:
        console.print(f"[yellow]{r['answer']}[/]")
        return
    if r["answer"]:
        console.print(r["answer"])
    else:
        console.print("[dim]（未配置 LLM，输出证据包）[/]")
        if show_context:
            console.print(r["evidence_pack"][:4000])

    t = Table(title="证据引用")
    t.add_column("编号"); t.add_column("级"); t.add_column("标题"); t.add_column("版本与位置")
    for c in r["citations"][:15]:
        p = c.get("provenance") or {}
        location = p.get("path") or c.get("source_ref") or ""
        if p.get("line_start"):
            location += f":{p['line_start']}-{p.get('line_end') or p['line_start']}"
        repo_id = p.get("repository", r["repository"])
        repo_label = repo_id.rstrip("/").rsplit("/", 1)[-1] or repo_id
        revision = p.get("revision", r["revision"])
        snapshot = f"{repo_label}@{revision[:12]}"
        symbol = p.get("symbol") or ""
        t.add_row(c["tag"], c["level"], (c["title"] or "")[:44],
                  f"{snapshot}\n{symbol} {location}"[:100])
    console.print(t)


# ------------------------------------------------------------------ impact

@app.command()
def impact(symbol: str, db: str = DEFAULT_DB, depth: int = 3):
    """变更影响分析。结论只用 certain 边，candidate 单列。"""
    from .graph import traverse
    conn = dbm.query(db)
    cands = traverse.resolve_symbol(conn, symbol)
    if not cands:
        console.print(f"[red]未找到符号 {symbol}[/]"); raise typer.Exit(1)
    if len(cands) > 1:
        console.print(f"[yellow]同名符号 {len(cands)} 个，取定义优先的那个[/]")
    node = cands[0]
    r = traverse.impact(conn, node["id"], max_hop=depth)

    console.print(f"\n[bold]改动 {node['name']}[/] ({node['path']}:{node['line_start']})\n")
    total = 0
    for hop in sorted(r["by_hop"]):
        names = [x["name"] for x in r["by_hop"][hop]]
        total += len(names)
        label = "直接调用者" if hop == 1 else f"{hop} 跳影响"
        console.print(f"[green]{label}[/] ({len(names)}): {', '.join(names[:12])}"
                      + (" …" if len(names) > 12 else ""))
    console.print(f"\n受影响文件 ({len(r['affected_files'])}): "
                  f"{', '.join(r['affected_files'][:8])}")
    console.print(f"受影响模块 ({len(r['affected_modules'])}): "
                  f"{', '.join(r['affected_modules'][:8])}")
    if r["affected_headers"]:
        console.print(f"受影响头文件: {', '.join(r['affected_headers'][:8])}")
    if r["candidate_impact"]:
        console.print(f"\n[yellow][WARN] 潜在影响 (candidate, 需人工确认) "
                      f"({len(r['candidate_impact'])}):[/]")
        for c in r["candidate_impact"][:8]:
            console.print(f"  · {c['name']}  [dim]({c['reason']})[/]")
    if r["regression_hints"]:
        console.print("\n[cyan]回归建议（来自已审核经验）:[/]")
        for h in r["regression_hints"]:
            console.print(f"  · {h['title']}: {h['verification']}")
    console.print(f"\n[dim]确定影响面 {total} 个函数 / {len(r['affected_files'])} 个文件[/]")


# -------------------------------------------------------------------- wiki

@wiki_app.callback(invoke_without_command=True)
def wiki(ctx: typer.Context, db: str = DEFAULT_DB, out: str = "kb/wiki", level: str = "all",
         no_resume: bool = False, use_llm: bool = False, verbose: bool = False):
    """生成分层 Wiki（file→module→repo）。断点续写，输入未变则跳过。"""
    if ctx.invoked_subcommand:
        return
    _setup_log(verbose)
    from .wiki import generator
    from .llm.client import get_client
    conn = dbm.connect(db)
    if dbm.get_meta(conn, "snapshot_enabled") == "1":
        console.print("Wiki 随 snapshot build 在 staging 中生成；active 未改变。")
        return
    repo = dbm.get_meta(conn, "repo", ".")
    levels = ("file", "module", "repo") if level == "all" else (level,)
    s = generator.generate(conn, repo, levels=levels, out_dir=out,
                           llm_client=get_client() if use_llm else None,
                           resume=not no_resume)
    console.print(f"[green][OK][/] Wiki: file {s['file']} / module {s['module']} / "
                  f"repo {s['repo']}  跳过{s['skipped']} "
                  f"过期{s['stale']} [red]失败{s['failed']}[/] LLM改写{s['llm']}  → {out}/")


@wiki_app.command("status")
def wiki_status(db: str = DEFAULT_DB):
    """查看当前固定快照的 Wiki 层级、状态和最近更新时间。"""
    conn = dbm.query(db)
    rows = conn.execute(
        "SELECT level,status,COUNT(*) c FROM wiki_page GROUP BY level,status ORDER BY level,status"
    ).fetchall()
    table = Table(title=(
        f"Wiki {dbm.get_meta(conn, 'repository_id', dbm.get_meta(conn, 'repo', 'unversioned'))}"
        f"@{dbm.get_meta(conn, 'revision', 'unversioned')[:12]}"
    ))
    table.add_column("层级"); table.add_column("状态"); table.add_column("页面", justify="right")
    for row in rows:
        table.add_row(row["level"], row["status"], str(row["c"]))
    console.print(table)


# --------------------------------------------------------------------- exp

@exp_app.command("import")
def exp_import(file: str, db: str = DEFAULT_DB):
    """导入经验（JSONL）。★ 全部落为 pending。"""
    from .experience import store
    conn = dbm.connect(db)
    s = store.import_jsonl(conn, file)
    console.print(f"[green][OK][/] 导入 {s['imported']} 条（待审核）"
                  f" 跳过{s['skipped']} 失败{s['failed']}")


@exp_app.command("pending")
def exp_pending(db: str = DEFAULT_DB):
    """查看待审核队列。"""
    from .experience import store
    conn = dbm.connect(db)
    rows = store.pending(conn)
    t = Table(title=f"待审核 {len(rows)} 条")
    t.add_column("ID"); t.add_column("标题"); t.add_column("现象")
    for r in rows:
        t.add_row(r["id"], r["title"][:30], (r["symptom"] or "")[:50])
    console.print(t)


@exp_app.command("review")
def exp_review(exp_id: str, action: str, note: str = "", db: str = DEFAULT_DB):
    """兼容旧命令；approve 仍必须先通过 card bind 绑定精确 USR。"""
    from .experience import store
    conn = dbm.connect(db)
    r = store.review(conn, exp_id, action, note)
    console.print(f"[green][OK][/] {r['id']} → {r['status']}（检索可见性已同步）")


# --------------------------------------------------------- public sessions/cards

@session_app.command("import")
def session_import(file: str, db: str = DEFAULT_DB):
    """导入公开合成会话；会话只用于审核，永不写入检索索引。"""
    from . import conversation
    conn = dbm.connect(db)
    try:
        result = conversation.import_public_session(conn, file)
    except conversation.ConversationError as exc:
        console.print(f"[red]会话导入失败：{exc}[/]")
        raise typer.Exit(2) from exc
    state = "重复，未新增" if result["duplicate"] else "已导入"
    console.print(f"[green][OK][/] 会话 {result['id']}：{state}")


@session_app.command("show")
def session_show(session_id: str, db: str = DEFAULT_DB):
    """查看审核会话，不触发检索或模型调用。"""
    from . import conversation
    try:
        session = conversation.get_session(dbm.connect(db), session_id)
    except KeyError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    console.print(f"[bold]{session['id']}[/]  {session['repository']}@{session['revision'][:12]}")
    for message in session["messages"]:
        console.print(f"[cyan]{message['ordinal']:02d} {message['role']}[/] {message['content']}")


@session_app.command("assess")
def session_assess(session_id: str, db: str = DEFAULT_DB):
    """执行公开来源、固定版本、复现与敏感信息质量门禁。"""
    from . import conversation
    try:
        result = conversation.assess_session(dbm.connect(db), session_id)
    except KeyError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    state = "可提炼待审核卡" if result["eligible"] else "不符合提炼条件"
    console.print(f"[green][OK][/] {session_id}：{state}")
    for name, passed in result["checks"].items():
        console.print(f"  {'✓' if passed else '✕'} {name}")
    for reason in result["reasons"]:
        console.print(f"  [yellow]{reason}[/]")


@session_app.command("goal")
def session_goal(session_id: str, goal: str, db: str = DEFAULT_DB):
    """设置本次会话要沉淀的知识目标。"""
    from . import conversation
    result = conversation.set_curation_goal(dbm.connect(db), session_id, goal)
    console.print(f"[green][OK][/] {result['session_id']} → {result['curation_goal']}")


@session_app.command("curate")
def session_curate(session_id: str, db: str = DEFAULT_DB, use_llm: bool = False):
    """生成候选列表；候选未确认前不会创建 pending 卡。"""
    from . import conversation
    from .llm.client import get_client
    try:
        rows = conversation.propose_candidates(
            dbm.connect(db), session_id, llm_client=get_client() if use_llm else None,
        )
    except (KeyError, conversation.ConversationError) as exc:
        console.print(f"[red]策划失败：{exc}[/]"); raise typer.Exit(2) from exc
    for row in rows:
        console.print(f"[cyan]{row['id']}[/] {row['payload']['title']} [{row['status']}]")


@session_app.command("decide")
def session_decide(candidate_id: str, action: str, db: str = DEFAULT_DB,
                   payload_file: str | None = None, reason: str = ""):
    """确认、编辑、纠正或跳过一个策划候选。"""
    from . import conversation
    payload = json.loads(Path(payload_file).read_text(encoding="utf-8")) if payload_file else None
    try:
        result = conversation.decide_candidate(
            dbm.connect(db), candidate_id, action, payload=payload, reason=reason,
        )
    except (KeyError, ValueError, conversation.ConversationError) as exc:
        console.print(f"[red]候选处理失败：{exc}[/]"); raise typer.Exit(2) from exc
    console.print(f"[green][OK][/] {candidate_id} → {result['status']}"
                  + (f" / {result['card_id']}" if result.get("card_id") else ""))


@card_app.command("pending")
def card_pending(db: str = DEFAULT_DB):
    """查看 pending/stale 知识卡及其审核原因。"""
    from .experience import store
    rows = store.pending(dbm.connect(db))
    table = Table(title=f"待处理知识卡 {len(rows)} 条")
    table.add_column("ID"); table.add_column("状态"); table.add_column("标题"); table.add_column("原因")
    for row in rows:
        table.add_row(row["id"], row["status"], (row["title"] or "")[:30], (row["stale_reason"] or "")[:45])
    console.print(table)


@card_app.command("bind")
def card_bind(exp_id: str, usr: str, db: str = DEFAULT_DB, reviewer: str = "local-reviewer",
              relation: str = "explains"):
    """把知识卡绑定到唯一的当前函数 USR；绑定后必须重新 approve。"""
    from .experience import store
    try:
        anchor = store.bind(dbm.connect(db), exp_id, usr, bound_by=reviewer,
                            relation=relation)
    except (KeyError, ValueError) as exc:
        console.print(f"[red]绑定失败：{exc}[/]")
        raise typer.Exit(2) from exc
    console.print(f"[green][OK][/] {exp_id} → {anchor['path']}:{anchor['line_start']}  {anchor['usr']}")


@card_app.command("qa-add")
def card_qa_add(exp_id: str, question: str, expected: list[str] = typer.Option(..., "--expected"),
                evidence: list[str] = typer.Option(..., "--evidence"), db: str = DEFAULT_DB):
    """为卡片增加一条待审核 QA。"""
    from .experience import store
    result = store.qa_add(dbm.connect(db), exp_id, question, expected, evidence)
    console.print(f"[green][OK][/] QA {result['id']} → {result['status']}")


@card_app.command("dependencies")
def card_dependencies(exp_id: str,
                      entity: list[str] = typer.Option([], "--entity"),
                      macro: list[str] = typer.Option([], "--macro"),
                      call_depth: int = typer.Option(0, min=0, max=2),
                      db: str = DEFAULT_DB):
    """声明卡片真正消费的函数/类型/全局量/宏；变更后必须重新审核。"""
    from .experience import store
    try:
        result = store.set_dependencies(
            dbm.connect(db), exp_id, entity_names=entity,
            macro_names=macro, call_depth=call_depth,
        )
    except (KeyError, ValueError) as exc:
        console.print(f"[red]依赖设置失败：{exc}[/]")
        raise typer.Exit(2) from exc
    console.print(f"[green][OK][/] {exp_id} → {len(result['dependencies'])} 项依赖，需重新 QA 与确认")


@card_app.command("qa-review")
def card_qa_review(qa_id: int, action: str, note: str, db: str = DEFAULT_DB,
                   reviewer: str = "local-reviewer", reviewer_type: str = "human",
                   evaluation_scope: str = "formal", model: str | None = None,
                   agent_id: str | None = None, prompt_hash: str | None = None,
                   input_hash: str | None = None):
    """审核 QA；action 为 pass 或 fail。"""
    from .experience import store
    if action not in ("pass", "fail"):
        console.print("[red]action 只能是 pass / fail[/]"); raise typer.Exit(2)
    result = store.qa_review(
        dbm.connect(db), qa_id, action == "pass", note, reviewer,
        reviewer_type=reviewer_type, evaluation_scope=evaluation_scope, model=model,
        agent_id=agent_id, prompt_hash=prompt_hash, input_hash=input_hash,
    )
    console.print(f"[green][OK][/] QA {qa_id} → {result['status']}")


@card_app.command("review")
def card_review(exp_id: str, action: str, note: str, db: str = DEFAULT_DB,
                reviewer: str = "local-reviewer", reviewer_type: str = "human",
                evaluation_scope: str = "formal", model: str | None = None,
                agent_id: str | None = None, prompt_hash: str | None = None,
                input_hash: str | None = None):
    """审核知识卡。approve 需要 active 精确锚点和审核说明。"""
    from .experience import store
    try:
        result = store.review(
            dbm.connect(db), exp_id, action, note, reviewer,
            reviewer_type=reviewer_type, evaluation_scope=evaluation_scope, model=model,
            agent_id=agent_id, prompt_hash=prompt_hash, input_hash=input_hash,
        )
    except (KeyError, ValueError) as exc:
        console.print(f"[red]审核失败：{exc}[/]")
        raise typer.Exit(2) from exc
    console.print(f"[green][OK][/] {result['id']} → {result['status']}")


@card_app.command("confirm")
def card_confirm(exp_id: str, db: str = DEFAULT_DB,
                 reviewer: str = "local-reviewer",
                 bundle_hash: str | None = None):
    """由 human 明确确认当前知识卡材料版本；内容变化后必须重新确认。"""
    from .experience import store
    c = dbm.connect(db)
    current = store.review_bundle(c, exp_id)
    expected = bundle_hash or current
    try:
        result = store.confirm_review(c, exp_id, expected, reviewer, "human")
    except (KeyError, ValueError) as exc:
        console.print(f"[red]确认失败：{exc}[/]")
        raise typer.Exit(2) from exc
    console.print(f"[green][OK][/] {exp_id} 当前材料已由 {result['reviewer']} 确认：{result['bundle_hash']}")


@card_app.command("stale-check")
def card_stale_check(db: str = DEFAULT_DB):
    """检查 approved 卡的 USR/定义哈希；失配卡立即隐藏。"""
    from .experience import store
    result = store.stale_check(dbm.connect(db))
    console.print(f"[green][OK][/] 已检查 {result['checked']} 张 approved 卡；过期 {len(result['stale'])} 张")
    for item in result["stale"]:
        console.print(f"  [yellow]{item['id']}[/] {item['reason']}")


@card_app.command("supersede")
def card_supersede(replacement_id: str, prior_id: str, db: str = DEFAULT_DB):
    """声明待审核卡替代旧卡；仅在新卡批准后正式生效。"""
    from .experience import store
    try:
        result = store.link_supersedes(dbm.connect(db), replacement_id, prior_id)
    except (KeyError, ValueError) as exc:
        console.print(f"[red]替代关系失败：{exc}[/]")
        raise typer.Exit(2) from exc
    console.print(f"[green][OK][/] {result['id']} 待审核替代 {result['supersedes_id']}")


@card_app.command("export")
def card_export(exp_id: str, out: str = "examples/cards", db: str = DEFAULT_DB):
    """导出已批准卡为可阅读 Markdown；不导出原始会话。"""
    from .experience import store
    try:
        path = store.export_markdown(dbm.connect(db), exp_id, out)
    except (KeyError, ValueError) as exc:
        console.print(f"[red]导出失败：{exc}[/]")
        raise typer.Exit(2) from exc
    console.print(f"[green][OK][/] {path}")


@card_app.command("check")
def card_check(db: str = DEFAULT_DB):
    """校验 Markdown 主源与 SQLite 投影一致性。"""
    from .experience import store
    result = store.consistency(dbm.connect(db))
    console.print(f"[{'green' if result['ok'] else 'red'}]{'OK' if result['ok'] else 'FAIL'}[/] "
                  f"checked={result['checked']} errors={len(result['errors'])}")
    if not result["ok"]:
        for error in result["errors"]:
            console.print(f"  [red]{error}[/]")
        raise typer.Exit(2)


@card_app.command("rebuild")
def card_rebuild(db: str = DEFAULT_DB, no_index: bool = False):
    """删除卡片派生投影并从 Markdown 完整重建。"""
    from .experience import store
    result = store.rebuild(dbm.connect(db), rebuild_index=not no_index)
    console.print(f"[green][OK][/] 从 Markdown 重建 {result['cards']} 张卡，"
                  f"一致性={result['ok']} 索引={result['index_rebuilt']}")


@card_app.command("release-export")
def card_release_export(exp_id: str, db: str = DEFAULT_DB,
                        project_root: str = ".", out: str = "knowledge"):
    """导出已提交的人类审核卡为可跟踪、可重建的正式发布包。"""
    from .experience.release import CardReleaseError, export_release
    try:
        result = export_release(dbm.connect(db), exp_id, project_root=project_root,
                                release_dir=out)
    except (KeyError, OSError, ValueError, CardReleaseError) as exc:
        console.print(f"[red]正式发布包导出失败：{exc}[/]")
        raise typer.Exit(2) from exc
    console.print_json(data=result)


@card_app.command("release-import")
def card_release_import(release: str, db: str = DEFAULT_DB,
                        project_root: str = ".",
                        rebuild_index: bool = typer.Option(
                            False, "--rebuild-index",
                            help="仅供非快照数据库；快照库应重新 build/activate。")):
    """将受版本控制的正式卡发布包投影到当前固定语料数据库。"""
    from .experience.release import CardReleaseError, import_release
    try:
        result = import_release(dbm.connect(db), release, project_root=project_root,
                                rebuild_index=rebuild_index)
    except (KeyError, OSError, ValueError, CardReleaseError) as exc:
        console.print(f"[red]正式发布包导入失败：{exc}[/]")
        raise typer.Exit(2) from exc
    console.print_json(data=result)


@agent_app.command("run")
def agent_run(question: str, db: str = DEFAULT_DB, data_dir: str = "data",
              session_id: str | None = None, save_draft: bool = False,
              mode: str = "auto", with_llm: bool = False, read_mode: str = "progressive"):
    """运行最多六次、按 discover/understand/verify 分阶段的只读 Agent。"""
    from . import agent
    from .indexer.build import get_embedder
    from .llm.client import get_client
    requested_mode = "model" if with_llm else mode
    llm_client = get_client() if requested_mode != "rule" else None
    try:
        result = agent.run(dbm.connect(db), question, data_dir=data_dir,
                           embedder=get_embedder("tfidf", data_dir), session_id=session_id,
                           llm_client=llm_client, save_draft=save_draft, mode=requested_mode, read_mode=read_mode)
    except (agent.AgentError, KeyError, ValueError) as exc:
        console.print(f"[red]Agent 运行失败：{exc}[/]")
        raise typer.Exit(2) from exc
    table = Table(title=f"Agent {result['id']} ({result['execution_mode']})")
    table.add_column("步骤"); table.add_column("阶段"); table.add_column("工具"); table.add_column("结果")
    for index, event in enumerate(result["events"], 1):
        table.add_row(str(index), event["phase"], event["tool"],
                      json.dumps(event["result"], ensure_ascii=False)[:120])
    console.print(table)
    if result["fallback_reason"]:
        console.print(f"[yellow][WARN][/] 已明确降级：{result['fallback_reason']}")
    if result["answer"]:
        console.print(result["answer"])
    console.print("[yellow]拒答[/]" if result["evidence"]["refused"] else "[green]证据包已生成[/]")
    if result["draft_preview"]:
        console.print(f"知识卡草稿：{result['draft_id'] or '预览（使用 --save-draft 持久化）'}")


# -------------------------------------------------------------------- eval

@eval_app.callback(invoke_without_command=True)
def eval_cmd(ctx: typer.Context, db: str = DEFAULT_DB, questions: str = "eval/questions.yaml",
             data_dir: str = "data", embedder: str = "tfidf",
             ablation: bool = True, out: str = "docs/EVAL.md", gen: int = 0):
    """跑评测集 + 消融实验，输出 Markdown 表格。"""
    if ctx.invoked_subcommand:
        return
    from .eval import run as evalrun
    conn = dbm.query(db)
    if dbm.get_meta(conn, "knowledge_set_id"):
        data_dir = str(Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent)
        embedder = "local"
    if gen:
        n = evalrun.autogen(conn, questions, n_per_type=gen)
        console.print(f"[green][OK][/] 自动生成 {n} 题 → {questions}")
    evalrun.main(conn, questions, data_dir=data_dir, embedder_kind=embedder,
                 ablation=ablation, out=out, db_path=db, console=console)


@eval_app.command("model")
def eval_model_cmd(db: str = DEFAULT_DB, tasks: str = "eval/tasks_cjson.yaml",
                   data_dir: str = "data", embedder: str = "tfidf",
                   out: str = "docs/MODEL-ABC-CJSON.md",
                   json_out: str | None = None, seed: int = 17, answer_key: str | None = None,
                   abc: bool = typer.Option(
                       True, "--abc/--protocol-only",
                       help="默认跑 A/B/C 对照；protocol-only 只验证 CodeAtlas Agent 协议。"),
                   runs: int = typer.Option(3, min=1,
                                            help="A/B/C 每题重复次数；必须为正奇数。"),
                   limit: int = typer.Option(
                       0, min=0,
                       help="0 跑完整题集；正数按任务类型均衡抽取代表题做接口冒烟。"),
                   value_smoke: bool = typer.Option(
                       False, "--value-smoke",
                       help="每库固定选 2 道代码、1 道经验、1 道拒答；与 --limit 互斥。"),
                   require_approved: bool = typer.Option(
                       True, "--require-approved/--allow-draft",
                       help="默认只运行已人工审核的固定任务。")):
    """可选真实模型协议评测；只从环境变量读取 OpenAI-compatible 配置。"""
    from .eval import model_eval
    from .indexer.build import get_embedder
    from .llm.client import get_client

    client = get_client()
    if client is None:
        console.print("[yellow]未配置模型：请设置 LLM_API_KEY、LLM_BASE_URL 和 LLM_MODEL。[/]")
        raise typer.Exit(2)
    try:
        runner = model_eval.main_abc if abc else model_eval.main
        kwargs = dict(
            client=client, data_dir=data_dir, embedder=get_embedder(embedder, data_dir),
            out=out, json_out=json_out, require_approved=require_approved,
        )
        if abc:
            kwargs["runs"] = runs
            kwargs["limit"] = limit or None
            kwargs["seed"] = seed
            kwargs["answer_key_path"] = answer_key
            kwargs["smoke_profile"] = "value" if value_smoke else None
        result = runner(dbm.connect(db), tasks, **kwargs)
    except ValueError as exc:
        console.print(f"[red]模型评测无法运行：{exc}[/]")
        raise typer.Exit(2) from exc
    if abc:
        console.print(f"试次收集完成；答案状态：{result['answer_review_status']} → {result['report']}")
    else:
        metrics = result["metrics"]
        console.print(
            f"[green][OK][/] 模型协议评测：原生规划 {metrics['native_planning_success_rate']:.1f}% / "
            f"安全门禁 {metrics['final_safety_gate_rate']:.1f}% → {result['report']}"
        )


@app.command(name="task-eval")
def task_eval_cmd(db: str = DEFAULT_DB, tasks: str = "eval/tasks_cjson.yaml",
                  data_dir: str = "data", embedder: str = "tfidf",
                  out: str = "docs/TASK-EVAL-CJSON.md",
                  require_approved: bool = typer.Option(
                      False, "--require-approved",
                      help="仅接受已审核任务；用于生成可公开引用的报告。"),
                  ):
    """跑轻量开发任务评测（默认无 LLM，draft 任务只生成待确认预览）。"""
    from .eval import task_eval
    conn = dbm.connect(db)
    try:
        task_eval.main(conn, tasks, data_dir=data_dir, embedder_kind=embedder,
                       out=out, db_path=db, require_approved=require_approved,
                       console=console)
    except task_eval.TaskEvalError as exc:
        console.print(f"[red]任务评测无法运行：{exc}[/]")
        raise typer.Exit(2) from exc


@app.command(name="workflow-eval")
def workflow_eval_cmd(
        db: str = DEFAULT_DB, work_dir: str = "data/workflow-eval",
        data_dir: str = "data", manifest: str = "eval/workflow_cjson.yaml",
        success_session: str = "examples/conversations/cjson-nesting-review.json",
        insufficient_session: str = "examples/conversations/cjson-insufficient-review.json",
        unrelated_session: str = "examples/conversations/cjson-unrelated-request.json",
        out: str = "docs/WORKFLOW-EVAL-CJSON.md"):
    """跑 6 个固定 Agent/知识卡闭环场景；只写隔离副本和报告。"""
    from .eval import workflow_eval
    try:
        result = workflow_eval.main(
            db, work_dir=work_dir, data_dir=data_dir, manifest_path=manifest,
            success_session=success_session, insufficient_session=insufficient_session,
            unrelated_session=unrelated_session, out=out,
        )
    except workflow_eval.WorkflowEvalError as exc:
        console.print(f"[red]闭环评测无法运行：{exc}[/]")
        raise typer.Exit(2) from exc
    m = result["metrics"]
    console.print("[green][OK][/] 闭环评测通过："
                  f"工具合法率 {m['tool_trace_legal_rate']:.1f}% / "
                  f"卡片门禁 {m['card_gate_correct_rate']:.1f}% / "
                  f"拒答 {m['refusal_accuracy']:.1f}% → {result['report']}")


@app.command(name="acceptance-eval")
def acceptance_eval_cmd(
        corpora: str = "eval/corpora.yaml",
        claims: str = "eval/public_verification.yaml",
        experiments: str = "eval/experiments.yaml",
        mode: str = typer.Option("fast", help="fast / full / release"),
        out: str = "docs/ACCEPTANCE-CJSON-LWIP.md",
        use_existing: bool = typer.Option(
            False, "--use-existing",
            help="不重跑流水线，只校验现有数据库与报告；release 模式始终如此。"),
        ):
    """统一运行并校验 cJSON 正确性基准、lwIP 规模基准和公开声明。"""
    from .eval import acceptance_eval
    execute = not use_existing and mode != "release"
    if mode == "release" and out == "docs/ACCEPTANCE-CJSON-LWIP.md":
        # Preserve the full snapshot that release verification is checking.
        out = "docs/RELEASE-VERIFICATION.md"
    if mode == "release" and not use_existing:
        console.print("[cyan]release[/] 只验证已提交报告，不重新生成非确定性运行时间数据。")
    try:
        result = acceptance_eval.main(
            corpora_path=corpora, claims_path=claims, mode=mode, out=out,
            experiments_path=experiments, execute=execute, console=console,
        )
    except acceptance_eval.AcceptanceEvalError as exc:
        console.print(f"[red]双语料验收无法运行：{exc}[/]")
        raise typer.Exit(2) from exc
    if not result["passed"]:
        raise typer.Exit(1)


# -------------------------------------------------------------- portfolio demo

@app.command(name="portfolio-demo")
def portfolio_demo_cmd(db: str = DEFAULT_DB, work_dir: str = "data/portfolio-demo",
                       experience: str = "examples/experience/public-cjson-review-example.jsonl",
                       embedder: str = "tfidf", with_llm: bool = False,
                       workflow: bool = typer.Option(False, "--workflow",
                                                      help="额外演示 Agent→知识卡→stale 闭环")):
    """运行面试用 cJSON 演示；所有写入都隔离在 work_dir。"""
    from . import portfolio_demo
    from .llm.client import get_client

    llm_client = get_client() if with_llm else None
    if with_llm and llm_client is None:
        console.print("[yellow][WARN][/] 未配置 API Key，继续输出无 LLM 证据包。")
    try:
        result = portfolio_demo.run(
            db, work_dir=work_dir, experience_file=experience,
            embedder_kind=embedder, llm_client=llm_client,
            conversation_file=("examples/conversations/cjson-nesting-review.json" if workflow else None),
        )
    except portfolio_demo.PortfolioDemoError as exc:
        console.print(f"[red]求职演示无法运行：{exc}[/]")
        raise typer.Exit(2) from exc

    p = result["evidence"]["provenance"]
    t = Table(title="CodeAtlas 求职演示（隔离数据库）")
    t.add_column("环节"); t.add_column("结果")
    t.add_row("A 级定位", f"{p['symbol']} → {p['path']}:L{p['line_start']}\n{p['repository']}@{p['revision'][:12]}")
    impact = result["impact"]
    t.add_row("certain 影响", f"parse_value 2 跳：{sum(len(v) for v in impact['by_hop'].values())} 个函数；"
              f"candidate 单列 {len(impact['candidate_impact'])}（图谱总计 {result['candidate_total']}）")
    t.add_row("经验审核", "pending 不可检索 → 模拟人工 approve 后成为 B 级证据")
    t.add_row("无证据拒答", f"refused={result['refusal']['refused']}；A/B={result['refusal']['ab_evidence']}")
    t.add_row("LLM 表达层", result["llm_mode"])
    if result.get("workflow"):
        wf = result["workflow"]
        t.add_row("Agent 闭环", f"{wf['tool_calls']} 次只读工具调用；"
                  f"审核后 B 可见={wf['b_visible_after_review']}；stale 后隐藏={wf['b_hidden_after_stale']}")
    console.print(t)
    out = Path(work_dir) / "PORTFOLIO-DEMO.md"
    out.write_text(portfolio_demo.render_markdown(result), encoding="utf-8")
    console.print(f"[green][OK][/] 演示完成（不改源库）→ {out}")


@card_app.command("experiment")
def card_experiment(exp_id: str, paths: list[str] = typer.Option(..., "--path"), db: str = DEFAULT_DB):
    from .experience.store import attach_experiments
    console.print_json(data=attach_experiments(dbm.connect(db), exp_id, paths))


# ------------------------------------------------------------------- stats

@app.command()
def stats(db: str = DEFAULT_DB, as_json: bool = False):
    """全量统计 —— 简历数字的来源。"""
    conn = dbm.query(db)
    q = lambda s: conn.execute(s).fetchone()[0]
    d = {
        "repo": dbm.get_meta(conn, "repo"),
        "nodes": q("SELECT COUNT(*) FROM node"),
        "functions": q("SELECT COUNT(*) FROM node WHERE kind='function' AND is_definition=1"),
        "structs": q("SELECT COUNT(*) FROM node WHERE kind='struct'"),
        "macros": q("SELECT COUNT(*) FROM node WHERE kind='macro'"),
        "files": q("SELECT COUNT(*) FROM node WHERE kind='file'"),
        "edges": q("SELECT COUNT(*) FROM edge"),
        "edges_certain": q("SELECT COUNT(*) FROM edge WHERE confidence='certain'"),
        "edges_candidate": q("SELECT COUNT(*) FROM edge WHERE confidence='candidate'"),
        "calls_certain": q("SELECT COUNT(*) FROM edge WHERE kind='calls' AND confidence='certain'"),
        "type_uses": q("SELECT COUNT(*) FROM edge WHERE kind='type_use' AND confidence='certain'"),
        "field_accesses": q("SELECT COUNT(*) FROM edge WHERE kind='field_access' AND confidence='certain'"),
        "global_refs": q("SELECT COUNT(*) FROM edge WHERE kind='global_ref' AND confidence='certain'"),
        "summary_heads": q("SELECT COUNT(*) FROM summary_head"),
        "head_max_token": q("SELECT COALESCE(MAX(token_len),0) FROM summary_head"),
        "head_avg_token": round(q("SELECT COALESCE(AVG(token_len),0) FROM summary_head"), 1),
        "chunks": q("SELECT COUNT(*) FROM chunk"),
        "chunks_visible": q("SELECT COUNT(*) FROM chunk WHERE visible=1"),
        "exp_pending": q("SELECT COUNT(*) FROM experience WHERE status='pending'"),
        "exp_approved": q("SELECT COUNT(*) FROM experience WHERE status='approved'"),
        "wiki_pages": q("SELECT COUNT(*) FROM wiki_page"),
        "wiki_ok": q("SELECT COUNT(*) FROM wiki_page WHERE status='ok'"),
    }
    if as_json:
        print(json.dumps(d, ensure_ascii=False, indent=2)); return
    t = Table(title="知识库统计")
    t.add_column("指标"); t.add_column("值", justify="right")
    for k, v in d.items():
        t.add_row(k, str(v))
    console.print(t)


@app.command()
def serve(db: str = DEFAULT_DB, data_dir: str = "data", embedder: str = "tfidf",
          host: str = "127.0.0.1", port: int = 8000):
    """启动 Web 服务（问答 / 影响分析 / 审核）。默认只绑本机。"""
    import uvicorn
    os.environ["CODEATLAS_DB"] = db
    os.environ["CODEATLAS_DATA"] = data_dir
    os.environ["CODEATLAS_EMBEDDER"] = embedder
    console.print(f"[green]→[/] http://{host}:{port}")
    uvicorn.run("codeatlas.server.app:app", host=host, port=port, log_level="warning")


def main():
    app()


if __name__ == "__main__":
    main()
