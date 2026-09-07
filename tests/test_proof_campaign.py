"""Campaign/ledger tests use only in-memory HTTP substitutes and temporary files."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml
from typer.testing import CliRunner

from codeatlas.eval import proof_campaign as campaign
from codeatlas.llm.client import OpenAICompatClient, BudgetExceeded, get_client, GO_BASE_URL, GO_MODEL
from codeatlas.llm.ledger import RequestLedger, LedgerBudgetExceeded

ROOT = Path(__file__).resolve().parents[1]
REUSE = [{"id": "reuse", "kind": "knowledge_reuse", "manifest": "eval/knowledge_reuse.yaml",
          "answer_key": "eval/knowledge_reuse.answers.yaml"}]


@pytest.fixture(autouse=True)
def fixture_configuration_only(monkeypatch):
    for name in ("LLM_API_KEY", "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_MAX_REQUESTS",
                 "LLM_MAX_COST_USD", "LLM_INPUT_USD_PER_MILLION", "LLM_OUTPUT_USD_PER_MILLION",
                 "LLM_USER_AGENT", "LLM_SESSION_ID", "LLM_THINKING_MODE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def transport(monkeypatch):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "资料不足，见 [M1]。"}}],
                               "usage": {"prompt_tokens": 13, "completion_tokens": 4, "total_tokens": 17}}).encode()

    def capture(request, **kwargs):
        requests.append(request)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", capture)
    return requests


def factory(**kwargs):
    return OpenAICompatClient(kwargs.pop("base_url"), "fixture-not-a-user-key", kwargs.pop("model"), **kwargs)


def test_go_environment_defaults_and_safe_explicit_headers(monkeypatch, transport):
    monkeypatch.setenv("LLM_PROVIDER", "opencode-go")
    monkeypatch.setenv("LLM_API_KEY", "fixture-not-a-user-key")
    for name in ("LLM_BASE_URL", "LLM_MODEL", "LLM_MAX_COST_USD", "LLM_MAX_REQUESTS"):
        monkeypatch.delenv(name, raising=False)
    client = get_client(session_id="frozen-session")
    assert (client.base_url, client.model) == (GO_BASE_URL, GO_MODEL)
    client.answer("system", "question", "evidence")
    request = transport[0]
    assert request.full_url == GO_BASE_URL + "/chat/completions"
    assert request.get_header("User-agent").startswith("CodeAtlas/")
    assert request.get_header("X-opencode-session") == "frozen-session"
    monkeypatch.delenv("LLM_API_KEY")
    assert get_client() is None


def test_ledger_survives_client_reset_and_another_process_handle(tmp_path, transport):
    path = tmp_path / "ledger.sqlite3"
    options = dict(max_requests=1, max_cost_usd=1., input_usd_per_million=1., output_usd_per_million=2.)
    client = factory(base_url=GO_BASE_URL, model=GO_MODEL, ledger=RequestLedger(path, **options))
    client.answer("system", "question", "evidence")
    client.reset_metrics()
    other = factory(base_url=GO_BASE_URL, model=GO_MODEL, ledger=RequestLedger(path, **options))
    with pytest.raises(BudgetExceeded, match="persistent request"):
        other.answer("system", "question", "evidence")
    assert len(transport) == 1
    summary = other.ledger.summary()
    assert summary["requests"] == 1 and summary["usage"]["total_tokens"] == 17
    assert summary["reserved_cost_usd"] > summary["measured_cost_usd"] > 0
    assert b"fixture-not-a-user-key" not in path.read_bytes()


def test_atomic_budget_reservation_and_unknown_usage(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger = RequestLedger(path, max_requests=1)
    def reserve(_):
        try:
            RequestLedger(path, max_requests=1).reserve(request_hash="a" * 64, model="fixture",
                session_id="session", input_tokens=10, output_tokens=20)
            return True
        except LedgerBudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, range(2))) == [False, True]
    assert ledger.summary()["uncertain"] == 1
    assert ledger.summary()["measured_cost_usd"] is None
    assert ledger.summary()["usage"]["prompt_tokens"] is None
    with pytest.raises(ValueError, match="frozen"):
        RequestLedger(path, max_requests=2)
    # A fresh interpreter is subject to the same already-used reservation.
    code = """
import sys
from codeatlas.llm.ledger import RequestLedger, LedgerBudgetExceeded
try:
    RequestLedger(sys.argv[1], max_requests=1).reserve(request_hash='b'*64,
        model='fixture', session_id='fresh-process', input_tokens=10, output_tokens=20)
except LedgerBudgetExceeded:
    sys.exit(7)
"""
    child = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True,
                           env={"PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 7


def test_ledger_retries_transient_connect_io_error_without_replaying_writes(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite3"
    original = __import__("sqlite3").connect
    attempts = []

    def transient(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise __import__("sqlite3").OperationalError("disk I/O error")
        return original(*args, **kwargs)

    monkeypatch.setattr("codeatlas.llm.ledger.sqlite3.connect", transient)
    ledger = RequestLedger(path, max_requests=1)
    reservation = ledger.reserve(request_hash="a" * 64, model="fixture", session_id="session",
                                 input_tokens=10, output_tokens=20)
    ledger.finish(reservation, usage={"prompt_tokens": 10, "completion_tokens": 5,
                                      "total_tokens": 15})
    assert len(attempts) >= 4  # constructor, reserve, finish and summary each open once
    assert ledger.summary()["completed"] == 1


def test_failed_call_retains_reservation_without_exception_or_key(tmp_path, monkeypatch):
    ledger = RequestLedger(tmp_path / "ledger.sqlite3", max_requests=2,
        max_cost_usd=.1, input_usd_per_million=1., output_usd_per_million=2.)
    def fail(*args, **kwargs):
        raise TimeoutError("sensitive upstream text")
    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(TimeoutError):
        factory(base_url=GO_BASE_URL, model=GO_MODEL, ledger=ledger).answer("sys", "q", "ctx")
    assert ledger.summary()["failed"] == 1
    assert ledger.summary()["reserved_cost_usd"] > 0
    assert ledger.summary()["measured_cost_usd"] is None
    assert b"sensitive upstream text" not in ledger.path.read_bytes()


def test_retryable_disconnect_is_retried_once_and_both_attempts_are_ledgered(tmp_path, monkeypatch):
    import urllib.error

    ledger = RequestLedger(tmp_path / "ledger.sqlite3", max_requests=3)
    attempts = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            }).encode()

    def flaky(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise urllib.error.URLError("temporary disconnect")
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", flaky)
    client = factory(base_url=GO_BASE_URL, model=GO_MODEL, ledger=ledger,
                     transport_retries=1)
    assert client.answer("system", "question", "evidence") == "ok"
    summary = ledger.summary()
    assert len(attempts) == 2
    assert summary["requests"] == 2 and summary["failed"] == 1 and summary["completed"] == 1
    assert client.last_response_meta["transport_attempts"] == 2


def test_prepare_keyless_and_fixed_schedule_no_formal_report_write(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    directory = tmp_path / "campaign"
    before = (ROOT / "docs/PROOF-KNOWLEDGE-REUSE.json").read_bytes()
    planned = campaign.prepare(directory, project_root=ROOT, jobs=REUSE, runs=1, smoke_trials=6)
    assert planned["planned_trials"] == 18
    frozen = json.loads((directory / "plan.json").read_text())
    assert frozen["workers"] == 1
    assert frozen["request_upper_bound"] == {
        "answer_trials": 18, "answer_requests": 108,
        "first_pass_review_requests": 36, "arbitration_requests": 18,
        "judge_calibration_requests": 36, "campaign_total": 198,
    }
    assert frozen["budget"]["max_requests"] == 5000
    result = campaign.run(directory)
    assert result["status"] == "not_run" and result["ledger"]["requests"] == 0
    assert result["preflight"] == {"status": "not_required", "passed": True}
    assert result["reports"]["reuse"]["case_count"] == 2
    assert result["reports"]["reuse"]["question_count"] == 6
    assert (ROOT / "docs/PROOF-KNOWLEDGE-REUSE.json").read_bytes() == before
    assert "LLM_API_KEY" not in (directory / "plan.json").read_text()
    with pytest.raises(campaign.CampaignError, match="already exists"):
        campaign.prepare(directory, project_root=ROOT, jobs=REUSE)


def test_campaign_freezes_and_uses_parallel_workers(tmp_path, monkeypatch):
    active = 0
    peak = 0
    lock = threading.Lock()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "资料不足，见 [M1]。"}}],
                "usage": {"prompt_tokens": 13, "completion_tokens": 4, "total_tokens": 17},
            }).encode()

    def delayed(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(.03)
        with lock:
            active -= 1
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", delayed)
    directory = tmp_path / "parallel-campaign"
    campaign.prepare(directory, project_root=ROOT, jobs=REUSE, runs=1,
                     smoke_trials=6, workers=3, transport_retries=1)
    result = campaign.run(directory, client_factory=factory)
    assert result["status"] == "smoke_completed"
    assert result["workers"] == 3 and result["transport_retries"] == 1 and peak >= 2
    assert json.loads((directory / "plan.json").read_text())["request_upper_bound"]["campaign_total"] == 396
    with pytest.raises(campaign.CampaignError, match="workers"):
        campaign.prepare(tmp_path / "bad-workers", project_root=ROOT, jobs=REUSE,
                         runs=1, smoke_trials=6, workers=0)


def test_smoke_to_full_preserves_completed_trials_and_requests(tmp_path, transport):
    directory = tmp_path / "campaign"
    campaign.prepare(directory, project_root=ROOT, jobs=REUSE, runs=1, smoke_trials=6)
    smoke = campaign.run(directory, client_factory=factory)
    assert smoke["status"] == "smoke_completed" and len(transport) == 6
    _, _, ledger = campaign._load(directory)
    with ledger.connect() as conn:
        frozen = {row["id"]: row["result_json"] for row in conn.execute("SELECT * FROM trials WHERE state='completed'")}
    full = campaign.run(directory, mode="full", resume=True, client_factory=factory)
    assert full["status"] == "completed" and len(transport) == 18
    assert full["ledger"]["requests"] == 18
    sessions = [request.get_header("X-opencode-session") for request in transport]
    assert len(set(sessions)) == 18
    assert all("-answer-" in session for session in sessions)
    assert not full["acceptance"]["effect_observed"]
    with ledger.connect() as conn:
        assert all(conn.execute("SELECT result_json FROM trials WHERE id=?", (identifier,)).fetchone()[0] == value
                   for identifier, value in frozen.items())
    campaign.run(directory, mode="full", resume=True, client_factory=factory)
    assert len(transport) == 18  # No result-based reruns.


def test_resume_retains_interrupted_trial_as_uncertain(tmp_path, transport):
    directory = tmp_path / "campaign"
    campaign.prepare(directory, project_root=ROOT, jobs=REUSE, runs=1, smoke_trials=6)
    count = 0
    def interrupt(job, event, client):
        nonlocal count
        count += 1
        if count == 2:
            raise KeyboardInterrupt()
        return campaign.execute_trial(job, event, client)
    with pytest.raises(KeyboardInterrupt):
        campaign.run(directory, client_factory=factory, executor=interrupt)
    result = campaign.run(directory, mode="full", resume=True, client_factory=factory)
    assert result["status"] == "unresolved"
    assert result["trial_states"] == {"completed": 17, "uncertain": 1}
    assert len(result["uncertain_trial_ids"]) == 1 and len(transport) == 17


def test_resume_rejects_changed_inputs_and_report_detects_trial_edits(tmp_path, transport):
    directory = tmp_path / "campaign"
    campaign.prepare(directory, project_root=ROOT, jobs=REUSE, runs=1, smoke_trials=3)
    campaign.run(directory, client_factory=factory)
    _, _, ledger = campaign._load(directory)
    with ledger.connect() as conn:
        conn.execute("UPDATE trials SET result_json='{}' WHERE sequence=0")
    with pytest.raises(campaign.CampaignError, match="trial content changed"):
        campaign.report(directory)
    plan = json.loads((directory / "plan.json").read_text())
    plan["model"] = "another-model"
    (directory / "plan.json").write_text(json.dumps(plan))
    with pytest.raises(campaign.CampaignError, match="plan hash changed"):
        campaign.run(directory, resume=True, client_factory=factory)


def test_campaign_cli_has_thin_lifecycle_commands():
    from codeatlas.cli import app
    response = CliRunner().invoke(app, ["eval", "campaign", "--help"])
    assert response.exit_code == 0
    for command in ("prepare", "run", "resume", "review", "report"):
        assert command in response.stdout
    proof = CliRunner().invoke(app, ["eval", "proof", "--help"])
    assert proof.exit_code == 0
    for command in ("prepare", "run", "review", "report"):
        assert command in proof.stdout
    run_help = CliRunner().invoke(app, ["eval", "proof", "run", "--help"])
    assert run_help.exit_code == 0
    assert "--campaign" in run_help.stdout and "--profile" in run_help.stdout and "--resume" in run_help.stdout
    prepare_help = CliRunner().invoke(app, ["eval", "proof", "prepare", "--help"])
    assert prepare_help.exit_code == 0 and "--workers" in prepare_help.stdout


def test_default_smoke_uses_separate_development_tasks_not_holdout_prefix():
    jobs = campaign.development_smoke_jobs(ROOT)
    assert [job["kind"] for job in jobs] == [
        "wiki_knowledge", "wiki_knowledge", "knowledge_reuse"
    ]
    assert all(job["scope"] == "development_smoke" for job in jobs)
    assert {job["manifest"] for job in jobs[:2]} == {
        "eval/tasks_cjson.yaml", "eval/tasks_lwip.yaml"
    }
    assert all(len(job["task_ids"]) == 3 for job in jobs[:2])
    assert jobs[2]["task_override"] == "eval/proof_smoke.yaml"
    smoke = yaml.safe_load((ROOT / jobs[2]["task_override"]).read_text())
    assert smoke["not_effect_evidence"] is True
    assert {task["case_id"] for task in smoke["tasks"]} == {"cjson-nesting", "lwip-netif"}


def test_reading_and_abc_reuse_existing_isolated_runners(kb, tmp_path, monkeypatch, transport):
    import sqlite3
    from codeatlas import agent
    from codeatlas.eval import reading_eval
    from tests.test_model_eval import _tasks
    source = reading_eval._copy(kb[0])
    source.execute("INSERT OR REPLACE INTO wiki_page(id,level,title,md,status,sources) VALUES('campaign-wiki','repo','Wiki','text','ok','[]')")
    source.commit()
    database = tmp_path / "fixture.sqlite3"
    with sqlite3.connect(database) as target:
        source.backup(target)
    source.close()
    tasks = _tasks(tmp_path)
    seen = []
    def isolated_run(conn, question, **kwargs):
        seen.append((question, conn.execute("SELECT COUNT(*) FROM wiki_page WHERE status='ok'").fetchone()[0]))
        conn.execute("UPDATE wiki_page SET status='stale'")
        raise RuntimeError("fixture error is retained, never selected away")
    monkeypatch.setattr(agent, "run", isolated_run)
    jobs = [{"id": kind, "kind": kind, "manifest": str(tasks), "database": str(database),
             "data_dir": kb[1]} for kind in ("wiki_knowledge", "abc")]
    directory = tmp_path / "campaign"
    campaign.prepare(directory, project_root=ROOT, jobs=jobs, runs=1, smoke_trials=6)
    result = campaign.run(directory, mode="full", client_factory=factory)
    assert result["status"] == "completed" and result["trial_states"] == {"completed": 12}
    assert len(seen) == 8  # Six reading arms and two C trials, including refusals.
    assert sum(wiki > 0 for _, wiki in seen) == 6
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM wiki_page WHERE status='ok'").fetchone()[0] > 0
    assert all(not report["acceptance"]["passed"] for report in result["reports"].values())


def test_campaign_persistent_budget_cannot_reset_on_resume(tmp_path, transport):
    directory = tmp_path / "campaign"
    campaign.prepare(directory, project_root=ROOT, jobs=REUSE, runs=1, smoke_trials=6, max_requests=2)
    first = campaign.run(directory, client_factory=factory)
    assert first["status"] == "partial_budget" and len(transport) == 2
    second = campaign.run(directory, mode="full", resume=True, client_factory=factory)
    assert second["status"] == "partial_budget" and len(transport) == 2
    assert second["ledger"]["requests"] == 2


def test_missing_price_remains_null_and_exported_job_accepts_existing_review_shape(tmp_path, transport):
    from codeatlas.cli import app
    directory = tmp_path / "campaign"
    campaign.prepare(directory, project_root=ROOT, jobs=REUSE, runs=1, smoke_trials=3)
    result = campaign.run(directory, client_factory=factory)
    trial = next(row["repetitions"][0] for rows in result["reports"]["reuse"]["raw_trials"].values()
                 for row in rows if row["repetitions"])
    assert trial["cost_usd"] is None and trial["cost_upper_bound_usd"] is None
    out = tmp_path / "reuse.raw.json"
    command = CliRunner().invoke(app, ["eval", "campaign", "report", str(directory), "--job", "reuse", "--out", str(out)])
    assert command.exit_code == 0
    exported = json.loads(out.read_text())
    assert exported["report_kind"] == "knowledge_reuse" and "raw_trials" in exported
    assert exported["run_hash"] and exported["blind_review"]["items"]


def test_prepare_freezes_environment_budgets_and_never_recreates_missing_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MAX_REQUESTS", "9")
    monkeypatch.setenv("LLM_MAX_COST_USD", "0.25")
    monkeypatch.setenv("LLM_INPUT_USD_PER_MILLION", "1")
    monkeypatch.setenv("LLM_OUTPUT_USD_PER_MILLION", "2")
    directory = tmp_path / "campaign"
    campaign.prepare(directory, project_root=ROOT, jobs=REUSE, runs=1, smoke_trials=3)
    _, plan, ledger = campaign._load(directory)
    assert plan["budget"]["max_requests"] == 9 and plan["budget"]["max_cost_usd"] == .25
    ledger.path.unlink()
    with pytest.raises(campaign.CampaignError, match="must not be recreated"):
        campaign.report(directory)
    assert not ledger.path.exists()
