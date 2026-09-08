"""Independent Wiki presentation checks; no ledger, corpus or gateway required."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from codeatlas.eval import wiki_review_projection as wiki


def _item(rows, *, gold=None):
    item = {
        "trial_id": "independent-wiki-trial", "task_id": "local-source-case", "task_type": "semantic",
        "question": "Explain the supplied source.",
        "answer": "The exact cited behavior is [E1].",
        "refused": False, "evidence_payload": rows, "citations": copy.deepcopy(rows),
        "answer_key": {"expected_points": [{"id": "p1", "text": "Preserve the exact expression."}],
                       "forbidden_claims": ["Do not replace the original expression."],
                       "evidence_payload": copy.deepcopy(gold or [])},
    }
    item["input_hash"] = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
    return item


def _read(text, *, tools=("code_read",)):
    return {"tag": "E1", "kind": "frozen_tool_excerpt", "text": text,
            "visibility": "original_tool_output",
            "restoration": {"source": "original_trial_events",
                            "fragments": [{"tool": tool, "event_index": i} for i, tool in enumerate(tools)]}}


def _pointer(document, ref):
    assert ref.startswith("#/")
    value = document
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def _body(document, value, active=()):
    if isinstance(value, str):
        return value
    if "$ref" in value:
        assert value["$ref"] not in active, "cyclic source reference"
        return _body(document, _pointer(document, value["$ref"]), (*active, value["$ref"]))
    if "substring" in value:
        span = value["substring"]
        ref = "#/bodies/" + span["body"]
        parent = _body(document, {"$ref": ref}, active)
        start, end = span["start_character"], span["end_character"]
        assert type(start) is int and type(end) is int and 0 <= start <= end <= len(parent)
        return parent[start:end]
    if "parts" in value:
        parts = []
        for part in value["parts"]:
            if isinstance(part, str):
                parts.append(part)
            else:
                parts.append(_body(document, {"substring": part}, active))
        return "".join(parts)
    return _body(document, value["text"], active)


def _rows(rendered):
    return {row["tag"]: row for row in rendered["evidence_payload"]["other_evidence"]}


def _check_all_refs(document, value):
    if isinstance(value, dict):
        if "$ref" in value:
            _pointer(document, value["$ref"])
        for child in value.values():
            _check_all_refs(document, child)
    elif isinstance(value, list):
        for child in value:
            _check_all_refs(document, child)


@pytest.mark.parametrize("prefix", ["", " ", "    ", "\t", " \t "])
def test_original_code_read_gutter_accepts_space_or_tab_padding(prefix):
    original = _item([_read(prefix + "1019 | return/**/42;\n" + prefix + "1020 | }")])
    rendered = wiki.view(original)
    assert _body(rendered, _rows(rendered)["E1"]["text"]) == "return 42;\n}"


@pytest.mark.parametrize("tools", [(), ("wiki_section",), ("code_read", "wiki_section"), ("unknown",)])
def test_document_and_mixed_fragments_keep_line_like_text_exactly(tools):
    text = " 1019 | column text\n// prose, not a C comment\n\t806 | more data"
    original = _item([_read(text, tools=tools)])
    rendered = wiki.view(original)
    assert _body(rendered, _rows(rendered)["E1"]["text"]) == text


@pytest.mark.parametrize("kind", ["experiment", "verified_citation_source", "frozen_tool_excerpt"])
def test_no_trace_never_infers_code_read_from_gutter_shape(kind):
    text = ' 1019 | "ordinary quoted text"\n  806 | another column'
    original = _item([{"tag": "E1", "kind": kind, "text": text}])
    rendered = wiki.view(original)
    assert _body(rendered, _rows(rendered)["E1"]["text"]) == text


def test_gutter_is_removed_exactly_once_when_c_expression_starts_with_number_and_pipe():
    trace = "  101 | return\n  102 | 1 | flags;"
    rendered = wiki.view(_item([_read(trace)]))
    assert _body(rendered, _rows(rendered)["E1"]["text"]) == "return\n1 | flags;"


def test_gutter_shaped_text_inside_a_line_spliced_string_is_not_removed():
    source = 'const char *s = "a\\\n1019 | keep this literal";'
    trace = "\n".join(f"{n:>5} | {line}" for n, line in enumerate(source.splitlines(), 71))
    rendered = wiki.view(_item([_read(trace)]))
    assert _body(rendered, _rows(rendered)["E1"]["text"]) == source


def test_quoted_gutter_and_comment_markers_inside_one_line_string_are_preserved():
    text = '  101 | const char *s = "1019 | /* not a comment */";'
    rendered = wiki.view(_item([_read(text)]))
    assert _body(rendered, _rows(rendered)["E1"]["text"]) == 'const char *s = "1019 | /* not a comment */";'


def test_pinned_source_numeric_expression_has_no_tool_gutter_to_remove():
    source = "return\n1019 | flags;"
    rendered = wiki.view(_item([{"tag": "E1", "kind": "pinned_source", "text": source}]))
    assert _body(rendered, _rows(rendered)["E1"]["text"]) == source


def test_exact_body_refs_substrings_and_gold_oracle_can_all_be_reconstructed():
    excerpt = "int f(void)\n{\nreturn 42;\n}"
    complete = "int before;\n" + excerpt + "\nint after;"
    trace = "\n".join(f"{n:>5} | {line}" for n, line in enumerate(excerpt.splitlines(), 806))
    original_source = _read(trace)
    original_source["verification_source"] = {
        "text": excerpt, "path": "src/a.c", "line_start": 806, "line_end": 809,
        "visibility": "reviewer_only_verification", "boundary": "Not evidence of candidate exposure.",
    }
    gold = {"kind": "pinned_source", "path": "src/a.c", "lines": "805-810", "text": complete}
    item = _item([original_source, {"tag": "E2", **gold}], gold=[gold])
    before = copy.deepcopy(item)
    rendered = wiki.view(item)
    rows = _rows(rendered)

    assert _body(rendered, rows["E1"]["text"]) == excerpt
    assert _body(rendered, rows["E1"]["verification_source"]["text"]) == excerpt
    assert _body(rendered, rows["E2"]["text"]) == complete
    assert _body(rendered, rendered["answer_key"]["evidence_payload"][0]) == complete
    assert sum("text" in body for body in rendered["bodies"].values()) == 1
    assert any("substring" in body for body in rendered["bodies"].values())
    _check_all_refs(rendered, rendered)

    assert rows["E1"]["visibility"] == "original_tool_output"
    assert rows["E1"]["verification_source"]["visibility"] == "reviewer_only_verification"
    assert item == before
    assert rendered["answer"] == before["answer"]
    assert rendered["input_hash"] == before["input_hash"]
    assert rendered["projection"]["original_input_hash"] == before["input_hash"]
    assert rendered["projection"]["version"] == wiki.VERSION
    for field in ("expected_points", "forbidden_claims"):
        assert rendered["answer_key"][field] == before["answer_key"][field]


def test_mutating_rendered_output_cannot_mutate_original_item_answer_or_gold():
    item = _item([_read(" 1019 | return 42;")])
    before = copy.deepcopy(item)
    rendered = wiki.view(item)
    rendered["answer_key"]["expected_points"][0]["text"] = "changed locally"
    rendered["answer"] = "changed locally"
    next(iter(rendered["bodies"].values()))["text"] = "changed locally"
    assert item == before


def test_partial_overlap_parts_are_lossless_with_unicode_and_two_parent_bodies():
    first_common = "".join(f"甲{i}: 独立原始证据甲段{'甲' * 24}\n" for i in range(8))
    second_common = "".join(f"乙{i}: 独立原始证据乙段{'乙' * 24}\n" for i in range(8))
    texts = [
        "PARENT-A\n" + first_common + "A" * 1300 + "\nEND-A",
        "PARENT-B\n" + second_common + "B" * 1200 + "\nEND-B",
        "UNIQUE-CHILD-PREFIX\n" + first_common + "UNIQUE-MIDDLE\n" + second_common + "UNIQUE-CHILD-END",
    ]
    item = _item([{"tag": f"E{i}", "kind": "experiment", "text": text}
                  for i, text in enumerate(texts, 1)])
    before = copy.deepcopy(item)
    rendered = wiki.view(item)
    rows = _rows(rendered)
    for i, expected in enumerate(texts, 1):
        assert _body(rendered, rows[f"E{i}"]["text"]) == expected
    child = _pointer(rendered, rows["E3"]["text"]["$ref"])
    assert "parts" in child
    assert len({part["body"] for part in child["parts"] if isinstance(part, dict)}) == 2
    assert item == before
    _check_all_refs(rendered, rendered)


def test_substring_of_a_body_later_factored_into_parts_remains_reconstructible():
    common = "".join(f"common-{i}: {'shared-source' * 4}\n" for i in range(8))
    smallest = "MID-ONLY-BEGIN\n" + "unique middle-only source " * 12 + "\nMID-ONLY-END"
    texts = ["PARENT\n" + common + "P" * 1600 + "\nPARENT-END",
             "MIDDLE\n" + common + smallest + "\nMIDDLE-END", smallest]
    item = _item([{"tag": f"E{i}", "kind": "experiment", "text": text}
                  for i, text in enumerate(texts, 1)])
    rendered = wiki.view(item)
    rows = _rows(rendered)
    middle_body = _pointer(rendered, rows["E2"]["text"]["$ref"])
    smallest_body = _pointer(rendered, rows["E3"]["text"]["$ref"])
    assert "parts" in middle_body
    assert "substring" in smallest_body
    assert rendered["bodies"][smallest_body["substring"]["body"]] is middle_body
    for i, expected in enumerate(texts, 1):
        assert _body(rendered, rows[f"E{i}"]["text"]) == expected
    _check_all_refs(rendered, rendered)
