"""Replay frozen reviewer evidence, keeping original exposure and truth separate."""
from __future__ import annotations

import copy
import json
import re
import sqlite3
from pathlib import Path

from ..contracts import digest
from . import closeout, protocol

VERSION = "review-evidence-repair-v1"
PROJECTION = "complete-evidence-deduplicated-v1"
IDENTITY = ("repository", "revision", "knowledge_set_id", "source_content_hash", "build_config_hash")


class FrozenSource:
    def __init__(self, spec, identity):
        self.root = Path(spec["root"]).resolve()
        raw = (self.root / "manifest.json").read_bytes()
        if digest(raw) != spec["manifest_hash"]:
            raise ValueError("frozen source manifest changed")
        self.manifest = json.loads(raw)
        if any(self.manifest.get(k) != identity.get(k) or not identity.get(k) for k in IDENTITY):
            raise ValueError("frozen source identity mismatch")
        self.identity = {k: identity[k] for k in IDENTITY}
        self._checked("facts.db")
        self.conn = sqlite3.connect((self.root / "facts.db").as_uri() + "?mode=ro&immutable=1", uri=True)
        self.conn.row_factory = sqlite3.Row
        meta = dict(self.conn.execute("SELECT key,value FROM meta"))
        if any(meta.get("repository_id" if k == "repository" else k) != v for k, v in self.identity.items()):
            self.conn.close()
            raise ValueError("frozen database identity mismatch")
        files = self.manifest.get("source_files") or {}
        if digest(files) != self.identity["source_content_hash"] or any(
                self.manifest["artifacts"].get("source/" + path) != sha for path, sha in files.items()):
            self.conn.close()
            raise ValueError("frozen source_files identity mismatch")

    def _checked(self, relative):
        path = (self.root / relative).resolve()
        if self.root not in path.parents or not path.is_file():
            raise ValueError("frozen evidence path escaped or is missing")
        raw = path.read_bytes()
        if digest(raw) != self.manifest["artifacts"].get(relative):
            raise ValueError("frozen evidence artifact changed")
        return raw

    def excerpt(self, origin, *, verify_anchor=True):
        if any(origin.get(k) != self.identity[k] for k in IDENTITY):
            raise ValueError("citation has a different frozen source identity")
        path = origin.get("path")
        if not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts:
            raise ValueError("invalid frozen citation path")
        if (self.root / "source").resolve() not in (self.root / "source" / path).resolve().parents:
            raise ValueError("frozen citation escaped source root")
        raw = self._checked("source/" + path)
        lines = raw.splitlines(keepends=True)
        start, end = origin.get("line_start"), origin.get("line_end")
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
            raise ValueError("frozen citation range is invalid")
        if verify_anchor and origin.get("usr"):
            nodes = self.conn.execute("SELECT * FROM node WHERE kind='function' AND is_definition=1 AND usr=? AND path=? AND definition_hash=?",
                                      (origin["usr"], path, origin.get("definition_hash"))).fetchall()
            if len(nodes) != 1:
                raise ValueError("frozen citation USR is not a unique definition")
            node = nodes[0]
            if (type(node["line_start"]) is not int or type(node["line_end"]) is not int
                    or not 1 <= node["line_start"] <= node["line_end"] <= len(lines)
                    or not node["line_start"] <= start <= end <= node["line_end"]
                    or digest(b"".join(lines[node["line_start"]-1:node["line_end"]])) != node["definition_hash"]):
                raise ValueError("frozen definition bytes or subrange changed")
        elif verify_anchor:
            if not self.conn.execute("SELECT 1 FROM node WHERE kind='file' AND path=?", (path,)).fetchone():
                raise ValueError("frozen file range has no file identity")
        body = b"".join(lines[start-1:end])
        return {**self.identity, "path": path, "line_start": start, "line_end": end,
                "file_sha256": digest(raw), "range_sha256": digest(body),
                "text": body.decode("utf-8"), "visibility": "reviewer_only_verification",
                "boundary": "Source verifying a citation; not proof the candidate saw this whole range."}


def apply_patch_to_packet(report, packet, spec):
    """Recompute all new text from frozen inputs; never accept supplied answers/gold."""
    if spec.get("version") != VERSION or spec.get("run_hash") != packet["run_hash"]:
        raise ValueError("review evidence repair identity changed")
    selected = set(spec["selected_trial_ids"])
    if not selected <= {i["trial_id"] for i in packet["items"]}:
        raise ValueError("review evidence repair contains unknown trials")
    # This helper expects the unmodified derived packet, not a recursive patch.
    traced = closeout.restore_traced_bodies(report, packet, verify_projection=False)
    traced_items = {i["trial_id"]: i for i in traced["items"]}
    result = copy.deepcopy(packet)
    source = FrozenSource(spec["snapshot"], {**report["identity"], "repository": report["repository"]})
    try:
        for index, item in enumerate(result["items"]):
            if item["trial_id"] not in selected:
                continue
            item = copy.deepcopy(traced_items[item["trial_id"]])
            tags = set(re.findall(r"\[(E\d+)\]", item.get("answer") or ""))
            for row in item["evidence_payload"]:
                origin = row.get("provenance") or {}
                if row.get("kind") == "frozen_tool_excerpt":
                    row["visibility"] = "original_tool_output"
                if row.get("tag") in tags and origin.get("path"):
                    row["verification_source"] = source.excerpt(origin)
                    from .task_eval import REFUSAL_TYPE
                    if item.get("task_type") == REFUSAL_TYPE:
                        row["verification_source"].pop("text")
                        row["verification_source"]["boundary"] = "Identity/range verified only. The frozen repository-scope oracle, not these function bodies, is the refusal evidence."
                    # Explicit kind prevents the old metadata-only anchor table.
                    row.setdefault("kind", "verified_citation_source")
            if item["task_id"] in spec.get("supplement_tasks", []):
                item["reviewer_verification"] = [source.excerpt(
                    {**source.identity, "path": "cJSON.c", "line_start": a, "line_end": b},
                    verify_anchor=False) for a, b in ((133, 154), (1951, 1991))]
                item["verification_note"] = "Same supplemental source for every detach trial; gold unchanged. Reviewer-only, not an added candidate citation."
            item["citations"] = copy.deepcopy(item["evidence_payload"])
            item["input_hash"] = digest({k: v for k, v in item.items() if k != "input_hash"})
            result["items"][index] = item
    finally:
        source.conn.close()
    result["citation_projection_version"] = VERSION
    result["evidence_repair_hash"] = digest(spec)
    result["packet_hash"] = digest({k: v for k, v in result.items() if k != "packet_hash"})
    return result


def compact_c(text):
    """Conservative presentation only; preserve line-spliced input verbatim."""
    if re.search(r"\\[ \t]*(?:\r\n|\n|\r)", text) or "\r" in text:
        return text
    # Protect strings/chars while replacing comments with whitespace, never
    # join return/**/42 or +/**/+ into different C tokens.
    tokens = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*[\s\S]*?\*/|//[^\n]*')
    value = tokens.sub(lambda m: " " if m[0].startswith(("/*", "//")) else m[0], text)
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


def judge_view(item):
    """Deduplicate identical source bodies, keep all cited bodies and gold."""
    from .campaign_review import _semantic_review_item
    value = _semantic_review_item(item)
    # Use the original source bytes rather than the legacy comment stripper.
    originals = {row.get("tag"): row for row in item["evidence_payload"]}
    bodies = {}
    def body_ref(text, *, code=False):
        rendered = compact_c(text) if code else text
        key = digest(rendered)[:16]
        if key in bodies and bodies[key]["text"] != rendered:
            raise ValueError("source body hash collision")
        entry = bodies.setdefault(key, {"text": rendered, "original_hashes": [], "code_compacted": code})
        if digest(text) not in entry["original_hashes"]:
            entry["original_hashes"].append(digest(text))
        return {"$ref": "#/bodies/" + key}
    for row in value["evidence_payload"]["other_evidence"]:
        original = originals.get(row.get("tag"), {})
        if isinstance(original.get("text"), str):
            fragments = (original.get("restoration") or {}).get("fragments", [])
            is_read = bool(fragments) and all(f.get("tool") == "code_read" for f in fragments)
            text = original["text"]
            if is_read:
                text = re.sub(r"(?m)^\d+ \| ?", "", text)
            row["text"] = body_ref(text, code=original.get("kind") == "pinned_source" or is_read)
        verification = row.get("verification_source")
        if verification and "text" in verification:
            verification["text"] = body_ref(verification["text"], code=True)
        # Hashes/provenance are kept in the bound original packet, once.
        row.pop("restoration", None)
        row.pop("representation", None)
    # Factor repeated snapshot identity without dropping unique ranges/entities.
    provenances = {}
    for row in value["evidence_payload"]["other_evidence"]:
        for key in ("provenance", "verification_source"):
            origin = row.get(key)
            if not isinstance(origin, dict):
                continue
            identity = {k: origin.pop(k) for k in IDENTITY if k in origin}
            if identity:
                code = digest(identity)[:16]
                provenances[code] = identity
                origin["snapshot"] = {"$ref": "#/snapshots/" + code}
            # Keep full hashes once in the immutable packet, not repeated to the judge.
            for hash_key in ("file_sha256", "range_sha256"):
                origin.pop(hash_key, None)
    value["snapshots"] = provenances
    for row in value.get("reviewer_verification", []):
        row["text"] = body_ref(row["text"], code=True)
    # Lossless substring factoring: keep one complete body, refer to exact
    # character intervals for duplicate excerpts (never summaries).
    ordered = sorted(bodies, key=lambda k: (-len(bodies[k]["text"]), k))
    full = []
    for key in ordered:
        text = bodies[key]["text"]
        parent = next((k for k in full if text and text in bodies[k]["text"]), None)
        if parent:
            start = bodies[parent]["text"].index(text)
            bodies[key].pop("text")
            bodies[key]["substring"] = {"body": parent, "start_character": start,
                                        "end_character": start + len(text)}
        else:
            full.append(key)
    value["bodies"] = bodies
    value["projection"]["version"] = PROJECTION
    value["projection"]["boundary"] = "All cited evidence and frozen gold retained; supplemental source is reviewer-only. Identical bodies stored once, not silently truncated."
    return value


def prepare_reuse(directory, out):
    """Freeze the first independent repair job without making model requests."""
    from . import campaign_review as review
    from ..contracts import estimated_tokens, INPUT_BUDGET
    from ..publication import atomic_text
    directory, out = Path(directory).resolve(), Path(out).resolve()
    with sqlite3.connect((directory / "ledger.sqlite3").as_uri() + "?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT id,binding_json,result_json,result_hash FROM campaign_review_batches").fetchall()
        matches = [(bid, json.loads(binding), json.loads(result), sha) for bid, binding, result, sha in rows
                   if json.loads(binding).get("attempt_id") == closeout.BASE_ATTEMPT]
        if len(matches) != 1:
            raise ValueError("base review does not resolve uniquely")
        bid, binding, result, sha = matches[0]
        if digest(result) != sha:
            raise ValueError("base review changed")
        initial = conn.execute("SELECT count(*) FROM requests").fetchone()[0]
    report = result["reviewed_reports"]["knowledge-reuse"]
    packet = rubric_packet = __import__("codeatlas.eval.rubric", fromlist=["blind_packet"]).blind_packet(copy.deepcopy(report))
    from . import rubric
    sizes = []
    for item in packet["items"]:
        # Reserve a complete worst-case matrix for both peers; no free text is
        # duplicated in the arbiter context in this projection.
        peer = {"review_hash": "f" * 64, "point_reviews": {
            p["id"]: "unresolved" for p in item["answer_key"]["expected_points"]},
            "false_claim_present": False, "citation_relation": "fabricated_content",
            "verdict": "reasonable_refusal", "completeness": 1.0}
        payload = {"item": judge_view(item), "rubric": {**rubric.RUBRIC,
            "judge_guidance": review.JUDGE_GUIDANCE, "response_schema": review.ANSWER_SCHEMA},
            "independent_reviews": [peer, peer]}
        size = estimated_tokens(review.SYSTEM + json.dumps(payload, ensure_ascii=False, sort_keys=True)) + 32
        sizes.append(size)
    if max(sizes) > INPUT_BUDGET:
        raise ValueError(f"complete reuse arbitration input exceeds budget: {max(sizes)}")
    scope_payload = {"version": "closeout-800-v1", "base_batch_id": bid,
        "base_result_hash": sha, "run_hashes": binding["run_hashes"],
        "max_requests": 800, "global_max_requests": 5000,
        "boundary": "Repair old Wiki and reuse reviews only; no new Agent answers or gold changes."}
    scope = {"scope_id": "closeout-evidence-repair-v1", "max_requests": 800, "manifest_hash": digest(scope_payload)}
    selection = {"base_batch_id": bid, "jobs": ["knowledge-reuse"],
        "run_hashes": {"knowledge-reuse": packet["run_hash"]},
        "packet_hashes": {"knowledge-reuse": packet["packet_hash"]},
        "projection_version": PROJECTION, "max_estimated_arbitration_input": max(sizes),
        "initial_requests": initial, "calibration": "eval/review_calibration_v4b.yaml",
        "calibration_sha256": digest(Path("eval/review_calibration_v4b.yaml").read_bytes()),
        "source_repair_runtime_hash": digest(Path(__file__).read_bytes())}
    selection["selection_hash"] = digest(selection)
    out.mkdir(parents=True, exist_ok=True)
    for name, value in (("reuse-selection.json", selection), ("scope.json", scope), ("scope-authorization.json", scope_payload)):
        path = out / name
        text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        if path.exists() and path.read_text() != text:
            raise ValueError("repair preflight already frozen")
        if not path.exists():
            atomic_text(path, text)
    return {"trials": len(packet["items"]), "max_estimated_arbitration_input": max(sizes),
        "selection": str(out / "reuse-selection.json"), "scope": str(out / "scope.json"), "new_requests": 0}
