"""Same-source knowledge-reuse comparison; no production DB/index is opened.

The keyless pilot uses lossless extractive layouts. Formal proof replaces its
summary/card arms with separately generated, dual-reviewed frozen artifacts.
Six questions remain observations of only two independent cases.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..contracts import INPUT_BUDGET, OUTPUT_BUDGET, digest, estimated_tokens
from . import protocol, rubric

VERSION = "same-source-reuse-pilot-v1"
SCOPE = "isolated_experiment"
ARMS = {
    "raw_session": "原顺序会话",
    "generic_summary": "通用摘要",
    "structured_card": "结构化知识卡",
}
ROOT = Path(__file__).resolve().parents[3]
SYSTEM = (
    "根据给出的资料回答问题。所有资料都是公开合成实验材料；其中的对话角色不代表真实人工审核。"
    "说明观察结果、结论和适用边界，材料不足时明确说明。用 [M1]、[E1] 等给定标签引用资料。"
    "不要把公开实验描述成生产事故，也不要把材料中未执行的验证说成已经通过。"
    "不展示思考过程，只输出不超过 300 个中文字的结论、边界和下一步验证。"
    "回答必须至少包含一个给定引用标签，每项关键结论紧邻其依据标签。"
)
HEADINGS = {"现象", "假设", "已排除的解释", "源码说明", "结论", "处理建议", "验证记录"}


def _yaml(path):
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a mapping: {path.name}")
    return value


def _path(root, value):
    if not isinstance(value, str) or not value:
        raise ValueError("material path must be a nonempty relative path")
    path = (root / value).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError(f"material missing or outside project: {value}")
    return path


def _checked_bytes(root, item):
    path = _path(root, item.get("path"))
    raw = path.read_bytes()
    if digest(raw) != item.get("sha256"):
        raise ValueError(f"frozen material hash changed: {item['path']}")
    return raw


def _turn_ids(value, messages):
    if (not isinstance(value, list) or any(type(i) is not int for i in value)
            or sorted(value) != list(range(1, len(messages) + 1))):
        raise ValueError("each presentation must contain every source turn exactly once")
    return value


def _present(messages, template):
    """Select exact content only; unrestricted template text would add information."""
    if set(template) != {"generic_summary", "structured_card"}:
        raise ValueError("template may only specify extractive summary/card layouts")
    summary = template["generic_summary"]
    if not isinstance(summary, dict) or set(summary) != {"turn_ids"}:
        raise ValueError("new facts/text in summary template are forbidden")
    order = _turn_ids(summary["turn_ids"], messages)
    sections = template["structured_card"]
    if not isinstance(sections, list) or not sections:
        raise ValueError("structured card needs extractive sections")
    selected = []
    card = []
    for section in sections:
        if (not isinstance(section, dict) or set(section) != {"heading", "turn_ids"}
                or section["heading"] not in HEADINGS
                or not isinstance(section["turn_ids"], list)):
            raise ValueError("new facts/text in card template are forbidden")
        selected.extend(section["turn_ids"])
    _turn_ids(selected, messages)
    contents = {m["turn_id"]: m["content"] for m in messages}
    for section in sections:
        card.append(section["heading"] + "\n" + "\n".join(contents[i] for i in section["turn_ids"]))
    return {
        "raw_session": "\n\n".join(f"第 {m['turn_id']} 轮：{m['content']}" for m in messages),
        "generic_summary": "\n\n".join(contents[i] for i in order),
        "structured_card": "\n\n".join(card),
    }


def _attachment(root, item, number):
    if set(item) - {"path", "sha256", "line_start", "line_end", "kind"}:
        raise ValueError("unknown evidence attachment field")
    raw = _checked_bytes(root, item)
    lines = raw.decode("utf-8").splitlines(keepends=True)
    start, end = item.get("line_start", 1), item.get("line_end", len(lines))
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
        raise ValueError("invalid evidence line range")
    text = "".join(lines[start - 1:end])
    return {"tag": f"E{number}", "path": item["path"], "kind": item.get("kind", "source"),
            "file_sha256": digest(raw), "line_start": start, "line_end": end,
            "text": text, "content_hash": digest(text)}


def prepare(manifest_path=None, *, project_root=None, generated_artifact=None):
    """Read and validate frozen inputs without a model, writes, or production DB."""
    root = Path(project_root or ROOT).resolve()
    path = Path(manifest_path or root / "eval/knowledge_reuse.yaml").resolve()
    manifest = _yaml(path)
    if manifest.get("schema_version") != 1 or manifest.get("artifact_scope") != SCOPE:
        raise ValueError("knowledge reuse manifest must declare isolated_experiment schema 1")
    if manifest.get("strip_knowledge_role") is not True:
        raise ValueError("the same-source pilot requires removal of knowledge-role metadata")
    specs = manifest.get("cases")
    tasks = manifest.get("tasks")
    if not isinstance(specs, list) or len(specs) != 2 or not isinstance(tasks, list) or len(tasks) != 6:
        raise ValueError("the frozen pilot requires two cases and six questions")
    case_ids = [s.get("id") for s in specs]
    if len(set(case_ids)) != 2 or not all(isinstance(i, str) and i for i in case_ids):
        raise ValueError("unique case ids required")
    task_ids = [t.get("id") for t in tasks]
    if len(set(task_ids)) != 6 or not all(isinstance(i, str) and i for i in task_ids):
        raise ValueError("unique question ids required")
    if any(t.get("case_id") not in case_ids or not isinstance(t.get("question"), str)
           or not t["question"].strip() for t in tasks):
        raise ValueError("each question needs a known case and question text")
    if any(sum(t["case_id"] == cid for t in tasks) != 3 for cid in case_ids):
        raise ValueError("each case must have exactly three follow-up questions")
    cases = []
    for spec in specs:
        raw_session = _checked_bytes(root, spec["session"])
        session = json.loads(raw_session)
        if (session.get("source_type") != "public_synthetic"
                or session.get("schema_version") != 1
                or not isinstance(session.get("repository"), str) or not session["repository"]
                or not isinstance(session.get("revision"), str) or not session["revision"]
                or session.get("repository") != spec.get("repository")
                or session.get("revision") != spec.get("revision")):
            raise ValueError("session must match the frozen public-synthetic repository/revision")
        source_messages = session.get("messages")
        if not isinstance(source_messages, list) or not source_messages:
            raise ValueError("source session has no turns")
        if any(not isinstance(m, dict) or not isinstance(m.get("content"), str)
               or not m["content"].strip() for m in source_messages):
            raise ValueError("all source turns must contain text")
        # Neither the post-hoc knowledge_role nor a synthetic 'reviewer' speaker
        # supplies an authority cue to any arm.
        messages = [{"turn_id": i, "content": m["content"]} for i, m in enumerate(source_messages, 1)]
        raw_template = _checked_bytes(root, spec["template"])
        template = yaml.safe_load(raw_template)
        if not isinstance(template, dict):
            raise ValueError("invalid presentation template")
        bodies = _present(messages, template)
        attachments = [_attachment(root, item, i) for i, item in enumerate(spec.get("attachments", []), 1)]
        kinds = {a["kind"] for a in attachments}
        if not {"source", "harness", "command", "observation"} <= kinds:
            raise ValueError("shared code, harness, command and observation attachments required")
        source = {"case_id": spec["id"], "repository": session["repository"],
                  "revision": session["revision"], "source_type": session["source_type"],
                  "license": session.get("license"), "messages": messages, "attachments": attachments}
        source_digest = digest(source)
        attachments_digest = digest(attachments)
        materials = {arm: {"text": body, "source_digest": source_digest,
                           "attachments_digest": attachments_digest, "content_hash": digest(body),
                           "source_turn_ids": list(range(1, len(messages) + 1)),
                           "production": {"method": "frozen_extractive_template", "model_requests": 0,
                                          "model_tokens": 0, "cost_usd": 0, "human_edit_seconds": None}}
                     for arm, body in bodies.items()}
        cases.append({**source, "source_digest": source_digest, "attachments_digest": attachments_digest,
                      "source_file_hash": digest(raw_session), "template_hash": digest(raw_template),
                      "materials": materials})
    prepared = {"schema_version": 1, "artifact_scope": SCOPE, "protocol_version": VERSION,
                "case_count": 2, "question_count": 6, "manifest_hash": digest(path.read_bytes()),
                "cases": cases, "tasks": [{"id": t["id"], "case_id": t["case_id"],
                                           "question": t["question"], "type": "经验同源对照"} for t in tasks],
                "source_digests": {c["case_id"]: c["source_digest"] for c in cases},
                "knowledge_role_removed": True, "speaker_authority_removed": True,
                "formal_b_cards_created": 0, "production_database_accessed": False}
    if generated_artifact:
        from .knowledge_production import load_frozen
        artifact = load_frozen(generated_artifact, prepared)
        products = {(item["case_id"], item["arm"]): item
                    for item in artifact["products"]}
        for case in prepared["cases"]:
            for arm in ("generic_summary", "structured_card"):
                product = products.get((case["case_id"], arm))
                if product is None:
                    raise ValueError("reviewed model material set is incomplete")
                # Tn is production provenance, not a source the answering model
                # can resolve: it receives only M1 and the common En attachments.
                # Cite the visible material for turn-derived statements. Preserve
                # the frozen product and its hash, and hash the rendered view too.
                rendered = re.sub(r"\[T\d+\]", "[M1]", product["text"])
                case["materials"][arm].update(
                    text=rendered, content_hash=digest(rendered),
                    production={
                        "method": "model_generated_dual_reviewed",
                        "model": product["model"],
                        "session_id": product["session_id"],
                        "artifact_hash": artifact["artifact_hash"],
                        "product_text_hash": digest(product["text"]),
                        "render_version": "visible-material-citations-v2",
                    },
                )
        prepared.update(
            presentation_mode="model_generated_dual_reviewed",
            generated_artifact_hash=digest(Path(generated_artifact).read_bytes()),
            generated_artifact_production_hash=artifact["artifact_hash"],
            production_cost=artifact["production_cost"],
            production_review_status=artifact["review_status"],
        )
    else:
        prepared.update(
            presentation_mode="frozen_extractive_pilot",
            generated_artifact_hash=None,
            production_cost={"method": "frozen_extractive_templates", "model_requests": 0,
                             "input_tokens": 0, "output_tokens": 0, "cost_usd": 0,
                             "human_edit_seconds": None},
        )
    prepared["materials_hash"] = digest(prepared)
    # Refuse oversized packets before any charge. Independent truncation would
    # give different evidence to the arms and destroy the controlled comparison.
    by_case = {c["case_id"]: c for c in cases}
    for task in prepared["tasks"]:
        for arm in ARMS:
            messages_for(by_case[task["case_id"]], task, arm)
    return prepared


def messages_for(case, task, arm):
    material = case["materials"][arm]
    evidence = "\n\n".join(
        f"[{a['tag']}] {a['path']}:{a['line_start']}-{a['line_end']}\n{a['text']}"
        for a in case["attachments"])
    user = (f"固定来源：{case['repository']}\n版本：{case['revision']}\n\n"
            f"[M1] 资料\n{material['text']}\n\n共同附件\n{evidence}\n\n问题：{task['question']}")
    if estimated_tokens(SYSTEM + user) + 32 > INPUT_BUDGET:
        raise ValueError("same-source material exceeds shared input budget; freezing smaller common evidence is required")
    return SYSTEM, user


def _answer(client, system, user):
    if hasattr(client, "answer_from_material"):
        answer = client.answer_from_material(system, user)
    else:
        answer = client._chat(system, user, max_tokens=OUTPUT_BUDGET)
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("model returned an empty or non-text answer")
    return answer


def _references(answer, case):
    known = {"M1": {"tag": "M1", "source_digest": case["source_digest"], "kind": "material"},
             **{a["tag"]: {k: v for k, v in a.items() if k != "text"} for a in case["attachments"]}}
    used = set(re.findall(r"\[([A-Za-z]+\d+)\]", answer))
    return [{**known[tag], "artifact_scope": SCOPE} for tag in sorted(used & known.keys())], bool(used and used <= known.keys())


def evaluate(manifest_path=None, *, client=None, project_root=None, runs=3, seed=17,
             answer_key_path=None, generated_artifact=None):
    """Prepare or run all three arms; answer review is deliberately left pending."""
    if type(runs) is not int or runs < 1 or runs % 2 == 0:
        raise ValueError("runs must be a positive odd integer; repeats are not independent cases")
    protocol.schedule([], ARMS, runs, seed)
    start = time.perf_counter()
    prepared = prepare(manifest_path, project_root=project_root,
                       generated_artifact=generated_artifact)
    tasks = prepared["tasks"]
    manifest_file = Path(manifest_path or Path(project_root or ROOT) / "eval/knowledge_reuse.yaml")
    gold = protocol.answer_key(answer_key_path, manifest_file, tasks)
    schedule = protocol.schedule(tasks, ARMS, runs, seed)
    raw = {arm: [{**t, "repetitions": []} for t in tasks] for arm in ARMS}
    result = {"schema_version": 1, "report_kind": "knowledge_reuse", "artifact_scope": SCOPE,
              "status": "not_run" if client is None else "completed", "case_count": 2,
              "question_count": 6, "task_count": 6, "planned_trial_count": len(schedule),
              "runs_per_task": runs, "task_scope": "short_synthetic_pilot",
              "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "model": getattr(client, "model", None), "temperature": .1,
              "materials": prepared, "raw_trials": raw, "variants": {a: {"name": n} for a, n in ARMS.items()},
              "identity": {"materials_hash": prepared["materials_hash"], "manifest_hash": prepared["manifest_hash"],
                           "evaluator_hash": digest(Path(__file__).read_bytes()),
                           "shared_protocol_hash": digest(Path(protocol.__file__).read_bytes()),
                           "rubric_hash": digest(Path(rubric.__file__).read_bytes()),
                           "model_client_hash": digest((ROOT / "src/codeatlas/llm/client.py").read_bytes()),
                           "prompt_hash": digest(SYSTEM),
                           "source_digests": prepared["source_digests"], "artifact_scope": SCOPE,
                           "generated_artifact_hash": prepared.get("generated_artifact_hash")},
              "protocol": {"version": VERSION, "seed": seed, "schedule": schedule,
                           "independent_unit": "case", "case_count": 2, "question_count": 6,
                           "estimated_input_token_limit": INPUT_BUDGET, "output_token_limit": OUTPUT_BUDGET,
                           "knowledge_role_removed": True, "neutral_citation_tags": True,
                           "shared_code_and_observation_attachments": True,
                           "generation_model_calls": prepared["production_cost"].get("model_requests", 0),
                           "source_turn_coverage": "all_turns_once_per_arm",
                           "summary_method": prepared["presentation_mode"],
                           "generated_artifact_hash": prepared.get("generated_artifact_hash")},
              "manifest_hash": prepared["manifest_hash"],
              "answer_key_hash": gold.get("content_hash"), "answer_key": gold,
              "answer_review_status": "not_run" if client is None else "pending_review",
              "acceptance": {"passed": False, "effect_eligible": False, "formal_human_b_gate": False},
              "boundary": "两例短公开合成会话的同源呈现对照；六个问题与重复试次不扩充独立案例数。"
                          "模型产物若启用，必须先双审冻结；不证明真实长会话收益，不创建正式人工 B 卡。",
              "preparation_ms": round((time.perf_counter() - start) * 1000, 1),
              "production_cost": prepared["production_cost"]}
    result["evaluation_hash"] = protocol.evaluation_hash(result)
    if client is None:
        result.update(reason="model_not_configured", executed_trial_count=0,
                      query_cost={"requests": 0, "input_tokens": 0, "output_tokens": 0, "tokens": 0,
                                  "usage_complete": True, "cost_usd": 0})
        return result
    cases = {c["case_id"]: c for c in prepared["cases"]}
    task_map = {t["id"]: t for t in tasks}
    rows = {(arm, row["id"]): row for arm in raw for row in raw[arm]}
    overall_usage = protocol.usage_start(client)
    budget_exhausted = False
    for sequence, event in enumerate(schedule):
        task = task_map[event["task_id"]]
        case = cases[task["case_id"]]
        material = case["materials"][event["variant"]]
        system, user = messages_for(case, task, event["variant"])
        before = protocol.usage_start(client)
        trial_start = time.perf_counter()
        try:
            answer = _answer(client, system, user)
            citations, valid = _references(answer, case)
            trial = {"answer": answer, "citations": citations, "citation_valid": valid,
                     "error_type": None, "fallback_reason": None}
        except Exception as exc:
            trial = protocol.error_trial(exc)
        trial.update(protocol.usage_delta(client, before))
        trial.update(case_id=case["case_id"], source_digest=case["source_digest"],
                     attachments_digest=case["attachments_digest"], material_hash=material["content_hash"],
                     sequence=sequence, repetition=event["repetition"],
                     latency_ms=round((time.perf_counter() - trial_start) * 1000, 1),
                     estimated_input_tokens=estimated_tokens(system + user) + 32,
                     prompt_hash=digest([system, user]), artifact_scope=SCOPE,
                     answer_verdict="pending_review", correct=None, complete=None,
                     tool_calls=0, native_planning=False, events=[])
        trial["trial_id"] = "reuse-" + digest([result["evaluation_hash"], event, trial])[:24]
        rows[(event["variant"], task["id"])]["repetitions"].append(trial)
        if protocol.budget_exhausted(trial):
            budget_exhausted = True
            break
    result["executed_trial_count"] = len(schedule)
    result["query_cost"] = protocol.usage_delta(client, overall_usage)
    result["paired_trials_complete"] = protocol.validate_pairs(result)
    if budget_exhausted:
        result.update(status="partial_budget", answer_review_status="unresolved")
        result["executed_trial_count"] = sum(
            len(row["repetitions"]) for rows in raw.values() for row in rows)
        result["acceptance"].update(passed=False, effect_eligible=False,
                                    budget_complete=False)
    else:
        result["blind_review"] = rubric.blind_packet(result)
        rubric.finalize(result)
    return result


def render(report):
    """Render measured state without turning pending AI review into an effect."""
    lines = [
        "# CodeAtlas 会话材料同源对照",
        "",
        report["boundary"],
        "",
        f"状态：`{report['status']}`；独立案例：{report['case_count']}；"
        f"问题：{report['question_count']}；计划答案试次：{report['planned_trial_count']}。",
        "",
        "| 呈现方式 | 冻结方式 | 已执行试次 | 回答复核 |",
        "|---|---|---:|---|",
    ]
    mode = (report.get("materials") or {}).get("presentation_mode", "unknown")
    for arm, label in ARMS.items():
        executed = sum(len(row["repetitions"]) for row in report["raw_trials"][arm])
        method = "raw_source_session" if arm == "raw_session" else mode
        lines.append(f"| {label} | {method} | {executed} | {report['answer_review_status']} |")
    lines += [
        "",
        "三组只改变同源会话材料的组织方式，共同源码、命令与实际输出保持一致。",
        "`knowledge_role` 与说话人权威提示已移除；QA 答案不进入被测上下文。",
        "正确率、错误假设继承和验证步骤可执行性只有在双 agent 复核完成后才生成。",
        "模型 usage 缺失时保持 `null`，不按零成本处理。",
        "",
    ]
    if report["status"] == "not_run":
        lines.append("当前未配置模型，因此只完成了冻结材料、哈希、同源性和预算校验，没有效果数字。")
    elif report["status"] == "partial_budget":
        lines.append("预算上限已触发；已完成试次保留，但配对不完整，不能生成效果结论。")
    else:
        lines.append("答案已收集但仍待盲审；本报告不能据此宣称结构化知识卡更有效。")
    lines.append("")
    return "\n".join(lines)
