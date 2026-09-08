"""Read-only proof of historical scores; never relabel them as new reviews."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from ..contracts import digest
from . import campaign_review as review, protocol, rubric


def _require(condition, message):
    if not condition:
        raise ValueError("review inheritance: " + message)


def _indexed(rows, key):
    result = {row[key]: row for row in rows}
    _require(len(result) == len(rows), "duplicate " + key)
    return result


class _Sources:
    def __init__(self, conn, base_id):
        self.conn = conn
        self.index = {row["id"]: dict(row) for row in conn.execute(
            "SELECT id,binding_json,result_hash FROM campaign_review_batches")}
        self.bindings = {key: json.loads(row["binding_json"]) for key, row in self.index.items()}
        self.allowed, self.results, self.certificates = set(), {}, {}
        pending = [base_id]
        while pending:
            key = pending.pop()
            if key in self.allowed:
                continue
            _require(key in self.index, "missing source batch")
            self.allowed.add(key)
            binding = self.bindings[key]
            _require(key == digest({k: v for k, v in binding.items() if k not in {"agents", "sessions"}}),
                     "source batch identity differs from binding")
            pending.extend(binding[field] for field in ("base_batch_id", "qualified_by_batch_id")
                           if binding.get(field))
            attempt = binding.get("reuse_replacements_from")
            if attempt:
                matches = [bid for bid, item in self.bindings.items() if item.get("attempt_id") == attempt]
                _require(len(matches) == 1, "replacement source attempt is ambiguous")
                pending.extend(matches)

    def result(self, key):
        if key not in self.results:
            row = self.conn.execute("SELECT result_json FROM campaign_review_batches WHERE id=?", (key,)).fetchone()
            _require(row is not None and row[0], "source batch lacks durable result")
            value = json.loads(row[0])
            _require(digest(value) == self.index[key]["result_hash"], "source batch result checksum changed")
            _require(value.get("binding") == self.bindings[key], "source result/binding mismatch")
            self.results[key] = value
        return self.results[key]

    def locate(self, profile, call_id):
        role = profile.get("reviewer")
        matches = []
        for key in sorted(self.allowed):
            binding = self.bindings[key]
            if ((binding.get("agents") or {}).get(role) != profile.get("agent")
                    or (binding.get("sessions") or {}).get(role) != profile.get("session_id")):
                continue
            row = self.conn.execute(
                "SELECT * FROM campaign_review_calls WHERE batch_id=? AND role=? AND call_id=?",
                (key, role, call_id)).fetchall()
            matches.extend((key, dict(item)) for item in row)
        _require(len(matches) == 1, "score must resolve to exactly one source call")
        key, row = matches[0]
        binding = self.bindings[key]
        self.result(key)
        _require(all(profile.get(field) == binding.get(target) for field, target in
                     (("provider", "provider"), ("model", "judge_model"), ("prompt_hash", "prompt_hash"))),
                 "score provider/model/prompt differs from source binding")
        _require(binding.get("prompt_hash") == digest(review.SYSTEM)
                 and binding.get("schema_hash") == digest([review.ANSWER_SCHEMA, review.CALIBRATION_SCHEMA])
                 and binding.get("judge_guidance_hash") == digest(review.JUDGE_GUIDANCE),
                 "unsupported historical prompt/schema/guidance; preserve its original implementation")
        _require(row["id"] == digest([key, role, call_id]), "source call identity changed")
        return key, binding, row

    def call(self, profile, call_id, payload, item, *, calibration=False):
        key, binding, row = self.locate(profile, call_id)
        _require(row["state"] == "completed" and row.get("result_json"), "source call is not completed")
        call = json.loads(row["result_json"])
        _require(digest(call) == row["result_hash"], "source call result checksum changed")
        expected = digest([review.SYSTEM, json.dumps(payload, ensure_ascii=False, sort_keys=True)])
        _require(row["request_hash"] == call.get("request_hash") == expected, "actual request hash differs")
        _require(call.get("state") == "completed" and isinstance(call.get("parsed"), dict)
                 and isinstance(call.get("raw_response"), str) and not call.get("error_type"),
                 "source call has no valid model result")
        _require(call.get("response_hash") == digest(call["raw_response"]), "raw response checksum changed")
        parsed = review._validate(review._strict_json(call["raw_response"]), item, calibration=calibration)
        _require(parsed == call["parsed"], "parsed result differs from raw model response")
        return key, binding, row, call

    def qualification(self, source_id, role, profile):
        binding, result = self.bindings[source_id], self.result(source_id)
        owner, field = source_id, "judge_calibration"
        model_scoped = role in {"judge-a", "judge-b"} and bool(binding.get("qualified_by_batch_id"))
        if model_scoped:
            owner = binding["qualified_by_batch_id"]
            result = self.result(owner)
        elif role == "replacement":
            field = "replacement_calibration"
        elif role == "recovery-arbiter":
            field = "arbiter_calibration"
        certificate = copy.deepcopy(result.get(field))
        _require(isinstance(certificate, dict), "missing original qualification certificate")
        source_certificate_hash = digest(certificate)
        if model_scoped:
            # The qualification batch may later append its own arbiter. Its
            # original model attestation is explicitly the first two runs.
            if len(certificate.get("review_runs") or []) > 2:
                certificate["review_runs"] = certificate["review_runs"][:2]
                certificate["reviewer_count"] = 2
                certificate["review_results_hash"] = digest(certificate["review_runs"])
            _require(digest(certificate) == binding.get("qualification_attestation_hash"),
                     "model qualification hash differs from source binding")
        cache_key = digest([owner, field, certificate])
        if cache_key not in self.certificates:
            root = Path(__file__).resolve().parents[3]
            path = (root / str(certificate.get("calibration_path", ""))).resolve()
            _require(root in path.parents, "calibration path escaped project")
            calibration = rubric.load_calibration(path)
            runs = certificate.get("review_runs") or []
            _require(certificate.get("passed") is True and runs
                     and certificate.get("review_results_hash") == digest(runs), "qualification did not pass")
            raw_runs, calls = [], []
            calibration_items = _indexed(review._calibration_items(calibration), "id")
            for run in runs:
                reviewer = run["reviewer"]
                observed = _indexed(run.get("items") or [], "id")
                _require(set(observed) == set(calibration_items), "qualification samples differ")
                raw_items, hashes = [], []
                for identifier, item in calibration_items.items():
                    payload = {"item": item, "rubric": {**calibration["rubric_contract"],
                        "judge_guidance": review.JUDGE_GUIDANCE, "response_schema": review.CALIBRATION_SCHEMA}}
                    bid, _binding, row, call = self.call(
                        reviewer, "calibration:" + identifier, payload, item, calibration=True)
                    _require(all(call["parsed"].get(k) == v for k, v in observed[identifier]["observed"].items()),
                             "qualification observations differ from original call")
                    raw_items.append({"id": identifier, **call["parsed"]})
                    hashes.append(call["request_hash"])
                    calls.append({"source_batch_id": bid, "call_id": row["id"],
                                  "request_hash": row["request_hash"], "result_hash": row["result_hash"]})
                _require(reviewer.get("request_hashes") == hashes, "qualification request list changed")
                raw_runs.append({"reviewer": reviewer, "items": raw_items})
            recomputed = rubric.score_calibration(path, {"reviews": raw_runs}, minimum_reviews=1)
            _require(recomputed == certificate, "qualification metrics/policy differ from source calls")
            self.certificates[cache_key] = {"source_batch_id": owner, "field": field,
                "source_certificate_hash": source_certificate_hash,
                "model_scoped_first_two": model_scoped,
                "certificate_hash": digest(certificate), "certificate": copy.deepcopy(certificate), "calls": calls}
        reviewers = [run["reviewer"] for run in certificate["review_runs"]]
        fields = ("provider", "model", "prompt_hash") if model_scoped else ("provider", "model", "prompt_hash", "agent")
        _require(any(all(person.get(k) == profile.get(k) for k in fields) for person in reviewers),
                 "qualification does not cover this historical scorer")
        if model_scoped:
            _require(certificate.get("reviewer_count") == 2, "model qualification requires two original reviewers")
        return cache_key

    def score(self, score, item, group, slot=None):
        role, identifier = score.get("reviewer"), item["trial_id"]
        if slot is not None:
            _require(role in {"judge-a", "judge-b", "replacement"}, "unsupported independent scorer role")
            call_id = f"replacement:{identifier}:{slot}" if role == "replacement" else identifier
            input_hash = item["input_hash"]
        else:
            _require(role in {"arbiter", "recovery-arbiter"}, "unsupported arbiter role")
            call_id = "arbitration:" + identifier if role == "recovery-arbiter" else identifier
            input_hash = digest({"packet_input_hash": item["input_hash"],
                                 "independent_reviews": group["independent_reviews"]})
        source_id, binding, _row = self.locate(score, call_id)
        projection = binding.get("projection_version")
        _require(projection in (None, review.SEMANTIC_REVIEW_PROJECTION), "unsupported historical projection")
        payload = {"item": review._judge_item(item, projection), "rubric": {**rubric.RUBRIC,
            "judge_guidance": review.JUDGE_GUIDANCE, "response_schema": review.ANSWER_SCHEMA}}
        if slot is None:
            from .review_recovery import _compact_arbiter_view
            peer = _compact_arbiter_view if role == "recovery-arbiter" else review._arbiter_view
            payload["independent_reviews"] = [peer(row) for row in group["independent_reviews"]]
        source_id, binding, row, call = self.call(score, call_id, payload, item)
        rebuilt = review._score_record(binding, role, input_hash, call)
        if slot is None:
            rebuilt.update(peer_reviews_visible=True, independent=False,
                           input_hash_scope="packet_input_hash_and_original_reviews",
                           resolves_review_hashes=[digest(part) for part in group["independent_reviews"]])
        _require(rebuilt == score, "score fields differ from original parsed call/profile")
        certificate = self.qualification(source_id, role, score)
        return {"source_batch_id": source_id, "role": role, "call_id": call_id,
                "ledger_call_id": row["id"], "request_hash": row["request_hash"],
                "result_hash": row["result_hash"], "score_hash": digest(score),
                "qualification_proof": certificate}


def validate_inheritance(ledger, base_batch_id, base_binding, base_result, packets, selected_trial_ids):
    """Return ``(unchanged_groups_by_job, audit)`` or reject before any dispatch.

    Only supported historical request encodings are reconstructed. New evidence
    projections cannot silently stand in for the original scoring requests.
    The caller must hold its campaign lock while composing/dispatching a batch.
    """
    inherited, entries = {}, []
    selected = set(selected_trial_ids)
    with ledger.connect() as conn:
        sources = _Sources(conn, base_batch_id)
        stored = sources.result(base_batch_id)
        _require(sources.bindings[base_batch_id] == base_binding and stored == base_result,
                 "supplied base differs from durable batch")
        all_ids = set()
        for job, packet in packets.items():
            _require(job in stored.get("reviewed_reports", {}) and job in stored.get("reviews", {}),
                     "job absent from historical batch")
            report = stored["reviewed_reports"][job]
            old_packet = report["blind_review"]
            _require(protocol.run_hash(report) == report["run_hash"] == packet.get("run_hash"),
                     "raw answer run changed")
            _require(packet.get("packet_hash") == digest({k: v for k, v in packet.items() if k != "packet_hash"}),
                     "new packet checksum changed")
            old_items = _indexed(old_packet["items"], "trial_id")
            new_items = _indexed(packet["items"], "trial_id")
            groups = _indexed(stored["reviews"][job]["items"], "trial_id")
            _require(set(old_items) == set(new_items) == set(groups), "trial coverage differs")
            _require(not all_ids.intersection(new_items), "trial appears in multiple jobs")
            all_ids.update(new_items)
            inherited[job] = []
            for identifier, item in new_items.items():
                old = old_items[identifier]
                _require(item.get("input_hash") == digest({k: v for k, v in item.items() if k != "input_hash"}),
                         "item checksum changed")
                _require(all(item.get(k) == old.get(k) for k in
                             ("answer", "answer_key", "task_id", "task_type", "question", "refused")),
                         "original answer/gold/question changed")
                if identifier in selected:
                    continue
                _require(item == old, "unselected item differs from original scored input")
                group = groups[identifier]
                scores = group.get("independent_reviews") or []
                _require(len(scores) == 2 and scores[0].get("agent") != scores[1].get("agent"),
                         "historical group lacks two isolated scores")
                proofs = [sources.score(score, old, group, slot) for slot, score in enumerate(scores)]
                if group.get("adjudication") is not None:
                    proofs.append(sources.score(group["adjudication"], old, group))
                inherited[job].append(copy.deepcopy(group))
                entries.append({"job": job, "trial_id": identifier, "input_hash": old["input_hash"],
                                "group_hash": digest(group), "scores": proofs})
        _require(selected <= all_ids, "selected trial absent from packets")
        audit = {"version": "historical-review-inheritance-v1", "base_batch_id": base_batch_id,
            "base_result_hash": sources.index[base_batch_id]["result_hash"], "inherited_trial_count": len(entries),
            "trials": entries, "source_batches": {key: {"binding": copy.deepcopy(sources.bindings[key]),
                "binding_hash": digest(sources.bindings[key]), "result_hash": sources.index[key]["result_hash"]}
                for key in sorted(sources.results)}, "qualifications": sources.certificates,
            "boundary": "Original groups and qualifications retained unchanged; these are not new-projection scores."}
        audit["audit_hash"] = digest(audit)
    return inherited, audit
