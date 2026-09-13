"""Content-derived dependencies and deterministic knowledge revalidation.

The first implementation bound every card to every parsed entity.  That was
safe, but it made an unrelated edit invalidate every card.  This module keeps
the fail-closed behaviour while recording the inputs a card actually consumes:
its anchor, reviewed/mentioned entities, structural references, semantic
skeletons, macros and build configuration.
"""
from __future__ import annotations
import json
import os
import re
from collections import deque
from pathlib import Path

from .parser.languages import READABLE_EXT
from . import db
from .contracts import digest

TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*|[A-Za-z_]\w*|\d+(?:\.\d+)?|\S', re.S)
IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
MACRO = re.compile(
    r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_][A-Za-z0-9_]*)(?:\([^\n]*?\))?[ \t]*(.*(?:\\\n.*)*)",
    re.M,
)
REFERENCE_KINDS = {"type_use", "field_access", "global_ref"}


def normalized(source: str) -> list[str]:
    # Keep literals, operators, directives; remove only whitespace and comments.
    return [x for x in TOKEN.findall(source) if not x.startswith(("//", "/*"))]


def _node_fingerprints(conn) -> dict[str, str]:
    root = Path(db.get_meta(conn, "repo", "."))
    result = {"build_config": db.get_meta(conn, "build_config_hash", "unknown")}
    lines = {}
    for row in conn.execute("SELECT * FROM node ORDER BY id"):
        if row["kind"] == "function" and row["definition_hash"]:
            # A parser-verified definition fingerprint survives pure location changes.
            result[row["id"]] = row["definition_hash"]
            continue
        path = row["path"]
        if not path:
            continue
        if path not in lines:
            try:
                lines[path] = (root / path).read_text(errors="replace").splitlines(keepends=True)
            except OSError:
                lines[path] = None
        if lines[path] is None:
            result[row["id"]] = "unknown"
            continue
        text = "".join(lines[path] if row["kind"] == "file" else
                       lines[path][max(0, (row["line_start"] or 1)-1):row["line_end"]])
        result[row["id"]] = digest(normalized(text))
    return result


def _stable_payload(value):
    """Drop location/config duplication while preserving predicates and literals."""
    if isinstance(value, dict):
        return {key: _stable_payload(item) for key, item in sorted(value.items())
                if key not in {"build_config_hash", "function_id", "line_start", "line_end"}}
    if isinstance(value, list):
        return [_stable_payload(item) for item in value]
    return value


def _semantic_fingerprints(conn) -> dict[str, str]:
    grouped: dict[str, list[dict]] = {}
    for row in conn.execute(
        "SELECT function_id,kind,payload_json FROM semantic_fact WHERE confidence='certain' "
        "ORDER BY function_id,kind,id"
    ):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = {"invalid_payload": row["payload_json"]}
        grouped.setdefault(row["function_id"], []).append({
            "kind": row["kind"], "payload": _stable_payload(payload),
        })
    return {"facts:" + function_id: digest(facts)
            for function_id, facts in grouped.items()}


def _macro_definitions_from_text(source: str, relative: str) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for match in MACRO.finditer(source):
        found.setdefault(match.group(1), []).append({
            "path": relative,
            "definition": normalized(match.group(0)),
        })
    return found


def _macro_definitions(root: Path, include_paths: set[str] | None = None) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    if not root.is_dir():
        return found
    source_paths = []
    for directory, directories, files in os.walk(root):
        directories[:] = sorted(name for name in directories
                                if name not in {".git", ".venv", "build", "dist", "node_modules"})
        source_paths.extend(Path(directory) / name for name in files
                            if Path(name).suffix.lower() in READABLE_EXT)
    for path in sorted(source_paths):
        relative = path.relative_to(root).as_posix()
        if include_paths is not None and relative not in include_paths:
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name, definitions in _macro_definitions_from_text(source, relative).items():
            found.setdefault(name, []).extend(definitions)
    return found


def _macro_fingerprints(conn) -> dict[str, str]:
    root = Path(db.get_meta(conn, "repo", "."))
    parsed_paths = {row["path"] for row in conn.execute(
        "SELECT DISTINCT path FROM node WHERE path IS NOT NULL AND path!=''"
    )}
    return {"macro:" + name: digest(definitions)
            for name, definitions in _macro_definitions(root, parsed_paths).items()}


def _matching_delimiter(source: str, start: int, opening: str, closing: str) -> int:
    """Find a matching C delimiter while ignoring comments and string literals."""
    depth = 0
    index = start
    state = "code"
    while index < len(source):
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if state == "code":
            if char == "/" and nxt == "/":
                state = "line_comment"; index += 2; continue
            if char == "/" and nxt == "*":
                state = "block_comment"; index += 2; continue
            if char in {'"', "'"}:
                state = char; index += 1; continue
            if char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0:
                    return index
        elif state == "line_comment":
            if char == "\n":
                state = "code"
        elif state == "block_comment":
            if char == "*" and nxt == "/":
                state = "code"; index += 2; continue
        else:
            if char == "\\":
                index += 2; continue
            if char == state:
                state = "code"
        index += 1
    raise ValueError("unbalanced C delimiter")


def _code_mask(source: str) -> str:
    """Preserve offsets/newlines while hiding comments and quoted literals."""
    output = list(source)
    index = 0
    state = "code"
    while index < len(source):
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if state == "code":
            if char == "/" and nxt == "/":
                output[index] = output[index + 1] = " "; state = "line_comment"; index += 2; continue
            if char == "/" and nxt == "*":
                output[index] = output[index + 1] = " "; state = "block_comment"; index += 2; continue
            if char in {'"', "'"}:
                output[index] = " "; state = char; index += 1; continue
        elif state == "line_comment":
            if char == "\n":
                state = "code"
            else:
                output[index] = " "
        elif state == "block_comment":
            if char == "*" and nxt == "/":
                output[index] = output[index + 1] = " "; state = "code"; index += 2; continue
            if char != "\n":
                output[index] = " "
        else:
            if char == "\\":
                output[index] = " "
                if index + 1 < len(source) and source[index + 1] != "\n":
                    output[index + 1] = " "
                index += 2; continue
            if char == state:
                output[index] = " "; state = "code"
            elif char != "\n":
                output[index] = " "
        index += 1
    return "".join(output)


def _function_definition(source: str, name: str) -> str:
    """Return one named C function definition, skipping declarations and calls."""
    masked = _code_mask(source)
    pattern = re.compile(r"\b" + re.escape(name) + r"\s*\(")
    for match in pattern.finditer(masked):
        open_paren = masked.find("(", match.start())
        try:
            close_paren = _matching_delimiter(masked, open_paren, "(", ")")
        except ValueError:
            continue
        cursor = close_paren + 1
        while cursor < len(masked) and masked[cursor].isspace():
            cursor += 1
        # The fixed public corpora use ordinary C definitions: after the final
        # parameter ')' only whitespace precedes '{'. Calls nested in if/return
        # expressions therefore cannot be mistaken for definitions.
        if cursor >= len(masked) or masked[cursor] != "{":
            continue
        brace = cursor
        try:
            end = _matching_delimiter(masked, brace, "{", "}")
        except ValueError:
            continue
        boundary = max(masked.rfind(";", 0, match.start()),
                       masked.rfind("}", 0, match.start()),
                       masked.rfind("{", 0, match.start()))
        return source[boundary + 1:end + 1].strip()
    raise ValueError(f"function dependency not found: {name}")


def source_dependency_fingerprints(
    root: str | Path,
    functions: list[dict],
    macro_names: list[str | dict] | tuple[str | dict, ...] = (),
) -> dict[str, str]:
    """Fingerprint a reviewed dependency declaration directly from frozen source.

    This is used by the upstream mutation harness and mirrors the card manifest's
    definition/macro inputs without defining the experiment's ground truth.
    """
    root = Path(root).resolve()
    result: dict[str, str] = {}
    for item in functions:
        path = Path(item["path"])
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("function dependency path escapes source root")
        source_path = (root / path).resolve()
        if not source_path.is_relative_to(root) or not source_path.is_file():
            raise ValueError(f"function dependency file not found: {path}")
        source = source_path.read_text(encoding="utf-8", errors="replace")
        key = f"function:{path.as_posix()}:{item['name']}"
        result[key] = digest(normalized(_function_definition(source, item["name"])))
    for declaration in macro_names:
        if isinstance(declaration, str):
            name = declaration
            definitions = _macro_definitions(root).get(name, [])
        else:
            name = declaration["name"]
            definitions = []
            for raw_path in declaration["paths"]:
                path = Path(raw_path)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("macro dependency path escapes source root")
                source_path = (root / path).resolve()
                if not source_path.is_relative_to(root) or not source_path.is_file():
                    raise ValueError(f"macro dependency file not found: {path}")
                source = source_path.read_text(encoding="utf-8", errors="replace")
                for macro_name, items in _macro_definitions_from_text(source, path.as_posix()).items():
                    if macro_name == name:
                        definitions.extend(items)
        if not definitions:
            raise ValueError(f"macro dependency not found: {name}")
        result["macro:" + name] = digest(definitions)
    return result


def fingerprints(conn) -> dict[str, str]:
    """Return the current dependency catalog, not a card's dependency set."""
    result = _node_fingerprints(conn)
    result.update(_semantic_fingerprints(conn))
    result.update(_macro_fingerprints(conn))
    return result


def _card_identifiers(conn, owner_id: str) -> set[str]:
    row = conn.execute("SELECT * FROM experience WHERE id=?", (owner_id,)).fetchone()
    if row is None:
        return set()
    fields = ("title", "symptom", "hypotheses", "dead_ends", "root_cause",
              "fix_steps", "verification")
    text = "\n".join(str(row[field] or "") for field in fields)
    for qa in conn.execute(
        "SELECT question,expected_points FROM experience_qa WHERE exp_id=?", (owner_id,)
    ):
        text += "\n" + str(qa["question"] or "") + "\n" + str(qa["expected_points"] or "")
    return set(IDENTIFIER.findall(text))


def _nodes_named(conn, names: set[str]) -> set[str]:
    if not names:
        return set()
    result: set[str] = set()
    values = sorted(names)
    for offset in range(0, len(values), 400):
        batch = values[offset:offset + 400]
        marks = ",".join("?" for _ in batch)
        result.update(row["id"] for row in conn.execute(
            f"SELECT id FROM node WHERE name IN ({marks}) AND kind!='file'", batch
        ))
    return result


def _source_text(conn, node_id: str) -> str:
    row = conn.execute("SELECT path,line_start,line_end FROM node WHERE id=?", (node_id,)).fetchone()
    if row is None or not row["path"]:
        return ""
    root = Path(db.get_meta(conn, "repo", "."))
    try:
        lines = (root / row["path"]).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    start = max(0, (row["line_start"] or 1) - 1)
    end = row["line_end"] or len(lines)
    return "\n".join(lines[start:end])


def dependency_rows(conn, node_id: str, *, owner_id: str | None = None,
                    entity_names: list[str] | tuple[str, ...] | None = None,
                    macro_names: list[str] | tuple[str, ...] | None = None,
                    call_depth: int = 0) -> list[dict]:
    """Build a bounded dependency manifest.

    ``entity_names`` and ``macro_names`` are the reviewed dependency declaration.
    Card identifiers are also resolved exactly, so a statement naming
    ``netif_default`` or ``CJSON_NESTING_LIMIT`` cannot silently omit it.  Call
    expansion is opt-in; production cards should declare the callees whose
    behaviour their conclusion consumes instead of depending on every callee.
    """
    if call_depth < 0 or call_depth > 2:
        raise ValueError("call_depth must be between 0 and 2")
    catalog = fingerprints(conn)
    identifiers = _card_identifiers(conn, owner_id) if owner_id else set()
    declared_names = set(entity_names or ())
    nodes = {node_id} | _nodes_named(conn, identifiers | declared_names)
    reasons = {node_id: "anchor"}
    for item in nodes - {node_id}:
        reasons[item] = "declared_entity" if conn.execute(
            "SELECT name FROM node WHERE id=?", (item,)
        ).fetchone()["name"] in declared_names else "mentioned_entity"

    frontier = {node_id}
    for _ in range(call_depth):
        if not frontier:
            break
        marks = ",".join("?" for _ in frontier)
        callees = {row["dst"] for row in conn.execute(
            f"SELECT dst FROM edge WHERE confidence='certain' AND kind='calls' "
            f"AND src IN ({marks})", sorted(frontier)
        )}
        frontier = callees - nodes
        nodes.update(frontier)
        for item in frontier:
            reasons[item] = "call_dependency"

    functions = {row["id"] for row in conn.execute(
        "SELECT id FROM node WHERE kind='function' AND id IN ({})".format(
            ",".join("?" for _ in nodes)
        ), sorted(nodes)
    )} if nodes else set()
    if functions:
        marks = ",".join("?" for _ in functions)
        for row in conn.execute(
            f"SELECT dst FROM edge WHERE confidence='certain' AND kind IN "
            f"('type_use','field_access','global_ref') AND src IN ({marks})",
            sorted(functions),
        ):
            nodes.add(row["dst"])
            reasons.setdefault(row["dst"], "referenced_entity")

    dependencies: dict[str, tuple[str, str]] = {
        "build_config": ("config", catalog.get("build_config", "unknown")),
    }
    for item in sorted(nodes):
        dependencies[item] = (reasons.get(item, "referenced_entity"), catalog.get(item, "unknown"))
    for function_id in sorted(functions):
        fact_id = "facts:" + function_id
        dependencies[fact_id] = ("semantic_skeleton", catalog.get(fact_id, digest([])))

    available_macros = {key[6:] for key in catalog if key.startswith("macro:")}
    consumed_text = "\n".join(_source_text(conn, item) for item in functions)
    inferred_macros = set(IDENTIFIER.findall(consumed_text)) & available_macros
    inferred_macros.update(identifiers & available_macros)
    declared_macros = set(macro_names or ())
    missing_macros = declared_macros - available_macros
    if missing_macros:
        raise ValueError("declared macro dependency not found: " + ", ".join(sorted(missing_macros)))
    for name in sorted(inferred_macros | declared_macros):
        key = "macro:" + name
        dependencies[key] = ("declared_macro" if name in declared_macros else "macro_definition",
                             catalog[key])

    return [{"dependency_id": key, "kind": kind, "fingerprint": fingerprint}
            for key, (kind, fingerprint) in sorted(dependencies.items())]


def capture(conn, owner_id: str, node_id: str, *,
            entity_names: list[str] | tuple[str, ...] | None = None,
            macro_names: list[str] | tuple[str, ...] | None = None,
            call_depth: int = 0) -> list[dict]:
    """Persist the card's bounded, reviewable dependency manifest."""
    rows = dependency_rows(conn, node_id, owner_id=owner_id,
                           entity_names=entity_names, macro_names=macro_names,
                           call_depth=call_depth)
    conn.execute("DELETE FROM knowledge_dependency WHERE owner_type='card' AND owner_id=?", (owner_id,))
    conn.executemany("INSERT INTO knowledge_dependency VALUES('card',?,?,?,?)",
                     [(owner_id, r["dependency_id"], r["kind"], r["fingerprint"]) for r in rows])
    return rows


def check(conn, owner_id: str) -> tuple[bool, str]:
    rows = conn.execute("SELECT * FROM knowledge_dependency WHERE owner_type='card' AND owner_id=?", (owner_id,)).fetchall()
    if not rows:
        return False, "dependency_manifest_missing"
    current = fingerprints(conn)
    for row in rows:
        after = current.get(row["dependency_id"])
        if after is None:
            return False, f"dependency_missing:{row['dependency_id']}"
        if after == "unknown" or row["fingerprint"] != after:
            return False, f"dependency_changed:{row['dependency_id']}"
    return True, ""


def propagate(changed: set[str], consumers: dict[str, set[str]], total: int,
              max_depth: int = 2) -> dict:
    queue = deque((x, [x]) for x in sorted(changed))
    visited, paths, full = set(), {}, False
    while queue:
        entity, path = queue.popleft()
        if entity in visited:
            continue
        visited.add(entity)
        paths[entity] = path
        children = sorted(consumers.get(entity, set()) - visited)
        if (len(path) > max_depth and children) or len(visited) > min(500, max(1, total * .3)):
            full = True
        # Always terminate the complete work list, never interpret the limit as safe.
        queue.extend((child, path + [child]) for child in children)
    return {"affected": sorted(visited), "paths": paths,
            "mode": "full_revalidation" if full else "dependency_revalidation"}


def record_wiki(conn):
    """File facts + config, then child summaries: explicit rebuild dependencies."""
    values = fingerprints(conn)
    pages = conn.execute("SELECT * FROM wiki_page WHERE status='ok'").fetchall()
    page_hashes = {"wiki:" + p["id"]: p["input_hash"] for p in pages}
    conn.execute("DELETE FROM knowledge_dependency WHERE owner_type='wiki'")
    for page in pages:
        consumed = {"build_config": values["build_config"]}
        for source in json.loads(page["sources"] or "[]"):
            if source.startswith("node:"):
                entity = source[5:]
                consumed[entity] = values.get(entity, "unknown")
            elif source.startswith("wiki:"):
                consumed[source] = page_hashes.get(source, "unknown")
        conn.executemany("INSERT INTO knowledge_dependency VALUES('wiki',?,?,?,?)",
                         [(page["id"], key, "consumed_input", value) for key, value in sorted(consumed.items())])
    conn.commit()


def audit_changes(previous, current):
    """Persist complete deterministic propagation; limits select full revalidation."""
    from datetime import datetime, timezone
    before, after = fingerprints(previous), fingerprints(current)
    for connection, values in ((previous, before), (current, after)):
        values.update({"wiki:" + r["id"]: r["input_hash"] for r in connection.execute(
            "SELECT id,input_hash FROM wiki_page WHERE status='ok'")})
    changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
    consumers = {}
    for row in previous.execute("SELECT * FROM knowledge_dependency"):
        owner = row["owner_type"] + ":" + row["owner_id"]
        consumers.setdefault(row["dependency_id"], set()).add(owner)
    result = propagate(changed, consumers, len(set(before) | set(after)))
    now = datetime.now(timezone.utc).isoformat()
    for target in result["affected"]:
        path = result["paths"][target]
        trigger = path[0]
        current.execute("""INSERT INTO invalidation_event
            (knowledge_set_id,target,trigger_id,rule,dependency_path,before_hash,after_hash,created_at)
            VALUES(?,?,?,?,?,?,?,?)""", (db.get_meta(current, "knowledge_set_id"), target, trigger,
            result["mode"], json.dumps(path), before.get(trigger), after.get(trigger), now))
    current.commit()
    return {"changed_count": len(changed), "affected_count": len(result["affected"]),
            "mode": result["mode"], "execution": "full_parse_and_deterministic_revalidation"}
