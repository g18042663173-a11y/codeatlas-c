import copy

from codeatlas.eval import closeout, rubric


def _report(lines):
    source = {"kind": "pinned_source", "repository": "repo", "revision": "a" * 40,
              "path": "file.c", "lines": lines, "text": "frozen public source"}
    anchor = {"tag": "A1", "source_ref": "file.c#L10-L20", "provenance": {
        "repository": "repo", "revision": "a" * 40, "path": "file.c",
        "line_start": 10, "line_end": 20}}
    report = {"identity": {}, "raw_trials": {"arm": [{"id": "q", "type": "semantic",
        "repetitions": [{"answer": "Mechanism [A1].", "citations": [anchor]}]}]},
        "answer_key": {"tasks": {"q": {"evidence_payload": [source]}}}}
    report["blind_review"] = rubric.blind_packet(report)
    return report


def test_pinned_body_must_match_repository_revision_and_cover_range():
    for lines, risk in (("10-20", 0), ("10-14,15-20", 0), ("10-12", 1), ("1-5", 1)):
        report = _report(lines)
        before = copy.deepcopy(report)
        result = closeout.audit_wiki(report)
        assert result["range_review_required_trials"] == risk
        assert result["mapping_changed_trials"] == 0
        assert report == before
    report = _report("10-20")
    report["answer_key"]["tasks"]["q"]["evidence_payload"][0]["revision"] = "b" * 40
    report["blind_review"] = rubric.blind_packet(report)
    assert closeout.audit_wiki(report)["range_review_required_trials"] == 1


def test_partial_and_absent_are_risk_not_automatic_wrong_answers():
    partial = closeout.audit_wiki(_report("10-12"))
    absent = closeout.audit_wiki(_report("1-5"))
    assert partial["trials"][0]["incomplete_cited_ranges"][0]["coverage"] == "partial"
    assert absent["trials"][0]["incomplete_cited_ranges"][0]["coverage"] == "absent"
    assert "correct" not in absent["trials"][0]
    assert closeout._ranges("0-10,20-19,nonsense,30") == {30}


def _traced_report(tool="wiki_section"):
    report = _report("1-5")
    trial = report["raw_trials"]["arm"][0]["repetitions"][0]
    anchor = trial["citations"][0]
    origin = anchor["provenance"]
    origin.update(knowledge_set_id="ks-frozen", source_content_hash="content",
                  build_config_hash="build", definition_hash="definition", usr="c:@F@foo")
    if tool == "wiki_section":
        origin.update(page_id="files/file.c", input_hash="page-input")
        output = {"page_id": origin["page_id"], "markdown": "## Flow\nfoo -> bar", "truncated": True,
                  "next_offset": 50, "citations": [copy.deepcopy(anchor)]}
    else:
        output = {"path": "file.c", "line_start": 10, "line_end": 20,
                  "text": "int foo(void) { return 42; }", "citations": [copy.deepcopy(anchor)]}
    trial["events"] = [{"tool": tool, "status": "completed", "result": output}]
    report["blind_review"] = rubric.blind_packet(report)
    return report


def test_restore_exact_original_tool_text_without_changing_answers_or_scores():
    from codeatlas.eval.campaign_review import _semantic_review_item
    for tool in ("wiki_section", "code_read"):
        report = _traced_report(tool)
        before = copy.deepcopy(report)
        repaired = closeout.restore_traced_bodies(report, report["blind_review"])
        item = repaired["items"][0]
        citation = item["evidence_payload"][0]
        assert citation["kind"] == "frozen_tool_excerpt"
        assert citation["text"] in ("## Flow\nfoo -> bar", "int foo(void) { return 42; }")
        assert any(e.get("text") == citation["text"] for e in _semantic_review_item(item)["evidence_payload"]["other_evidence"])
        assert item["answer"] == report["blind_review"]["items"][0]["answer"]
        assert item["answer_key"] == report["blind_review"]["items"][0]["answer_key"]
        assert repaired["run_hash"] == report["run_hash"]
        assert report == before
        if tool == "wiki_section":
            assert citation["restoration"]["fragments"][0]["truncated"] is True


def test_restore_rejects_cross_version_missing_identity_and_tampered_projection():
    import pytest
    from codeatlas.contracts import digest
    for field in ("knowledge_set_id", "revision", "input_hash"):
        report = _traced_report()
        trial = report["raw_trials"]["arm"][0]["repetitions"][0]
        trial["events"][0]["result"]["citations"][0]["provenance"][field] = "different"
        packet = rubric.blind_packet(report)
        assert closeout.restore_traced_bodies(report, packet)["restoration"]["entries"] == []
    report = _traced_report()
    del report["raw_trials"]["arm"][0]["repetitions"][0]["citations"][0]["provenance"]["knowledge_set_id"]
    packet = rubric.blind_packet(report)
    assert closeout.restore_traced_bodies(report, packet)["restoration"]["entries"] == []
    report = _traced_report()
    packet = copy.deepcopy(report["blind_review"])
    packet["items"][0]["answer"] = "a different answer"
    packet["packet_hash"] = digest({k: v for k, v in packet.items() if k != "packet_hash"})
    with pytest.raises(ValueError, match="unchanged answer/gold"):
        closeout.restore_traced_bodies(report, packet)
