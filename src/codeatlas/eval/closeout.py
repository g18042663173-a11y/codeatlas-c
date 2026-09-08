"""Bounded closeout preflight. Never turns an incomplete audit into paid work."""
from __future__ import annotations

import copy
import json
import re
import sqlite3
from pathlib import Path

from ..contracts import digest
from ..publication import atomic_text
from . import protocol, rubric

BASE_ATTEMPT = "v4b-sol.semantic-review-v1.format-gap-recovery-v2"


def restore_traced_bodies(report, packet, *, verify_projection=True):
    """Create a new review projection from bodies in the original tool trace.

    Never read today's source/Wiki or infer a body from a symbol name. This is
    transport repair, not permission to re-score or a semantic verdict. Search
    context without a per-citation body remains unresolved by this operation.
    """
    before = digest(report)
    if protocol.run_hash(report) != report.get("run_hash") or packet.get("run_hash") != report["run_hash"]:
        raise ValueError("frozen report and packet do not match")
    if packet.get("packet_hash") != digest({k: v for k, v in packet.items() if k != "packet_hash"}):
        raise ValueError("review packet checksum mismatch")
    if verify_projection and packet["packet_hash"] != rubric.blind_packet(copy.deepcopy(report))["packet_hash"]:
        raise ValueError("review packet is not the unchanged answer/gold projection")
    trials = {t["trial_id"]: t for rows in report["raw_trials"].values()
              for row in rows for t in row["repetitions"]}
    result, restored = copy.deepcopy(packet), []
    common = ("repository", "revision", "knowledge_set_id", "source_content_hash", "build_config_hash")
    for item in result["items"]:
        trial = trials.get(item["trial_id"])
        if trial is None:
            raise ValueError("packet references an unknown frozen trial")
        cited = set(re.findall(r"\[(E\d+)\]", item.get("answer") or ""))
        for citation in item["evidence_payload"]:
            if citation.get("tag") not in cited or citation.get("text"):
                continue
            origin = citation.get("provenance") or {}
            # Missing identity never means that two unknown snapshots match.
            if not all(origin.get(key) for key in common) or origin.get("stale") is True:
                continue
            pieces = []
            for offset, event in enumerate(trial.get("events") or []):
                if event.get("status") != "completed":
                    continue
                output = event.get("result") or {}
                tool = event.get("tool")
                if tool == "wiki_section":
                    fields, body = ("page_id", "input_hash"), output.get("markdown")
                    if output.get("page_id") != origin.get("page_id"):
                        continue
                elif tool == "code_read":
                    fields, body = ("path", "usr", "line_start", "line_end", "definition_hash"), output.get("text")
                    if any(output.get(k) != origin.get(k) for k in ("path", "line_start", "line_end")):
                        continue
                else:
                    continue
                if not isinstance(body, str) or not body or not all(origin.get(k) for k in fields):
                    continue
                matched = any(
                    all((row.get("provenance") or {}).get(k) == origin[k] for k in (*common, *fields))
                    and (row.get("provenance") or {}).get("stale") is not True
                    for row in output.get("citations") or [] if isinstance(row, dict))
                if not matched:
                    continue
                pieces.append({"event_index": offset, "tool": tool,
                               "event_hash": digest(event), "body_hash": digest(body),
                               "truncated": output.get("truncated", False),
                               "next_offset": output.get("next_offset"), "text": body})
            if pieces:
                citation.update(kind="frozen_tool_excerpt", text="\n\n".join(p["text"] for p in pieces),
                                restoration={"source": "original_trial_events", "fragments": [
                                    {k: v for k, v in p.items() if k != "text"} for p in pieces]})
                citation["payload_hash"] = digest({k: v for k, v in citation.items() if k != "payload_hash"})
                restored.append({"trial_id": item["trial_id"], "tag": citation["tag"],
                                 "body_hash": digest(citation["text"])})
        item["citations"] = copy.deepcopy(item["evidence_payload"])
        item["input_hash"] = digest({k: v for k, v in item.items() if k != "input_hash"})
    result["citation_projection_version"] = "neutral-citations-v3-frozen-tool-bodies"
    result["restoration"] = {"base_packet_hash": packet["packet_hash"], "entries": restored,
                             "boundary": "Original tool-output bytes only; unresolved source bodies are not invented; no scores changed."}
    result["packet_hash"] = digest({k: v for k, v in result.items() if k != "packet_hash"})
    if digest(report) != before:
        raise ValueError("body restoration mutated frozen answers")
    return result


def _ranges(value):
    result = set()
    for part in str(value or "").split(","):
        match = re.fullmatch(r"\s*(\d+)(?:-(\d+))?\s*", part)
        if match:
            start, end = int(match[1]), int(match[2] or match[1])
            if 0 < start <= end and end - start < 100000:
                result.update(range(start, end + 1))
    return result


def audit_wiki(report):
    """Exact transport/range audit, not a judgment of the candidate's claims.

    The old reviewer can use a separately bound pinned-source excerpt for an
    anchor. Count that only when its repository/revision/path match and it
    covers the range. Partial coverage is a risk requiring inspection, not
    evidence that an answer was semantically incorrect.
    """
    before = digest(report)
    repaired = rubric.blind_packet(copy.deepcopy(report))
    previous = {i["trial_id"]: i for i in report["blind_review"]["items"]}
    rows = []
    for item in repaired["items"]:
        old = previous[item["trial_id"]]
        same = all(item.get(k) == old.get(k) for k in set(item) | set(old) if k != "input_hash")
        tags = set(re.findall(r"\[(E\d+)\]", item.get("answer") or ""))
        missing = []
        for citation in item["evidence_payload"]:
            if citation["tag"] not in tags or citation.get("text"):
                continue
            origin = citation.get("provenance") or citation
            start, end = origin.get("line_start"), origin.get("line_end")
            covered, matched = set(), []
            for source in item["evidence_payload"]:
                if (source.get("kind") == "pinned_source" and source.get("text")
                        and all(source.get(k) == origin.get(k) for k in ("repository", "revision", "path"))):
                    covered |= _ranges(source.get("lines"))
                    matched.append({"tag": source["tag"], "lines": source.get("lines"),
                                    "text_hash": digest(source["text"])})
            wanted = _ranges(f"{start}-{end}")
            if not wanted or not wanted <= covered:
                missing.append({"tag": citation["tag"], "source_ref": citation.get("source_ref"),
                                "path": origin.get("path"), "line_start": start, "line_end": end,
                                "coverage": "partial" if wanted & covered else "absent",
                                "pinned_excerpts": matched})
        rows.append({"trial_id": item["trial_id"], "task_id": item["task_id"],
                     "input_hash": old["input_hash"], "mapping_unchanged": same,
                     "incomplete_cited_ranges": missing})
    if digest(report) != before or protocol.run_hash(report) != report["run_hash"]:
        raise ValueError("frozen answers changed during audit")
    return {"run_hash": report["run_hash"], "trials": rows, "trial_count": len(rows),
            "mapping_changed_trials": sum(not r["mapping_unchanged"] for r in rows),
            "range_review_required_trials": sum(bool(r["incomplete_cited_ranges"]) for r in rows),
            "boundary": "Range completeness is a conservative evidence-input gate, not answer correctness."}


def write_wiki_notice(report, *, historical_path, out):
    """Withhold an aggregate pending input audit, without invalidating each answer."""
    historical_path = Path(historical_path)
    historical = json.loads(historical_path.read_text(encoding="utf-8"))
    if historical.get("run_hash") != report.get("run_hash"):
        raise ValueError("historical aggregate does not match raw answers")
    audit = audit_wiki(report)
    packet = rubric.blind_packet(copy.deepcopy(report))
    repaired = restore_traced_bodies(report, packet)
    entries = repaired["restoration"]["entries"]
    value = {"schema_version": 1, "scope": "review_only", "status": "pending_evidence_audit",
             "reason": "cited_body_omitted_from_review_input", "run_hash": report["run_hash"],
             "historical_report_sha256": digest(historical_path.read_bytes()),
             "trial_count": audit["trial_count"], "mapping_broken_trials": audit["mapping_changed_trials"],
             "range_review_required_trials": audit["range_review_required_trials"],
             "restorable_trace_body_trials": len({e["trial_id"] for e in entries}),
             "restorable_trace_citations": len(entries),
             "restored_packet_hash": repaired["packet_hash"],
             "range_audit": audit, "trace_restoration": repaired["restoration"],
             "model_requests": 0, "rescored": False,
             "boundary": "The historical aggregate is withheld pending claim-level evidence audit. Range gaps and restored bodies are not counts of incorrect answers. No frozen answer, gold, score or model request was changed."}
    value["audit_hash"] = digest(value)
    target = Path(out)
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if target.exists() and target.read_text(encoding="utf-8") != text:
        raise ValueError("Wiki audit notice already frozen; choose a new version")
    if not target.exists():
        atomic_text(target, text)
    return {k: v for k, v in value.items() if k not in {"range_audit", "trace_restoration"}}


def prepare(campaign_dir, *, out_dir, base_attempt=BASE_ATTEMPT):
    """Freeze a no-dispatch audit before creating any new billable request."""
    directory = Path(campaign_dir).resolve()
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    if plan.get("plan_hash") != digest({k: v for k, v in plan.items() if k != "plan_hash"}):
        raise ValueError("campaign manifest checksum mismatch")
    with sqlite3.connect((directory / "ledger.sqlite3").as_uri() + "?mode=ro", uri=True) as conn:
        batches = conn.execute("SELECT id,binding_json,result_json,result_hash FROM campaign_review_batches").fetchall()
        selected = [b for b in batches if json.loads(b[1]).get("attempt_id") == base_attempt]
        if len(selected) != 1:
            raise ValueError("base attempt must resolve uniquely")
        batch_id, _, result_json, result_hash = selected[0]
        stored = json.loads(result_json)
        if digest(stored) != result_hash:
            raise ValueError("base review batch checksum mismatch")
        request_count = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        trial_states = dict(conn.execute("SELECT state,COUNT(*) FROM trials GROUP BY state").fetchall())
    wiki = {key: audit_wiki(report) for key, report in stored["reviewed_reports"].items()
            if key in {"wiki-cjson", "wiki-lwip"}}
    from .review_packet_audit import audit
    reuse = audit(stored["reviewed_reports"]["knowledge-reuse"])
    risks = sum(item["range_review_required_trials"] + item["mapping_changed_trials"] for item in wiki.values())
    payload = {"schema_version": 1, "base_attempt": base_attempt, "base_batch_id": batch_id,
               "campaign_plan_hash": plan["plan_hash"], "answer_states": trial_states,
               "initial_requests": request_count, "max_new_requests": 800,
               "global_ceiling": plan["budget"]["max_requests"], "workers": 2,
               "status": "requires_evidence_scope_review" if risks else "offline_preflight_passed",
               "dispatch_allowed": False, "new_model_requests": 0,
               "wiki_audit": wiki, "reuse_audit": reuse,
               "boundary": "No new scoring is authorized by an audit result. Additional evidence gaps require scope review; no automatic budget expansion."}
    payload["closeout_hash"] = digest(payload)
    target = Path(out_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    output = target / "preflight.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output.exists() and output.read_text(encoding="utf-8") != text:
        raise ValueError("preflight already frozen with different inputs; choose a new version")
    if not output.exists():
        atomic_text(output, text)
    return {"status": payload["status"], "closeout_hash": payload["closeout_hash"],
            "output": str(output), "dispatch_allowed": False, "new_model_requests": 0,
            "initial_requests": request_count,
            "wiki_range_review_trials": {k: v["range_review_required_trials"] for k, v in wiki.items()}}
