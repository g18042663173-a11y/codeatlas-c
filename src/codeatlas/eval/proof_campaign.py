"""Resumable collection only: frozen schedule, isolated trials, no auto-review.

The default smoke profile uses separate development questions and is excluded
from formal effect estimates. Interrupted started trials remain uncertain and
are never silently retried.
"""
from __future__ import annotations

import copy
import fcntl
import json
import os
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from ..contracts import digest, estimated_tokens
from ..llm.client import GO_BASE_URL, GO_MODEL, get_client
from ..llm.ledger import RequestLedger
from ..publication import atomic_text
from . import protocol, rubric


class CampaignError(ValueError):
    pass


@contextmanager
def _snapshot(spec):
    from .. import db
    control = db.read_only(spec["database"])
    try:
        if db.get_meta(control, "snapshot_enabled") == "1":
            from ..snapshots import pin
            with pin(control) as context:
                yield context.conn, str(context.directory)
        else:
            yield control, spec.get("data_dir", "data")
    finally:
        control.close()


def _job(spec, root, runs, seed, model):
    from . import knowledge_reuse_eval as reuse, reading_eval, model_eval, task_eval
    spec = copy.deepcopy(spec)
    for key in ("manifest", "answer_key", "database", "data_dir", "generated_artifact",
                "task_override"):
        if spec.get(key):
            spec[key] = str((root / spec[key]).resolve())
    if spec["kind"] == "knowledge_reuse":
        template = reuse.evaluate(spec["manifest"], project_root=root, runs=runs, seed=seed,
                                  answer_key_path=spec.get("answer_key"),
                                  generated_artifact=spec.get("generated_artifact"))
        tasks = template["materials"]["tasks"]
        arms = reuse.ARMS
        if spec.get("task_override"):
            override = yaml.safe_load(Path(spec["task_override"]).read_text(encoding="utf-8"))
            if (not isinstance(override, dict)
                    or override.get("split") != "development_smoke"
                    or override.get("not_effect_evidence") is not True
                    or not isinstance(override.get("tasks"), list)):
                raise CampaignError("knowledge smoke tasks must be explicit non-effect development material")
            known_cases = {case["case_id"] for case in template["materials"]["cases"]}
            tasks = [{"id": row["id"], "case_id": row["case_id"],
                      "question": row["question"], "type": "开发冒烟"}
                     for row in override["tasks"]]
            if (len(tasks) != 2 or len({row["id"] for row in tasks}) != 2
                    or {row["case_id"] for row in tasks} != known_cases
                    or any(not row["question"].strip() for row in tasks)):
                raise CampaignError("knowledge smoke requires one unique question per public case")
            template.update(task_scope="development_smoke", question_count=2,
                            task_count=2, answer_review_status="not_run",
                            answer_key={"status": "not_provided", "split": "development_smoke",
                                        "effect_eligible": False, "tasks": {}})
    else:
        with _snapshot(spec) as (conn, directory):
            manifest = task_eval.load_task_set(conn, spec["manifest"], require_approved=True)
            if spec["kind"] == "wiki_knowledge":
                template = reading_eval.evaluate(conn, spec["manifest"], runs=runs, seed=seed,
                    data_dir=directory, answer_key_path=spec.get("answer_key"))
                if template.get("reason") != "model_not_configured":
                    raise CampaignError("reading preparation requires an available Wiki")
                tasks = [t for t in manifest["tasks"] if t["type"] != task_eval.EXPERIENCE_TYPE]
                arms = reading_eval.ARMS
                template["variants"] = {arm: {"name": label[0]} for arm, label in arms.items()}
                template["protocol"].update(embedding="local-hash-384", wiki_in_search_results=False,
                    experience_in_any_arm=False, wiki_flat_meaning="free tool reading, not automatic full-page injection")
            elif spec["kind"] == "abc":
                tasks, arms = manifest["tasks"], model_eval.ABC_VARIANTS
                template = {"report_kind": "abc", "task_scope": "full", "task_set": spec["manifest"],
                    "repository": manifest["repository"], "revision": manifest["revision"],
                    "identity": rubric.identity(conn, spec["manifest"]),
                    "answer_key": protocol.answer_key(spec.get("answer_key"), spec["manifest"], tasks),
                    "variants": {arm: {"name": name} for arm, name in arms.items()},
                    "protocol": {"version": protocol.VERSION, "seed": seed, "max_tool_calls": 6,
                        "max_code_read_lines": 300, "estimated_input_token_limit": 8000,
                        "output_token_limit": 1200, "experience_information_parity": False}}
            else:
                raise CampaignError("unsupported model experiment kind")
    if spec.get("task_ids"):
        selected = set(spec["task_ids"])
        tasks = [task for task in tasks if task["id"] in selected]
        if len(tasks) != len(selected):
            raise CampaignError("development smoke task id is missing from its manifest")
        template.update(task_scope="development_smoke", task_count=len(tasks),
                        answer_review_status="not_run",
                        answer_key={"status": "not_provided", "split": "development_smoke",
                                    "effect_eligible": False, "tasks": {}})
    order = protocol.schedule(tasks, arms, runs, seed)
    template.update(model=model, temperature=.1, runs_per_task=runs, task_count=len(tasks),
                    proof_scope=spec.get("scope", "formal_proof"),
                    task_manifest=copy.deepcopy(tasks), status="not_run", executed_trial_count=0,
                    raw_trials={arm: [{**copy.deepcopy(task), "repetitions": []} for task in tasks]
                                for arm in arms})
    template["protocol"]["schedule"] = order
    template["protocol"]["variants"] = list(arms)
    template["evaluation_hash"] = protocol.evaluation_hash(template)
    return {**spec, "tasks": tasks, "template": template, "schedule": order}


def default_jobs(root, *, include_abc=False):
    corpora = yaml.safe_load((root / "eval/corpora.yaml").read_text())["corpora"]
    by_id = {row["id"]: row for row in corpora}
    experiments = yaml.safe_load((root / "eval/experiments.yaml").read_text())["experiments"]
    jobs = []
    for row in experiments:
        if row["kind"] == "maintenance":
            continue  # Not a model run; never rewrite its independently collected report.
        spec = {key: row[key] for key in (
            "id", "kind", "manifest", "answer_key", "generated_artifact"
        ) if key in row}
        spec["scope"] = "formal_proof"
        if row["kind"] == "wiki_knowledge":
            spec.update({key: by_id[row["corpus"]][key] for key in ("database", "data_dir")})
        jobs.append(spec)
    if include_abc:
        jobs.extend({"id": "abc-" + c["id"], "kind": "abc", "manifest": c["task_manifest"],
                     "database": c["database"], "data_dir": c["data_dir"]} for c in corpora)
    return jobs


def development_smoke_jobs(root):
    """Eight frozen development tasks; never a prefix of the held-out proof set."""
    corpora = yaml.safe_load((root / "eval/corpora.yaml").read_text())["corpora"]
    by_id = {row["id"]: row for row in corpora}
    common = dict(scope="development_smoke")
    wiki = [
        {**common, "id": "smoke-wiki-cjson", "kind": "wiki_knowledge",
         "manifest": "eval/tasks_cjson.yaml",
         "task_ids": ["semantic-parse-tree", "semantic-serialize", "refuse-kubernetes-tls"],
         **{key: by_id["cjson"][key] for key in ("database", "data_dir")}},
        {**common, "id": "smoke-wiki-lwip", "kind": "wiki_knowledge",
         "manifest": "eval/tasks_lwip.yaml",
         "task_ids": ["semantic-pbuf-release", "semantic-netif-state", "refuse-postgresql-index"],
         **{key: by_id["lwip"][key] for key in ("database", "data_dir")}},
    ]
    experiments = yaml.safe_load((root / "eval/experiments.yaml").read_text())["experiments"]
    reuse = next(row for row in experiments if row["kind"] == "knowledge_reuse")
    return [*wiki, {**common, "id": "smoke-knowledge-reuse", "kind": "knowledge_reuse",
        "manifest": reuse["manifest"], "generated_artifact": reuse.get("generated_artifact"),
        "task_override": "eval/proof_smoke.yaml"}]


def _runtime_hash():
    root = Path(__file__).resolve().parents[1]
    return digest({str(p.relative_to(root)): digest(p.read_bytes()) for p in sorted(root.rglob("*.py"))})


def prepare(directory, *, project_root=None, jobs=None, include_abc=False, runs=3, seed=17,
            smoke_trials=24, max_requests=None, max_cost_usd=None,
            input_usd_per_million=None, output_usd_per_million=None, base_url=None, model=None,
            thinking_mode="disabled", workers=1, transport_retries=0):
    root = Path(project_root or Path.cwd()).resolve()
    target = Path(directory).resolve()
    if target.exists():
        raise CampaignError("campaign directory already exists; use resume/report or a new directory")
    if type(smoke_trials) is not int or smoke_trials < 1 or smoke_trials % 3:
        raise CampaignError("smoke_trials must be a positive multiple of three (complete paired arms)")
    endpoint = base_url or os.environ.get("LLM_BASE_URL", GO_BASE_URL)
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CampaignError("campaign endpoint must be credential-free HTTP(S)")
    selected_model = model or os.environ.get("LLM_MODEL", GO_MODEL)
    if thinking_mode not in {"enabled", "disabled"}:
        raise CampaignError("proof campaign thinking_mode must be enabled or disabled")
    if type(workers) is not int or not 1 <= workers <= 32:
        raise CampaignError("campaign workers must be an integer from 1 to 32")
    if type(transport_retries) is not int or not 0 <= transport_retries <= 3:
        raise CampaignError("transport_retries must be an integer from 0 to 3")
    def configured(value, name, cast, default=None):
        raw = os.environ.get(name)
        return value if value is not None else cast(raw) if raw else default
    max_requests = configured(max_requests, "LLM_MAX_REQUESTS", int, 5000)
    max_cost_usd = configured(max_cost_usd, "LLM_MAX_COST_USD", float)
    input_usd_per_million = configured(input_usd_per_million, "LLM_INPUT_USD_PER_MILLION", float)
    output_usd_per_million = configured(output_usd_per_million, "LLM_OUTPUT_USD_PER_MILLION", float)
    default_campaign = jobs is None
    specs = jobs if jobs is not None else [
        *development_smoke_jobs(root), *default_jobs(root, include_abc=include_abc)
    ]
    ids = [spec["id"] for spec in specs]
    if not ids or len(ids) != len(set(ids)):
        raise CampaignError("campaign jobs require unique ids")
    prepared = [_job(spec, root, 1 if spec.get("scope") == "development_smoke" else runs,
                     seed, selected_model) for spec in specs]
    def interleave(selected):
        rows = []
        if not selected:
            return rows
        # Interleave complete three-arm blocks across repositories/experiments.
        for offset in range(0, max(len(job["schedule"]) for job in selected), 3):
            for job in selected:
                for number in range(offset, min(offset + 3, len(job["schedule"]))):
                    event = job["schedule"][number]
                    rows.append({"job_id": job["id"], "local_sequence": number, **event})
        return rows
    smoke_jobs = [job for job in prepared if job.get("scope") == "development_smoke"]
    formal_jobs = [job for job in prepared if job.get("scope") != "development_smoke"]
    if default_campaign:
        smoke_count = sum(len(job["schedule"]) for job in smoke_jobs)
        if smoke_count != 24 or smoke_trials != 24:
            raise CampaignError("the default proof campaign has a fixed 24-trial development smoke profile")
    schedule = interleave(smoke_jobs) + interleave(formal_jobs)
    for sequence, event in enumerate(schedule):
        event.update(sequence=sequence, trial_id=digest(event))
    inputs = {job[key]: digest(Path(job[key]).read_bytes()) for job in prepared
              for key in ("manifest", "answer_key", "generated_artifact", "task_override")
              if job.get(key)}
    formal_trial_count = sum(len(job["schedule"]) for job in formal_jobs)
    development_smoke_trial_count = sum(len(job["schedule"]) for job in smoke_jobs)
    # Offline ceiling: every answer may consume all six allowed model attempts;
    # formal answers then receive two reviews, a worst-case arbitration, and
    # each of the three judge roles runs the 12-item calibration once.
    request_upper_bound = {
        "answer_trials": len(schedule),
        "answer_requests": len(schedule) * 6 * (transport_retries + 1),
        "first_pass_review_requests": formal_trial_count * 2 * (transport_retries + 1),
        "arbitration_requests": formal_trial_count * (transport_retries + 1),
        "judge_calibration_requests": 36 * (transport_retries + 1),
    }
    if default_campaign:
        request_upper_bound["protocol_probe_requests"] = transport_retries + 1
    request_upper_bound["campaign_total"] = sum(
        value for key, value in request_upper_bound.items() if key != "answer_trials"
    )
    plan = {"schema_version": 1, "campaign_id": "campaign-" + uuid.uuid4().hex,
            "artifact_scope": "isolated_experiment", "project_root": str(root), "seed": seed,
            "runs_per_task": runs, "smoke_trials": min(smoke_trials, len(schedule)),
            "formal_trial_count": formal_trial_count,
            "development_smoke_trial_count": development_smoke_trial_count,
            "base_url": endpoint.rstrip("/"), "model": selected_model,
            "thinking_mode": thinking_mode, "workers": workers,
            "transport_retries": transport_retries,
            "runtime_hash": _runtime_hash(),
            "default_campaign": default_campaign,
            "input_hashes": inputs, "jobs": prepared, "schedule": schedule,
            "budget": dict(max_requests=max_requests, max_cost_usd=max_cost_usd,
                input_usd_per_million=input_usd_per_million, output_usd_per_million=output_usd_per_million),
            "request_upper_bound": request_upper_bound,
            "boundary": "Collection only; no human approval or effect claim. The 24-trial smoke uses separate development tasks, never held-out proof answers; failures and uncertain trials are retained."}
    plan["plan_hash"] = digest(plan)
    target.mkdir(parents=True, exist_ok=False)
    ledger = RequestLedger(target / "ledger.sqlite3", **plan["budget"])
    with ledger.connect() as conn:
        conn.executescript("""
            CREATE TABLE campaign (id INTEGER PRIMARY KEY, plan_hash TEXT NOT NULL, last_mode TEXT);
            CREATE TABLE protocol_probe (
                id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL,
                result_json TEXT, result_hash TEXT, finished_at REAL
            );
            CREATE TABLE trials (id TEXT PRIMARY KEY, sequence INTEGER UNIQUE NOT NULL,
                state TEXT NOT NULL, result_json TEXT, result_hash TEXT, started_at REAL, finished_at REAL);
        """)
        conn.execute("INSERT INTO campaign VALUES(1,?,NULL)", (plan["plan_hash"],))
        conn.execute("INSERT INTO protocol_probe(id,state) VALUES(1,'pending')")
        conn.executemany("INSERT INTO trials(id,sequence,state) VALUES(?,?,'pending')",
                         [(event["trial_id"], event["sequence"]) for event in schedule])
    atomic_text(target / "plan.json", json.dumps(plan, ensure_ascii=False, indent=2))
    return {"status": "prepared", "campaign_id": plan["campaign_id"], "plan_hash": plan["plan_hash"],
            "planned_trials": len(schedule), "smoke_trials": plan["smoke_trials"], "directory": str(target)}


def preflight(directory, *, client_factory=None):
    """Verify the frozen provider contract without exposing held-out tasks."""
    target, plan, ledger = _load(directory)
    if not plan.get("default_campaign"):
        return {"status": "not_required", "passed": True}
    _check_inputs(plan)
    with (target / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CampaignError("campaign is already running") from exc
        with ledger.connect() as conn:
            row = conn.execute("SELECT * FROM protocol_probe WHERE id=1").fetchone()
            if row["state"] != "pending":
                result = json.loads(row["result_json"] or "{}")
                if row["result_hash"] and digest(result) != row["result_hash"]:
                    raise CampaignError("protocol probe content changed")
                return result
            conn.execute("UPDATE protocol_probe SET state='running' WHERE id=1 AND state='pending'")
        factory = client_factory or get_client
        session_id = plan["campaign_id"] + "-protocol-probe"
        client = factory(ledger=ledger, session_id=session_id, base_url=plan["base_url"],
                         model=plan["model"],
                         thinking_mode=plan.get("thinking_mode", "disabled"),
                         transport_retries=plan.get("transport_retries", 0))
        if client is None:
            with ledger.connect() as conn:
                conn.execute("UPDATE protocol_probe SET state='pending' WHERE id=1")
            return {"status": "not_run", "passed": False, "reason": "model_not_configured"}
        client.trial_id = "protocol-probe"
        try:
            raw = client._chat(
                "Return exactly one JSON object and no prose.",
                'Return {"status":"ok"}.', max_tokens=80, json_object=True,
            )
            parsed = client._json(raw)
            if parsed != {"status": "ok"}:
                raise ValueError("protocol probe returned an unexpected object")
            result = {
                "status": "completed", "passed": True,
                "model": client.model, "base_url": client.base_url,
                "thinking_mode": client.thinking_mode,
                "response_format": "json_object",
                "response_meta": client.last_response_meta,
                "usage": ledger.summary(trial_id="protocol-probe"),
            }
            state = "completed"
        except Exception as exc:
            result = {
                "status": "failed", "passed": False,
                "reason": getattr(exc, "category", type(exc).__name__),
                "model": client.model, "base_url": client.base_url,
                "thinking_mode": client.thinking_mode,
                "usage": ledger.summary(trial_id="protocol-probe"),
            }
            if isinstance(getattr(exc, "http_status", None), int):
                result["http_status"] = exc.http_status
            state = "failed"
        finally:
            client.trial_id = None
        with ledger.connect() as conn:
            conn.execute(
                "UPDATE protocol_probe SET state=?,result_json=?,result_hash=?,finished_at=? WHERE id=1",
                (state, json.dumps(result, ensure_ascii=False), digest(result), time.time()),
            )
        return result


def _load(directory):
    target = Path(directory).resolve()
    plan = json.loads((target / "plan.json").read_text())
    if plan.get("plan_hash") != digest({k: v for k, v in plan.items() if k != "plan_hash"}):
        raise CampaignError("campaign plan hash changed")
    if not (target / "ledger.sqlite3").is_file():
        raise CampaignError("campaign ledger is missing; it must not be recreated or reset")
    ledger = RequestLedger(target / "ledger.sqlite3", **plan["budget"])
    with ledger.connect() as conn:
        if conn.execute("SELECT plan_hash FROM campaign WHERE id=1").fetchone()[0] != plan["plan_hash"]:
            raise CampaignError("ledger belongs to another plan")
    return target, plan, ledger


def _check_inputs(plan):
    if _runtime_hash() != plan["runtime_hash"]:
        raise CampaignError("campaign runtime changed; completed trials retained, but resume is refused")
    if any(not Path(path).is_file() or digest(Path(path).read_bytes()) != expected
           for path, expected in plan["input_hashes"].items()):
        raise CampaignError("campaign input changed")
    for job in plan["jobs"]:
        if job["kind"] == "knowledge_reuse":
            from .knowledge_reuse_eval import prepare as materials
            current = materials(
                job["manifest"], project_root=plan["project_root"],
                generated_artifact=job.get("generated_artifact"),
            )
            if current["materials_hash"] != job["template"]["identity"]["materials_hash"]:
                raise CampaignError("shared materials changed")
        else:
            with _snapshot(job) as (conn, _):
                if rubric.identity(conn, job["manifest"]) != job["template"]["identity"]:
                    raise CampaignError("campaign snapshot changed")


def execute_trial(job, event, client):
    """Reuse the existing evidence isolation, tool runner and citation projection."""
    from .. import agent
    from . import model_eval, reading_eval, knowledge_reuse_eval as reuse
    task = next(t for t in job["tasks"] if t["id"] == event["task_id"])
    arm = event["variant"]
    if job["kind"] == "knowledge_reuse":
        case = next(c for c in job["template"]["materials"]["cases"] if c["case_id"] == task["case_id"])
        system, user = reuse.messages_for(case, task, arm)
        answer = reuse._answer(client, system, user)
        citations, valid = reuse._references(answer, case)
        return {"answer": answer, "citations": citations, "citation_valid": valid,
                "case_id": case["case_id"], "source_digest": case["source_digest"],
                "attachments_digest": case["attachments_digest"],
                "material_hash": case["materials"][arm]["content_hash"],
                "prompt_hash": digest([system, user]), "estimated_input_tokens": estimated_tokens(system + user) + 32,
                "artifact_scope": reuse.SCOPE, "events": [], "tool_calls": 0,
                "native_planning": False, "fallback_reason": None, "error_type": None}
    with _snapshot(job) as (conn, data_dir):
        if rubric.identity(conn, job["manifest"]) != job["template"]["identity"]:
            raise CampaignError("snapshot changed before trial")
        isolated = reading_eval._copy(conn)
        try:
            if job["kind"] == "wiki_knowledge":
                with tempfile.TemporaryDirectory(prefix="codeatlas-campaign-reading-") as directory:
                    embedder, projection = reading_eval.code_projection(isolated, directory)
                    if arm == "source_only":
                        isolated.execute("UPDATE wiki_page SET status='stale'")
                        isolated.commit()
                    once = model_eval.agent_trial(agent.run(isolated, task["question"], llm_client=client,
                        mode="model", read_mode=reading_eval.ARMS[arm][1], embedder=embedder, data_dir=directory))
                    once["retrieval_projection_hash"] = projection
            elif arm in ("A", "B"):
                once = model_eval._baseline_once(isolated, task, client=client, fixed_skill=arm == "B")
            else:
                from ..indexer.build import get_embedder
                once = model_eval.agent_trial(agent.run(isolated, task["question"], llm_client=client,
                    mode="model", data_dir=data_dir, embedder=get_embedder("local", data_dir)))
        finally:
            isolated.close()
    return {**once, **model_eval._abc_grade(task, once, variant=arm)}


def _smoke_gate(plan, rows) -> dict:
    """Development-only go/no-go gate; never contributes to effect estimates."""
    if not plan.get("default_campaign"):
        return {"required": False, "passed": True}
    from .task_eval import REFUSAL_TYPE
    jobs = {job["id"]: job for job in plan["jobs"]}
    selected = []
    for row, event in zip(rows[:plan["smoke_trials"]], plan["schedule"], strict=False):
        result = json.loads(row["result_json"]) if row["result_json"] else None
        job = jobs[event["job_id"]]
        task = next(item for item in job["tasks"] if item["id"] == event["task_id"])
        selected.append((row["state"], job, task, result))
    complete = len(selected) == plan["smoke_trials"] and all(state == "completed" and result
                                                              for state, _, _, result in selected)
    code = [result for state, job, task, result in selected
            if state == "completed" and result and job["kind"] == "wiki_knowledge"
            and task["type"] != REFUSAL_TYPE]
    refusals = [result for state, job, task, result in selected
                if state == "completed" and result and job["kind"] == "wiki_knowledge"
                and task["type"] == REFUSAL_TYPE]
    non_refusal = [result for state, job, task, result in selected
                   if state == "completed" and result and not (
                       job["kind"] == "wiki_knowledge" and task["type"] == REFUSAL_TYPE)]
    native = sum(bool(item.get("native_planning")) for item in code)
    citation = sum(bool(item.get("citation_valid", item.get("final_safe"))) for item in non_refusal)
    refusal_ok = sum(bool(item.get("refused") and item.get("final_safe")) for item in refusals)
    unstable = [item for _, _, _, item in selected if item and (
        item.get("error_type") or any(token in str(item.get("fallback_reason") or "")
            for token in ("OutputTruncated", "JSONDecodeError", "TimeoutError", "SSLError"))
    )]
    gates = {
        "all_24_completed": complete,
        "native_code_planning": len(code) == 12 and native >= 11,
        "non_refusal_citations": len(non_refusal) == 18 and citation == 18,
        "refusal_accuracy": len(refusals) == 6 and refusal_ok == 6,
        "no_truncation_or_transport_error": not unstable,
    }
    return {
        "required": True, "passed": all(gates.values()), "gates": gates,
        "counts": {"completed": sum(state == "completed" for state, _, _, _ in selected),
                   "native_code": native, "code_trials": len(code),
                   "valid_non_refusal_citations": citation,
                   "non_refusal_trials": len(non_refusal),
                   "correct_refusals": refusal_ok, "refusal_trials": len(refusals),
                   "unstable_trials": len(unstable)},
        "boundary": "Development smoke only; this gate permits collection but is not effect evidence.",
    }


def run(directory, *, mode="smoke", resume=False, client_factory=None, executor=None):
    if mode not in {"smoke", "full"}:
        raise CampaignError("mode must be smoke or full")
    target, plan, ledger = _load(directory)
    with (target / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CampaignError("campaign is already running") from exc
        _check_inputs(plan)
        if plan.get("default_campaign"):
            with ledger.connect() as conn:
                probe_row = conn.execute("SELECT * FROM protocol_probe WHERE id=1").fetchone()
            if probe_row["state"] != "completed":
                return {**report(directory), "status": "preflight_required",
                        "reason": "run eval proof preflight before answer trials"}
        factory = client_factory or get_client
        probe = factory(ledger=ledger, session_id=plan["campaign_id"] + "-configuration-check",
                        base_url=plan["base_url"], model=plan["model"],
                        thinking_mode=plan.get("thinking_mode", "disabled"),
                        transport_retries=plan.get("transport_retries", 0))
        if probe is None:
            return {**report(directory), "status": "not_run", "reason": "model_not_configured"}
        if probe.model != plan["model"] or probe.base_url != plan["base_url"]:
            raise CampaignError("client differs from the frozen campaign model/endpoint")
        with ledger.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT last_mode FROM campaign WHERE id=1").fetchone()[0]
            if previous and not resume:
                raise CampaignError("campaign already started; use resume (completed trials are immutable)")
            if previous == "full" and mode == "smoke":
                raise CampaignError("cannot shrink a full campaign to its smoke subset")
            conn.execute("UPDATE campaign SET last_mode=? WHERE id=1", (mode,))
            # A process died after claiming these trials. Never hide/retry them.
            conn.execute("UPDATE trials SET state='uncertain',finished_at=? WHERE state='running'", (time.time(),))
        jobs = {job["id"]: job for job in plan["jobs"]}

        def execute_event(event):
            with ledger.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                changed = conn.execute("UPDATE trials SET state='running',started_at=? WHERE id=? AND state='pending'",
                                       (time.time(), event["trial_id"]))
                if not changed.rowcount:
                    return None
            # Each answer trial has a separate upstream session. Iterative tool
            # planning inside that answer retains the same session, while no
            # provider-side conversation identity is shared with another trial.
            session_id = f"{plan['campaign_id']}-answer-{event['trial_id']}"
            client = factory(ledger=ledger, session_id=session_id,
                             base_url=plan["base_url"], model=plan["model"],
                             thinking_mode=plan.get("thinking_mode", "disabled"),
                             transport_retries=plan.get("transport_retries", 0))
            if client is None:
                raise CampaignError("model configuration disappeared after campaign start")
            if client.model != plan["model"] or client.base_url != plan["base_url"]:
                raise CampaignError("trial client differs from the frozen campaign model/endpoint")
            # Enforce the frozen campaign limits, independent of process-local
            # client resets; the shared ledger is the authoritative ceiling.
            client.ledger, client.session_id = ledger, session_id
            client.max_requests = client.max_cost_usd = None
            client.input_usd_per_million = plan["budget"]["input_usd_per_million"]
            client.output_usd_per_million = plan["budget"]["output_usd_per_million"]
            client.trial_id = event["trial_id"]
            before, started = protocol.usage_start(client), time.perf_counter()
            try:
                once = (executor or execute_trial)(jobs[event["job_id"]], event, client)
            except Exception as exc:
                once = protocol.error_trial(exc)
            once.update(protocol.usage_delta(client, before))
            measured = ledger.summary(trial_id=event["trial_id"])
            once.update(cost_usd=measured["measured_cost_usd"],
                        cost_upper_bound_usd=measured["reserved_cost_usd"])
            once.update(sequence=event["local_sequence"], repetition=event["repetition"],
                        campaign_sequence=event["sequence"], campaign_trial_id=event["trial_id"],
                        latency_ms=round((time.perf_counter() - started) * 1000, 1),
                        persistent_usage=measured)
            with ledger.connect() as conn:
                conn.execute("UPDATE trials SET state=?,result_json=?,result_hash=?,finished_at=? WHERE id=? AND state='running'",
                    ("budget_blocked" if protocol.budget_exhausted(once) else "completed",
                     json.dumps(once, ensure_ascii=False), digest(once), time.time(), event["trial_id"]))
            return once

        def execute_phase(events):
            # Worker count is frozen into plan.json.  Completion order may vary,
            # but the predeclared schedule and per-trial identity never do.
            with ThreadPoolExecutor(max_workers=plan.get("workers", 1),
                                    thread_name_prefix="codeatlas-proof") as pool:
                return list(pool.map(execute_event, events))

        smoke = plan["schedule"][:plan["smoke_trials"]]
        execute_phase(smoke)
        if mode == "full":
            with ledger.connect() as conn:
                smoke_rows = conn.execute("SELECT * FROM trials ORDER BY sequence").fetchall()
            if _smoke_gate(plan, smoke_rows)["passed"]:
                execute_phase(plan["schedule"][plan["smoke_trials"]:])
    return report(directory)


def report(directory):
    _, plan, ledger = _load(directory)
    with ledger.connect() as conn:
        rows = conn.execute("SELECT * FROM trials ORDER BY sequence").fetchall()
        mode = conn.execute("SELECT last_mode FROM campaign WHERE id=1").fetchone()[0]
        probe_row = conn.execute("SELECT * FROM protocol_probe WHERE id=1").fetchone()
    if not plan.get("default_campaign"):
        preflight_result = {"status": "not_required", "passed": True}
    elif probe_row["result_json"]:
        preflight_result = json.loads(probe_row["result_json"])
        if (probe_row["result_hash"]
                and digest(preflight_result) != probe_row["result_hash"]):
            raise CampaignError("protocol probe content changed")
    else:
        preflight_result = {"status": probe_row["state"], "passed": False}
    reports = {job["id"]: copy.deepcopy(job["template"]) for job in plan["jobs"]}
    states = {}
    for row, event in zip(rows, plan["schedule"], strict=True):
        if row["id"] != event["trial_id"]:
            raise CampaignError("ledger trial order differs from plan")
        states[row["state"]] = states.get(row["state"], 0) + 1
        if row["result_json"] is None:
            continue
        result = json.loads(row["result_json"])
        if digest(result) != row["result_hash"]:
            raise CampaignError("completed trial content changed")
        template = reports[event["job_id"]]
        target = next(t for t in template["raw_trials"][event["variant"]] if t["id"] == event["task_id"])
        target["repetitions"].append(result)
    for value in reports.values():
        count = sum(len(row["repetitions"]) for rows_ in value["raw_trials"].values() for row in rows_)
        complete = count == len(value["protocol"]["schedule"])
        value.update(status="completed" if complete else "not_run" if not count else "partial_campaign",
                     executed_trial_count=count, answer_review_status="pending_review" if count else "not_run")
        value["acceptance"] = {"passed": False, "effect_observed": False, "human_release_eligible": False,
                               "execution_complete": complete}
        if count and value.get("proof_scope") != "development_smoke":
            value["blind_review"] = rubric.blind_packet(value)
            if complete:
                rubric.finalize(value)
        elif count:
            value["development_smoke_only"] = True
            value["boundary"] = (
                "Development smoke verifies request/tool plumbing only; it is not reviewed "
                "or included in held-out effect claims."
            )
    smoke_gate = _smoke_gate(plan, rows)
    status = ("preflight_failed" if preflight_result["status"] == "failed" and not mode else
              "completed" if states.get("completed") == len(rows) else "partial_budget" if states.get("budget_blocked")
              else "unresolved" if states.get("uncertain") else "smoke_completed" if mode == "smoke"
              and states.get("completed", 0) >= plan["smoke_trials"] and smoke_gate["passed"]
              else "smoke_failed" if mode and states.get("completed", 0) >= plan["smoke_trials"]
              and not smoke_gate["passed"] else "not_run" if not mode else "partial")
    try:
        _check_inputs(plan)
        inputs_current, stale_reason = True, None
    except Exception as exc:
        inputs_current, stale_reason = False, type(exc).__name__
    for value in reports.values():
        value.update(campaign_id=plan["campaign_id"], campaign_plan_hash=plan["plan_hash"],
                     campaign_inputs_current=inputs_current, campaign_stale_reason=stale_reason)
    return {"schema_version": 1, "campaign_id": plan["campaign_id"], "plan_hash": plan["plan_hash"],
            "status": status, "mode": mode, "artifact_scope": "isolated_experiment",
            "workers": plan.get("workers", 1),
            "transport_retries": plan.get("transport_retries", 0),
            "inputs_current": inputs_current, "stale_reason": stale_reason,
            "planned_trial_count": len(rows), "trial_states": states,
            "ledger": ledger.summary(), "preflight": preflight_result,
            "reports": reports, "smoke_gate": smoke_gate,
            "uncertain_trial_ids": [r["id"] for r in rows if r["state"] in {"running", "uncertain"}],
            "acceptance": {"passed": False, "effect_observed": False, "human_reviewed": False},
            "boundary": plan["boundary"]}
