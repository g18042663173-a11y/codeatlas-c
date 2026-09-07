from __future__ import annotations

from collections import Counter
import copy
from pathlib import Path

import pytest
import yaml

from codeatlas.eval import maintenance_eval as m


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "eval/upstream_maintenance.yaml"


def test_upstream_manifest_is_balanced_pinned_and_has_no_synthetic_source() -> None:
    validated = m.load_upstream_manifest(MANIFEST)

    assert validated["counts"] == {
        "cjson": {"effective": 4, "unrelated_to_claim": 4, "failure": 4},
        "lwip": {"effective": 4, "unrelated_to_claim": 4, "failure": 4},
    }
    corpora = validated["manifest"]["corpora"]
    assert corpora["cjson"]["repository"] == "corpus/cJSON"
    assert corpora["cjson"]["revision"] == "fb16e5cf358798aabb049655975cde8427101056"
    assert corpora["cjson"]["claim_anchor"] == {
        "path": "cJSON.c",
        "start_marker": "static cJSON_bool parse_value(cJSON * const item, parse_buffer * const input_buffer);",
        "end_marker": "\n/* Render an object to text. */",
    }
    assert corpora["cjson"]["claim_dependencies"] == {
        "functions": [
            {"path": "cJSON.c", "name": name}
            for name in ("parse_value", "parse_array", "parse_object",
                         "cJSON_ParseWithOpts", "cJSON_ParseWithLengthOpts")
        ],
        "macros": [{"name": "CJSON_NESTING_LIMIT", "paths": ["cJSON.h", "cJSON.c"]}],
    }
    assert corpora["lwip"]["repository"] == "corpus/lwip"
    assert corpora["lwip"]["revision"] == "3d896ba0a37ff3ce73270ca5e230707fe47f60e3"
    assert corpora["lwip"]["claim_anchor"] == {
        "path": "src/core/netif.c",
        "start_marker": "struct netif *\nnetif_add(",
        "end_marker": "\nstatic void\nnetif_do_ip_addr_changed",
    }
    assert corpora["lwip"]["claim_dependencies"] == {
        "functions": [
            {"path": "src/core/netif.c", "name": "netif_add"},
            {"path": "src/core/netif.c", "name": "netif_set_default"},
        ],
        "macros": [{"name": "LWIP_SINGLE_NETIF", "paths": ["src/include/lwip/opt.h"]}],
    }
    cases = [item["case"] for item in validated["cases"]]
    assert Counter((case["corpus"], case["failure_mode"])
                   for case in cases if case["classification"] == "failure") == Counter({
        (corpus, mode): 1
        for corpus in ("cjson", "lwip") for mode in m.UPSTREAM_FAILURE_MODES
    })
    assert Counter(case["execution_status"] for case in cases) == {"ready": 24}
    semantic = [case for case in cases if case["classification"] != "failure"]
    assert len({case["artifacts"]["patch"]["sha256"] for case in semantic}) == 16
    assert all(any(assertion["id"].startswith("target_") for assertion in case["oracle"]["assertions"])
               for case in semantic)
    assert all(any(assertion["id"].startswith("guard_") for assertion in case["oracle"]["assertions"])
               for case in semantic if case["classification"] == "unrelated_to_claim")
    assert not list((ROOT / "examples/maintenance_upstream").rglob("source.c"))


def test_upstream_manifest_rejects_revision_or_fixture_hash_drift() -> None:
    value = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    changed = copy.deepcopy(value)
    changed["corpora"]["cjson"]["revision"] = "0" * 40
    with pytest.raises(m.MaintenanceError, match="pinned repository and revision"):
        m.validate_upstream_manifest(changed, project_root=ROOT)

    changed = copy.deepcopy(value)
    ready = next(case for case in changed["cases"] if case["execution_status"] == "ready")
    ready["artifacts"]["patch"]["sha256"] = "0" * 64
    with pytest.raises(m.MaintenanceError, match="artifact hash mismatch"):
        m.validate_upstream_manifest(changed, project_root=ROOT)


def test_validate_mode_is_non_executing() -> None:
    report = m.evaluate_upstream(MANIFEST, mode="validate")

    assert report["status"] == "validated_not_run"
    assert report["summary"]["outcomes"] == {"not_run": 24}
    assert report["acceptance"]["all_24_executed"] is False
    assert report["acceptance"]["pinned_upstream_archives"] is False
    assert report["summary"]["blocked"] == 0


@pytest.fixture(scope="module")
def executed() -> dict:
    return m.evaluate_upstream(MANIFEST, mode="execute")


def test_real_upstream_ready_cases_run_in_archived_temporary_copies(executed: dict) -> None:
    assert executed["status"] == "executed"
    assert executed["summary"]["ready"] == 24
    assert executed["summary"]["blocked"] == 0
    assert executed["summary"]["ready_expected_outcomes"] == 24
    assert executed["acceptance"] == {
        "manifest_valid": True,
        "all_24_executed": True,
        "all_ready_expected_outcomes": True,
        "ready_semantic_decisions_correct": True,
        "blocked_not_counted_as_complete": True,
        "pinned_upstream_archives": True,
        "current_decisions_passed": True,
        "current_missed_invalidation": 0,
        "current_false_invalidation": 0,
        "oracle_independently_validated": True,
    }
    ready = [row for row in executed["cases"] if row["execution_status"] == "ready"]
    assert all(row["provenance"]["isolated_temporary_copy"] is True for row in ready)
    assert {row["provenance"]["revision"] for row in ready if row["corpus"] == "cjson"} == {
        "fb16e5cf358798aabb049655975cde8427101056"
    }
    assert {row["provenance"]["revision"] for row in ready if row["corpus"] == "lwip"} == {
        "3d896ba0a37ff3ce73270ca5e230707fe47f60e3"
    }


def test_semantic_controls_compile_run_assert_and_publish(executed: dict) -> None:
    semantic = [row for row in executed["cases"]
                if row["execution_status"] == "ready" and row["classification"] != "failure"]
    assert len(semantic) == 16
    assert all(row["outcome"] == "completed" for row in semantic)
    # The current adapter evaluates the reviewed consumed-dependency manifest,
    # not the whole source tree and not merely the primary anchor.
    assert sum(row["adapters"]["current"]["decision"] == "revalidate"
               for row in semantic if row["classification"] == "effective") == 8
    assert sum(row["adapters"]["current"]["decision"] == "revalidate"
               for row in semantic if row["classification"] == "unrelated_to_claim") == 0
    assert executed["summary"]["adapters"]["current"]["missed_invalidation"] == 0
    assert executed["summary"]["adapters"]["current"]["false_invalidation"] == 0
    assert executed["summary"]["adapters"]["main_anchor_hash"]["missed_invalidation"] == 2
    assert executed["summary"]["adapters"]["main_anchor_hash"]["false_invalidation"] == 1
    assert executed["summary"]["adapters"]["invalidate_all"]["false_invalidation"] == 8
    assert all(all(row["stages"][stage]["status"] == "passed" for stage in m.UPSTREAM_STAGES)
               for row in semantic)
    assert all(all(assertion["passed"] is True for assertion in row["assertions"])
               for row in semantic)


@pytest.mark.parametrize("corpus", ["cjson", "lwip"])
def test_each_failure_control_records_the_expected_stage_boundary(executed: dict, corpus: str) -> None:
    failures = {row["failure_mode"]: row for row in executed["cases"]
                if row["corpus"] == corpus and row["classification"] == "failure"}

    compile_failure = failures["compile_failure"]
    assert compile_failure["outcome"] == "compile_failed"
    assert compile_failure["stages"]["compile"]["status"] == "failed"
    assert compile_failure["stages"]["run"]["status"] == "skipped"

    tamper = failures["artifact_tamper"]
    assert tamper["outcome"] == "artifact_tamper"
    assert tamper["artifact_integrity"]["status"] == "failed"
    assert tamper["artifact_integrity"]["original_untouched"] is True
    assert tamper["stages"]["compile"]["status"] == "skipped"

    interrupted = failures["publication_interruption"]
    assert interrupted["outcome"] == "publication_interrupted"
    assert all(interrupted["stages"][stage]["status"] == "passed"
               for stage in ("compile", "run", "assertion", "snapshot", "card", "citation"))
    assert interrupted["stages"]["wiki"]["status"] == "interrupted"
    assert interrupted["stages"]["wiki"]["before_sha256"] == interrupted["stages"]["wiki"]["after_sha256"]
    assert interrupted["stages"]["wiki"]["journal_state"] == "rolled_back"
    assert interrupted["stages"]["wiki"]["recovered"] is True
    assert interrupted["stages"]["wiki"]["card_removed"] is True
    assert interrupted["stages"]["wiki"]["citation_removed"] is True

    rollback = failures["governance_rollback"]
    assert rollback["outcome"] == "governance_rolled_back"
    assert all(rollback["stages"][stage]["status"] == "passed"
               for stage in ("compile", "run", "assertion", "snapshot"))
    assert all(rollback["stages"][stage]["status"] == "rolled_back"
               for stage in ("card", "citation", "wiki"))
    assert rollback["stages"]["card"]["artifact_removed"] is True
    assert rollback["stages"]["citation"]["artifact_removed"] is True
    assert rollback["stages"]["wiki"]["journal_state"] == "rolled_back"
    assert rollback["stages"]["wiki"]["recovered_operation"] is True
    assert rollback["stages"]["wiki"]["before_sha256"] == rollback["stages"]["wiki"]["after_sha256"]


def test_execution_does_not_modify_bound_upstream_or_fixture_bytes() -> None:
    tracked = [
        ROOT / "corpus/cJSON/cJSON.c",
        ROOT / "corpus/lwip/src/core/netif.c",
        MANIFEST,
        *sorted((ROOT / "examples/maintenance_upstream").rglob("*")),
    ]
    tracked = [path for path in tracked if path.is_file()]
    before = {path: m.file_hash(path) for path in tracked}

    m.evaluate_upstream(MANIFEST, mode="execute")

    assert {path: m.file_hash(path) for path in tracked} == before


def test_upstream_report_is_rederived_and_tampering_is_rejected(executed: dict) -> None:
    verified = m.verify_upstream_report(executed, MANIFEST)
    assert verified["execution_complete"] is True
    assert verified["review_status"] == "deterministic_oracle"
    assert verified["claim_supported"] is False
    assert verified["acceptance"]["unsafe_missed_invalidation"] is False
    assert verified["acceptance"]["maintenance_value_measured"] is False

    changed = copy.deepcopy(executed)
    changed["summary"]["adapters"]["current"]["false_invalidation"] = 1
    with pytest.raises(m.MaintenanceError, match="aggregate"):
        m.verify_upstream_report(changed, MANIFEST)

    changed = copy.deepcopy(executed)
    interrupted = next(row for row in changed["cases"]
                       if row["failure_mode"] == "publication_interruption")
    interrupted["stages"]["wiki"]["after_sha256"] = "0" * 64
    with pytest.raises(m.MaintenanceError):
        m.verify_upstream_report(changed, MANIFEST)
