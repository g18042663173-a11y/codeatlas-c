"""Protocol-level OpenAI-compatible Agent tests against a local HTTP server."""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from codeatlas import agent
from codeatlas.llm.client import (
    BudgetExceeded,
    OpenAICompatClient,
    OutputTruncated,
    ProviderUsageLimitReached,
)


@contextmanager
def _fake_openai(contents: list[str], *, delay: float = 0.0,
                 finish_reason: str | None = None, status: int = 200,
                 error_payload: dict | None = None):
    state = SimpleNamespace(contents=list(contents), requests=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size))
            state.requests.append({
                "path": self.path, "model": body.get("model"),
                "authorized": self.headers.get("Authorization") == "Bearer test-key",
                "messages": body.get("messages"),
                "thinking": body.get("thinking"),
                "response_format": body.get("response_format"),
            })
            if delay:
                time.sleep(delay)
            if status != 200:
                payload = json.dumps(error_payload or {"error": {"type": "fixture"}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            content = state.contents.pop(0)
            payload = json.dumps({
                "choices": [{"message": {"content": content},
                             "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
            }).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except OSError:
                # The timeout test intentionally closes the client socket first.
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _client(base_url: str) -> OpenAICompatClient:
    return OpenAICompatClient(base_url, "test-key", "fixture-model")


def test_real_http_protocol_runs_model_first_agent_without_key_leak(kb):
    conn, data_dir = kb
    responses = [
        json.dumps({"action": "tool", "tool": "search_evidence",
                    "arguments": {"query": "parse_value 的影响范围"}}),
        json.dumps({"action": "finish"}),
        "parse_value 的影响范围来自当前固定快照 [A1]。",
    ]
    with _fake_openai(responses) as (base_url, state):
        client = _client(base_url)
        result = agent.run(conn, "parse_value 的影响范围", data_dir=data_dir,
                           llm_client=client, mode="auto")
    assert result["execution_mode"] == "model" and result["answer_source"] == "model"
    assert result["fallback_reason"] is None and len(state.requests) == 3
    assert all(item["path"] == "/v1/chat/completions" for item in state.requests)
    assert all(item["authorized"] and item["model"] == "fixture-model" for item in state.requests)
    first_planner_input = json.loads(state.requests[0]["messages"][1]["content"])
    assert first_planner_input["allowed_next"] == [
        "resolve_symbol", "code_read", "analyze_impact", "finish",
    ]
    assert client.last_usage["total_tokens"] == 16 and client.last_latency_ms >= 0
    assert client.request_count == 3 and client.usage_totals["total_tokens"] == 48
    assert "test-key" not in json.dumps(agent.get_run(conn, result["id"]), ensure_ascii=False)


def test_structured_calls_disable_thinking_and_request_json_object():
    response = json.dumps({"action": "finish"})
    with _fake_openai([response]) as (base_url, state):
        client = OpenAICompatClient(
            base_url, "test-key", "fixture-model", thinking_mode="disabled",
        )
        assert client.next_action("where", [], read_mode="flat") == {"action": "finish"}
    assert state.requests[0]["thinking"] == {"type": "disabled"}
    assert state.requests[0]["response_format"] == {"type": "json_object"}


def test_length_finish_reason_is_a_recorded_failure_not_partial_output(tmp_path):
    from codeatlas.llm.ledger import RequestLedger

    ledger = RequestLedger(tmp_path / "ledger.sqlite3", max_requests=1)
    with _fake_openai(["partial"], finish_reason="length") as (base_url, _state):
        client = OpenAICompatClient(
            base_url, "test-key", "fixture-model", ledger=ledger,
            thinking_mode="disabled",
        )
        with pytest.raises(OutputTruncated):
            client.answer("system", "question", "evidence")
    assert client.last_response_meta["finish_reason"] == "length"
    assert ledger.summary()["failed"] == 1


def test_provider_usage_limit_is_classified_without_retaining_response_body(tmp_path):
    from codeatlas.llm.ledger import RequestLedger

    ledger = RequestLedger(tmp_path / "ledger.sqlite3", max_requests=1)
    payload = {
        "error": {
            "type": "GoUsageLimitError",
            "message": "private provider workspace and billing URL",
        }
    }
    with _fake_openai([], status=429, error_payload=payload) as (base_url, _state):
        client = OpenAICompatClient(
            base_url, "test-key", "fixture-model", ledger=ledger,
            thinking_mode="disabled",
        )
        with pytest.raises(ProviderUsageLimitReached, match="provider_usage_limit") as caught:
            client._chat("system", "question", json_object=True)
    assert caught.value.http_status == 429
    rows = ledger.summary()
    assert rows["failed"] == 1 and rows["requests"] == 1
    assert "private provider" not in json.dumps(rows)


def test_invalid_http_json_falls_back_with_reason(kb):
    conn, data_dir = kb
    with _fake_openai(["not-json"]) as (base_url, _state):
        result = agent.run(conn, "parse_value 的影响范围", data_dir=data_dir,
                           llm_client=_client(base_url), mode="model")
    assert result["execution_mode"] == "rule"
    assert result["fallback_reason"].startswith("model_plan_invalid")
    assert result["events"] and all(e["tool"] in agent.ALLOWED_TOOLS for e in result["events"] if e["status"] != "rejected")
    assert any(e["status"] == "rejected" for e in result["events"])


def test_http_model_cannot_call_non_whitelisted_tool(kb):
    conn, data_dir = kb
    forbidden = json.dumps({"action": "tool", "tool": "shell",
                            "arguments": {"command": "rm -rf /"}})
    with _fake_openai([forbidden]) as (base_url, _state):
        result = agent.run(conn, "parse_value 的影响范围", data_dir=data_dir,
                           llm_client=_client(base_url), mode="model")
    assert result["execution_mode"] == "rule"
    assert result["fallback_reason"].startswith("model_plan_invalid")
    assert all(e["tool"] in agent.ALLOWED_TOOLS for e in result["events"] if e["status"] != "rejected")


def test_http_timeout_is_visible_safe_fallback(kb, monkeypatch):
    conn, data_dir = kb
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "0.01")
    action = json.dumps({"action": "tool", "tool": "search_evidence",
                         "arguments": {"query": "parse_value 的影响范围"}})
    with _fake_openai([action], delay=0.05) as (base_url, _state):
        result = agent.run(conn, "parse_value 的影响范围", data_dir=data_dir,
                           llm_client=_client(base_url), mode="model")
    assert result["execution_mode"] == "rule"
    assert result["fallback_reason"].startswith("model_plan_invalid")
    assert result["answer_source"] == "evidence_pack"


def test_predeclared_request_and_cost_budgets_stop_before_network_call():
    with _fake_openai(["ok"]) as (base_url, state):
        client = OpenAICompatClient(
            base_url, "test-key", "fixture-model", max_requests=1,
            max_cost_usd=1.0, input_usd_per_million=1.0,
            output_usd_per_million=1.0,
        )
        assert client._chat("system", "question", max_tokens=10) == "ok"
        assert client.measured_cost_usd == pytest.approx(16 / 1_000_000)
        assert client.reserved_cost_usd > 0
        with pytest.raises(BudgetExceeded, match="MAX_REQUESTS"):
            client._chat("system", "second", max_tokens=10)
        assert len(state.requests) == 1

    with _fake_openai(["must-not-run"]) as (base_url, state):
        client = OpenAICompatClient(
            base_url, "test-key", "fixture-model", max_cost_usd=0.000001,
            input_usd_per_million=10.0, output_usd_per_million=10.0,
        )
        with pytest.raises(BudgetExceeded, match="MAX_COST_USD"):
            client._chat("system", "question", max_tokens=1200)
        assert state.requests == []


def test_answer_budget_truncates_evidence_without_dropping_question():
    question = "UNIQUE-QUESTION: which guard controls cleanup?"
    with _fake_openai(["ok"]) as (base_url, state):
        client = _client(base_url)
        assert client.answer("system", question, "old evidence\n" * 50000) == "ok"
    user = state.requests[0]["messages"][1]["content"]
    assert user.startswith(f"问题：{question}\n\n证据：")
    assert question in user
    assert "[context truncated by shared input budget]" in user
