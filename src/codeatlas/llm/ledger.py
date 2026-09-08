"""Local SQLite request ledger; reservations survive failure and process restarts.

No credentials, prompts, HTTP headers or exception messages are stored here.
Reservations are conservative estimates, not provider invoices or token guarantees.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class LedgerBudgetExceeded(RuntimeError):
    pass


class RequestLedger:
    def __init__(self, path, *, max_requests=None, max_cost_usd=None,
                 input_usd_per_million=None, output_usd_per_million=None):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if max_requests is not None and (type(max_requests) is not int or max_requests < 1):
            raise ValueError("max_requests must be a positive integer")
        for value in (max_cost_usd, input_usd_per_million, output_usd_per_million):
            if value is not None and (type(value) not in (int, float)
                                      or not math.isfinite(value) or value < 0):
                raise ValueError("ledger budgets and prices must be finite nonnegative numbers")
        if max_cost_usd is not None and None in (input_usd_per_million, output_usd_per_million):
            raise ValueError("a cost ceiling requires both token prices")
        self.settings = dict(max_requests=max_requests, max_cost_usd=max_cost_usd,
                             input_usd_per_million=input_usd_per_million,
                             output_usd_per_million=output_usd_per_million)
        self.scope_id = None
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS ledger_config (id INTEGER PRIMARY KEY CHECK(id=1), settings TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, trial_id TEXT, request_hash TEXT NOT NULL,
                    model TEXT NOT NULL, session_id TEXT NOT NULL, state TEXT NOT NULL,
                    reserved_usd REAL, measured_usd REAL, usage_json TEXT,
                    error_type TEXT, latency_ms REAL, created_at REAL NOT NULL, finished_at REAL);
                CREATE TABLE IF NOT EXISTS active_request_scope (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), scope_id TEXT NOT NULL);
            """)
            conn.execute("BEGIN IMMEDIATE")
            encoded = json.dumps(self.settings, sort_keys=True)
            conn.execute("INSERT OR IGNORE INTO ledger_config VALUES(1,?)", (encoded,))
            if conn.execute("SELECT settings FROM ledger_config WHERE id=1").fetchone()[0] != encoded:
                raise ValueError("ledger budget/prices are frozen; create a new campaign to change them")

    @contextmanager
    def connect(self):
        conn = None
        # A campaign can run for hours and reopen this ledger after every
        # upstream request.  APFS/iCloud-backed workspaces have occasionally
        # returned a transient ``disk I/O error`` while opening an otherwise
        # healthy database.  Retry only connection/setup failures that are
        # safe before a transaction has started; never replay a write.
        for attempt in range(4):
            try:
                conn = sqlite3.connect(self.path, timeout=30)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA synchronous=FULL")
                break
            except sqlite3.OperationalError as exc:
                if conn is not None:
                    conn.close()
                    conn = None
                retryable = "disk i/o error" in str(exc).lower()
                if not retryable or attempt == 3:
                    raise
                time.sleep(0.05 * (2 ** attempt))
        assert conn is not None
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def bind_scope(self, scope_id, *, max_requests, manifest_hash):
        """Attach a frozen additive cap without resetting the parent ledger.

        A scope and its request link are checked/inserted in the same reservation
        transaction. Once activated, the scope applies to every new reservation
        in this ledger, including old clients and restarted/legacy entry points.
        It cannot be replaced by changing a caller's local scope id.
        """
        if (not isinstance(scope_id, str) or not scope_id
                or type(max_requests) is not int or max_requests < 1
                or not isinstance(manifest_hash, str) or len(manifest_hash) != 64):
            raise ValueError("invalid request budget scope")
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS request_scopes (
                    id TEXT PRIMARY KEY, max_requests INTEGER NOT NULL,
                    manifest_hash TEXT NOT NULL, initial_requests INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS request_scope_uses (
                    request_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL);
            """)
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute("SELECT scope_id FROM active_request_scope WHERE singleton=1").fetchone()
            if active is not None and active[0] != scope_id:
                raise ValueError("active request scope cannot be changed")
            initial = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO request_scopes VALUES(?,?,?,?)",
                         (scope_id, max_requests, manifest_hash, initial))
            row = conn.execute("SELECT * FROM request_scopes WHERE id=?", (scope_id,)).fetchone()
            if row["max_requests"] != max_requests or row["manifest_hash"] != manifest_hash:
                raise ValueError("request scope budget or manifest changed")
            conn.execute("INSERT OR IGNORE INTO active_request_scope VALUES(1,?)", (scope_id,))
        self.scope_id = scope_id
        return self

    def scope_summary(self):
        with self.connect() as conn:
            active = conn.execute("SELECT scope_id FROM active_request_scope WHERE singleton=1").fetchone()
            if active is None:
                return None
            row = conn.execute("SELECT * FROM request_scopes WHERE id=?", (active[0],)).fetchone()
            used = conn.execute("SELECT COUNT(*) FROM request_scope_uses WHERE scope_id=?",
                                (active[0],)).fetchone()[0]
        return {**dict(row), "used": used, "remaining": max(0, row["max_requests"] - used)}

    def reserve(self, *, request_hash, model, session_id, input_tokens, output_tokens, trial_id=None):
        prices = self.settings
        reserve = ((input_tokens * prices["input_usd_per_million"]
                    + output_tokens * prices["output_usd_per_million"]) / 1_000_000
                   if prices["input_usd_per_million"] is not None
                   and prices["output_usd_per_million"] is not None else None)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(MAX(COALESCE(reserved_usd,0),COALESCE(measured_usd,0))),0) cost FROM requests").fetchone()
            if prices["max_requests"] is not None and row["n"] >= prices["max_requests"]:
                raise LedgerBudgetExceeded("persistent request ceiling reached")
            active = conn.execute("SELECT scope_id FROM active_request_scope WHERE singleton=1").fetchone()
            scope_id = active[0] if active is not None else None
            if self.scope_id is not None and self.scope_id != scope_id:
                raise ValueError("local request scope differs from persistent scope")
            if scope_id is not None:
                scope = conn.execute("SELECT max_requests FROM request_scopes WHERE id=?",
                                     (scope_id,)).fetchone()
                used = conn.execute("SELECT COUNT(*) FROM request_scope_uses WHERE scope_id=?",
                                    (scope_id,)).fetchone()[0]
                if scope is None or used >= scope[0]:
                    raise LedgerBudgetExceeded("persistent scope request ceiling reached")
            if prices["max_cost_usd"] is not None and row["cost"] + reserve > prices["max_cost_usd"] + 1e-12:
                raise LedgerBudgetExceeded("persistent reservation ceiling reached")
            identifier = uuid.uuid4().hex
            conn.execute("INSERT INTO requests(id,trial_id,request_hash,model,session_id,state,reserved_usd,created_at) VALUES(?,?,?,?,?,'reserved',?,?)",
                         (identifier, trial_id, request_hash, model, session_id, reserve, time.time()))
            if scope_id is not None:
                conn.execute("INSERT INTO request_scope_uses VALUES(?,?)", (identifier, scope_id))
        return identifier

    def finish(self, identifier, *, usage=None, error_type=None, latency_ms=None):
        # Usage keys are provider token counters, never arbitrary response content.
        safe = {key: value for key, value in (usage or {}).items()
                if key in {"prompt_tokens", "completion_tokens", "total_tokens",
                           "cache_read_input_tokens", "cache_creation_input_tokens"}
                and type(value) is int and value >= 0}
        measured = None
        if (all(key in safe for key in ("prompt_tokens", "completion_tokens"))
                and self.settings["input_usd_per_million"] is not None
                and self.settings["output_usd_per_million"] is not None):
            measured = (safe["prompt_tokens"] * self.settings["input_usd_per_million"]
                        + safe["completion_tokens"] * self.settings["output_usd_per_million"]) / 1_000_000
        with self.connect() as conn:
            changed = conn.execute("UPDATE requests SET state=?,usage_json=?,measured_usd=?,error_type=?,latency_ms=?,finished_at=? WHERE id=? AND state='reserved'",
                ("failed" if error_type else "completed", json.dumps(safe), measured,
                 error_type, latency_ms, time.time(), identifier))
            if changed.rowcount != 1:
                raise ValueError("request is missing or already finalized")

    def summary(self, *, trial_id=None):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM requests" + (" WHERE trial_id=?" if trial_id else ""),
                                (trial_id,) if trial_id else ()).fetchall()
        usage = [json.loads(row["usage_json"] or "{}") for row in rows]
        totals = {key: sum(value[key] for value in usage) if all(key in value for value in usage) else None
                  for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        measured = [row["measured_usd"] for row in rows]
        reserved = [row["reserved_usd"] for row in rows]
        return {"requests": len(rows), "completed": sum(r["state"] == "completed" for r in rows),
                "failed": sum(r["state"] == "failed" for r in rows),
                "uncertain": sum(r["state"] == "reserved" for r in rows), "usage": totals,
                "usage_complete": all(v is not None for v in totals.values()),
                "measured_cost_usd": sum(measured) if all(v is not None for v in measured) else None,
                "reserved_cost_usd": sum(reserved) if all(v is not None for v in reserved) else None,
                "budget": self.settings,
                "boundary": "Failed/in-flight reservations remain charged; estimated prices are not an invoice."}
