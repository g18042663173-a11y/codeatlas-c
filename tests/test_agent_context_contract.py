"""Agent planner/context regression tests. Every transport is an offline stub."""
from __future__ import annotations

import copy
import hashlib
import json
import urllib.request
import sqlite3

import pytest

from codeatlas import agent, contracts
from codeatlas.llm.client import OpenAICompatClient, OutputTruncated


def event(tool="code_read", *, status="completed", text="return 42;", arguments=None):
    return {"tool": tool, "phase": "verify", "status": status,
            "wiki_available": True, "wiki_required": True,
            "arguments": arguments or {"path": "a.c", "line_start": 10, "line_end": 20},
            "result": {"text": text, "citations": [{"tag": "A1", "level": "A", "uid": "code:a",
                "provenance": {"path": "a.c", "line_start": 10, "line_end": 20}}]}}


def stub_http(monkeypatch, content='{"action":"finish"}', *, finish="stop", usage=None):
    calls = []
    payload = {"model": "fixture", "choices": [{"finish_reason": finish,
               "message": {"content": content, "reasoning_content": "PRIVATE_HIDDEN_REASONING"}}],
               "usage": usage or {"prompt_tokens": 44, "completion_tokens": 9, "total_tokens": 53}}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    def urlopen(request, **_kwargs):
        calls.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


def client():
    return OpenAICompatClient("https://fixture.invalid/v1", "UNIT_TEST_SECRET", "fixture")


def test_flat_contract_includes_wiki_and_search_and_executes_same_contract(monkeypatch):
    calls = stub_http(monkeypatch, '{"action":"tool","tool":"wiki_outline","arguments":{"offset":2,"budget":400}}')
    obj = client()
    action = obj.next_action("query", [], read_mode="flat")
    prompt = json.loads(calls[0]["messages"][1]["content"])
    assert {"wiki_outline", "wiki_section", "search_evidence", "finish"} <= set(prompt["allowed_next"])
    assert set(prompt["allowed_next"]) == set(contracts.agent_action_contract([], read_mode="flat")["allowed_next"])
    assert "offset" in json.dumps(prompt["tool_contract"], ensure_ascii=False)
    assert agent._validate_action(action)["tool"] == "wiki_outline"


@pytest.mark.parametrize("status", ["failed", "rejected", None])
def test_only_completed_wiki_prerequisites_unlock_structured_reads(monkeypatch, status):
    events = [event("search_evidence"), event("wiki_outline"), event("wiki_section", status=status)]
    contract = contracts.agent_action_contract(events, read_mode="progressive")
    assert "code_read" not in contract["allowed_next"]
    assert "finish" not in contract["allowed_next"]
    action = {"action": "tool", "tool": "code_read", "arguments": {"usr": "f"}}
    with pytest.raises(agent.AgentError):
        agent._enforce_wiki_order("如何工作", events, action, True)
    calls = stub_http(monkeypatch, '{"action":"tool","tool":"wiki_section","arguments":{"page_id":"repo","section":"Flow"}}')
    client().next_action("如何工作", events)
    assert json.loads(calls[0]["messages"][1]["content"])["allowed_next"] == contract["allowed_next"]


def test_completed_wiki_read_keeps_pagination_tools_available():
    events = [event("search_evidence"), event("wiki_outline"), event("wiki_section")]
    allowed = contracts.agent_action_contract(events, read_mode="progressive")["allowed_next"]
    assert set(allowed) == agent.ALLOWED_TOOLS | {"finish"}


def test_unavailable_wiki_is_removed_in_both_modes():
    events = [dict(event("search_evidence"), wiki_available=False)]
    for mode in ("flat", "progressive"):
        allowed = contracts.agent_action_contract(events, read_mode=mode)["allowed_next"]
        assert "wiki_outline" not in allowed and "wiki_section" not in allowed
        assert {"search_evidence", "code_read", "finish"} <= set(allowed)


@pytest.mark.parametrize("arguments", [
    {"usr": "f", "path": "a.c", "line_start": 1, "line_end": 2},
    {"usr": "f", "line_start": 1, "line_end": 2},
    {"path": "a.c", "line_start": True, "line_end": 2},
    {"path": "a.c", "line_start": 1, "line_end": 301},
])
def test_shared_code_read_parameter_forms_are_exclusive_and_keep_300_line_cap(arguments):
    with pytest.raises(agent.AgentError):
        agent._validate_action({"action": "tool", "tool": "code_read", "arguments": arguments})


def test_invalid_action_extra_fields_are_not_an_ignored_reasoning_channel():
    with pytest.raises(agent.AgentError):
        agent._validate_action({"action": "tool", "tool": "resolve_symbol",
                                "arguments": {"symbol": "f"}, "reasoning": "private"})
    assert agent._validate_action({"tool": "resolve_symbol", "arguments": {"symbol": "f"}})["action"] == "tool"


def test_compact_events_preserves_complete_recent_result_status_and_continuation():
    original = event(text="整块源码证据" * 1500)
    original["result"].update(next_offset=4500, truncated=True,
                              continuation={"tool": "wiki_section", "arguments": {"page_id": "repo", "section": "Flow", "offset": 4500}})
    before = copy.deepcopy(original)
    compacted = agent._compact_events([original])[0]
    assert compacted["result"]["text"] == original["result"]["text"]
    assert compacted["status"] == "completed"
    assert compacted["result"]["continuation"] == original["result"]["continuation"]
    assert original == before


def test_structured_budget_omits_old_blocks_whole_but_keeps_query_actions_latest_and_evidence():
    events = [event(text="OLD_BLOCK" * 5000), event(text="中间完整块" * 2500), event(text="LATEST_COMPLETE" * 300)]
    evidence = [{"tag": "A1", "path": "a.c", "line_start": 10, "line_end": 20}]
    payload = {"query": "问题必须完整" * 100, "allowed_next": ["code_read", "finish"],
               "tool_contract": {"code_read": "path/start/end or usr"},
               "evidence": evidence, "prior_events": events}
    before = copy.deepcopy(payload)
    system, user, meta = contracts.budget_json_messages("SYSTEM", payload)
    sent = json.loads(user)
    assert sent["query"] == payload["query"]
    assert sent["allowed_next"] == payload["allowed_next"]
    assert sent["tool_contract"] == payload["tool_contract"]
    assert sent["evidence"] == evidence
    assert sent["prior_events"][-1] == events[-1]
    assert meta["omitted_event_indices"] == [0, 1]
    assert sent["prior_events"][0]["result"]["content_hash"] == contracts.digest(events[0]["result"])
    assert "OLD_BLOCK" not in user and "中间完整块" not in user
    assert contracts.estimated_tokens(system + user) + 32 <= 8000
    assert payload == before


def test_latest_completed_result_remains_when_the_last_attempt_failed():
    events = [event(text="ancient" * 10000), event(text="SUCCESS" * 1000),
              event(status="failed", text="failure metadata")]
    _, raw, _ = contracts.budget_json_messages("system", {"query": "q", "prior_events": events})
    sent = json.loads(raw)
    assert sent["prior_events"][-2:] == events[-2:]


def test_required_oversize_block_raises_before_transport_and_offers_narrower_read(monkeypatch):
    calls = stub_http(monkeypatch)
    obj = client()
    newest = event(text="required" * 9000)
    with pytest.raises(contracts.ContextBudgetExceeded) as raised:
        obj.next_action("where is f", [dict(newest, wiki_required=False)], read_mode="flat")
    assert calls == []
    assert raised.value.details["status"] == "oversize"
    continuation = raised.value.details["continuations"][0]
    assert continuation["tool"] == "code_read"
    assert continuation["arguments"]["line_end"] < newest["arguments"]["line_end"]
    assert obj.last_planner_diagnostics["error_class"] == "ContextBudgetExceeded"


def test_planner_transport_gets_valid_bounded_json_with_old_results_omitted(monkeypatch):
    calls = stub_http(monkeypatch)
    events = [dict(event(text="old" * 16000), wiki_required=False),
              dict(event(text="latest" * 700), wiki_required=False)]
    obj = client()
    obj.next_action("query完整", events, read_mode="flat")
    wire = calls[0]
    prompt = json.loads(wire["messages"][1]["content"])
    assert prompt["query"] == "query完整"
    assert prompt["prior_events"][-1]["result"]["text"] == events[-1]["result"]["text"]
    assert wire["max_tokens"] <= 1200
    assert contracts.estimated_tokens("".join(m["content"] for m in wire["messages"])) + 32 <= 8000
    assert obj.last_budget["truncated"] is False


def test_answer_structured_preserves_registry_and_recent_source_as_complete_json(monkeypatch):
    calls = stub_http(monkeypatch, "Verified [A1].")
    events = [event(text="old" * 15000), event(text="IMPORTANT_SOURCE" * 600)]
    registry = [{"tag": "A1", "path": "a.c", "range": [10, 20]}]
    assert client().answer_structured("SYSTEM", "QUESTION", events, registry) == "Verified [A1]."
    wire = calls[0]
    prompt = json.loads(wire["messages"][1]["content"])
    assert prompt["query"] == "QUESTION" and prompt["evidence"] == registry
    assert prompt["prior_events"][-1]["result"]["text"] == events[-1]["result"]["text"]


@pytest.mark.parametrize("raw,error", [("not JSON UNIT_TEST_SECRET", json.JSONDecodeError),
                                        ('{"action":"finish","reasoning":"PRIVATE_HIDDEN_REASONING"}', ValueError)])
def test_planner_diagnostics_hash_invalid_content_without_retaining_secrets(monkeypatch, raw, error):
    calls = stub_http(monkeypatch, raw, usage={"prompt_tokens": 44, "completion_tokens": 9,
                      "api_key": "UNIT_TEST_SECRET", "reasoning": "PRIVATE_HIDDEN_REASONING",
                      "completion_tokens_details": {"reasoning_tokens": 7, "reasoning_text": "PRIVATE_HIDDEN_REASONING"}})
    obj = client()
    with pytest.raises(error):
        obj.next_action("where", [], read_mode="flat")
    diagnostic = obj.last_planner_diagnostics
    assert diagnostic["content_hash"] == hashlib.sha256(raw.encode()).hexdigest()
    assert diagnostic["finish_reason"] == "stop"
    assert diagnostic["error_class"] == error.__name__
    assert diagnostic["usage"]["prompt_tokens"] == 44
    encoded = json.dumps(diagnostic)
    assert "UNIT_TEST_SECRET" not in encoded and "PRIVATE_HIDDEN_REASONING" not in encoded
    assert len(calls) == 1


def test_length_diagnostics_survive_output_truncation_without_hidden_reasoning(monkeypatch):
    stub_http(monkeypatch, '{"action":', finish="length")
    obj = client()
    with pytest.raises(OutputTruncated):
        obj.next_action("where", [], read_mode="flat")
    assert obj.last_planner_diagnostics["finish_reason"] == "length"
    assert obj.last_planner_diagnostics["content_hash"] == hashlib.sha256(b'{"action":').hexdigest()
    assert obj.last_planner_diagnostics["error_class"] == "OutputTruncated"
    assert "PRIVATE_HIDDEN_REASONING" not in json.dumps(obj.last_planner_diagnostics)


def test_diagnostics_are_reset_after_a_previous_success(monkeypatch):
    stub_http(monkeypatch)
    obj = client()
    obj.next_action("where", [], read_mode="flat")
    with pytest.raises(contracts.ContextBudgetExceeded):
        obj.next_action("x" * 50000, [], read_mode="flat")
    assert obj.last_planner_diagnostics["content_hash"] is None
    assert obj.last_planner_diagnostics["finish_reason"] is None
    assert obj.last_planner_diagnostics["usage"] == {}


def test_legacy_plan_tool_client_still_receives_complete_events():
    class Legacy:
        def plan_tool(self, query, events):
            assert query == "q" and events[-1]["result"]["text"] == "complete" * 1000
            return {"tool": "resolve_symbol", "arguments": {"symbol": "f"}}
    assert agent._model_action(Legacy(), "q", [dict(event(text="complete" * 1000), wiki_required=False)], "flat")["tool"] == "resolve_symbol"


def test_failed_tools_do_not_complete_wiki_prerequisites_and_expose_continuation(kb, monkeypatch):
    conn, data_dir = kb
    monkeypatch.setattr(agent, "_execute", lambda *_a, **_k: {"error": "wiki_section_not_found"})
    failed = agent._run_call(conn, {"tool": "wiki_section", "arguments": {"page_id": "repo", "section": "missing"}},
                             embedder=None, data_dir=data_dir)
    assert failed["status"] == "failed"
    monkeypatch.setattr(agent, "_execute", lambda *_a, **_k: {"markdown": "complete page", "truncated": True, "next_offset": 600})
    completed = agent._run_call(conn, {"tool": "wiki_section", "arguments": {"page_id": "repo", "section": "Flow", "budget": 200}},
                                embedder=None, data_dir=data_dir)
    assert completed["result"]["continuation"] == {"tool": "wiki_section", "arguments": {"page_id": "repo", "section": "Flow", "budget": 200, "offset": 600}}


def test_flat_rule_mode_can_read_wiki_and_attempt_cap_is_unchanged(kb):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    kb[0].backup(conn)
    try:
        agent.wiki_generator.generate(conn, "tests/fixtures/mini_c")
        calls = agent.run(conn, "parse_value 如何工作", data_dir=kb[1], mode="rule", read_mode="flat")
        assert any(row["tool"] == "wiki_outline" for row in calls["events"])
        assert len(calls["events"]) <= agent.MAX_TOOL_CALLS == 6
    finally:
        conn.close()


def test_legacy_finish_answer_remains_model_mode(kb):
    from tests.test_model_eval import _ProtocolModel
    conn, data_dir = kb
    result = agent.run(conn, "cJSON_Delete 在哪里定义？", data_dir=data_dir,
                       mode="model", read_mode="flat", llm_client=_ProtocolModel())
    assert result["execution_mode"] == "model", result["fallback_reason"]


def test_planner_diagnostics_are_safe_in_persisted_rejection(kb, monkeypatch):
    raw = '{"action":"finish","reasoning":"PRIVATE_HIDDEN_REASONING UNIT_TEST_SECRET"}'
    calls = stub_http(monkeypatch, raw)
    conn, data_dir = kb
    result = agent.run(conn, "cJSON_Delete 在哪里定义？", data_dir=data_dir,
                       mode="model", read_mode="flat", llm_client=client())
    assert len(calls) == 1 and result["execution_mode"] == "rule"
    assert result["planner_diagnostics"][0]["content_hash"] == hashlib.sha256(raw.encode()).hexdigest()
    stored = agent.get_run(conn, result["id"])
    diagnostic = next(e for e in stored["events"] if e["tool_name"] == "planning_attempt")["result"]["planner_diagnostics"][0]
    assert diagnostic["error_class"] == "ValueError"
    assert "UNIT_TEST_SECRET" not in json.dumps(stored)
    assert "PRIVATE_HIDDEN_REASONING" not in json.dumps(stored)


def test_diagnostics_untrusted_metadata_is_not_a_storage_channel():
    diagnostic = contracts.planner_diagnostics(content_hash="UNIT_TEST_SECRET", finish_reason={"raw": "secret"},
                                               error_class=["PRIVATE_HIDDEN_REASONING"],
                                               usage={"prompt_tokens": True, "output_tokens": -1})
    assert diagnostic == {"content_hash": None, "finish_reason": "unknown", "error_class": "OtherError", "usage": {}}


def test_oversize_user_question_and_registry_never_get_dropped(monkeypatch):
    calls = stub_http(monkeypatch)
    for field in ("query", "evidence"):
        payload = {"query": "q", "evidence": [], "prior_events": [event()]}
        payload[field] = "REQUIRED" * 12000
        with pytest.raises(contracts.ContextBudgetExceeded):
            contracts.budget_json_messages("SYSTEM", payload)
    assert calls == []


def test_budget_trace_pointer_recovers_the_exact_original_body():
    original = [event(text="omitted" * 10000), event(text="retained")]
    _, user, _ = contracts.budget_json_messages("SYSTEM", {"prior_events": original})
    omitted = json.loads(user)["prior_events"][0]["result"]
    assert omitted["continuation"]["kind"] == "local_event"
    recovered = original[omitted["continuation"]["event_index"]]["result"]
    assert contracts.digest(recovered) == omitted["content_hash"]


def test_planner_rejects_tool_outside_completed_prerequisites_before_execution(monkeypatch):
    calls = stub_http(monkeypatch, '{"action":"tool","tool":"code_read","arguments":{"usr":"f"}}')
    obj = client()
    with pytest.raises(ValueError):
        obj.next_action("如何工作", [event("search_evidence")])
    assert len(calls) == 1 and obj.last_planner_diagnostics["error_class"] == "ValueError"


def test_identical_citation_metadata_can_be_referenced_without_losing_recent_result():
    citation = {"tag": "A1", "provenance": {"identity": "FULL_IDENTITY" * 1100}}
    newest = event(text="COMPLETE_SOURCE" * 300)
    newest["result"]["citations"] = [citation]
    payload = {"query": "q", "evidence": [citation], "prior_events": [newest]}
    original = copy.deepcopy(payload)
    _, user, meta = contracts.budget_json_messages("SYSTEM", payload)
    sent = json.loads(user)
    projected = sent["prior_events"][-1]["result"]
    assert projected["text"] == newest["result"]["text"]
    refs = projected["citations"]
    assert refs == [{"evidence_ref": 0}]
    projected["citations"] = [sent["evidence"][ref["evidence_ref"]] for ref in refs]
    assert projected == newest["result"]
    assert meta["omitted_event_indices"] == [] and payload == original


def test_omitted_body_hash_is_of_original_even_after_citation_deduplication():
    citation = {"tag": "A1", "provenance": {"identity": "SAME" * 1000}}
    old = event(text="OLD" * 15000)
    old["result"]["citations"] = [citation]
    _, user, _ = contracts.budget_json_messages("SYSTEM", {
        "evidence": [citation], "prior_events": [old, event()]})
    assert json.loads(user)["prior_events"][0]["result"]["content_hash"] == contracts.digest(old["result"])


def test_oversize_usr_read_returns_explicit_bounded_source_continuation(kb):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    kb[0].backup(conn)
    try:
        row = conn.execute("SELECT usr,line_start FROM node WHERE name='cJSON_Delete' AND is_definition=1").fetchone()
        conn.execute("UPDATE node SET line_end=line_start+400 WHERE usr=?", (row["usr"],))
        read = agent._run_call(conn, {"tool": "code_read", "arguments": {"usr": row["usr"]}},
                               embedder=None, data_dir=kb[1])
        assert read["status"] == "failed"
        result = read["result"]
        assert result["status"] == "oversize" and "text" not in result
        pointer = result["continuation"]
        assert pointer["tool"] == "code_read" and "usr" not in pointer["arguments"]
        assert pointer["arguments"]["line_start"] == row["line_start"]
        assert pointer["arguments"]["line_end"] - row["line_start"] + 1 == 300
        assert agent._validate_action(pointer)["tool"] == "code_read"
    finally:
        conn.close()


def test_baseline_planner_does_not_slice_json_either(monkeypatch):
    calls = stub_http(monkeypatch)
    obj = client()
    obj.next_baseline_action("q", [event(text="old" * 20000), event(text="latest" * 800)])
    prompt = json.loads(calls[0]["messages"][1]["content"])
    assert prompt["prior_events"][-1]["result"]["text"] == "latest" * 800
    assert prompt["allowed_next"] == ["repo_search", "code_read", "finish"]


def test_structured_answer_oversize_is_never_sent_or_retried(monkeypatch):
    calls = stub_http(monkeypatch)
    obj = client()
    with pytest.raises(contracts.ContextBudgetExceeded):
        obj.answer_structured("SYSTEM", "QUESTION", [event(text="full" * 20000)], [])
    assert calls == [] and obj.request_count == 0
    assert obj.last_context_budget["status"] == "oversize"


def test_finish_diagnostic_is_persisted_without_consuming_a_tool_slot(kb, monkeypatch):
    calls = stub_http(monkeypatch)
    obj = client()
    monkeypatch.setattr(obj, "answer_structured", lambda *_a: "Confirmed [A1].")
    result = agent.run(kb[0], "cJSON_Delete 在哪里定义？", data_dir=kb[1],
                       mode="model", read_mode="flat", llm_client=obj)
    assert result["execution_mode"] == "model" and len(calls) == 1
    assert result["tool_attempts"] == 1 and len(result["planner_diagnostics"]) == 1
    stored = agent.get_run(kb[0], result["id"])
    assert stored["events"][0]["result"]["planner_diagnostics"][0]["finish_reason"] == "stop"


def test_failed_wiki_rule_step_does_not_unlock_structured_tools(kb, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    kb[0].backup(conn)
    try:
        agent.wiki_generator.generate(conn, "tests/fixtures/mini_c")
        execute = agent._execute
        def fail_section(db, call, **kwargs):
            return {"error": "missing_section"} if call["tool"] == "wiki_section" else execute(db, call, **kwargs)
        monkeypatch.setattr(agent, "_execute", fail_section)
        result = agent.run(conn, "parse_value 如何工作", data_dir=kb[1], mode="rule")
        assert [e["tool"] for e in result["events"]] == ["search_evidence", "wiki_outline", "wiki_section"]
        assert result["events"][-1]["status"] == "failed"
    finally:
        conn.close()


def test_rule_mode_does_not_call_unavailable_wiki(kb):
    result = agent.run(kb[0], "parse_value 如何工作", data_dir=kb[1], mode="rule")
    assert not any(e["tool"].startswith("wiki_") for e in result["events"])


def test_planning_trace_body_hash_still_matches_after_registry_persistence(kb, monkeypatch):
    projected = []
    class Planner:
        def next_action(self, query, events):
            projected.append(copy.deepcopy(events))
            if len(projected) == 1:
                return {"tool": "code_read", "arguments": {"path": "mini.c", "line_start": 20, "line_end": 23}}
            return {"action": "finish"}
    result = agent.run(kb[0], "cJSON_Delete 在哪里定义？", data_dir=kb[1],
                       mode="model", read_mode="flat", llm_client=Planner())
    persisted_projection = agent._compact_events(result["events"])
    assert contracts.digest(projected[-1][-1]["result"]) == contracts.digest(persisted_projection[-1]["result"])


def test_nontext_output_keeps_finish_reason_without_retaining_payload(monkeypatch):
    stub_http(monkeypatch, {"raw": "UNIT_TEST_SECRET PRIVATE_HIDDEN_REASONING"})
    obj = client()
    with pytest.raises(ValueError):
        obj.next_action("q", [], read_mode="flat")
    assert obj.last_planner_diagnostics["finish_reason"] == "stop"
    assert obj.last_planner_diagnostics["content_hash"] is None
    assert "UNIT_TEST_SECRET" not in json.dumps(obj.last_planner_diagnostics)
