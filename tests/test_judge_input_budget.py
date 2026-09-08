"""Wire-level evidence preservation; no real provider or credential needed."""
import json

import pytest

from codeatlas.contracts import INPUT_BUDGET, estimated_tokens
from codeatlas.llm.client import OpenAICompatClient
from codeatlas.eval import campaign_review as review
from tests.test_campaign_review import collected, factory, fake_http


@pytest.fixture
def transport(monkeypatch):
    calls = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps({"model": "test-model", "usage": {
                "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}).encode()
    def send(req, timeout):
        calls.append((json.loads(req.data), timeout))
        return Response()
    monkeypatch.setattr("urllib.request.urlopen", send)
    return calls


def client():
    return OpenAICompatClient("http://127.0.0.1:4321/v1", "test-key", "test-model")


def test_judge_16k_sends_complete_body_but_agent_remains_8k(transport):
    user = "alpha " * 4800 + "EXACT_FINAL_EVIDENCE"
    system = "Frozen reviewer instructions."
    assert 8000 < estimated_tokens(system + user) + 32 < 16000
    judge = client()
    judge._chat(system, user, max_tokens=5000, complete_input_budget=16000)
    sent = transport[-1][0]
    assert sent["messages"] == [{"role": "system", "content": system},
                                {"role": "user", "content": user}]
    assert sent["max_tokens"] == 1200 and judge.last_budget["truncated"] is False
    agent = client()
    agent._chat(system, user)
    assert INPUT_BUDGET == 8000
    assert agent.last_budget["estimated_input_tokens"] <= 8000
    assert agent.last_budget["truncated"] is True
    assert "EXACT_FINAL_EVIDENCE" not in transport[-1][0]["messages"][1]["content"]


@pytest.mark.parametrize("budget", [8000, 16000, 16001, True, 32000, 16000.0])
def test_complete_input_never_reserves_or_sends_when_rejected(transport, budget):
    obj = client()
    class NoReserve:
        def reserve(self, **kwargs):
            pytest.fail("rejected input must not reserve a billable request")
    obj.ledger = NoReserve()
    with pytest.raises(ValueError):
        obj._chat("system", "x" * 50000, complete_input_budget=budget)
    assert not transport and obj.request_count == 0


def test_frozen_review_timeout_ignores_changed_environment(transport, monkeypatch):
    obj = client()
    obj.request_timeout_seconds = 180
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "0.01")
    obj._chat("system", "user", complete_input_budget=16000)
    assert transport[0][1] == 180


@pytest.mark.parametrize("options", [
    {"judge_input_budget": 16000},
    {"judge_input_budget": 32000},
    {"judge_input_budget": 16000, "budget_authorization": {"authorized_by": "user"}},
    {"request_timeout_seconds": 181},
])
def test_review_budget_authorization_fails_before_campaign_or_network(tmp_path, transport, options):
    with pytest.raises(ValueError):
        review.review(tmp_path / "nonexistent-campaign", **options)
    assert not transport


def test_16k_requires_frozen_timeout_before_loading_campaign(tmp_path, transport):
    from codeatlas.contracts import digest
    scope = {"scope_id": "test", "max_requests": 800, "manifest_hash": "a" * 64}
    auth = {"authorized_by": "user", "judge_input_budget": 16000,
            "agent_input_budget": 8000, "scope": scope}
    auth["authorization_hash"] = digest(auth)
    with pytest.raises(ValueError, match="authorization"):
        review.review(tmp_path / "absent", attempt_id="review16", judge_input_budget=16000,
                      budget_scope=scope, budget_authorization=auth, transport_retries=0)
    assert not transport


def test_authorized_campaign_binds_budget_timeout_and_never_resends_on_resume(tmp_path, fake_http):
    from codeatlas.contracts import digest
    directory = collected(tmp_path)
    scope = {"scope_id": "review16", "max_requests": 100, "manifest_hash": "b" * 64}
    auth = {"authorized_by": "user", "judge_input_budget": 16000,
            "agent_input_budget": 8000, "scope": scope, "request_timeout_seconds": 180}
    auth["authorization_hash"] = digest(auth)
    options = dict(attempt_id="frozen16", judge_input_budget=16000,
                   budget_scope=scope, budget_authorization=auth, transport_retries=0,
                   request_timeout_seconds=180, client_factory=factory)
    result = review.review(directory, **options)
    assert result["status"] == "completed"
    assert result["binding"]["judge_input_budget"] == 16000
    assert result["binding"]["agent_input_budget"] == 8000
    assert result["binding"]["request_timeout_seconds"] == 180
    count = len(fake_http["requests"])
    assert review.review(directory, resume=True, **options)["reviews"] == result["reviews"]
    assert len(fake_http["requests"]) == count
    with pytest.raises(ValueError, match="already frozen"):
        review.review(directory, resume=True, **{**options, "judge_input_budget": 8000})
    changed_auth = {**auth, "request_timeout_seconds": 120}
    changed_auth["authorization_hash"] = digest({k: v for k, v in changed_auth.items()
                                                  if k != "authorization_hash"})
    with pytest.raises(ValueError, match="already frozen"):
        review.review(directory, resume=True, **{**options, "request_timeout_seconds": 120,
                                                 "budget_authorization": changed_auth})
    assert len(fake_http["requests"]) == count
