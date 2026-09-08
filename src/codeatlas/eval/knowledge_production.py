"""Produce and review same-source knowledge-reuse materials before answer trials.

Production receives only the frozen public session and common evidence.  It never
receives follow-up questions, answer keys, arm scores, or production knowledge
cards.  Model output remains unusable by the experiment until two isolated
reviewers (and an arbitrator on disagreement) validate every cited section.
"""
from __future__ import annotations

import copy
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..contracts import INPUT_BUDGET, OUTPUT_BUDGET, digest, estimated_tokens
from ..publication import atomic_text
from . import protocol

VERSION = "same-source-model-production-v1"
VERSION_V2 = "same-source-model-production-v2"
REQUIRED_FACT_FIELDS = ("symptom", "hypotheses", "conditions", "wrong_explanations",
                        "root_cause", "handling", "verification")
ARMS = ("generic_summary", "structured_card")
MAX_OUTPUT_TOKENS = OUTPUT_BUDGET
SYSTEM = (
    "你只负责整理一份公开合成开发会话，不回答任何后续问题。"
    "只能使用输入中的 [Tn] 会话轮次和 [En] 公共源码/实验材料；不得新增事实。"
    "输出严格 JSON：{\"sections\":[{\"heading\":\"...\","
    "\"content\":\"...\",\"evidence_tags\":[\"T1\",\"E1\"]}]}。"
    "每个 section 都必须有非空引用。最多输出 6 个 section，每个 content 不超过 160 个汉字。"
    "不得把单次观察推广成通用规律；条件式行为必须保留全部适用条件；"
    "会话中的因果判断若未被共同源码或实验材料验证，必须标为待验证或证据不足。"
    "不要输出思考过程，直接输出一个 JSON 对象。材料不足时明确写证据不足。"
)
SYSTEM_V2 = SYSTEM.replace("最多输出 6 个 section，每个 content 不超过 160 个汉字。",
                          "必须逐字保留冻结必要事实及其引用；可改变组织顺序，不能改写条件或否定关系。")


def validate_required_facts(facts, case: dict) -> dict:
    """Freeze exact, half-open character spans, not post-hoc knowledge labels.

    This verifies the declared contract, not its semantic exhaustiveness. Dual
    source review is still required before model products become usable.
    """
    if not isinstance(facts, dict) or set(facts) != set(REQUIRED_FACT_FIELDS):
        raise ValueError("required_facts must cover every required material field")
    sources = {f"T{m['turn_id']}": m["content"] for m in case["messages"]}
    sources.update({a["tag"]: a["text"] for a in case["attachments"]})
    covered = set()
    for field in REQUIRED_FACT_FIELDS:
        if not isinstance(facts[field], list) or not facts[field]:
            raise ValueError("each required fact field needs nonempty source facts")
        for fact in facts[field]:
            if not isinstance(fact, dict) or set(fact) != {"text", "evidence_tags", "source_spans"}:
                raise ValueError("required fact needs text, evidence_tags and source_spans")
            text, tags, spans = fact["text"], fact["evidence_tags"], fact["source_spans"]
            if (not isinstance(text, str) or not text.strip() or text != text.strip()
                    or not isinstance(tags, list) or not tags
                    or any(not isinstance(tag, str) or tag not in sources for tag in tags)
                    or len(set(tags)) != len(tags) or not isinstance(spans, list) or not spans):
                raise ValueError("required fact has invalid source text or citations")
            span_tags = set()
            for span in spans:
                if not isinstance(span, dict) or set(span) != {"tag", "start", "end"}:
                    raise ValueError("required source span needs tag/start/end")
                tag, start, end = span["tag"], span["start"], span["end"]
                if (not isinstance(tag, str) or tag not in sources
                        or type(start) is not int or type(end) is not int
                        or not 0 <= start < end <= len(sources[tag])
                        or sources[tag][start:end] != text):
                    raise ValueError("required source span differs from exact frozen fact")
                span_tags.add(tag)
            if span_tags != set(tags):
                raise ValueError("required fact citations differ from source spans")
            covered.update(tags)
    if not {f"T{m['turn_id']}" for m in case["messages"]} <= covered:
        raise ValueError("required facts must cover every source turn")
    return copy.deepcopy(facts)


def _budget(case):
    raw = case["materials"]["raw_session"]["text"]
    tokens = estimated_tokens(raw)
    return tokens, min(600, tokens * 3 // 5)


def _full_sections(case, arm):
    # The same fallback for both arms, with all original turns unmodified.
    rows = [{"heading": f"会话 T{m['turn_id']}", "content": m["content"],
             "evidence_tags": [f"T{m['turn_id']}"]} for m in case["messages"]]
    needed = {tag for facts in case["required_facts"].values() for fact in facts
              for tag in fact["evidence_tags"] if tag.startswith("E")}
    rows.extend({"heading": a["tag"], "content": a["text"], "evidence_tags": [a["tag"]]}
                for a in case["attachments"] if a["tag"] in needed)
    return [{**row, "id": f"{case['case_id']}:{arm}:s{i}"} for i, row in enumerate(rows, 1)]


def _compression_errors(case, sections):
    reasons = []
    for field, facts in case["required_facts"].items():
        for fact in facts:
            if not any(fact["text"] in section["content"]
                       and set(fact["evidence_tags"]) <= set(section["evidence_tags"])
                       for section in sections):
                reasons.append("missing_required_fact:" + field)
    if estimated_tokens(_material_text(sections)) > _budget(case)[1]:
        reasons.append("material_budget_exceeded")
    return sorted(set(reasons))


def _select_product(case, arm, sections, *, candidate_hash=None, error_type=None):
    raw_tokens, budget = _budget(case)
    reasons = ["invalid_candidate:" + error_type] if error_type else _compression_errors(case, sections)
    candidate_hash = candidate_hash or digest(sections)
    candidate_tokens = estimated_tokens(_material_text(sections)) if sections else None
    effective = _full_sections(case, arm) if reasons else sections
    text = _material_text(effective)
    return {"sections": effective, "text": text, "compression": {
        "version": 2, "status": "compression_infeasible" if reasons else "compressed",
        "raw_session_tokens": raw_tokens, "budget_tokens": budget,
        "candidate_tokens": candidate_tokens, "estimated_tokens": estimated_tokens(text),
        "candidate_hash": candidate_hash, "reasons": reasons, "used_full_material": bool(reasons),
        "required_facts_hash": digest(case["required_facts"]),
        # A failed compression is never reported as a saving, even if the raw
        # source formatting happens to be larger than its fallback rendering.
        "saved_tokens": None if reasons else raw_tokens - estimated_tokens(text),
    }}


def extractive_product(case, arm):
    """Zero-model source-span candidate; it is not a model-produced result."""
    sections = []
    for field in REQUIRED_FACT_FIELDS:
        for fact in case["required_facts"][field]:
            sections.append({"id": f"{case['case_id']}:{arm}:s{len(sections) + 1}",
                             "heading": field if arm == "structured_card" else "摘要",
                             "content": fact["text"], "evidence_tags": fact["evidence_tags"]})
    return _select_product(case, arm, sections)


def formal_card_answer_view(card, messages):
    """Read-only, lossless presentation of an already approved card, never a bundle edit.

    Conditions may be embedded anywhere in a legacy card: preserve every content
    field in full. If no measurable source or any field is absent, show the full
    material and explicitly decline to claim successful compression.
    """
    if card.get("status") != "approved" or card.get("artifact_scope", "formal") != "formal":
        return None
    fields = ("title", "symptom", "hypotheses", "dead_ends", "root_cause", "fix_steps", "verification")
    labels = ("标题", "现象", "假设", "错误解释/无效尝试", "根因及条件", "处理", "验证")
    rows, missing = [], []
    for field, label in zip(fields, labels):
        value = card.get(field)
        if field in ("hypotheses", "dead_ends") and isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        text = "；".join(str(item) for item in value) if isinstance(value, list) else str(value or "")
        if not text.strip():
            missing.append(field)
        rows.append(f"{label}：{text}")
    for field in ("evidence_tags", "evidence_turns"):
        value = card.get(field)
        try:
            value = json.loads(value) if isinstance(value, str) else value
        except ValueError:
            value = None
        if not isinstance(value, list) or not value:
            missing.append(field)
        rows.append(f"{field}：{json.dumps(value, ensure_ascii=False)}")
    text = "\n".join(rows)
    raw = "\n\n".join(f"第 {i} 轮：{m['content']}" for i, m in enumerate(messages, 1))
    raw_tokens = estimated_tokens(raw) if raw else 0
    budget = min(600, raw_tokens * 3 // 5)
    reasons = (["missing_fields:" + ",".join(missing)] if missing else [])
    if (not card.get("review_bundle_hash")
            or card.get("review_bundle_hash") != card.get("current_review_bundle_hash")):
        reasons.append("approved_bundle_not_current")
    if not raw:
        reasons.append("source_session_unavailable")
    if estimated_tokens(text) > budget:
        reasons.append("material_budget_exceeded")
    return {"version": 2, "status": "compression_infeasible" if reasons else "compressed",
            "text": text, "reasons": reasons, "used_full_material": bool(reasons),
            "raw_session_tokens": raw_tokens, "budget_tokens": budget,
            "estimated_tokens": estimated_tokens(text), "content_hash": digest(text),
            "source_bundle_hash": card.get("current_review_bundle_hash"),
            "source_session_hash": digest(raw) if raw else None,
            "saved_tokens": None if reasons else raw_tokens - estimated_tokens(text)}


def _production_payload(artifact: dict) -> dict:
    return {key: value for key, value in artifact.items()
            if key not in {"artifact_hash", "reviewed_artifact_hash", "review_status",
                           "status", "material_reviews", "section_verdicts"}}


def _source_packet(case: dict) -> str:
    turns = "\n\n".join(
        f"[T{message['turn_id']}] {message['content']}" for message in case["messages"]
    )
    evidence = "\n\n".join(
        f"[{item['tag']}] {item['path']}:{item['line_start']}-{item['line_end']}\n{item['text']}"
        for item in case["attachments"]
    )
    return (
        f"公开仓库：{case['repository']}\n固定版本：{case['revision']}\n\n"
        f"会话（角色标签已移除）\n{turns}\n\n共同证据\n{evidence}"
    )


def _production_messages(case, arm):
    instruction = (
        "生成不带固定栏目约束的通用摘要，压缩重复内容但保留条件、反例和验证边界。"
        if arm == "generic_summary" else
        "生成结构化知识卡，栏目应覆盖现象、假设、排除项、根因、处理步骤、验证和边界。"
    )
    system = SYSTEM
    if "required_facts" in case:
        system = SYSTEM_V2
        instruction += (f"\n最终资料（含栏目和引用）的估算 token 必须 <= {_budget(case)[1]}。"
                        "以下必要事实两组相同，必须逐字保留正文与引用：\n"
                        + json.dumps(case["required_facts"], ensure_ascii=False, sort_keys=True))
    return system, instruction + "\n\n" + _source_packet(case)


def _parse(client, case: dict, arm: str, capture=None) -> list[dict]:
    system, user = _production_messages(case, arm)
    options = {"complete_input_budget": INPUT_BUDGET} if "required_facts" in case else {}
    try:
        raw = client._chat(system, user, max_tokens=MAX_OUTPUT_TOKENS, json_object=True, **options)
    finally:
        if capture is not None:
            meta = getattr(client, "last_response_meta", {}) or {}
            capture["response_metadata"] = {key: meta.get(key) for key in
                                            ("finish_reason", "transport_attempts", "content_hash")}
    if capture is not None:
        capture["raw"] = raw
        if capture["response_metadata"]["finish_reason"] == "length":
            from ..llm.client import OutputTruncated
            raise OutputTruncated("material completion limit reached")
    try:
        payload = (json.loads(raw) if "required_facts" in case else
                   client._json(raw) if hasattr(client, "_json") else json.loads(raw))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("knowledge material is not valid JSON") from exc
    return _checked_sections(payload, case, arm)


def _checked_sections(payload, case, arm):
    sections = payload.get("sections") if isinstance(payload, dict) else None
    if "required_facts" in case and isinstance(payload, dict) and set(payload) != {"sections"}:
        raise ValueError("material JSON may only contain sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("knowledge material needs nonempty sections")
    allowed = {f"T{m['turn_id']}" for m in case["messages"]} | {
        item["tag"] for item in case["attachments"]
    }
    checked = []
    for index, section in enumerate(sections, 1):
        if not isinstance(section, dict) or set(section) != {
                "heading", "content", "evidence_tags"}:
            raise ValueError("each material section must contain only heading/content/evidence_tags")
        heading, content, tags = section["heading"], section["content"], section["evidence_tags"]
        if (not isinstance(heading, str) or not heading.strip()
                or not isinstance(content, str) or not content.strip()
                or not isinstance(tags, list) or not tags
                or any(not isinstance(tag, str) or tag not in allowed for tag in tags)):
            raise ValueError("material section has empty text or an unknown evidence tag")
        checked.append({
            "id": f"{case['case_id']}:{arm}:s{index}",
            "heading": heading.strip(), "content": content.strip(),
            "evidence_tags": sorted(set(tags)),
        })
    return checked


def _material_text(sections: list[dict]) -> str:
    return "\n\n".join(
        f"{section['heading']}\n{section['content']} "
        f"{' '.join('[' + tag + ']' for tag in section['evidence_tags'])}"
        for section in sections
    )


def produce(manifest_path: str | Path, *, out: str | Path | None = None,
            project_root: str | Path | None = None, client_factory=None,
            ledger=None, campaign_id: str | None = None) -> dict:
    """Create paired isolated model products; no questions or gold are exposed."""
    from . import knowledge_reuse_eval as reuse
    from ..llm.client import get_client, OutputTruncated

    prepared = reuse.prepare(manifest_path, project_root=project_root)
    identifier = campaign_id or "knowledge-production-" + uuid.uuid4().hex
    factory = client_factory or (
        lambda role, case_id: get_client(
            ledger=ledger, session_id=f"{identifier}:{role}:{case_id}",
            transport_retries=0 if prepared["schema_version"] == 2 else None,
        )
    )
    products, sessions = [], set()
    started = time.perf_counter()
    schema = prepared["schema_version"]
    clients = {}
    if schema == 2:
        # Fail shared model/session/input-budget preflight before the first call.
        for case in prepared["cases"]:
            for arm in ARMS:
                clients[(case["case_id"], arm)] = factory(arm, case["case_id"])
                system, user = _production_messages(case, arm)
                if estimated_tokens(system + user) + 32 > INPUT_BUDGET:
                    raise ValueError("knowledge production exceeds shared input budget")
        configured = [c for c in clients.values() if c is not None]
        if configured:
            if any(getattr(c, "transport_retries", 0) != 0 for c in configured):
                raise ValueError("schema 2 production requires zero transport retries")
            if len({getattr(c, "model", None) for c in configured}) != 1 or not getattr(configured[0], "model", None):
                raise ValueError("paired production requires the same model")
            session_ids = [getattr(c, "session_id", None) for c in configured]
            if not all(session_ids) or len(set(session_ids)) != len(session_ids):
                raise ValueError("each knowledge-production role requires an isolated model session")
        if len(configured) != len(clients):
            return {"schema_version": 2, "status": "not_run", "reason": "model_not_configured",
                    "planned_requests": len(clients), "executed_requests": 0,
                    "manifest_hash": prepared["manifest_hash"]}
    for case in prepared["cases"]:
        for arm in ARMS:
            client = clients[(case["case_id"], arm)] if schema == 2 else factory(arm, case["case_id"])
            if client is None:
                return {
                    "schema_version": 1, "status": "not_run",
                    "reason": "model_not_configured", "model": None,
                    "planned_requests": len(prepared["cases"]) * len(ARMS),
                    "executed_requests": len(products),
                    "manifest_hash": prepared["manifest_hash"],
                    "source_materials_hash": digest([
                        (c["case_id"], c["source_digest"], c["attachments_digest"])
                        for c in prepared["cases"]
                    ]),
                }
            session = getattr(client, "session_id", None)
            if not session or session in sessions:
                raise ValueError("each knowledge-production role requires an isolated model session")
            sessions.add(session)
            before = protocol.usage_start(client)
            capture, error_type = {}, None
            try:
                sections = _parse(client, case, arm, capture if schema == 2 else None)
            except (ValueError, TypeError, OutputTruncated) as exc:
                if schema != 2 or ("raw" not in capture and not isinstance(exc, OutputTruncated)):
                    raise
                sections, error_type = [], type(exc).__name__
                capture.setdefault("raw", None)
            usage = protocol.usage_delta(client, before)
            if schema == 2 and hasattr(client, "input_usd_per_million") and (
                    client.input_usd_per_million is None or client.output_usd_per_million is None):
                usage["cost_usd"] = None
            if error_type == "OutputTruncated":
                # _chat deliberately raises before cumulative usage accounting.
                # Preserve that failed attempt's observed usage, never zero it.
                observed = getattr(client, "last_usage", {}) or {}
                for field, output in (("prompt_tokens", "input_tokens"),
                                      ("completion_tokens", "output_tokens"), ("total_tokens", "tokens")):
                    value = observed.get(field)
                    usage[output] = value if type(value) is int and value >= 0 else None
                usage["usage_complete"] = all(usage[k] is not None for k in ("input_tokens", "output_tokens", "tokens"))
                usage["cost_usd"] = None
                rates = [getattr(client, key, None) for key in ("input_usd_per_million", "output_usd_per_million")]
                if all(isinstance(n, (int, float)) and not isinstance(n, bool) and n >= 0 for n in rates) and usage["usage_complete"]:
                    usage["cost_usd"] = (usage["input_tokens"] * rates[0] + usage["output_tokens"] * rates[1]) / 1_000_000
            product = {
                "id": f"{case['case_id']}:{arm}", "case_id": case["case_id"],
                "arm": arm, "model": getattr(client, "model", "openai-compatible"),
                "session_id": session, "sections": sections,
                "text": _material_text(sections), "usage": usage,
            }
            if schema == 2:
                candidate = {**capture, "error_type": error_type}
                product.update(_select_product(case, arm, sections,
                                               candidate_hash=digest(candidate), error_type=error_type))
                product["candidate"] = candidate
                product["production_limits"] = {"input_tokens": INPUT_BUDGET, "output_tokens": MAX_OUTPUT_TOKENS,
                                                "transport_retries": 0}
            products.append(product)
    artifact = {
        "schema_version": schema, "version": VERSION if schema == 1 else VERSION_V2, "status": "pending_review",
        "review_status": "unresolved", "artifact_scope": "isolated_experiment",
        "campaign_id": identifier, "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "manifest_hash": prepared["manifest_hash"],
        "source_materials_hash": digest([
            (c["case_id"], c["source_digest"], c["attachments_digest"])
            for c in prepared["cases"]
        ]),
        "prompt_hash": digest([SYSTEM, VERSION, MAX_OUTPUT_TOKENS]) if schema == 1 else
                       digest([SYSTEM_V2, VERSION_V2, INPUT_BUDGET, MAX_OUTPUT_TOKENS]),
        "products": products,
        "production_cost": {
            "model_requests": len(products),
            "input_tokens": sum(p["usage"].get("input_tokens") or 0 for p in products)
                if all(p["usage"].get("input_tokens") is not None for p in products) else None,
            "output_tokens": sum(p["usage"].get("output_tokens") or 0 for p in products)
                if all(p["usage"].get("output_tokens") is not None for p in products) else None,
            "cost_usd": sum(p["usage"].get("cost_usd") or 0 for p in products)
                if all(p["usage"].get("cost_usd") is not None for p in products) else None,
            "human_edit_seconds": None,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        },
        "boundary": "Model-produced materials are isolated and unusable until dual review; follow-up questions and answer keys were not model inputs.",
    }
    artifact["artifact_hash"] = digest(_production_payload(artifact))
    if out:
        atomic_text(Path(out), json.dumps(artifact, ensure_ascii=False, indent=2))
    return artifact


def apply_reviews(artifact: dict, submission: dict) -> dict:
    """Validate two isolated section reviews and optional third-party arbitration."""
    if artifact.get("artifact_hash") != digest(_production_payload(artifact)):
        raise ValueError("generated material artifact changed")
    if submission.get("artifact_hash") != artifact["artifact_hash"]:
        raise ValueError("material review targets another artifact")
    expected = {
        section["id"] for product in artifact.get("products", [])
        for section in product.get("sections", [])
    }
    reviewers = submission.get("independent_reviews")
    if not isinstance(reviewers, list) or len(reviewers) != 2:
        raise ValueError("material review requires exactly two independent reviewers")
    identities, decisions = set(), []
    occupied_sessions = {p.get("session_id") for p in artifact.get("products", [])}
    for review in reviewers:
        identity = (review.get("agent"), review.get("session_id"))
        if (review.get("reviewer_kind") != "agent" or not all(identity)
                or review.get("peer_reviews_visible") is not False or identity in identities):
            raise ValueError("material reviewers must be isolated, distinct agents")
        identities.add(identity)
        if artifact.get("schema_version") == 2:
            if identity[1] in occupied_sessions:
                raise ValueError("material reviewer session must be separate from production and peers")
            occupied_sessions.add(identity[1])
        items = review.get("items")
        if (not isinstance(items, list) or len(items) != len(expected)
                or {item.get("section_id") for item in items} != expected):
            raise ValueError("material review must cover every generated section")
        if any(item.get("verdict") not in {"supported", "unsupported", "unresolved"}
               for item in items):
            raise ValueError("invalid material-review verdict")
        decisions.append({item["section_id"]: item["verdict"] for item in items})
    disagreements = sorted(section for section in expected
                           if decisions[0][section] != decisions[1][section])
    arbitration = submission.get("arbitration")
    selected = {}
    if disagreements:
        if not isinstance(arbitration, dict):
            selected = {section: "unresolved" for section in disagreements}
        else:
            identity = (arbitration.get("agent"), arbitration.get("session_id"))
            if (arbitration.get("reviewer_kind") != "agent" or not all(identity)
                    or identity in identities or arbitration.get("peer_reviews_visible") is not False):
                raise ValueError("arbitrator must use a third isolated agent session")
            if artifact.get("schema_version") == 2 and identity[1] in occupied_sessions:
                raise ValueError("arbitrator session must be separate from production and reviewers")
            rows = arbitration.get("items") or []
            if len(rows) != len(disagreements) or {row.get("section_id") for row in rows} != set(disagreements):
                raise ValueError("arbitration must cover exactly the disagreements")
            selected.update({row["section_id"]: row.get("verdict") for row in rows})
    for section in expected - set(disagreements):
        selected[section] = decisions[0][section]
    if any(value not in {"supported", "unsupported", "unresolved"}
           for value in selected.values()):
        raise ValueError("invalid arbitration verdict")
    status = "frozen" if selected and all(value == "supported" for value in selected.values()) \
        else "rejected" if any(value == "unsupported" for value in selected.values()) \
        else "pending_review"
    result = copy.deepcopy(artifact)
    result.update(
        status=status,
        review_status="ai_reviewed" if status in {"frozen", "rejected"} else "unresolved",
        material_reviews=copy.deepcopy(submission),
        section_verdicts=selected,
    )
    result["reviewed_artifact_hash"] = digest(result)
    return result


def load_frozen(path: str | Path, prepared: dict) -> dict:
    artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    original = artifact.get("artifact_hash")
    if original != digest(_production_payload(artifact)):
        raise ValueError("generated material artifact changed")
    if artifact.get("reviewed_artifact_hash") != digest({
            key: value for key, value in artifact.items() if key != "reviewed_artifact_hash"}):
        raise ValueError("material review envelope changed")
    if artifact.get("status") != "frozen" or artifact.get("review_status") != "ai_reviewed":
        raise ValueError("model-produced materials require completed dual review")
    if artifact.get("manifest_hash") != prepared["manifest_hash"]:
        raise ValueError("model-produced materials target another source manifest")
    source_hash = digest([
        (c["case_id"], c["source_digest"], c["attachments_digest"])
        for c in prepared["cases"]
    ])
    if artifact.get("source_materials_hash") != source_hash:
        raise ValueError("model-produced materials target different source bytes")
    if len(artifact.get("products", [])) != len(prepared["cases"]) * len(ARMS):
        raise ValueError("model-produced material set is incomplete")
    if prepared["schema_version"] == 2:
        if artifact.get("schema_version") != 2 or artifact.get("version") != VERSION_V2:
            raise ValueError("schema 2 requires budget-validated material production")
        submission = artifact.get("material_reviews")
        if not isinstance(submission, dict):
            raise ValueError("material dual review is missing")
        replayed = apply_reviews({k: v for k, v in artifact.items() if k != "reviewed_artifact_hash"}, submission)
        if replayed != artifact:
            raise ValueError("material dual review derived fields differ from replay")
        expected = {(c["case_id"], arm) for c in prepared["cases"] for arm in ARMS}
        products = artifact["products"]
        if {(p.get("case_id"), p.get("arm")) for p in products} != expected:
            raise ValueError("model-produced material set is incomplete")
        if len({p.get("model") for p in products}) != 1 or not all(p.get("model") for p in products):
            raise ValueError("paired production requires the same model")
        if len({p.get("session_id") for p in products}) != len(products) or not all(p.get("session_id") for p in products):
            raise ValueError("material production requires isolated sessions")
        cases = {c["case_id"]: c for c in prepared["cases"]}
        for product in products:
            case, arm = cases[product["case_id"]], product["arm"]
            candidate = product.get("candidate") or {}
            if (set(candidate) != {"raw", "error_type", "response_metadata"}
                    or not isinstance(candidate["response_metadata"], dict)):
                raise ValueError("compression candidate audit record is missing")
            error_type = None
            if candidate["response_metadata"].get("finish_reason") == "length":
                sections, error_type = [], "OutputTruncated"
            else:
                if not isinstance(candidate.get("raw"), str):
                    raise ValueError("compression candidate raw content is missing")
                try:
                    sections = _checked_sections(json.loads(candidate["raw"]), case, arm)
                except (ValueError, TypeError) as exc:
                    sections, error_type = [], type(exc).__name__
                    if isinstance(exc, json.JSONDecodeError):
                        error_type = "ValueError"
            if candidate["error_type"] != error_type:
                raise ValueError("compression candidate error differs from replay")
            selected = _select_product(case, arm, sections, candidate_hash=digest(candidate),
                                       error_type=error_type)
            if any(product.get(key) != value for key, value in selected.items()):
                raise ValueError("compression text/sections/metadata differs from validated source contract")
            if product.get("production_limits") != {"input_tokens": INPUT_BUDGET, "output_tokens": MAX_OUTPUT_TOKENS,
                                                    "transport_retries": 0}:
                raise ValueError("paired production budget changed")
    return artifact
