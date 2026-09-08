#!/usr/bin/env python3
"""Build/verify a task-free schema-2 source catalog without model requests."""
import argparse
import json
from pathlib import Path
from run import CASES, HERE, ROOT, descriptor, material, run, sha


def catalog():
    cases = []
    for cid, (repo, _) in CASES.items():
        folder = HERE / cid
        record = json.loads((folder / "execution.json").read_text())
        for item in [record["input"]["fixture"], record["runner"], *record["sources"],
                     *record["build_config"]["files"], *record["dependencies"]]:
            assert descriptor(ROOT / item["path"]) == item, item["path"]
        for anchor in record["anchors"]:
            assert anchor["text"].partition(anchor["symbol"])[0].strip(), "definition signature omits return type: " + anchor["symbol"]
            lines = (ROOT / anchor["path"]).read_text().splitlines(keepends=True)
            assert "".join(lines[anchor["line_start"]-1:anchor["line_end"]]) == anchor["text"]
            assert sha(anchor["text"].encode()) == anchor["content_hash"]
        text = (folder / "material.txt").read_text()
        assert material(record) == text
        assert len(text.encode()) == record["material_utf8_bytes"]
        tokens = (len(text.encode())+2)//3
        assert tokens == record["material_estimated_tokens"]
        assert all(step["exit_code"] == 0 for step in record["steps"])
        assert json.loads((folder / "commands.json").read_text()) == record["steps"][:-1]
        assert json.loads((folder / "observation.json").read_text()) == record["steps"][-1]
        session = json.loads((folder / "session.json").read_text())
        assert session["revision"] == record["revision"] and session["id"] == cid
        assert session["not_a_human_transcript"] is True and "knowledge_role" not in json.dumps(session)
        assert len(session["messages"]) >= 3
        assert (ROOT / record["input"]["fixture"]["path"]).read_text() in session["messages"][0]["content"]
        assert json.loads(session["messages"][1]["content"].partition("\n")[2]) == record["steps"][:-1]
        assert json.loads(session["messages"][2]["content"].partition("\n")[2]) == record["steps"][-1]
        attachments = [{"path": a["path"], "sha256": a["sha256"], "line_start": a["line_start"],
                        "line_end": a["line_end"], "kind": "source"} for a in record["anchors"]]
        attachments.extend({**descriptor(folder / name), "kind": kind} for name, kind in
                           (("reproduce.c", "harness"), ("commands.json", "command"), ("observation.json", "observation")))
        for config in record["build_config"]["files"]:
            if config["path"].endswith("/lwipopts.h"):
                attachments.append({**config, "kind": "build_configuration"})
        prior = folder / "initial-failure.json"
        history = []
        if prior.exists():
            history.append(descriptor(prior))
            failed = json.loads(prior.read_text())
            assert failed["status"] == "reproducibility_blocked" and failed["steps"][-1]["exit_code"] != 0
            prior_fixture = folder / "initial-failure.c"
            if prior_fixture.exists():
                assert sha(prior_fixture.read_bytes()) == failed["input"]["fixture"]["sha256"]
                history.append(descriptor(prior_fixture))
        cases.append({"id": cid, "corpus": repo, "repository": session["repository"],
                      "revision": record["revision"], "status": record["status"],
                      "session": descriptor(folder / "session.json"), "attachments": attachments,
                      "execution": descriptor(folder / "execution.json"),
                      "full_material": descriptor(folder / "material.txt"),
                      "material_estimated_tokens": tokens, "material_utf8_bytes": len(text.encode()),
                      "initial_failure_records": history})
    for repo in ("cjson", "lwip"):
        ordered = sorted((c for c in cases if c["corpus"] == repo), key=lambda c: (c["material_estimated_tokens"], c["id"]))
        assert len(ordered) == 6
        for rank, case in enumerate(ordered):
            case.update(length_rank_within_repository=rank+1, length_bucket=("short", "medium", "long")[rank//2])
    return {"schema_version": 2, "artifact_kind": "knowledge_reuse_source_manifest",
            "artifact_scope": "isolated_experiment", "stage": "source_only", "strip_knowledge_role": True,
            "case_count": len(cases), "cases": cases,
            "source_contract": {"inputs": "fixed public C fixtures and observed local traces",
                                "downstream_missing": ["follow_up_tasks", "required_facts_source_span_contract", "extractive_layout_template", "reviewed_answer_key"],
                                "not_an_executable_answer_manifest": True,
                                "models_called": 0, "summary_or_card_products": 0},
            "length_metric": {"formula": "ceil(len(material.txt UTF-8 bytes) / 3)",
                              "includes": ["complete fixture inputs", "selected complete source definitions", "fixed lwipopts", "all final commands/stdout/stderr/exit", "preserved initial failures"],
                              "audit_only_excluded": ["hash indexes", "compiler version banner", "transitive header bodies not part of selected definitions"],
                              "stratification": "Within each repository sort by measured tokens then id; ranks 1-2 short, 3-4 medium, 5-6 long. Relative strata, not absolute long-session claims.",
                              "padding_added": False},
            "runner": descriptor(HERE / "run.py"), "verifier": descriptor(Path(__file__).resolve()),
            "boundary": "Local API/fixture observations only; not network/production incidents, independent holdout certification, model answers, summaries, cards, or evidence of compression gain."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", action="store_true")
    parser.add_argument("--rerun", action="store_true")
    args = parser.parse_args()
    current = catalog()
    if args.catalog:
        print(json.dumps(current, ensure_ascii=False, indent=2))
    else:
        frozen = json.loads((HERE / "source-manifest.json").read_text())
        assert current == frozen, "source catalog changed"
        if args.rerun:
            for cid in CASES:
                measured = run(cid)
                recorded = json.loads((HERE / cid / "execution.json").read_text())
                assert measured.pop("material_text") == (HERE / cid / "material.txt").read_text()
                assert measured == recorded, "reproduction capture differs: " + cid
        print(json.dumps({"case_count": len(CASES), "source_hashes_verified": True,
                          "rerun_count": len(CASES) if args.rerun else 0,
                          "manifest_sha256": sha((HERE / "source-manifest.json").read_bytes())}))
