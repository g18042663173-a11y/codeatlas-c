"""Shared verification contracts. Provenance is not a correctness verdict."""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

INPUT_BUDGET = 8000
OUTPUT_BUDGET = 1200
POLICY_VERSION = "snapshot-budget-v1"
AGENT_CONTEXT_VERSION = "agent-block-budget-v1"
AGENT_TOOL_CONTRACT = {
    "search_evidence": {"parameters": {"query": "nonempty string"}, "required": ["query"]},
    "wiki_outline": {"parameters": {"module": "nonempty string", "offset": "integer >= 0",
                                     "budget": "integer > 0"}, "required": []},
    "wiki_section": {"parameters": {"page_id": "nonempty string", "section": "nonempty string",
                                     "offset": "integer >= 0", "budget": "integer > 0"},
                     "required": ["page_id", "section"]},
    "resolve_symbol": {"parameters": {"symbol": "nonempty string"}, "required": ["symbol"]},
    "code_read": {"parameters": {"usr": "nonempty string", "path": "nonempty string",
                                  "line_start": "integer >= 1", "line_end": "integer >= line_start"},
                  "forms": [["usr"], ["path", "line_start", "line_end"]], "max_lines": 300},
    "analyze_impact": {"parameters": {"symbol": "nonempty string", "depth": "integer from 1 to 4"},
                       "required": ["symbol"]},
}


def agent_action_contract(events: list[dict], *, read_mode: str = "progressive",
                          wiki_available: bool | None = None,
                          wiki_required: bool | None = None) -> dict:
    """One action/parameter contract shared by the planner and local executor."""
    if read_mode not in {"flat", "progressive"}:
        raise ValueError("read_mode must be flat or progressive")
    if wiki_available is None:
        wiki_available = not any(e.get("wiki_available") is False for e in events)
    if wiki_required is None:
        wiki_required = any(e.get("wiki_required") is True for e in events)
    allowed = [tool for tool in AGENT_TOOL_CONTRACT
               if wiki_available or not tool.startswith("wiki_")] + ["finish"]
    completed = {e.get("tool") for e in events if e.get("status") == "completed"}
    if read_mode == "progressive" and wiki_available and wiki_required:
        for prerequisite in ("search_evidence", "wiki_outline", "wiki_section"):
            if prerequisite not in completed:
                allowed = [prerequisite]
                break
    return {"allowed_next": allowed,
            "tool_contract": deepcopy({name: spec for name, spec in AGENT_TOOL_CONTRACT.items()
                                        if wiki_available or not name.startswith("wiki_")})}


def validate_agent_action(action: dict, *, allowed_next: list[str] | None = None) -> dict:
    """Validate exact action fields, retaining the legacy bare tool-call form."""
    if not isinstance(action, dict):
        raise ValueError("action must be an object")
    action = deepcopy(action)
    if "action" not in action and set(action) == {"tool", "arguments"}:
        action["action"] = "tool"
    if action.get("action") == "finish":
        if set(action) != {"action"}:
            raise ValueError("finish has no additional fields")
        tool = "finish"
    elif action.get("action") == "tool":
        if set(action) != {"action", "tool", "arguments"}:
            raise ValueError("tool action has unexpected fields")
        tool, args = action["tool"], action["arguments"]
        if not isinstance(tool, str) or tool not in AGENT_TOOL_CONTRACT:
            raise ValueError("tool is not allowed")
        spec = AGENT_TOOL_CONTRACT[tool]
        if not isinstance(args, dict) or not set(args) <= spec["parameters"].keys():
            raise ValueError("tool arguments have unexpected fields")
        if not set(spec.get("required", [])) <= args.keys():
            raise ValueError("required tool arguments missing")
        if "forms" in spec and set(args) not in [set(form) for form in spec["forms"]]:
            raise ValueError("code_read requires usr OR path/line_start/line_end")
        for key, value in args.items():
            if spec["parameters"][key] == "nonempty string":
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("tool string arguments must be nonempty")
            elif type(value) is not int:
                raise ValueError("tool numeric arguments must be integers, not booleans")
        if (args.get("offset", 0) < 0 or args.get("budget", 1) < 1
                or not 1 <= args.get("depth", 1) <= 4):
            raise ValueError("tool numeric argument outside allowed range")
        if tool == "code_read" and "path" in args:
            start, end = args["line_start"], args["line_end"]
            if start < 1 or end < start or end - start + 1 > spec["max_lines"]:
                raise ValueError("code_read requires an ordered range of at most 300 lines")
    else:
        raise ValueError("action must be tool or finish")
    if allowed_next is not None and tool not in allowed_next:
        raise ValueError("action violates the current completed-tool prerequisites")
    return action


class ContextBudgetExceeded(ValueError):
    """Required complete blocks do not fit; no model request may be sent."""

    def __init__(self, details: dict) -> None:
        self.details = details
        super().__init__("required complete context exceeds the 8000-token input budget")


def tool_continuation(tool: str, arguments: dict, result: dict, *, narrower: bool = False) -> dict | None:
    """A local replay/page pointer, not an instruction to make another model call."""
    if result.get("error") and not narrower:
        # An oversize/error result may carry its own larger-block or safe-start
        # continuation. Never overwrite it with the same failed page request.
        return None
    args = deepcopy(arguments)
    if tool in {"wiki_outline", "wiki_section"}:
        if narrower:
            args["budget"] = max(1, args.get("budget", 2000) // 2)
        elif result.get("truncated") and type(result.get("next_offset")) is int:
            args["offset"] = result["next_offset"]
        else:
            return None
    elif tool == "code_read" and narrower:
        path = args.get("path") or result.get("path")
        start = args.get("line_start", result.get("line_start"))
        end = args.get("line_end", result.get("line_end"))
        if not path or type(start) is not int or type(end) is not int or end <= start:
            return None
        args = {"path": path, "line_start": start,
                "line_end": min(start + 299, start + (end - start) // 2)}
    else:
        return None
    try:
        validate_agent_action({"tool": tool, "arguments": args})
    except ValueError:
        return None
    return {"tool": tool, "arguments": args}


def budget_json_messages(system: str, payload: dict) -> tuple[str, str, dict]:
    """Budget complete JSON blocks; required evidence and newest results never split.

    Historical result bodies may be replaced by hash-addressed local trace
    pointers. The last attempt and last completed result are both mandatory.
    All fields outside prior_events (including question/actions/evidence) stay
    intact. Identical citation metadata may reference the full evidence registry
    losslessly; source bodies are never changed. Oversize required blocks fail
    closed before any transport call.
    """
    packed = deepcopy(payload)
    events = packed.get("prior_events", [])
    if not isinstance(events, list):
        raise ValueError("prior_events must be a list")
    required = {len(events) - 1} if events else set()
    completed = [i for i, e in enumerate(events) if e.get("status") == "completed"]
    if completed:
        required.add(completed[-1])
    encode = lambda: json.dumps(packed, ensure_ascii=False, separators=(",", ":"))
    user = encode()
    before = estimated_tokens(system + user) + 32
    referenced = 0
    if before > INPUT_BUDGET and isinstance(packed.get("evidence"), list):
        evidence = packed["evidence"]
        lookup = {digest(citation): index for index, citation in enumerate(evidence)}
        for event in events:
            result = event.get("result", {})
            if not isinstance(result.get("citations"), list):
                continue
            for index, citation in enumerate(result["citations"]):
                target = lookup.get(digest(citation))
                if target is not None and citation == evidence[target]:
                    result["citations"][index] = {"evidence_ref": target}
                    referenced += 1
        if referenced:
            packed["reference_contract"] = {"evidence_ref": "zero-based index into the complete evidence list"}
            user = encode()
    omitted = []
    for index, event in enumerate(events):
        if estimated_tokens(system + user) + 32 <= INPUT_BUDGET:
            break
        if index in required or "result" not in event:
            continue
        content_hash = digest(payload["prior_events"][index]["result"])
        event["result"] = {"omitted_for_budget": True, "content_hash": content_hash,
                           "continuation": {"kind": "local_event", "event_index": index,
                                            "tool": event.get("tool"),
                                            "result_projection": event.get("result_projection", "identity"),
                                            "arguments": deepcopy(event.get("arguments", {}))}}
        omitted.append(index)
        user = encode()
    size = estimated_tokens(system + user) + 32
    meta = {"policy_version": AGENT_CONTEXT_VERSION, "estimated_input_tokens": size,
            "untrimmed_input_tokens": before, "truncated": False,
            "omitted_event_indices": omitted, "referenced_citations": referenced}
    if size > INPUT_BUDGET:
        continuations = []
        for index in sorted(required):
            event = events[index]
            pointer = tool_continuation(event.get("tool"), event.get("arguments", {}),
                                        event.get("result", {}), narrower=True)
            if pointer:
                continuations.append(pointer)
        raise ContextBudgetExceeded({**meta, "status": "oversize", "input_budget": INPUT_BUDGET,
                                     "required_event_indices": sorted(required),
                                     "content_hash": digest(payload), "continuations": continuations})
    return system, user, meta


def planner_diagnostics(*, content_hash=None, finish_reason=None, error_class=None, usage=None) -> dict:
    """Whitelist only hashes, enum metadata and numeric usage; never raw/reasoning."""
    usage = usage if isinstance(usage, dict) else {}
    counts = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                                        "input_tokens", "output_tokens")
              if type(usage.get(key)) is int and usage[key] >= 0}
    return {"content_hash": content_hash if isinstance(content_hash, str)
            and re.fullmatch(r"[0-9a-f]{64}", content_hash) else None,
            "finish_reason": finish_reason if finish_reason in
            ("stop", "length", "tool_calls", "function_call", "content_filter", None) else "unknown",
            "error_class": error_class if (error_class is None or isinstance(error_class, str)) and error_class in {
                None, "ValueError", "JSONDecodeError", "AgentError", "OutputTruncated",
                "ContextBudgetExceeded", "BudgetExceeded", "UpstreamHTTPError",
                "ProviderUsageLimitReached", "URLError", "TimeoutError", "ConnectionError",
                "RemoteDisconnected", "KeyError", "IndexError", "TypeError"} else "OtherError",
            "usage": counts}


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
    evidence afterwards. Legacy string callers retain head-preserving behavior;
    Agent planning/answers instead use budget_json_messages for complete blocks.
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
        from .parser.languages import is_readable_source
        root = Path(db.get_meta(conn, "repo", ".")).resolve()
        target = (root / (p.get("path") or "")).resolve()
        return bool(root in target.parents and is_readable_source(target) and target.is_file()
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
