"""Presentation and provenance contracts, without network or production writes."""
import json
from pathlib import Path

import pytest
import yaml

from codeatlas.contracts import digest
from codeatlas.eval import knowledge_production, knowledge_reuse_eval as reuse, protocol, rubric


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def material_manifest(tmp_path):
    def save(name, content):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        return {"path": name, "sha256": digest(path.read_bytes())}
    template = save("presentation.yaml", (ROOT / "eval/templates/knowledge_reuse_presentations.yaml").read_text())
    attachments = [dict(save(f"evidence-{i}.txt", text), kind=kind) for i, (kind, text) in enumerate([
        ("source", "int parse(void) { return 0; }\n"),
        ("harness", "int main(void) { return parse(); }\n"),
        ("command", "cc source.c harness.c -o probe\n"),
        ("observation", "result=ok\n"),
    ])]
    cases, tasks = [], []
    for cid in ("case-one", "case-two"):
        session = {"schema_version": 1, "id": cid, "source_type": "public_synthetic",
                   "repository": "https://example.invalid/public", "revision": "fixture-v1",
                   "license": "CC0-1.0", "messages": [
                       {"role": "reviewer", "knowledge_role": "root_cause", "content": f"{cid} source fact {i}."}
                       for i in range(1, 8)]}
        cases.append({"id": cid, "repository": session["repository"], "revision": session["revision"],
                      "session": save(cid + ".json", json.dumps(session)),
                      "template": template, "attachments": attachments})
        tasks.extend({"id": f"{cid}-q{i}", "case_id": cid, "question": f"Explain observation {i}."}
                     for i in range(3))
    manifest = {"schema_version": 1, "artifact_scope": "isolated_experiment",
                "strip_knowledge_role": True, "cases": cases, "tasks": tasks}
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return path, tmp_path, manifest


class MaterialClient:
    model = "fixture-model"

    def __init__(self, fail_at=None, omit_usage_at=None):
        self.request_count = 0
        self.usage_totals = {}
        self.usage_observations = {}
        self.calls = []
        self.fail_at, self.omit_usage_at = fail_at, omit_usage_at

    def answer_from_material(self, system, user):
        self.request_count += 1
        self.calls.append((system, user))
        if self.request_count == self.fail_at:
            raise RuntimeError("Authorization: private-key-must-not-be-logged")
        if self.request_count != self.omit_usage_at:
            for key, value in (("prompt_tokens", 80), ("completion_tokens", 10), ("total_tokens", 90)):
                self.usage_totals[key] = self.usage_totals.get(key, 0) + value
                self.usage_observations[key] = self.usage_observations.get(key, 0) + 1
        return "The observation is documented [E4]; its scope is limited [M1]."


class ProductionClient:
    model = "deepseek-v4-flash"

    def __init__(self, session_id, calls):
        self.session_id = session_id
        self.calls = calls
        self.request_count = 0
        self.usage_totals = {}
        self.usage_observations = {}

    def _chat(self, system, user, max_tokens, *, json_object=False):
        assert json_object is True
        self.request_count += 1
        for key, value in (("prompt_tokens", 80), ("completion_tokens", 20),
                           ("total_tokens", 100)):
            self.usage_totals[key] = self.usage_totals.get(key, 0) + value
            self.usage_observations[key] = self.usage_observations.get(key, 0) + 1
        self.calls.append((self.session_id, system, user, max_tokens))
        return json.dumps({"sections": [{
            "heading": "证据整理", "content": "仅复述固定会话和共同材料。",
            "evidence_tags": ["T1", "E1"],
        }]})

    @staticmethod
    def _json(raw):
        return json.loads(raw)


def test_preparation_removes_posthoc_labels_and_has_identical_facts_and_attachments(material_manifest):
    path, root, _ = material_manifest
    prepared = reuse.prepare(path, project_root=root)
    assert prepared["case_count"] == 2 and prepared["question_count"] == 6
    assert prepared["knowledge_role_removed"] and prepared["speaker_authority_removed"]
    for case in prepared["cases"]:
        assert len({m["source_digest"] for m in case["materials"].values()}) == 1
        assert len({m["attachments_digest"] for m in case["materials"].values()}) == 1
        assert all(set(message) == {"turn_id", "content"} for message in case["messages"])
        task = next(t for t in prepared["tasks"] if t["case_id"] == case["case_id"])
        prompts = []
        for arm, material in case["materials"].items():
            for message in case["messages"]:
                assert material["text"].count(message["content"]) == 1
            system, user = reuse.messages_for(case, task, arm)
            assert "knowledge_role" not in user and "reviewer" not in user
            assert "[B1]" not in system + user
            prompts.append(user.split("共同附件\n", 1)[1])
        assert len(set(prompts)) == 1


def test_no_key_prepares_all_materials_without_simulated_answers_or_db_writes(material_manifest, monkeypatch):
    path, root, _ = material_manifest
    from codeatlas import db
    monkeypatch.setattr(db, "connect", lambda *a, **k: pytest.fail("production database accessed"))
    sentinel = root / "production.db"
    sentinel.write_bytes(b"pending human cards remain pending")
    before = {str(p): digest(p.read_bytes()) for p in root.iterdir() if p.is_file()}
    report = reuse.evaluate(path, project_root=root)
    after = {str(p): digest(p.read_bytes()) for p in root.iterdir() if p.is_file()}
    assert before == after
    assert report["status"] == "not_run" and report["reason"] == "model_not_configured"
    assert report["planned_trial_count"] == 54 and report["executed_trial_count"] == 0
    assert all(not row["repetitions"] for rows in report["raw_trials"].values() for row in rows)
    assert report["query_cost"]["requests"] == 0
    assert report["production_cost"]["human_edit_seconds"] is None
    assert report["materials"]["formal_b_cards_created"] == 0
    assert not report["acceptance"]["formal_human_b_gate"]


def test_fifty_four_trials_are_interleaved_and_count_as_two_cases(material_manifest):
    path, root, _ = material_manifest
    client = MaterialClient()
    report = reuse.evaluate(path, project_root=root, client=client, runs=3, seed=19)
    assert report["status"] == "completed" and report["executed_trial_count"] == 54
    assert report["case_count"] == 2 and report["question_count"] == 6
    assert len(client.calls) == 54 and protocol.validate_pairs(report)
    schedule = report["protocol"]["schedule"]
    assert schedule == protocol.schedule(report["materials"]["tasks"], reuse.ARMS, 3, 19)
    assert schedule != protocol.schedule(report["materials"]["tasks"], reuse.ARMS, 3, 20)
    for index in range(0, len(schedule), 3):
        block = schedule[index:index + 3]
        assert len({event["task_id"] for event in block}) == 1
        assert set(event["variant"] for event in block) == set(reuse.ARMS)
        evidence = [user.split("共同附件\n", 1)[1] for _, user in client.calls[index:index + 3]]
        assert len(set(evidence)) == 1
    trials = [t for rows in report["raw_trials"].values() for row in rows for t in row["repetitions"]]
    assert len({trial["trial_id"] for trial in trials}) == 54
    assert all(t["correct"] is None and t["complete"] is None and "human_review" not in t for t in trials)
    assert all(t["citation_valid"] and t["input_tokens"] == 80 for t in trials)
    assert report["query_cost"]["input_tokens"] == 4320
    assert report["query_cost"]["cost_usd"] is None
    assert not report["acceptance"]["passed"]


def test_failed_trials_stay_in_pairs_and_partial_usage_is_unknown(material_manifest):
    path, root, _ = material_manifest
    client = MaterialClient(fail_at=2, omit_usage_at=3)
    report = reuse.evaluate(path, project_root=root, client=client, runs=1)
    trials = [t for rows in report["raw_trials"].values() for row in rows for t in row["repetitions"]]
    assert len(trials) == 18 and protocol.validate_pairs(report)
    failure = next(t for t in trials if t["sequence"] == 1)
    missing_usage = next(t for t in trials if t["sequence"] == 2)
    assert failure["error_type"] == "RuntimeError" and failure["answer"] is None
    assert failure["input_tokens"] is None and missing_usage["input_tokens"] is None
    assert report["query_cost"]["input_tokens"] is None
    assert "private-key" not in json.dumps(report)


@pytest.mark.parametrize("runs", [0, 2, -1, True, 1.0])
def test_invalid_repeat_count_is_rejected_even_without_key(material_manifest, runs):
    path, root, _ = material_manifest
    with pytest.raises(ValueError, match="positive odd"):
        reuse.evaluate(path, project_root=root, runs=runs)


def test_unfrozen_source_change_is_rejected_before_model_call(material_manifest):
    path, root, manifest = material_manifest
    source = root / manifest["cases"][0]["attachments"][0]["path"]
    source.write_text("altered evidence")
    client = MaterialClient()
    with pytest.raises(ValueError, match="hash changed"):
        reuse.evaluate(path, project_root=root, client=client)
    assert client.request_count == 0


def test_oversized_shared_evidence_is_rejected_without_asymmetric_truncation(material_manifest):
    path, root, manifest = material_manifest
    attachment = manifest["cases"][0]["attachments"][0]
    source = root / attachment["path"]
    source.write_text("long source line\n" * 5000)
    attachment["sha256"] = digest(source.read_bytes())
    path.write_text(yaml.safe_dump(manifest))
    client = MaterialClient()
    with pytest.raises(ValueError, match="exceeds shared input budget"):
        reuse.evaluate(path, project_root=root, client=client)
    assert client.request_count == 0


def test_attachments_cannot_escape_the_experiment_root(material_manifest):
    path, root, manifest = material_manifest
    manifest["cases"][0]["session"]["path"] = "../outside-session.json"
    path.write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError, match="outside project"):
        reuse.prepare(path, project_root=root)


@pytest.mark.parametrize("damage", ["new_fact", "missing_turn", "duplicate_turn"])
def test_card_cannot_gain_information_or_omit_source_facts(material_manifest, damage):
    path, root, manifest = material_manifest
    template_path = root / manifest["cases"][0]["template"]["path"]
    template = yaml.safe_load(template_path.read_text())
    if damage == "new_fact":
        template["structured_card"][0]["text"] = "All incidents share this cause."
    elif damage == "missing_turn":
        template["generic_summary"]["turn_ids"].pop()
    else:
        template["structured_card"][0]["turn_ids"].append(1)
    template_path.write_text(yaml.safe_dump(template, allow_unicode=True))
    for case in manifest["cases"]:
        case["template"]["sha256"] = digest(template_path.read_bytes())
    path.write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError, match="facts|every source turn"):
        reuse.prepare(path, project_root=root)


def test_pilot_shape_cannot_turn_questions_into_cases(material_manifest):
    path, root, manifest = material_manifest
    manifest["tasks"][0]["case_id"] = "case-two"
    path.write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError, match="exactly three"):
        reuse.prepare(path, project_root=root)


def test_existing_compat_client_chat_interface_is_supported(material_manifest):
    path, root, _ = material_manifest
    class ChatClient:
        model = "chat-fixture"
        def _chat(self, system, user, max_tokens):
            assert max_tokens == 1200
            assert user.endswith(".") and "问题：" in user
            return "A bounded answer [M1]."
    report = reuse.evaluate(path, project_root=root, client=ChatClient(), runs=1)
    assert report["executed_trial_count"] == 18
    assert report["query_cost"]["input_tokens"] is None


def test_default_manifest_is_preparable_and_old_ten_tasks_remain_unchanged():
    paths = [ROOT / "eval/tasks_cjson.yaml", ROOT / "eval/tasks_lwip.yaml"]
    before = [p.read_bytes() for p in paths]
    old_tasks = [task for p in paths for task in yaml.safe_load(p.read_text())["tasks"]
                 if task["type"] == "已审核经验复用"]
    assert len(old_tasks) == 10
    manifest = yaml.safe_load((ROOT / "eval/knowledge_reuse.yaml").read_text())
    if any(not (ROOT / a["path"]).is_file() for case in manifest["cases"] for a in case["attachments"]):
        pytest.skip("optional pinned corpus checkouts are not present")
    report = reuse.evaluate(project_root=ROOT)
    assert report["status"] == "not_run" and report["case_count"] == 2
    assert before == [p.read_bytes() for p in paths]
    assert not set(t["id"] for t in old_tasks) & set(t["id"] for t in report["materials"]["tasks"])


def test_gold_template_is_draft_and_never_in_model_materials(material_manifest):
    template = yaml.safe_load((ROOT / "eval/templates/knowledge_reuse_answer_key.template.yaml").read_text())
    assert template["status"] == "draft" and template["reviewer"] is None
    assert not template["effect_eligible"] and len(template["tasks"]) == 6
    path, root, _ = material_manifest
    client = MaterialClient()
    reuse.evaluate(path, project_root=root, client=client, runs=1)
    assert all("expected_points" not in user and "forbidden_claims" not in user for _, user in client.calls)


def test_model_material_production_is_isolated_from_questions_and_requires_dual_review(
        material_manifest, tmp_path):
    path, root, manifest = material_manifest
    calls = []
    artifact = knowledge_production.produce(
        path, project_root=root,
        client_factory=lambda role, case_id: ProductionClient(
            f"production:{role}:{case_id}", calls
        ),
    )
    assert artifact["status"] == "pending_review"
    assert len(artifact["products"]) == 4 and len(calls) == 4
    questions = {task["question"] for task in manifest["tasks"]}
    assert all(question not in system + user for _, system, user, _ in calls
               for question in questions)
    assert len({session for session, *_ in calls}) == 4

    section_ids = [section["id"] for product in artifact["products"]
                   for section in product["sections"]]
    reviews = {
        "artifact_hash": artifact["artifact_hash"],
        "independent_reviews": [{
            "reviewer_kind": "agent", "agent": f"judge-{number}",
            "session_id": f"judge-session-{number}", "peer_reviews_visible": False,
            "items": [{"section_id": section_id, "verdict": "supported"}
                      for section_id in section_ids],
        } for number in (1, 2)],
    }
    frozen = knowledge_production.apply_reviews(artifact, reviews)
    assert frozen["status"] == "frozen" and frozen["review_status"] == "ai_reviewed"
    frozen_path = tmp_path / "frozen.json"
    frozen_path.write_text(json.dumps(frozen), encoding="utf-8")
    prepared = reuse.prepare(path, project_root=root, generated_artifact=frozen_path)
    assert prepared["presentation_mode"] == "model_generated_dual_reviewed"
    assert all(case["materials"][arm]["production"]["method"] ==
               "model_generated_dual_reviewed"
               for case in prepared["cases"]
               for arm in ("generic_summary", "structured_card"))
    for case in prepared["cases"]:
        for arm in ("generic_summary", "structured_card"):
            text = case["materials"][arm]["text"]
            assert "[T1]" not in text and "[M1]" in text and "[E1]" in text
            assert case["materials"][arm]["content_hash"] == digest(text)
    assert all("[T1]" in product["text"] for product in frozen["products"])


def test_blind_packet_rehydrates_exact_reuse_body_without_mutating_raw_answers(material_manifest):
    import copy
    path, root, _ = material_manifest
    report = reuse.evaluate(path, project_root=root, client=MaterialClient(), runs=1)
    before = copy.deepcopy(report["raw_trials"])
    original_hash = protocol.run_hash(report)
    packet = rubric.blind_packet(report)
    assert report["raw_trials"] == before
    assert packet["run_hash"] == original_hash
    for item in packet["items"]:
        mapping = packet["audit_only_citation_map"][item["trial_id"]]
        reverse = {original: neutral for neutral, original in mapping.items()}
        citations = {citation["tag"]: citation for citation in item["citations"]}
        assert f"[{reverse['E4']}]" in item["answer"]
        assert citations[reverse['E4']]["text"] == "result=ok\n"
        assert citations[reverse['M1']]["text"]
    # No filesystem reload can silently replace the frozen source body.
    report["materials"]["cases"][0]["attachments"][3]["text"] = "result=changed\n"
    with pytest.raises(ValueError, match="attachment text changed"):
        rubric.blind_packet(report)


def test_model_material_disagreement_stays_unusable(material_manifest, tmp_path):
    path, root, _ = material_manifest
    artifact = knowledge_production.produce(
        path, project_root=root,
        client_factory=lambda role, case_id: ProductionClient(
            f"production:{role}:{case_id}", []
        ),
    )
    ids = [section["id"] for product in artifact["products"]
           for section in product["sections"]]
    reviews = {
        "artifact_hash": artifact["artifact_hash"],
        "independent_reviews": [
            {"reviewer_kind": "agent", "agent": "judge-a", "session_id": "a",
             "peer_reviews_visible": False,
             "items": [{"section_id": item, "verdict": "supported"} for item in ids]},
            {"reviewer_kind": "agent", "agent": "judge-b", "session_id": "b",
             "peer_reviews_visible": False,
             "items": [{"section_id": item, "verdict":
                        ("unsupported" if item == ids[0] else "supported")} for item in ids]},
        ],
    }
    pending = knowledge_production.apply_reviews(artifact, reviews)
    assert pending["status"] == "pending_review"
    target = tmp_path / "pending.json"
    target.write_text(json.dumps(pending), encoding="utf-8")
    with pytest.raises(ValueError, match="completed dual review"):
        reuse.prepare(path, project_root=root, generated_artifact=target)
