from concurrent.futures import ThreadPoolExecutor

import pytest

from codeatlas.llm.ledger import LedgerBudgetExceeded, RequestLedger


def test_scope_is_shared_and_cannot_reset_or_overwrite(tmp_path):
    path = tmp_path / "requests.db"
    ledger = RequestLedger(path, max_requests=5)
    old = ledger.reserve(request_hash="old", model="model", session_id="old",
                         input_tokens=1, output_tokens=1)
    ledger.finish(old)
    for index in range(2):
        local = RequestLedger(path, max_requests=5).bind_scope(
            "closeout", max_requests=2, manifest_hash="a" * 64)
        local.reserve(request_hash=str(index), model="model", session_id=str(index),
                      input_tokens=1, output_tokens=1)
    resumed = RequestLedger(path, max_requests=5).bind_scope(
        "closeout", max_requests=2, manifest_hash="a" * 64)
    with pytest.raises(LedgerBudgetExceeded):
        resumed.reserve(request_hash="extra", model="model", session_id="resumed",
                        input_tokens=1, output_tokens=1)
    assert resumed.scope_summary()["initial_requests"] == 1
    assert resumed.scope_summary()["used"] == 2
    assert resumed.summary()["requests"] == 3
    for cap, manifest in ((3, "a" * 64), (2, "b" * 64)):
        with pytest.raises(ValueError, match="changed"):
            resumed.bind_scope("closeout", max_requests=cap, manifest_hash=manifest)


def test_scope_reservation_is_atomic_and_counts_failed_requests(tmp_path):
    path = tmp_path / "requests.db"
    RequestLedger(path, max_requests=20).bind_scope(
        "closeout", max_requests=3, manifest_hash="a" * 64)
    def reserve(index):
        local = RequestLedger(path, max_requests=20).bind_scope(
            "closeout", max_requests=3, manifest_hash="a" * 64)
        try:
            request = local.reserve(request_hash=str(index), model="model",
                                    session_id=str(index), input_tokens=1, output_tokens=1)
        except LedgerBudgetExceeded:
            return False
        local.finish(request, error_type="ConnectionError")
        return True
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, range(12))) == 3
    assert RequestLedger(path, max_requests=20).summary()["failed"] == 3


def test_scope_does_not_bypass_global_limit(tmp_path):
    ledger = RequestLedger(tmp_path / "requests.db", max_requests=1).bind_scope(
        "closeout", max_requests=800, manifest_hash="a" * 64)
    ledger.reserve(request_hash="one", model="model", session_id="one",
                   input_tokens=1, output_tokens=1)
    with pytest.raises(LedgerBudgetExceeded, match="persistent request"):
        ledger.reserve(request_hash="two", model="model", session_id="two",
                       input_tokens=1, output_tokens=1)


def test_unbound_restarted_and_preexisting_clients_cannot_bypass_scope(tmp_path):
    path = tmp_path / "requests.db"
    old_client = RequestLedger(path, max_requests=10)
    scoped = RequestLedger(path, max_requests=10).bind_scope(
        "bounded", max_requests=1, manifest_hash="a" * 64)
    old_client.reserve(request_hash="one", model="model", session_id="old",
                       input_tokens=1, output_tokens=1)
    for client in (old_client, scoped, RequestLedger(path, max_requests=10)):
        with pytest.raises(LedgerBudgetExceeded, match="scope"):
            client.reserve(request_hash="two", model="model", session_id="new",
                           input_tokens=1, output_tokens=1)
        assert client.scope_summary()["used"] == 1
    with pytest.raises(ValueError, match="cannot be changed"):
        old_client.bind_scope("fresh-budget", max_requests=100, manifest_hash="b" * 64)
