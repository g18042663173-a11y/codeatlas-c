"""Local AST observations, never a whole-program control/data-flow proof."""
from __future__ import annotations
import json
from ..contracts import digest


def extract(fn, function_id: str, path: str, tu: str, config: str) -> list[dict]:
    facts = []
    from pathlib import Path
    source_file = fn.extent.start.file.name if fn.extent.start.file else None
    source = Path(source_file).read_bytes() if source_file else b""
    cache = {}

    def tokens(cursor):
        extent = cursor.extent
        key = (extent.start.offset, extent.end.offset)
        if key not in cache:
            if (extent.start.file and extent.start.file.name == source_file
                    and 0 <= key[0] <= key[1] <= len(source)):
                cache[key] = source[key[0]:key[1]].decode("utf-8", "replace").strip()
            else:
                cache[key] = " ".join(t.spelling for t in cursor.get_tokens())
        return cache[key]

    def visit(cur, conditions):
        kind = cur.kind.name
        children = list(cur.get_children())
        payload = None
        fact_kind = None
        if kind == "IF_STMT" and len(children) >= 2:
            condition = tokens(children[0])
            payload = {"condition": condition,
                       "then": tokens(children[1]),
                       "else": tokens(children[2]) if len(children) > 2 else None,
                       "implicit_else": len(children) == 2}
            fact_kind = "guard" if any(c.kind.name == "RETURN_STMT" for c in children[1].walk_preorder()) else "branch_set"
        elif kind in ("SWITCH_STMT", "FOR_STMT", "WHILE_STMT", "DO_STMT"):
            payload = {"control_kind": kind, "source": tokens(cur),
                       "fallthrough": "not_inferred" if kind == "SWITCH_STMT" else None,
                       "loop_boundary": "explicit_source_only" if kind != "SWITCH_STMT" else None}
            if kind == "SWITCH_STMT":
                labels = [c for c in cur.walk_preorder() if c.kind.name in ("CASE_STMT", "DEFAULT_STMT")]
                payload["case_labels"] = [{"kind": c.kind.name, "source": tokens(c),
                    "line_start": c.extent.start.line, "line_end": c.extent.end.line,
                    "next_label_line": labels[i+1].extent.start.line if i+1 < len(labels) else None,
                    "fallthrough_confidence": "candidate"} for i, c in enumerate(labels)]
            else:
                payload["components"] = [{"kind": c.kind.name, "source": tokens(c),
                    "line_start": c.extent.start.line, "line_end": c.extent.end.line} for c in children]
            fact_kind = "decider"
        elif kind == "RETURN_STMT":
            payload = {"expression": tokens(cur), "error_classification": "unknown"}
            fact_kind = "error_path"  # A potential exit; nonzero does NOT mean failure.
        elif kind in ("BINARY_OPERATOR", "COMPOUND_ASSIGNMENT_OPERATOR") and len(children) == 2:
            between = [t.spelling for t in cur.get_tokens()
                       if children[0].extent.end.offset <= t.extent.start.offset < children[1].extent.start.offset]
            if any(t in ("=", "+=", "-=", "*=", "/=", "|=", "&=", "^=", "<<=", ">>=") for t in between):
                payload = {"target": tokens(children[0]), "operator": " ".join(between),
                           "value": tokens(children[1]), "alias_effect": "not_inferred"}
                fact_kind = "state_write"
        if payload is not None:
            payload.update({"enclosing_conditions": conditions, "function_id": function_id,
                            "build_config_hash": config})
            fid = digest([function_id, fact_kind, payload, len(facts)])[:24]
            facts.append({"id": fid, "function_id": function_id, "kind": fact_kind,
                          "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                          "confidence": "certain", "path": path,
                          "line_start": cur.extent.start.line, "line_end": cur.extent.end.line, "tu": tu})
        for index, child in enumerate(children):
            nested = conditions
            if kind == "IF_STMT" and index > 0:
                nested = conditions + [{"condition": tokens(children[0]), "branch": "then" if index == 1 else "else"}]
            visit(child, nested)

    visit(fn, [])
    return facts
