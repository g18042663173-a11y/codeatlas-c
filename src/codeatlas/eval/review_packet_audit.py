"""Read-only diagnosis of reuse citation transport, not a new semantic score.

Rebuilds only the blind view from frozen answers and material. No model requests,
gold edits, raw-answer mutations or publication approvals are performed.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sqlite3
from pathlib import Path

from ..contracts import digest, estimated_tokens
from ..publication import atomic_text
from . import campaign_review, protocol, rubric

TAGS = re.compile(r"\[([A-Za-z]+\d+)\]")


def audit(report):
    if report.get("report_kind") != "knowledge_reuse":
        raise ValueError("this audit requires a frozen knowledge-reuse report")
    before = digest(report)
    if protocol.run_hash(report) != report.get("run_hash"):
        raise ValueError("frozen raw answer hash mismatch")
    repaired = copy.deepcopy(report)
    packet = rubric.blind_packet(repaired)
    old_packet = report["blind_review"]
    old_items = {i["trial_id"]: i for i in old_packet["items"]}
    new_items = {i["trial_id"]: i for i in packet["items"]}
    rows = []
    for arm, tasks in report["raw_trials"].items():
        for task in tasks:
            for trial in task["repetitions"]:
                identifier = trial["trial_id"]
                original = trial["answer"]
                old = old_items[identifier]
                new = new_items[identifier]
                old_map = old_packet["audit_only_citation_map"][identifier]
                old_reverse = {original: neutral for neutral, original in old_map.items()}
                expected = TAGS.sub(lambda m: f"[{old_reverse.get(m[1], m[1])}]", original)
                new_map = packet["audit_only_citation_map"][identifier]
                new_reverse = {original: neutral for neutral, original in new_map.items()}
                source = {c["tag"]: c for c in trial["citations"]}
                projected = {c["tag"]: c for c in new["citations"]}
                integrity = new["answer"] == TAGS.sub(
                    lambda m: f"[{new_reverse.get(m[1], m[1])}]", original)
                for tag in set(TAGS.findall(original)) & source.keys():
                    citation = projected[new_reverse[tag]]
                    integrity &= all(citation.get(k) == value for k, value in source[tag].items()
                                     if k not in {"tag", "level", "title", "uid", "source_type"})
                    integrity &= bool(citation.get("text"))
                payload = {"item": campaign_review._semantic_review_item(new),
                           "rubric": {"response_schema": campaign_review.ANSWER_SCHEMA,
                                      "judge_guidance": campaign_review.JUDGE_GUIDANCE}}
                rows.append({
                    "trial_id": identifier, "task_id": task["id"], "variant": arm,
                    "repetition": trial["repetition"], "raw_trial_hash": trial["raw_trial_hash"],
                    "old_input_hash": old["input_hash"], "repaired_input_hash": new["input_hash"],
                    "old_citation_mapping_broken": expected != old["answer"],
                    "old_cited_bodies_missing": sum(
                        not c.get("text") for c in old["citations"][:len(trial["citations"])]),
                    "production_turn_tag_used": bool(re.search(r"\[T\d+\]", original)),
                    "historical_citation_valid": trial["citation_valid"],
                    "historical_complete": trial.get("complete"),
                    "historical_verdict": trial.get("answer_verdict"),
                    "repaired_mapping_and_bodies_valid": bool(integrity),
                    "estimated_repaired_review_tokens": estimated_tokens(
                        campaign_review.SYSTEM + json.dumps(payload, ensure_ascii=False)) + 32,
                })
    material_lengths = []
    for case in report["materials"]["cases"]:
        lengths = {arm: len(m["text"]) for arm, m in case["materials"].items()}
        material_lengths.append({"case_id": case["case_id"], "characters": lengths,
                                 "card_over_raw_pct": (lengths["structured_card"] /
                                                       lengths["raw_session"] - 1) * 100})
    if digest(report) != before or packet["run_hash"] != report["run_hash"]:
        raise ValueError("audit changed frozen answers")
    return {
        "schema_version": 1, "status": "invalid_review", "scope": "review_only",
        "reason": "citation_remap_and_missing_source_bodies",
        "run_hash": report["run_hash"], "old_packet_hash": old_packet["packet_hash"],
        "repaired_packet_hash": packet["packet_hash"], "raw_answers_unchanged": True,
        "model_requests": 0, "rescored": False, "trial_count": len(rows),
        "mapping_broken_trials": sum(r["old_citation_mapping_broken"] for r in rows),
        "missing_body_trials": sum(r["old_cited_bodies_missing"] > 0 for r in rows),
        "turn_tag_trials": sum(r["production_turn_tag_used"] for r in rows),
        "historical_all_points_met_but_incorrect": sum(
            r["historical_complete"] == 1 and r["historical_verdict"] == "incorrect" for r in rows),
        "repaired_mapping_passed": sum(r["repaired_mapping_and_bodies_valid"] for r in rows),
        "material_lengths": material_lengths, "trials": rows,
        "boundary": "Historical semantic scores are invalid evidence of benefit or harm. "
                    "The repaired packet has not been rescored. Character lengths are not model tokens. "
                    "Frozen answers and gold are unchanged; no human review is claimed.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    db = args.campaign.resolve() / "ledger.sqlite3"
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
        record = conn.execute("SELECT result_json, result_hash FROM campaign_review_batches WHERE id=?",
                              (args.batch,)).fetchone()
    if not record:
        raise ValueError("review batch not found")
    batch = json.loads(record[0])
    if digest(batch) != record[1]:
        raise ValueError("review batch checksum mismatch")
    report = batch["reviewed_reports"]["knowledge-reuse"]
    result = audit(report)
    published = json.loads(args.report.read_text(encoding="utf-8"))
    if published["run_hash"] != result["run_hash"]:
        raise ValueError("published report does not bind these frozen answers")
    result.update(batch_id=args.batch, historical_report=str(args.report),
                  historical_report_sha256=digest(args.report.read_bytes()))
    result["audit_hash"] = digest(result)
    atomic_text(args.out, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in {"trials", "material_lengths"}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
