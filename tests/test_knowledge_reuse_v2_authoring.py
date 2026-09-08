"""Source-only 12-case task/gold checks; no old gold, products or gateway reads."""
import importlib.util
import json
from collections import Counter
from pathlib import Path

import pytest
import yaml

from codeatlas.contracts import digest
from codeatlas.eval import knowledge_reuse_eval as reuse, protocol


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "eval/knowledge_reuse_v2/build_manifest.py"
SOURCE_HASH = "3870534cc47af568ed09e6efe512de309ac75253b556972d45b901ce79e561c1"
spec = importlib.util.spec_from_file_location("source_only_authoring", SCRIPT)
authoring = importlib.util.module_from_spec(spec)
spec.loader.exec_module(authoring)


@pytest.fixture(scope="module")
def artifacts():
    manifest = yaml.safe_load((ROOT / authoring.MANIFEST).read_text())
    key = yaml.safe_load((ROOT / authoring.ANSWER_KEY).read_text())
    return manifest, key


def test_reviewed_source_identity_and_deterministic_artifacts():
    assert digest((ROOT / authoring.SOURCE_MANIFEST).read_bytes()) == SOURCE_HASH
    files = authoring.build(ROOT, expected_source_hash=SOURCE_HASH)
    for name, text in files.items():
        assert (ROOT / name).read_text() == text
    with pytest.raises(ValueError, match="source manifest hash changed"):
        authoring.build(ROOT, expected_source_hash="0" * 64)


def test_twelve_cases_each_have_three_different_followup_kinds(artifacts):
    manifest, key = artifacts
    assert manifest["schema_version"] == key["schema_version"] == 2
    assert manifest["source_manifest"]["sha256"] == key["source_manifest_hash"] == SOURCE_HASH
    assert len(manifest["cases"]) == 12 and len(manifest["tasks"]) == 36
    assert Counter(t["case_id"] for t in manifest["tasks"]) == dict.fromkeys(authoring.CASES, 3)
    assert set(key["tasks"]) == {t["id"] for t in manifest["tasks"]}
    assert Counter(c["corpus"] for c in manifest["cases"]) == {"cjson": 6, "lwip": 6}
    assert Counter((c["corpus"], c["length_bucket"]) for c in manifest["cases"]) == {
        (repo, bucket): 2 for repo in ("cjson", "lwip") for bucket in ("short", "medium", "long")}
    for cid in authoring.CASES:
        tasks = [t for t in manifest["tasks"] if t["case_id"] == cid]
        assert {t["question_kind"] for t in tasks} == set(authoring.KINDS)
        assert len({t["question"] for t in tasks}) == 3


def test_prepare_and_answer_key_are_keyless_and_not_human_approved(artifacts, monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("network/model connection attempted"))
    prepared = reuse.prepare(ROOT / authoring.MANIFEST, project_root=ROOT)
    key = protocol.answer_key(ROOT / authoring.ANSWER_KEY, ROOT / authoring.MANIFEST, prepared["tasks"])
    assert (prepared["case_count"], prepared["question_count"]) == (12, 36)
    assert prepared["production_cost"]["model_requests"] == 0
    assert prepared["formal_b_cards_created"] == 0 and not prepared["production_database_accessed"]
    assert prepared["knowledge_role_removed"] and prepared["speaker_authority_removed"]
    assert key["status"] == "approved" and key["reviewer_kind"] == "agent"
    assert key["split"] == "held_out"
    assert key["effect_eligible"] and not key["human_release_eligible"]
    assert key["review_status"] == "ai_reviewed"
    assert key["independent_review"]["status"] == "ai_reviewed"
    assert key["independent_review"]["human_reviewed"] is False
    assert len(key["review"]["independent_reviews"]) == 2


def test_gold_release_binds_candidate_reviews_and_released_key():
    release = json.loads((ROOT / "eval/knowledge_reuse_v2/gold-release.json").read_text())
    assert release["source_manifest_sha256"] == SOURCE_HASH
    assert release["manifest_sha256"] == digest((ROOT / authoring.MANIFEST).read_bytes())
    assert release["released_answer_key_sha256"] == digest(
        (ROOT / authoring.ANSWER_KEY).read_bytes()
    )
    assert release["reviewed_candidate_answer_key_sha256"] == authoring.REVIEWED_CANDIDATE_HASH
    for path, expected in release["review_files"].items():
        assert digest((ROOT / path).read_bytes()) == expected
    assert release["effect_eligible"] is True
    assert release["human_release_eligible"] is False
    assert release["models_called_for_answers_or_products"] == 0


@pytest.mark.parametrize("cid", list(authoring.CASES))
def test_every_gold_claim_and_executable_assertion_has_exact_source_ranges(artifacts, cid):
    manifest, key = artifacts
    case = next(c for c in manifest["cases"] if c["id"] == cid)
    session = json.loads((ROOT / case["session"]["path"]).read_text())
    for task in (t for t in manifest["tasks"] if t["case_id"] == cid):
        row = key["tasks"][task["id"]]
        payload = {e["id"]: e for e in row["evidence_payload"]}
        assert len(payload) == len(row["evidence_payload"])
        for evidence in payload.values():
            raw = (ROOT / evidence["path"]).read_bytes()
            assert digest(raw) == evidence["file_sha256"]
            assert digest(evidence["text"].encode()) == evidence["text_sha256"]
            span = evidence["source_span"]
            assert span["tag"] == evidence["tag"]
            if evidence["tag"].startswith("T"):
                assert evidence["json_pointer"] == "/messages/3/content"
                full = session["messages"][int(evidence["tag"][1:]) - 1]["content"]
            else:
                attachment = case["attachments"][int(evidence["tag"][1:]) - 1]
                assert attachment["path"] == evidence["path"]
                lines = raw.decode().splitlines(keepends=True)
                start, end = evidence["line_start"], evidence["line_end"]
                astart, aend = attachment.get("line_start", 1), attachment.get("line_end", len(lines))
                assert astart <= start <= end <= aend
                assert "".join(lines[start - 1:end]) == evidence["text"]
                full = "".join(lines[astart - 1:aend])
            assert full[span["start"]:span["end"]] == evidence["text"]
        for point in row["expected_points"]:
            assert point["text"] and set(point["evidence"]) <= payload.keys()
        assert len(row["forbidden_claims"]) == len(row["forbidden_evidence"]) >= 2
        for index, forbidden in enumerate(row["forbidden_evidence"]):
            assert forbidden["claim_index"] == index
            assert forbidden["evidence"] and set(forbidden["evidence"]) <= payload.keys()
        verification = row["executable_verification"]
        assert verification["command"][-1] == cid
        assert verification["expected_exit_code"] == 0 and verification["assertions"]
        for assertion in verification["assertions"]:
            assert assertion["text"] in payload[assertion["evidence"][0]]["text"]
            assert "assert(" in assertion["text"]


@pytest.mark.parametrize("cid", ["cjson-unicode", "lwip-address-forms"])
def test_initial_failures_remain_in_required_facts_and_gold(artifacts, cid):
    manifest, key = artifacts
    case = next(c for c in manifest["cases"] if c["id"] == cid)
    facts = case["required_facts"]["verification"]
    failure = next(f for f in facts if f["evidence_tags"] == ["T4"])
    assert "Preserved initial failure" in failure["text"]
    assert "exit:" in failure["text"]
    assert any(e["id"] == "F" for e in key["tasks"][f"{cid}.wrong_explanation"]["evidence_payload"])


def test_build_refuses_drifted_or_outside_source(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        authoring.checked(tmp_path, {"path": "../outside.txt", "sha256": "0" * 64})
    path = tmp_path / "source.c"
    path.write_text("int x;\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        authoring.checked(tmp_path, {"path": "source.c", "sha256": "0" * 64})
