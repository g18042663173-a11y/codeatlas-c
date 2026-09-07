"""Shared verification contracts. Provenance is not a correctness verdict."""
from __future__ import annotations

import hashlib
import json
import re

INPUT_BUDGET = 8000
OUTPUT_BUDGET = 1200
POLICY_VERSION = "snapshot-budget-v1"


def digest(value) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def estimated_tokens(text: str) -> int:
    # Conservative and deterministic across machines; not provider usage.
    return (len(text.encode("utf-8")) + 2) // 3


def budget_messages(system: str, user: str, *, protected_prefix: str = "") -> tuple[str, str, dict]:
    """Apply the shared input ceiling while preserving a required user prefix.

    Answer requests put the question in ``protected_prefix`` and the expandable
    evidence afterwards.  Generic planning calls leave it empty and retain the
    original head-preserving behavior.
    """
    overhead = 32
    before = estimated_tokens(system + user) + overhead
    available = max(0, (INPUT_BUDGET - overhead - estimated_tokens(system)) * 3)
    marker = "\n[context truncated by shared input budget]"
    if estimated_tokens(system) + overhead >= INPUT_BUDGET:
        raise ValueError("system prompt exceeds context budget")
    if protected_prefix and not user.startswith(protected_prefix):
        raise ValueError("protected input prefix is not at the start of the user message")
    if len(protected_prefix.encode()) + len(marker.encode()) > available:
        raise ValueError("system prompt and protected question exceed context budget")
    if len(user.encode()) > available:
        suffix = user[len(protected_prefix):]
        suffix_budget = max(0, available - len(protected_prefix.encode()) - len(marker.encode()))
        user = protected_prefix + suffix.encode()[:suffix_budget].decode("utf-8", "ignore") + marker
    return system, user, {"estimated_input_tokens": estimated_tokens(system + user) + overhead,
                          "untrimmed_input_tokens": before, "truncated": before > INPUT_BUDGET}


def citation_valid(conn, citation: dict) -> bool:
    from . import db
    p = citation.get("provenance") or {}
    if p.get("stale"):
        return False
    for key in ("revision", "repository"):
        meta = "repository_id" if key == "repository" else key
        if p.get(key) != db.get_meta(conn, meta):
            return False
    if p.get("knowledge_set_id") and p["knowledge_set_id"] != db.get_meta(conn, "knowledge_set_id"):
        return False
    level = citation.get("level")
    if level == "A":
        start, end = p.get("line_start"), p.get("line_end")
        if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
            return False
        if p.get("usr"):
            row = conn.execute("SELECT * FROM node WHERE usr=? AND path=? AND is_definition=1",
                               (p["usr"], p.get("path"))).fetchone()
            return bool(row and row["line_start"] <= start <= end <= row["line_end"]
                        and (not p.get("definition_hash") or p["definition_hash"] == row["definition_hash"]))
        from pathlib import Path
        root = Path(db.get_meta(conn, "repo", ".")).resolve()
        target = (root / (p.get("path") or "")).resolve()
        return bool(root in target.parents and target.suffix in (".c", ".h") and target.is_file()
                    and conn.execute("SELECT 1 FROM node WHERE kind='file' AND path=?", (p.get("path"),)).fetchone()
                    and end <= len(target.read_bytes().splitlines()))
    uid = citation.get("uid", "")
    if level == "B":
        card_id = uid.removeprefix("exp:")
        row = conn.execute(
            """SELECT 1 FROM experience WHERE id=? AND status='approved'
               AND COALESCE(artifact_scope,'formal')=COALESCE(
                 (SELECT value FROM meta WHERE key='evaluation_scope'),'formal')""",
            (card_id,),
        ).fetchone()
        if not row:
            return False
        from .experience.store import approved_current
        return approved_current(conn, card_id)[0]
    if level == "C":
        return bool(conn.execute("SELECT 1 FROM wiki_page WHERE id=? AND status='ok'", (uid.removeprefix("wiki:"),)).fetchone())
    return False


def validate_fact_selection(conn, claims: list[dict]) -> bool:
    """Only exact structured compiler predicates pass; real USR + invented text fails."""
    if not claims:
        return False
    for claim in claims:
        if set(claim) != {"fact_id", "payload"}:
            return False
        row = conn.execute("SELECT payload_json FROM semantic_fact WHERE id=? AND confidence='certain'",
                           (claim["fact_id"],)).fetchone()
        if not row or json.loads(row[0]) != claim["payload"]:
            return False
    return True


def answer_verification(answer, citations, conn=None) -> dict:
    tags = set(re.findall(r"\[([ABC]\d+)\]", answer or ""))
    known = {c.get("tag"): c for c in citations}
    valid = bool(answer and tags and tags <= known.keys())
    if conn is not None and valid:
        valid = all(citation_valid(conn, known[tag]) for tag in tags)
    return {"citation_valid": valid, "fact_validation": "not_structured",
            "answer_correctness": "pending_human_review" if valid else "invalid_citation",
            "trusted": False}
