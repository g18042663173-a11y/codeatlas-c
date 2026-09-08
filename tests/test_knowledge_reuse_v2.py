"""Schema 2 material contracts; all clients are local fakes, never gateways."""
import copy
import io
import json

import pytest
import yaml

from codeatlas.contracts import digest, estimated_tokens
from codeatlas.eval import knowledge_production as production, knowledge_reuse_eval as reuse


FIELDS = ("symptom", "hypotheses", "conditions", "wrong_explanations",
          "root_cause", "handling", "verification")


@pytest.fixture
def v2_manifest(tmp_path):
    def save(name, value):
        path = tmp_path / name
        path.write_text(value)
        return {"path": name, "sha256": digest(path.read_bytes())}
    facts = {key: [{"text": f"{key}: only when flag is set.", "evidence_tags": [f"T{i}"]}]
             for i, key in enumerate(FIELDS, 1)}
    for rows in facts.values():
        fact = rows[0]
        fact["source_spans"] = [{"tag": fact["evidence_tags"][0], "start": 0, "end": len(fact["text"])}]
    session = {"schema_version": 1, "source_type": "public_synthetic", "repository": "fixture",
               "revision": "fixed", "messages": [
                   {"role": "reviewer", "knowledge_role": "root_cause",
                    "content": fact[0]["text"] + " Repeated context." * 18}
                   for fact in facts.values()]}
    template = {"generic_summary": {"turn_ids": list(range(1, 8))},
                "structured_card": [{"heading": "结论", "turn_ids": list(range(1, 8))}]}
    attachments = [dict(save(f"{kind}.txt", f"Shared {kind}."), kind=kind)
                   for kind in ("source", "harness", "command", "observation")]
    case = {"id": "one", "repository": "fixture", "revision": "fixed",
            "session": save("session.json", json.dumps(session)),
            "template": save("template.yaml", yaml.safe_dump(template)),
            "attachments": attachments, "required_facts": facts}
    manifest = {"schema_version": 2, "artifact_scope": "isolated_experiment",
                "strip_knowledge_role": True, "cases": [case],
                "tasks": [{"id": "q1", "case_id": "one", "question": "What are the conditions?"}]}
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(manifest))
    return path, tmp_path, manifest


def sections_for(case):
    return [{"heading": key, "content": fact["text"], "evidence_tags": fact["evidence_tags"]}
            for key, facts in case["required_facts"].items() for fact in facts]


class FakeProducer:
    model = "same-fixed-model"

    def __init__(self, session_id, calls, sections):
        self.session_id, self.calls, self.sections = session_id, calls, sections
        self.request_count = 0
        self.usage_totals, self.usage_observations = {}, {}

    def _chat(self, system, user, max_tokens, *, json_object=False, complete_input_budget=None):
        assert complete_input_budget == 8000
        self.calls.append((system, user, max_tokens))
        self.request_count += 1
        self.usage_totals = {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}
        self.usage_observations = dict.fromkeys(self.usage_totals, 1)
        return json.dumps({"sections": self.sections})


def make_artifact(v2_manifest, damage=None):
    path, root, _ = v2_manifest
    case = reuse.prepare(path, project_root=root)["cases"][0]
    sections = sections_for(case)
    if damage == "missing_fact":
        sections = sections[:-1]
    elif damage == "oversize":
        sections[0]["content"] += " padding" * 1000
    elif damage == "bad_citation":
        sections[0]["evidence_tags"] = ["E99"]
    calls = []
    artifact = production.produce(path, project_root=root,
                                  client_factory=lambda arm, cid: FakeProducer(arm + cid, calls, sections))
    return artifact, calls


def freeze(artifact):
    ids = [s["id"] for p in artifact["products"] for s in p["sections"]]
    return production.apply_reviews(artifact, {"artifact_hash": artifact["artifact_hash"],
        "independent_reviews": [{"reviewer_kind": "agent", "agent": role, "session_id": role,
                                 "peer_reviews_visible": False,
                                 "items": [{"section_id": sid, "verdict": "supported"} for sid in ids]}
                                for role in ("a", "b")]})


def test_v2_variable_cases_and_questions_without_models(v2_manifest):
    path, root, manifest = v2_manifest
    report = reuse.evaluate(path, project_root=root, runs=1)
    assert report["schema_version"] == 2
    assert report["case_count"] == report["question_count"] == report["task_count"] == 1
    assert report["planned_trial_count"] == 3 and report["executed_trial_count"] == 0
    assert report["protocol"]["case_count"] == 1
    assert "两例" not in report["boundary"] and "short_synthetic_pilot" != report["task_scope"]
    second = copy.deepcopy(manifest["cases"][0]); second["id"] = "two"
    manifest["cases"].append(second)
    manifest["tasks"].extend({"id": f"q{i}", "case_id": "two", "question": "Explain."} for i in (2, 3))
    path.write_text(yaml.safe_dump(manifest))
    report = reuse.evaluate(path, project_root=root, runs=1)
    assert (report["case_count"], report["question_count"], report["planned_trial_count"]) == (2, 3, 9)


@pytest.mark.parametrize("damage", ["field", "text", "citation", "span", "span_bool", "extra_field",
                                    "uncovered_turn", "case_without_question"])
def test_v2_rejects_invalid_source_contract_before_production(v2_manifest, damage):
    path, root, manifest = v2_manifest
    facts = manifest["cases"][0]["required_facts"]
    if damage == "field":
        del facts["conditions"]
    elif damage == "text":
        facts["conditions"][0]["text"] = "A fact absent from source."
    elif damage == "citation":
        facts["conditions"][0]["evidence_tags"] = ["T999"]
    elif damage in {"span", "span_bool"}:
        facts["conditions"][0]["source_spans"][0]["start"] = 1 if damage == "span" else False
    elif damage == "extra_field":
        facts["conditions"][0]["knowledge_role"] = "authority"
    elif damage == "uncovered_turn":
        facts["verification"] = copy.deepcopy(facts["handling"])
    else:
        manifest["cases"].append({**manifest["cases"][0], "id": "unused"})
    path.write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError, match="required|source|case|fact|turn|question"):
        production.produce(path, project_root=root, client_factory=lambda *a: pytest.fail("called model"))


def test_v2_production_enforces_shared_source_model_and_budget(v2_manifest):
    artifact, calls = make_artifact(v2_manifest)
    assert artifact["schema_version"] == 2
    assert artifact["production_cost"]["model_requests"] == 2
    assert artifact["production_cost"]["input_tokens"] == 200
    assert calls[0][0] == calls[1][0] and calls[0][2] == calls[1][2]
    assert calls[0][1].split("公开仓库：", 1)[1] == calls[1][1].split("公开仓库：", 1)[1]
    assert "knowledge_role" not in str(calls) and "What are the conditions?" not in str(calls)
    for product in artifact["products"]:
        meta = product["compression"]
        assert meta["status"] == "compressed"
        assert meta["budget_tokens"] == min(600, meta["raw_session_tokens"] * 3 // 5)
        assert estimated_tokens(product["text"]) <= meta["budget_tokens"]


@pytest.mark.parametrize("damage", ["missing_fact", "oversize", "bad_citation"])
def test_failed_compression_preserves_full_valid_material_and_attempt_cost(v2_manifest, damage):
    artifact, calls = make_artifact(v2_manifest, damage)
    path, root, _ = v2_manifest
    case = reuse.prepare(path, project_root=root)["cases"][0]
    assert len(calls) == 2 and artifact["production_cost"]["input_tokens"] == 200
    for product in artifact["products"]:
        assert product["compression"]["status"] == "compression_infeasible"
        assert product["compression"]["used_full_material"] is True
        assert product["compression"]["candidate_hash"]
        for message in case["messages"]:
            assert message["content"] in product["text"]
    frozen_path = root / "frozen.json"
    frozen_path.write_text(json.dumps(freeze(artifact)))
    prepared = reuse.prepare(path, project_root=root, generated_artifact=frozen_path)
    assert prepared["production_cost"]["input_tokens"] == 200
    for arm in ("generic_summary", "structured_card"):
        assert prepared["cases"][0]["materials"][arm]["compression"]["status"] == "compression_infeasible"


def test_v2_model_mismatch_is_rejected_before_requests(v2_manifest):
    path, root, _ = v2_manifest
    calls = []
    def factory(arm, cid):
        client = FakeProducer(arm + cid, calls, [])
        client.model = arm
        return client
    with pytest.raises(ValueError, match="same model"):
        production.produce(path, project_root=root, client_factory=factory)
    assert calls == []


def test_v2_short_source_budget_uses_sixty_percent_not_just_six_hundred(v2_manifest):
    path, root, manifest = v2_manifest
    item = manifest["cases"][0]["session"]
    session = json.loads((root / item["path"]).read_text())
    for message in session["messages"]:
        message["content"] = message["content"].split(" Repeated context.")[0]
    (root / item["path"]).write_text(json.dumps(session))
    item["sha256"] = digest((root / item["path"]).read_bytes())
    path.write_text(yaml.safe_dump(manifest))
    artifact, _ = make_artifact(v2_manifest)
    assert all(p["compression"]["budget_tokens"] < 600 for p in artifact["products"])
    assert all(p["compression"]["status"] == "compression_infeasible" for p in artifact["products"])


def test_v2_all_answer_arms_keep_common_evidence_same_model_and_no_tools(v2_manifest):
    path, root, _ = v2_manifest
    class AnswerClient:
        model = "one-answer-model"
        def __init__(self):
            self.calls = []
        def _chat(self, system, user, max_tokens, *, complete_input_budget=None):
            assert complete_input_budget == 8000
            self.calls.append((system, user, max_tokens))
            return "Limited to the observed condition [M1] [E1]."
    client = AnswerClient()
    report = reuse.evaluate(path, project_root=root, runs=1, client=client)
    assert report["executed_trial_count"] == 3
    assert len({(s, n) for s, u, n in client.calls}) == 1
    assert len({u.split("共同附件\n", 1)[1] for s, u, n in client.calls}) == 1
    trials = [t for rows in report["raw_trials"].values() for row in rows for t in row["repetitions"]]
    assert all(t["tool_calls"] == 0 and not t["native_planning"] for t in trials)


def test_v2_three_repeat_evaluator_uses_the_real_fixed_case_scope(v2_manifest):
    path, root, _ = v2_manifest
    class AnswerClient:
        model = "one-answer-model"
        transport_retries = 0
        def __init__(self):
            self.calls = []
        def _chat(self, system, user, max_tokens, *, complete_input_budget=None):
            assert complete_input_budget == 8000
            self.calls.append((system, user, max_tokens))
            return "Limited to the observed condition [M1] [E1]."
    client = AnswerClient()
    report = reuse.evaluate(path, project_root=root, runs=3, client=client)
    assert report["task_scope"] == "fixed_public_cases"
    assert report["executed_trial_count"] == 9
    assert report["acceptance"]["gates"]["full_three_repeat_run"] is True


@pytest.mark.parametrize("raw", ["{broken JSON", '{"sections":[]}', '{"sections":[{"heading":"x","content":"x","evidence_tags":["E99"]}]}'])
def test_invalid_candidate_is_retained_and_costed_without_retry(v2_manifest, raw):
    path, root, _ = v2_manifest
    calls = []
    class InvalidProducer(FakeProducer):
        def _chat(self, *args, **kwargs):
            super()._chat(*args, **kwargs)
            return raw
    artifact = production.produce(path, project_root=root,
                                  client_factory=lambda arm, cid: InvalidProducer(arm + cid, calls, []))
    assert len(calls) == 2 and artifact["production_cost"]["output_tokens"] == 60
    assert all(p["candidate"]["raw"] == raw and p["compression"]["saved_tokens"] is None
               for p in artifact["products"])
    target = root / "frozen.json"
    target.write_text(json.dumps(freeze(artifact)))
    assert reuse.prepare(path, project_root=root, generated_artifact=target)["production_cost"]["model_requests"] == 2


@pytest.mark.parametrize("damage", ["budget", "model", "duplicate", "session", "metadata"])
def test_rehash_cannot_bypass_v2_product_identity_and_limits(v2_manifest, damage):
    path, root, _ = v2_manifest
    artifact, _ = make_artifact(v2_manifest)
    first, second = artifact["products"]
    if damage == "budget":
        first["production_limits"]["output_tokens"] += 1
    elif damage == "model":
        first["model"] = "another-model"
    elif damage == "duplicate":
        artifact["products"][1] = copy.deepcopy(first)
    elif damage == "session":
        second["session_id"] = first["session_id"]
    else:
        first["compression"]["budget_tokens"] += 1
    artifact["artifact_hash"] = digest(production._production_payload(artifact))
    target = root / "bad.json"
    with pytest.raises(ValueError, match="compression|budget|same model|incomplete|session|section"):
        target.write_text(json.dumps(freeze(artifact)))
        reuse.prepare(path, project_root=root, generated_artifact=target)


@pytest.mark.parametrize("damage", ["missing_reviewers", "duplicate_item", "forged_verdict"])
def test_rehashed_frozen_envelope_must_replay_dual_review(v2_manifest, damage):
    path, root, _ = v2_manifest
    artifact, _ = make_artifact(v2_manifest)
    artifact = freeze(artifact)
    if damage == "missing_reviewers":
        artifact["material_reviews"]["independent_reviews"] = []
    elif damage == "duplicate_item":
        items = artifact["material_reviews"]["independent_reviews"][0]["items"]
        items.append(copy.deepcopy(items[0]))
    else:
        artifact["section_verdicts"][next(iter(artifact["section_verdicts"]))] = "unsupported"
    artifact["reviewed_artifact_hash"] = digest({k: v for k, v in artifact.items() if k != "reviewed_artifact_hash"})
    target = root / "forged-review.json"
    target.write_text(json.dumps(artifact))
    with pytest.raises(ValueError, match="review|section"):
        reuse.prepare(path, project_root=root, generated_artifact=target)


def test_v2_default_factory_locks_zero_retries_and_custom_retry_is_rejected(v2_manifest, monkeypatch):
    from codeatlas.llm import client as client_module
    path, root, _ = v2_manifest
    kwargs_seen = []
    def unconfigured(**kwargs):
        kwargs_seen.append(kwargs)
    monkeypatch.setattr(client_module, "get_client", unconfigured)
    result = production.produce(path, project_root=root)
    assert result["status"] == "not_run" and len(kwargs_seen) == 2
    assert all(k["transport_retries"] == 0 for k in kwargs_seen)
    def retry_factory(arm, cid):
        client = FakeProducer(arm + cid, [], [])
        client.transport_retries = 1
        return client
    with pytest.raises(ValueError, match="zero transport retries"):
        production.produce(path, project_root=root, client_factory=retry_factory)


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_real_client_v2_wire_preserves_exact_eight_k_input_and_finish_usage(v2_manifest, monkeypatch, finish_reason):
    from codeatlas.llm.client import OpenAICompatClient
    import urllib.request
    path, root, _ = v2_manifest
    case = reuse.prepare(path, project_root=root)["cases"][0]
    system = production.SYSTEM_V2
    user = "x" * ((8000 - 32) * 3 - len(system.encode("utf-8")))
    assert estimated_tokens(system + user) + 32 == 8000
    monkeypatch.setattr(production, "_production_messages", lambda *args: (system, user))
    wire = []
    def response(request, timeout):
        wire.append(json.loads(request.data))
        payload = {"model": "same-model", "choices": [{"finish_reason": finish_reason,
                   "message": {"content": json.dumps({"sections": sections_for(case)})}}],
                   "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}}
        return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(urllib.request, "urlopen", response)
    clients = []
    def factory(arm, cid):
        client = OpenAICompatClient("https://fixture.invalid/v1", "fake-offline-key", "same-model",
                                    session_id=arm + cid, transport_retries=0)
        clients.append(client)
        return client
    artifact = production.produce(path, project_root=root, client_factory=factory)
    assert len(wire) == 2 and all(c.request_count == 1 for c in clients)
    assert all(p["messages"] == [{"role": "system", "content": system}, {"role": "user", "content": user}]
               for p in wire)
    assert all(c.last_budget["complete_input_budget"] == 8000 for c in clients)
    assert artifact["production_cost"]["input_tokens"] == 200
    assert artifact["production_cost"]["output_tokens"] == 60
    assert artifact["production_cost"]["cost_usd"] is None
    assert all(p["candidate"]["response_metadata"]["finish_reason"] == finish_reason for p in artifact["products"])
    if finish_reason == "length":
        assert all(p["compression"]["status"] == "compression_infeasible" for p in artifact["products"])
        assert artifact["production_cost"]["cost_usd"] is None
    target = root / "wire-frozen.json"
    target.write_text(json.dumps(freeze(artifact)))
    assert reuse.prepare(path, project_root=root, generated_artifact=target)["production_cost"]["input_tokens"] == 200


def test_v2_answer_transport_retry_is_rejected_before_any_trial(v2_manifest):
    path, root, _ = v2_manifest
    client = FakeProducer("answer", [], [])
    client.transport_retries = 1
    with pytest.raises(ValueError, match="zero transport retries"):
        reuse.evaluate(path, project_root=root, client=client, runs=1)
    assert client.request_count == 0


@pytest.mark.parametrize("damage", ["shared_reviewer_session", "producer_session", "arbiter_session"])
def test_v2_material_review_sessions_are_disjoint_from_each_other_and_production(v2_manifest, damage):
    artifact, _ = make_artifact(v2_manifest)
    submission = copy.deepcopy(freeze(artifact)["material_reviews"])
    reviews = submission["independent_reviews"]
    if damage == "shared_reviewer_session":
        reviews[1]["session_id"] = reviews[0]["session_id"]
    elif damage == "producer_session":
        reviews[0]["session_id"] = artifact["products"][0]["session_id"]
    else:
        reviews[1]["items"][0]["verdict"] = "unsupported"
        submission["arbitration"] = {
            "reviewer_kind": "agent", "agent": "arbiter", "session_id": reviews[0]["session_id"],
            "peer_reviews_visible": False,
            "items": [{"section_id": reviews[0]["items"][0]["section_id"], "verdict": "supported"}]}
    with pytest.raises(ValueError, match="session"):
        production.apply_reviews(artifact, submission)


def test_formal_card_view_is_derived_read_only_and_fails_closed_without_source():
    card = {"id": "formal", "status": "approved", "title": "Issue", "symptom": "Observed failure.",
            "hypotheses": '["Flag?"]', "dead_ends": '["Not memory corruption."]',
            "root_cause": "Only with flag set; not otherwise.", "fix_steps": "Clear flag.",
            "verification": "Run both flag states; not yet executed.", "evidence_turns": "[1]",
            "evidence_tags": '["A1"]', "review_bundle_hash": "frozen", "current_review_bundle_hash": "frozen"}
    before = copy.deepcopy(card)
    view = production.formal_card_answer_view(card, [{"content": "Source. " * 500}])
    assert card == before and view["source_bundle_hash"] == "frozen"
    assert view["status"] == "compressed" and view["estimated_tokens"] <= view["budget_tokens"]
    assert card["root_cause"] in view["text"] and card["verification"] in view["text"]
    assert "Not memory corruption." in view["text"] and "A1" in view["text"]
    assert production.formal_card_answer_view(card, [])["status"] == "compression_infeasible"
    assert production.formal_card_answer_view({**card, "status": "pending"}, []) is None


def test_v2_revalidated_frozen_contract_cannot_be_bypassed_by_rehash(v2_manifest):
    path, root, _ = v2_manifest
    artifact, _ = make_artifact(v2_manifest)
    artifact["products"][0]["text"] = "The essential condition disappeared."
    artifact["artifact_hash"] = digest(production._production_payload(artifact))
    target = root / "bad.json"
    target.write_text(json.dumps(freeze(artifact)))
    with pytest.raises(ValueError, match="compression|text|section"):
        reuse.prepare(path, project_root=root, generated_artifact=target)
