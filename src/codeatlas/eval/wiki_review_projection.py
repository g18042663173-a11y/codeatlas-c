"""Wiki-only rendering; preserve frozen review inputs and first repair batch."""
import copy
import difflib
import json
import re
from pathlib import Path

from .evidence_repair import judge_view

VERSION = "complete-wiki-evidence-v2"


def expand_bodies(bodies):
    expanded, visiting = {}, set()
    def resolve(key):
        if key in visiting or key not in bodies:
            raise ValueError("invalid or cyclic body reference")
        if key in expanded:
            return expanded[key]
        visiting.add(key)
        body = bodies[key]
        def part(value):
            if isinstance(value, str):
                return value
            text = resolve(value["body"])
            start, end = value["start_character"], value["end_character"]
            if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(text):
                raise ValueError("body substring is out of range")
            return text[start:end]
        forms = set(body) & {"text", "substring", "parts"}
        if len(forms) != 1:
            raise ValueError("ambiguous body encoding")
        text = body["text"] if "text" in forms else part(body["substring"]) if "substring" in forms else "".join(map(part, body["parts"]))
        visiting.remove(key)
        expanded[key] = text
        return text
    for key in bodies:
        resolve(key)
    return expanded


def view(item):
    rendered = copy.deepcopy(item)
    for row in rendered.get("evidence_payload", []):
        fragments = (row.get("restoration") or {}).get("fragments", [])
        if fragments and all(f.get("tool") == "code_read" for f in fragments):
            row["text"] = re.sub(r"(?m)^[ \t]+(?=\d+ \|)", "", row["text"])
    result = judge_view(rendered)
    # Integrity was checked against the immutable source, and remains bound by
    # original input_hash. Hash strings are not textual semantic evidence.
    for row in result["evidence_payload"]["other_evidence"]:
        row.pop("original_text_hash", None)
        origin = row.get("provenance")
        if isinstance(origin, dict):
            origin.pop("definition_hash", None)
            if origin.get("path"):
                origin.pop("source_ref", None)
        verification = row.get("verification_source")
        if verification:
            verification.pop("boundary", None)
    for body in result["bodies"].values():
        body.pop("original_hashes", None)
    # Factor overlapping multi-line excerpts, not just exact full substrings.
    # A part always references an earlier FULL body; reconstruction is exact.
    full = []
    bodies = result["bodies"]
    before_factoring = expand_bodies(bodies)
    for key in sorted(bodies, key=lambda k: (-len(bodies[k].get("text", "")), k)):
        body = bodies[key]
        if "text" not in body:
            continue
        text = body["text"]
        lines = text.splitlines(keepends=True)
        candidates = []
        for parent in full:
            other = bodies[parent]["text"].splitlines(keepends=True)
            for match in difflib.SequenceMatcher(None, lines, other, autojunk=False).get_matching_blocks():
                content = "".join(lines[match.a:match.a+match.size])
                if len(content) >= 180:
                    candidates.append((sum(map(len, lines[:match.a])), len(content), parent,
                                       sum(map(len, other[:match.b]))))
        cursor, parts = 0, []
        for start, length, parent, offset in sorted(candidates, key=lambda c: (c[0], -c[1], c[2])):
            if start < cursor:
                continue
            if start > cursor:
                parts.append(text[cursor:start])
            parts.append({"body": parent, "start_character": offset, "end_character": offset+length})
            cursor = start + length
        if parts:
            if cursor < len(text):
                parts.append(text[cursor:])
            body.pop("text")
            body["parts"] = parts
        else:
            full.append(key)
    if expand_bodies(bodies) != before_factoring:
        raise ValueError("body factoring changed evidence")
    result["body_encoding"] = "text is literal; parts concatenate literals and body ranges; substring ranges use Python character offsets into recursively expanded body, end exclusive. All references checked acyclic and reconstruction exact."
    result["projection"]["version"] = VERSION
    result["projection"]["boundary"] += " Source-display line gutters removed; original text/hashes remain in the immutable input packet. Verification-only source never proves candidate exposure."
    return result


def prepare(directory, job_id, out):
    """Freeze risk-based selection and verified inheritance before dispatch."""
    from . import proof_campaign as campaign, campaign_review as review, rubric, closeout, evidence_repair
    from .review_recovery import _stored_batch
    from .review_inheritance import validate_inheritance
    from ..contracts import digest, estimated_tokens, INPUT_BUDGET
    from ..publication import atomic_text
    _, plan, ledger = campaign._load(directory)
    bid, binding, stored = _stored_batch(ledger, closeout.BASE_ATTEMPT)
    if job_id not in {"wiki-cjson", "wiki-lwip"}:
        raise ValueError("only frozen Wiki proof jobs may use this repair")
    report = copy.deepcopy(stored["reviewed_reports"][job_id])
    audit = closeout.audit_wiki(report)
    ids = sorted(row["trial_id"] for row in audit["trials"] if row["incomplete_cited_ranges"]
                 or row["task_id"] == "ho-cjson-flow-detach-list-rewire")
    job = next(j for j in plan["jobs"] if j["id"] == job_id)
    database = Path(job["database"])
    snapshot = database.parent / (database.stem + "-sets") / report["identity"]["knowledge_set_id"]
    spec = {"version": evidence_repair.VERSION, "run_hash": report["run_hash"],
        "selected_trial_ids": ids, "snapshot": {"root": str(snapshot),
            "manifest_hash": digest((snapshot / "manifest.json").read_bytes())},
        "supplement_tasks": ["ho-cjson-flow-detach-list-rewire"] if job_id == "wiki-cjson" else []}
    report["review_evidence"] = spec
    packet = rubric.blind_packet(report)
    sizes = {}
    for item in packet["items"]:
        if item["trial_id"] not in ids:
            continue
        peer = {"review_hash": "f" * 64, "point_reviews": {
            p["id"]: "unresolved" for p in item["answer_key"]["expected_points"]},
            "false_claim_present": False, "citation_relation": "fabricated_content",
            "verdict": "reasonable_refusal", "completeness": 1.0}
        payload = {"item": view(item), "rubric": {**rubric.RUBRIC,
            "judge_guidance": review.JUDGE_GUIDANCE, "response_schema": review.ANSWER_SCHEMA},
            "independent_reviews": [peer, peer]}
        sizes[item["trial_id"]] = estimated_tokens(review.SYSTEM + json.dumps(payload, ensure_ascii=False, sort_keys=True)) + 32
    inherited, inheritance = validate_inheritance(ledger, bid, binding, stored, {job_id: packet}, ids)
    value = {"base_batch_id": bid, "jobs": [job_id], "run_hashes": {job_id: packet["run_hash"]},
        "packet_hashes": {job_id: packet["packet_hash"]}, "trial_ids": {job_id: ids},
        "evidence_repairs": {job_id: spec}, "inheritance_audit_hash": inheritance["audit_hash"],
        "projection_version": VERSION, "source_repair_runtime_hash": digest(Path(evidence_repair.__file__).read_bytes()),
        "wiki_projection_runtime_hash": digest(Path(__file__).read_bytes()),
        "calibration_sha256": digest(Path("eval/review_calibration_v4b.yaml").read_bytes()),
        "estimated_arbitration_inputs": sizes, "input_budget": INPUT_BUDGET,
        "oversize_trial_ids": sorted(k for k, v in sizes.items() if v > INPUT_BUDGET),
        "boundary": "Selection by evidence risk, not old scores; original answers/gold unchanged. Inherited scores retain their old request views."}
    value["selection_hash"] = digest(value)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for name, data in ((job_id + "-selection.json", value), (job_id + "-inheritance.json", inheritance)):
        target = out / name
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        if target.exists() and target.read_text() != text:
            raise ValueError("Wiki selection already frozen")
        if not target.exists():
            atomic_text(target, text)
    return {"selected": len(ids), "inherited": len(inherited[job_id]),
        "max_arbitration_input": max(sizes.values()), "oversize": len(value["oversize_trial_ids"]),
        "selection": str(out / (job_id + "-selection.json")), "new_requests": 0}
