"""Independent, local source-identity and C-presentation checks; no gateway calls."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import contextmanager

import pytest

from codeatlas.eval.evidence_repair import FrozenSource, compact_c


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json_hash(value):
    return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())


@pytest.fixture
def frozen(tmp_path):
    root = tmp_path / "snapshot"
    source = root / "source"
    source.mkdir(parents=True)
    raws = {
        "a.c": b"/* banner */\r\nstatic int helper(void)\r\n{\r\n    return/**/42;\r\n}\r\n/* footer */\r\n",
        "b.c": b"/* banner */\nstatic int helper(void)\n{\n    return 99;\n}\n/* footer */\n",
    }
    for name, raw in raws.items():
        (source / name).write_bytes(raw)
    source_files = {name: _sha(raw) for name, raw in raws.items()}
    identity = {"repository": "fixture-public-c", "revision": "a" * 40,
                "knowledge_set_id": "ks-independent", "source_content_hash": _json_hash(source_files),
                "build_config_hash": "b" * 64}
    conn = sqlite3.connect(root / "facts.db")
    conn.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE node(id TEXT PRIMARY KEY, kind TEXT, name TEXT, usr TEXT,
            path TEXT, line_start INTEGER, line_end INTEGER, definition_hash TEXT,
            is_definition INTEGER);
    """)
    conn.executemany("INSERT INTO meta VALUES(?,?)",
                     [("repository_id" if k == "repository" else k, v) for k, v in identity.items()])
    for name, raw in raws.items():
        definition = _sha(b"".join(raw.splitlines(keepends=True)[1:5]))
        conn.execute("INSERT INTO node VALUES(?,?,?,?,?,?,?,?,?)",
                     ("file-" + name, "file", name, None, name, 1, 6, None, 1))
        conn.execute("INSERT INTO node VALUES(?,?,?,?,?,?,?,?,?)",
                     ("fn-" + name, "function", "helper", f"c:{name}@F@helper",
                      name, 2, 5, definition, 1))
    conn.commit()
    conn.close()
    manifest = {**identity, "schema_version": 1, "source_files": source_files, "artifacts": {}}
    spec = {"root": str(root)}

    def reseal():
        manifest["artifacts"] = {"facts.db": _sha((root / "facts.db").read_bytes()),
                                 **{"source/" + name: _sha((source / name).read_bytes()) for name in raws}}
        encoded = json.dumps(manifest, sort_keys=True).encode()
        (root / "manifest.json").write_bytes(encoded)
        spec["manifest_hash"] = _sha(encoded)

    reseal()
    origin = {**identity, "path": "a.c", "usr": "c:a.c@F@helper", "line_start": 4, "line_end": 4,
              "definition_hash": _sha(b"".join(raws["a.c"].splitlines(keepends=True)[1:5]))}
    return {"root": root, "raws": raws, "identity": identity, "manifest": manifest,
            "spec": spec, "origin": origin, "reseal": reseal}


@contextmanager
def _open(frozen):
    source = FrozenSource(frozen["spec"], frozen["identity"])
    try:
        yield source
    finally:
        source.conn.close()


def test_exact_subrange_keeps_raw_line_endings_and_hashes_full_definition(frozen):
    with _open(frozen) as source:
        result = source.excerpt(frozen["origin"])
    raw = frozen["raws"]["a.c"]
    body = b"    return/**/42;\r\n"
    assert result["text"].encode() == body
    assert result["range_sha256"] == _sha(body)
    assert result["file_sha256"] == _sha(raw)
    assert result["range_sha256"] != frozen["origin"]["definition_hash"]
    assert result["visibility"] == "reviewer_only_verification"


def test_manifest_bytes_are_bound_by_the_external_spec(frozen):
    path = frozen["root"] / "manifest.json"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="manifest changed"):
        with _open(frozen):
            pass


def test_database_artifact_cannot_change_after_sealing(frozen):
    with sqlite3.connect(frozen["root"] / "facts.db") as conn:
        conn.execute("UPDATE meta SET value='different' WHERE key='revision'")
    with pytest.raises(ValueError, match="artifact changed"):
        with _open(frozen):
            pass


def test_source_artifact_cannot_change_after_sealing(frozen):
    (frozen["root"] / "source/a.c").write_bytes(frozen["raws"]["a.c"].replace(b"42", b"43"))
    with _open(frozen) as source:
        with pytest.raises(ValueError, match="artifact changed"):
            source.excerpt(frozen["origin"])


@pytest.mark.parametrize("field", ["repository", "revision", "knowledge_set_id", "source_content_hash", "build_config_hash"])
def test_every_identity_field_must_match_at_construction_and_read(frozen, field):
    bad_identity = {**frozen["identity"], field: "different"}
    with pytest.raises(ValueError, match="identity mismatch"):
        FrozenSource(frozen["spec"], bad_identity)
    with _open(frozen) as source:
        with pytest.raises(ValueError, match="different frozen source identity"):
            source.excerpt({**frozen["origin"], field: "different"})


@pytest.mark.parametrize("change", [
    {"usr": "helper"}, {"usr": "c:b.c@F@helper"}, {"path": "b.c"},
    {"definition_hash": "0" * 64}, {"definition_hash": None},
])
def test_function_name_or_other_path_never_substitutes_for_exact_anchor(frozen, change):
    with _open(frozen) as source:
        with pytest.raises(ValueError, match="unique definition"):
            source.excerpt({**frozen["origin"], **change})


def test_duplicate_exact_usr_path_hash_is_rejected(frozen):
    with sqlite3.connect(frozen["root"] / "facts.db") as conn:
        conn.execute("INSERT INTO node SELECT 'duplicate',kind,name,usr,path,line_start,line_end,definition_hash,is_definition FROM node WHERE id='fn-a.c'")
    frozen["reseal"]()
    with _open(frozen) as source:
        with pytest.raises(ValueError, match="unique definition"):
            source.excerpt(frozen["origin"])


def test_same_external_usr_in_another_path_does_not_create_ambiguity(frozen):
    with sqlite3.connect(frozen["root"] / "facts.db") as conn:
        conn.execute("UPDATE node SET usr='c:@F@main' WHERE kind='function'")
    frozen["reseal"]()
    with _open(frozen) as source:
        assert "42" in source.excerpt({**frozen["origin"], "usr": "c:@F@main"})["text"]


@pytest.mark.parametrize("path", ["../outside.c", "dir/../../a.c", "/private/tmp/outside.c", ""])
def test_absolute_and_traversal_paths_are_rejected(frozen, path):
    with _open(frozen) as source:
        with pytest.raises(ValueError):
            source.excerpt({**frozen["origin"], "path": path})


def test_symlink_cannot_escape_snapshot(frozen, tmp_path):
    outside = tmp_path / "outside.c"
    outside.write_bytes(frozen["raws"]["a.c"])
    target = frozen["root"] / "source/a.c"
    target.unlink()
    target.symlink_to(outside)
    with _open(frozen) as source:
        with pytest.raises(ValueError, match="escaped"):
            source.excerpt(frozen["origin"])


def test_source_symlink_cannot_read_a_sibling_artifact_outside_source_root(frozen):
    sibling = frozen["root"] / "external-copy.c"
    sibling.write_bytes(frozen["raws"]["a.c"])
    target = frozen["root"] / "source/a.c"
    target.unlink()
    target.symlink_to(sibling)
    # Identical bytes still must not expand the source boundary to the set root.
    with _open(frozen) as source:
        with pytest.raises(ValueError):
            source.excerpt(frozen["origin"])


@pytest.mark.parametrize("start,end", [(0, 1), (4, 3), (4, 999), (True, 4), (4, 4.0), ("4", 4)])
def test_invalid_ranges_are_never_clamped_or_coerced(frozen, start, end):
    with _open(frozen) as source:
        with pytest.raises(ValueError, match="range is invalid"):
            source.excerpt({**frozen["origin"], "line_start": start, "line_end": end})


def test_valid_file_line_outside_function_is_not_a_function_subrange(frozen):
    with _open(frozen) as source:
        with pytest.raises(ValueError, match="subrange changed"):
            source.excerpt({**frozen["origin"], "line_start": 6, "line_end": 6})


def test_definition_hash_is_recomputed_independently_of_artifact_hash(frozen):
    path = frozen["root"] / "source/a.c"
    path.write_bytes(frozen["raws"]["a.c"].replace(b"42", b"43"))
    frozen["manifest"]["source_files"]["a.c"] = _sha(path.read_bytes())
    new_hash = _json_hash(frozen["manifest"]["source_files"])
    for value in (frozen["manifest"], frozen["identity"], frozen["origin"]):
        value["source_content_hash"] = new_hash
    with sqlite3.connect(frozen["root"] / "facts.db") as conn:
        conn.execute("UPDATE meta SET value=? WHERE key='source_content_hash'", (new_hash,))
    frozen["reseal"]()
    with _open(frozen) as source:
        with pytest.raises(ValueError, match="definition bytes"):
            source.excerpt(frozen["origin"])


def test_definition_span_itself_cannot_extend_past_eof(frozen):
    truncated_hash = _sha(b"".join(frozen["raws"]["a.c"].splitlines(keepends=True)[1:999]))
    with sqlite3.connect(frozen["root"] / "facts.db") as conn:
        conn.execute("UPDATE node SET line_end=999,definition_hash=? WHERE id='fn-a.c'", (truncated_hash,))
    frozen["reseal"]()
    with _open(frozen) as source:
        with pytest.raises(ValueError):
            source.excerpt({**frozen["origin"], "definition_hash": truncated_hash})


def test_no_usr_file_range_uses_a_real_file_node(frozen):
    origin = copy.deepcopy(frozen["origin"])
    origin.update(usr=None, definition_hash=None, line_start=1, line_end=6)
    with _open(frozen) as source:
        assert source.excerpt(origin)["text"].encode() == frozen["raws"]["a.c"]
    with sqlite3.connect(frozen["root"] / "facts.db") as conn:
        conn.execute("DELETE FROM node WHERE kind='file' AND path='a.c'")
    frozen["reseal"]()
    with _open(frozen) as source:
        with pytest.raises(ValueError, match="no file identity"):
            source.excerpt(origin)


def test_facts_meta_must_agree_with_sealed_manifest_identity(frozen):
    with sqlite3.connect(frozen["root"] / "facts.db") as conn:
        conn.execute("UPDATE meta SET value='another-build' WHERE key='build_config_hash'")
    frozen["reseal"]()
    with pytest.raises(ValueError):
        with _open(frozen) as source:
            source.excerpt(frozen["origin"])


def test_source_files_digest_must_agree_with_actual_artifact(frozen):
    frozen["manifest"]["source_files"]["a.c"] = "0" * 64
    frozen["reseal"]()
    with pytest.raises(ValueError):
        with _open(frozen) as source:
            source.excerpt(frozen["origin"])


def test_coherent_source_map_identity_still_requires_each_file_hash_to_match(frozen):
    frozen["manifest"]["source_files"]["a.c"] = "0" * 64
    new_hash = _json_hash(frozen["manifest"]["source_files"])
    for value in (frozen["manifest"], frozen["identity"], frozen["origin"]):
        value["source_content_hash"] = new_hash
    with sqlite3.connect(frozen["root"] / "facts.db") as conn:
        conn.execute("UPDATE meta SET value=? WHERE key='source_content_hash'", (new_hash,))
    frozen["reseal"]()
    with pytest.raises(ValueError):
        with _open(frozen) as source:
            source.excerpt(frozen["origin"])


@pytest.mark.parametrize("original,expected", [
    ("return/*x*/42;", "return 42;"),
    ("return a+/**/+a;", "return a+ +a;"),
    ("#define F/**/(x) x", "#define F (x) x"),
    ('const char *s="/*kept*/ // kept";', 'const char *s="/*kept*/ // kept";'),
    ("int x = '/'; // removed\nreturn x;", "int x = '/';\nreturn x;"),
])
def test_comments_do_not_merge_tokens_and_literals_are_preserved(original, expected):
    assert compact_c(original) == expected


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("source", ['const char *s = "a\\{nl}  b";', '// hidden \\{nl}return 1;{nl}return 0;'])
def test_standard_backslash_continuations_remain_verbatim(newline, source):
    original = source.format(nl=newline)
    assert compact_c(original) == original


@pytest.mark.parametrize("gap", [" ", "\t"])
def test_compiler_accepted_whitespace_before_spliced_newline_is_preserved(gap):
    original = "int f(void) {\n// hidden \\" + gap + "\nreturn 1;\nreturn 0;\n}"
    assert compact_c(original) == original


def test_carriage_return_lines_do_not_turn_all_remaining_code_into_a_comment():
    original = "int f(void) {\r// hidden\rreturn 0;\r}"
    compacted = compact_c(original)
    assert "return 0;" in compacted
    assert compacted.endswith("}")
