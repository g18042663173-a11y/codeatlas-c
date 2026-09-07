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

from ..contracts import OUTPUT_BUDGET, digest
from ..publication import atomic_text
from . import protocol

VERSION = "same-source-model-production-v1"
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


def _parse(client, case: dict, arm: str) -> list[dict]:
    instruction = (
        "生成不带固定栏目约束的通用摘要，压缩重复内容但保留条件、反例和验证边界。"
        if arm == "generic_summary" else
        "生成结构化知识卡，栏目应覆盖现象、假设、排除项、根因、处理步骤、验证和边界。"
    )
    raw = client._chat(SYSTEM, instruction + "\n\n" + _source_packet(case),
                       max_tokens=MAX_OUTPUT_TOKENS, json_object=True)
    try:
        payload = client._json(raw) if hasattr(client, "_json") else json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("knowledge material is not valid JSON") from exc
    sections = payload.get("sections") if isinstance(payload, dict) else None
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
    """Create four isolated model products; no questions or gold are exposed."""
    from . import knowledge_reuse_eval as reuse
    from ..llm.client import get_client

    prepared = reuse.prepare(manifest_path, project_root=project_root)
    identifier = campaign_id or "knowledge-production-" + uuid.uuid4().hex
    factory = client_factory or (
        lambda role, case_id: get_client(
            ledger=ledger, session_id=f"{identifier}:{role}:{case_id}"
        )
    )
    products, sessions = [], set()
    started = time.perf_counter()
    for case in prepared["cases"]:
        for arm in ARMS:
            client = factory(arm, case["case_id"])
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
            sections = _parse(client, case, arm)
            usage = protocol.usage_delta(client, before)
            products.append({
                "id": f"{case['case_id']}:{arm}", "case_id": case["case_id"],
                "arm": arm, "model": getattr(client, "model", "openai-compatible"),
                "session_id": session, "sections": sections,
                "text": _material_text(sections), "usage": usage,
            })
    artifact = {
        "schema_version": 1, "version": VERSION, "status": "pending_review",
        "review_status": "unresolved", "artifact_scope": "isolated_experiment",
        "campaign_id": identifier, "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "manifest_hash": prepared["manifest_hash"],
        "source_materials_hash": digest([
            (c["case_id"], c["source_digest"], c["attachments_digest"])
            for c in prepared["cases"]
        ]),
        "prompt_hash": digest([SYSTEM, VERSION, MAX_OUTPUT_TOKENS]),
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
    for review in reviewers:
        identity = (review.get("agent"), review.get("session_id"))
        if (review.get("reviewer_kind") != "agent" or not all(identity)
                or review.get("peer_reviews_visible") is not False or identity in identities):
            raise ValueError("material reviewers must be isolated, distinct agents")
        identities.add(identity)
        items = review.get("items")
        if not isinstance(items, list) or {item.get("section_id") for item in items} != expected:
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
            rows = arbitration.get("items") or []
            if {row.get("section_id") for row in rows} != set(disagreements):
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
    return artifact
