"""Offline integrity checks for the twelve task-free public source cases."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "examples" / "reproductions" / "knowledge-sources-v2"
MANIFEST = SOURCE_DIR / "source-manifest.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_source_manifest_and_derived_material_are_hash_complete():
    result = subprocess.run(
        [sys.executable, str(SOURCE_DIR / "verify.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    assert report == {
        "case_count": 12,
        "source_hashes_verified": True,
        "rerun_count": 0,
        "manifest_sha256": _sha(MANIFEST),
    }


def test_source_manifest_has_balanced_unpadded_relative_length_strata():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["stage"] == "source_only"
    assert manifest["source_contract"]["models_called"] == 0
    assert manifest["source_contract"]["summary_or_card_products"] == 0
    assert manifest["length_metric"]["padding_added"] is False
    assert len(manifest["cases"]) == 12
    for corpus in ("cjson", "lwip"):
        cases = [case for case in manifest["cases"] if case["corpus"] == corpus]
        assert len(cases) == 6
        assert sorted(case["length_bucket"] for case in cases) == [
            "long", "long", "medium", "medium", "short", "short",
        ]
        assert sorted(case["length_rank_within_repository"] for case in cases) == list(range(1, 7))


def test_two_independent_ai_source_reviews_bind_the_final_manifest():
    manifest_hash = _sha(MANIFEST)
    paths = [
        ROOT / "eval" / "knowledge_reuse_v2.source-review-root.json",
        ROOT / "eval" / "knowledge_reuse_v2.source-review-independent.json",
    ]
    reviews = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    assert {review["reviewer_id"] for review in reviews} == {
        "/root", "/root/closeout_plan_review",
    }
    for review in reviews:
        assert review["reviewer_type"] == "ai"
        assert review["review_status"] == "ai_reviewed"
        assert review["source_manifest_sha256"] == manifest_hash
        assert review["independence"] == {
            "read_new_wiki": False,
            "read_generated_summary_or_card": False,
            "read_follow_up_questions_or_gold": False,
            "model_gateway_calls": 0,
        }
        assert review["rerun"]["all_passed"] is True
