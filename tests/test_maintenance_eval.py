"""Maintenance evaluator contract tests; never mutate or compile upstream corpora."""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import shutil

import pytest
import yaml

from codeatlas.eval import maintenance_eval as m

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "eval/maintenance.yaml"


@pytest.fixture
def bundle(tmp_path):
    target = tmp_path / "eval"
    target.mkdir()
    shutil.copyfile(MANIFEST, target / "maintenance.yaml")
    shutil.copytree(ROOT / "eval/templates/maintenance_cases", target / "templates/maintenance_cases")
    return target / "maintenance.yaml"


@lru_cache(maxsize=1)
def declarations():
    return {item["case"]["id"]: item for item in m.load_manifest(MANIFEST)["cases"]}


def observed_executor(case, inputs):
    """Explicit synthetic test observations, not claimed compiler execution."""
    assert "classification" not in case and "external_oracle" not in inputs["paths"]
    assert "external_oracle" not in case["artifacts"]
    declaration = declarations()[case["id"]]
    oracle = declaration["oracle"]
    if declaration["case"]["classification"] == "failure":
        return {"compile_exit": 1, "run_exit": None, "assertions": []}
    return {"compile_exit": 0, "run_exit": 0,
            "assertions": [{"id": a["id"], "actual": a["expected"]} for a in oracle["assertions"]],
            "expected_revalidation": "untrusted executor override must be ignored",
            "main_anchor_before": "1" * 64, "main_anchor_after": "1" * 64,
            "adapters": {"current": {"decision": "revalidate" if oracle["expected_revalidation"] else "keep",
                                      "citation_refreshed": True, "wiki_updated": oracle["expected_revalidation"],
                                      "elapsed_ms": 4, "cost": {"value": 0.1, "unit": "USD"}}}}


def test_shipped_manifest_has_24_separate_bound_specs_and_no_claimed_run():
    validated = m.load_manifest(MANIFEST)
    assert validated["counts"] == {c: {k: 4 for k in m.CLASSIFICATIONS} for c in ("cjson", "lwip")}
    assert len({x["directory"] for x in validated["cases"]}) == 24
    for item in validated["cases"]:
        assert item["case"]["material_kind"] == "synthetic_fixture"
        assert all(m.file_hash(p) == item["case"]["artifacts"][k]["sha256"] for k, p in item["paths"].items())
    report = m.evaluate(MANIFEST)
    assert report["status"] == "not_run" and report["summary"]["not_run"] == 24
    assert report["acceptance"]["manifest_valid"] and not report["acceptance"]["execution_complete"]
    assert not report["acceptance"]["upstream_validated"]
    assert all(r["compile_exit"] is None and r["run_exit"] is None for r in report["cases"])
    assert report["summary"]["adapters"]["current"]["decision_correct"]["rate"] is None
    assert "not_run" in m.render(report)


@pytest.mark.parametrize("kind", m.ARTIFACTS)
def test_every_input_hash_is_enforced(bundle, kind):
    item = m.load_manifest(bundle)["cases"][0]
    with item["paths"][kind].open("a") as out:
        out.write("\nTAMPERED")
    with pytest.raises(m.MaintenanceError, match="hash mismatch"):
        m.evaluate(bundle)


@pytest.mark.parametrize("damage", ["distribution", "duplicate", "overlap", "traversal"])
def test_distribution_ids_and_isolation_are_strict(damage):
    value = deepcopy(m.load_manifest(MANIFEST)["manifest"])
    if damage == "distribution":
        value["cases"].pop()
    elif damage == "duplicate":
        value["cases"][1]["id"] = value["cases"][0]["id"]
    elif damage == "overlap":
        value["cases"][1]["case_dir"] = value["cases"][0]["case_dir"]
    else:
        value["cases"][0]["artifacts"]["source"]["path"] = "../source.c"
    with pytest.raises(m.MaintenanceError):
        m.validate_manifest(value, root=MANIFEST.parent)


def test_codeatlas_derived_oracle_rejected_even_if_hash_is_rebound(bundle):
    loaded = m.load_manifest(bundle)
    item = loaded["cases"][0]
    oracle = item["oracle"]
    oracle["derived_from_codeatlas"] = True
    item["paths"]["external_oracle"].write_text(json.dumps(oracle))
    value = loaded["manifest"]
    value["cases"][0]["artifacts"]["external_oracle"]["sha256"] = m.file_hash(item["paths"]["external_oracle"])
    bundle.write_text(yaml.safe_dump(value))
    with pytest.raises(m.MaintenanceError, match="cannot define"):
        m.evaluate(bundle)


def test_oracle_classification_not_replaceable_by_metadata():
    value = deepcopy(m.load_manifest(MANIFEST)["manifest"])
    value["cases"][0]["classification"] = "unrelated_to_claim"
    with pytest.raises(m.MaintenanceError, match="expected_revalidation conflicts"):
        m.validate_manifest(value, root=MANIFEST.parent)
    value = deepcopy(m.load_manifest(MANIFEST)["manifest"])
    value["cases"][0]["material_kind"] = "upstream_mutation"
    with pytest.raises(m.MaintenanceError, match="source kind must match"):
        m.validate_manifest(value, root=MANIFEST.parent)


def test_all_fixture_patches_bind_exact_original_source_without_touching_corpora():
    for item in m.load_manifest(MANIFEST)["cases"]:
        patch = item["paths"]["patch"].read_text().splitlines()
        original = "\n".join(line[1:] for line in patch[3:] if line.startswith("-")) + "\n"
        assert original == item["paths"]["source"].read_text()


def test_explicit_executor_and_semantic_denominators():
    with pytest.raises(m.MaintenanceError, match="explicit executor"):
        m.evaluate(MANIFEST, mode="execute")
    with pytest.raises(m.MaintenanceError, match="never execute"):
        m.evaluate(MANIFEST, executor=observed_executor)
    report = m.evaluate(MANIFEST, mode="execute", executor=observed_executor)
    summary = report["summary"]
    assert summary["compile_failed"] == 8 and summary["semantic_eligible"] == 16
    assert report["acceptance"]["all_expected_outcomes"]
    assert report["acceptance"]["current_decisions_passed"]
    assert not report["acceptance"]["upstream_validated"]
    current = summary["adapters"]["current"]
    assert current["decision_correct"] == {"count": 16, "denominator": 16, "rate": 1}
    assert current["missed_invalidation"]["denominator"] == 8
    assert current["false_invalidation"]["denominator"] == 8
    assert current["citation_refreshed"]["rate"] == 1
    assert current["wiki_updated"]["count"] == 8
    assert summary["adapters"]["main_anchor_hash"]["missed_invalidation"]["rate"] == 1
    assert summary["adapters"]["invalidate_all"]["false_invalidation"]["rate"] == 1
    assert report["cases"][0]["expected_revalidation"] is True
    assert report["cases"][0]["compile_ms"] is None
    assert report["cases"][0]["adapters"]["main_anchor_hash"]["cost"] is None
    assert report["amortization"]["break_even_queries"] is None


def test_report_verifier_rederives_cases_and_rejects_green_flag_forgery():
    report = m.evaluate(MANIFEST, mode="execute", executor=observed_executor)
    verified = m.verify_report(report, MANIFEST)
    assert verified["integrity_ok"] and verified["execution_complete"]
    assert not verified["claim_supported"]  # the oracle still lacks an independent review envelope

    forged = deepcopy(report)
    forged["acceptance"]["oracle_independently_validated"] = True
    with pytest.raises(m.MaintenanceError, match="acceptance flag"):
        m.verify_report(forged, MANIFEST)


@pytest.mark.parametrize("failure", ["compile", "run", "assertions", "adapter", "missing"])
def test_unexpected_failure_never_improves_semantic_metrics(failure):
    def executor(case, inputs):
        raw = observed_executor(case, inputs)
        if case["id"] == "cjson-nesting-limit":
            if failure == "compile":
                raw.update(compile_exit=2, run_exit=None)
            elif failure == "run":
                raw["run_exit"] = 3
            elif failure == "assertions":
                raw["assertions"][0]["actual"] = "WRONG"
            elif failure == "adapter":
                raw["adapters"]["current"]["decision"] = "unavailable"
            else:
                raw["adapters"].pop("current")
        return raw
    report = m.evaluate(MANIFEST, mode="execute", executor=executor)
    assert not report["acceptance"]["current_decisions_passed"]
    assert report["summary"]["adapters"]["current"]["decision_count"] == 15
    assert report["summary"]["adapters"]["current"]["missed_invalidation"]["denominator"] == 7


def test_bad_current_decision_cannot_pass_from_compiler_success():
    def executor(case, inputs):
        raw = observed_executor(case, inputs)
        if raw.get("adapters"):
            raw["adapters"]["current"]["decision"] = "keep"
        return raw
    report = m.evaluate(MANIFEST, mode="execute", executor=executor)
    assert report["acceptance"]["all_expected_outcomes"]
    assert not report["acceptance"]["current_decisions_passed"]
    assert report["summary"]["adapters"]["current"]["missed_invalidation"]["rate"] == 1


def test_executor_errors_sanitized_and_artifact_mutation_rejected(bundle):
    def broken(case, inputs):
        raise RuntimeError("secret request details")
    report = m.evaluate(bundle, mode="execute", executor=broken)
    assert report["summary"]["outcomes"] == {"executor_failed": 24}
    assert "secret request details" not in json.dumps(report)
    def mutation(case, inputs):
        inputs["paths"]["source"].write_text("illegal in-place modification")
        return {}
    with pytest.raises(m.MaintenanceError, match="modified a pre-registered artifact"):
        m.evaluate(bundle, mode="execute", executor=mutation)


def test_hash_baseline_distinguishes_changed_deleted_and_unchanged():
    assert m.main_anchor_hash_decision("1" * 64, "1" * 64) == "keep"
    assert m.main_anchor_hash_decision("1" * 64, "2" * 64) == "revalidate"
    assert m.main_anchor_hash_decision("1" * 64, None) == "revalidate"
    with pytest.raises(m.MaintenanceError):
        m.main_anchor_hash_decision(None, None)


@pytest.mark.parametrize("damage", ["adapter_shape", "unknown_adapter", "unknown_assertion", "missing_hash", "nan_time"])
def test_invalid_observations_fail_closed(damage):
    def executor(case, inputs):
        raw = observed_executor(case, inputs)
        if case["id"] == "cjson-nesting-limit":
            if damage == "adapter_shape":
                raw["adapters"]["current"] = "keep"
            elif damage == "unknown_adapter":
                raw["adapters"]["gold_oracle"] = {}
            elif damage == "unknown_assertion":
                raw["assertions"].append({"id": "new_post_hoc_gold", "actual": True})
            elif damage == "missing_hash":
                raw.pop("main_anchor_after")
            else:
                raw["compile_ms"] = float("nan")
        return raw
    with pytest.raises(m.MaintenanceError):
        m.evaluate(MANIFEST, mode="execute", executor=executor)


@pytest.mark.parametrize("quality,unit,base,candidate,reason", [
    (False, "USD", 5, 1, "quality_not_comparable"),
    (True, "ms", 5, 1, "unit_mismatch"),
    (True, "USD", 1, 1, "no_positive_saving"),
    (True, "USD", 1, 5, "no_positive_saving"),
    (True, "USD", None, 1, "missing_measurements"),
    (True, "USD", float("nan"), 1, "missing_measurements"),
])
def test_amortization_guards(quality, unit, base, candidate, reason):
    value = m.amortization({"value": 12, "unit": "USD"}, {"value": base, "unit": unit},
                           {"value": candidate, "unit": "USD"}, quality_comparable=quality)
    assert value["break_even_queries"] is None and value["reason"] == reason


def test_amortization_rounds_up_only_quality_matched_positive_same_unit_savings():
    assert m.amortization({"value": 11, "unit": "USD"}, {"value": 5, "unit": "USD"},
                          {"value": 1, "unit": "USD"}, quality_comparable=True)["break_even_queries"] == 3
