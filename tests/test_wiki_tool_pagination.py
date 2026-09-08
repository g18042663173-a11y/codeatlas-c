"""Offline Wiki read gates and lossless, complete-block pagination."""
import json

import pytest

from codeatlas import agent, contracts, db
from codeatlas.wiki import generator


@pytest.fixture
def wiki():
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO wiki_page(id,level,title,md,status,input_hash,repository,revision) "
                 "VALUES('repo','repo','Fixture','## Flow\nComplete content.','ok','fixed','fixture','fixed')")
    conn.commit()
    yield conn
    conn.close()


def body(conn, text):
    conn.execute("UPDATE wiki_page SET md=? WHERE id='repo'", ("## Flow\n" + text,))
    conn.commit()


@pytest.mark.parametrize("result", [{"pages": [], "wiki_unavailable": True},
                                    {"pages": [], "wiki_unavailable": False},
                                    {"pages": [{"page_id": "repo"}], "wiki_unavailable": True},
                                    {"pages": [{"page_id": "repo", "sections": []}]},
                                    {"pages": [None]}, {"pages": "not a list"}])
def test_empty_or_unavailable_outline_cannot_complete_prerequisite(wiki, monkeypatch, result):
    monkeypatch.setattr(agent, "_execute", lambda *_a, **_k: result)
    call = agent._run_call(wiki, {"tool": "wiki_outline", "arguments": {}}, embedder=None, data_dir="unused")
    assert call["status"] == "failed"
    events = [{"tool": "search_evidence", "status": "completed", "wiki_required": True}, call]
    assert contracts.agent_action_contract(events)["allowed_next"] == ["wiki_outline"]


@pytest.mark.parametrize("markdown", ["", " \n\t", "## Flow\n\n"])
def test_empty_section_is_not_completed(wiki, monkeypatch, markdown):
    monkeypatch.setattr(agent, "_execute", lambda *_a, **_k: {"markdown": markdown})
    call = agent._run_call(wiki, {"tool": "wiki_section", "arguments": {"page_id": "repo", "section": "Flow"}},
                           embedder=None, data_dir="unused")
    assert call["status"] == "failed"


def test_real_missing_module_outline_does_not_unlock_section(wiki):
    call = agent._run_call(wiki, {"tool": "wiki_outline", "arguments": {"module": "DOES_NOT_EXIST"}},
                           embedder=None, data_dir="unused")
    assert call["result"]["wiki_unavailable"] is True
    assert call["status"] == "failed"


def test_condition_paragraph_is_never_cut_to_fit_a_byte_window(wiki):
    condition = "It is safe only when " + "the prerequisite holds and " * 30 + "the resource is owned."
    body(wiki, condition)
    result = generator.section(wiki, "repo", "Flow", budget=100)
    assert result["status"] == "oversize" and result["markdown"] == ""
    pointer = result["continuation"]
    assert pointer["arguments"]["offset"] == 0 and pointer["arguments"]["budget"] > 100
    full = generator.section(wiki, "repo", "Flow", offset=0, budget=pointer["arguments"]["budget"])
    assert full["markdown"] == "## Flow\n" + condition and full["next_offset"] is None


def test_paragraph_pages_rejoin_exactly_with_utf8_offsets(wiki):
    paragraphs = [f"完整条件 {i}：只有满足前提且指针有效，才能执行操作。" * 2 for i in range(5)]
    body(wiki, "\n\n".join(paragraphs))
    offset, parts = 0, []
    while True:
        result = generator.section(wiki, "repo", "Flow", offset=offset, budget=150)
        assert "error" not in result
        part = result["markdown"]
        assert part.encode().decode() == part and len(part.encode()) <= 450
        parts.append(part)
        if result["next_offset"] is None:
            break
        assert result["next_offset"] == sum(len(p.encode()) for p in parts)
        offset = result["next_offset"]
        assert len(parts) < 10
    assert len(parts) > 1
    assert "".join(parts) == "## Flow\n" + "\n\n".join(paragraphs)


def test_json_fence_and_condition_entry_are_atomic_even_with_blank_lines(wiki):
    payload = {"condition": "ready && owned", "then": "detach", "otherwise": "leave linked"}
    item = "- Conditional operation\n```json\n" + json.dumps(payload, indent=2) + "\n\n```"
    body(wiki, item + "\n- Independent complete entry.\n")
    budget = (len(("## Flow\n" + item + "\n").encode()) + 2) // 3
    first = generator.section(wiki, "repo", "Flow", budget=budget)
    assert first["markdown"] == "## Flow\n" + item + "\n"
    fenced = first["markdown"].split("```json\n", 1)[1].split("```", 1)[0]
    assert json.loads(fenced) == payload
    second = generator.section(wiki, "repo", "Flow", offset=first["next_offset"], budget=100)
    assert second["markdown"] == "- Independent complete entry."


def test_code_fence_heading_does_not_end_a_section(wiki):
    content = "```c\n## not a Wiki heading\n\nif (ready) { return 42; }\n```\n\nComplete conclusion."
    body(wiki, content + "\n\n## Other\nNot this section.")
    result = generator.section(wiki, "repo", "Flow", budget=1000)
    assert result["markdown"] == "## Flow\n" + content
    outline = generator.outline(wiki)
    assert outline["pages"][0]["sections"] == ["Flow", "Other"]


def test_mechanism_entry_does_not_split_between_claim_and_boundary(wiki):
    mechanism = '<a id="mechanism-m1"></a>\n### Entry\n\n- Claim.\n\n- Only when ' + "condition " * 40 + "."
    body(wiki, mechanism)
    result = generator.section(wiki, "repo", "Flow", budget=100)
    assert result["status"] == "oversize" and result["markdown"] == ""


def test_offset_inside_utf8_or_paragraph_is_rejected_without_skipping_content(wiki):
    body(wiki, "中文条件完整，不得跳过前提。\n\n下一段完整。")
    result = generator.section(wiki, "repo", "Flow", offset=10, budget=100)
    assert result["error"] == "wiki_section_invalid_offset"
    assert result["markdown"] == ""
    assert result["continuation"]["arguments"]["offset"] == 0


def test_outline_never_injects_an_oversize_first_json_entry(wiki):
    wiki.execute("UPDATE wiki_page SET title=?", ("完整标题" * 200,))
    result = generator.outline(wiki, budget=100)
    assert result["status"] == "oversize" and result["pages"] == []
    pointer = result["continuation"]
    full = generator.outline(wiki, **pointer["arguments"])
    assert full["pages"][0]["title"] == "完整标题" * 200
    assert json.loads(json.dumps(full["pages"])) == full["pages"]


def test_unreadable_single_block_above_maximum_fails_closed_without_loop(wiki):
    body(wiki, "indivisible condition " * 1500)
    result = generator.section(wiki, "repo", "Flow", budget=8000)
    assert result["status"] == "oversize" and result["required_budget"] > 8000
    assert result["markdown"] == "" and result["continuation"] is None
    call = agent._run_call(wiki, {"tool": "wiki_section", "arguments": {"page_id": "repo", "section": "Flow", "budget": 8000}},
                           embedder=None, data_dir="unused")
    assert call["status"] == "failed" and call["result"]["continuation"] is None


def test_blank_section_and_exhausted_outline_are_failed_reads(wiki):
    body(wiki, "\n \n")
    assert generator.section(wiki, "repo", "Flow")["error"] == "wiki_section_empty"
    exhausted = agent._run_call(wiki, {"tool": "wiki_outline", "arguments": {"offset": 100}},
                                embedder=None, data_dir="unused")
    assert exhausted["status"] == "failed"


def test_outline_pages_are_complete_json_and_offsets_do_not_skip_objects(wiki):
    for index in range(3):
        wiki.execute("INSERT INTO wiki_page(id,level,title,md,status) VALUES(?, 'file', ?, '## Flow\nBody', 'ok')",
                     (f"file-{index}", "Title " * 20))
    offset, pages = 0, []
    while True:
        result = generator.outline(wiki, offset=offset, budget=100)
        assert "error" not in result
        assert len(json.dumps(result["pages"], ensure_ascii=False, separators=(",", ":")).encode()) <= 300
        pages.extend(result["pages"])
        if result["next_offset"] is None:
            break
        assert result["next_offset"] == len(pages)
        offset = result["next_offset"]
        assert offset < 5
    assert [page["page_id"] for page in pages] == ["repo", "file-0", "file-1", "file-2"]


@pytest.mark.parametrize("marker", ["```", "~~~~"])
def test_large_fenced_json_never_leaks_a_partial_document(wiki, marker):
    item = f"- Condition\n{marker}json\n" + json.dumps({"guard": "ready && " * 100 + "owned"}) + f"\n{marker}"
    body(wiki, item + "\n- Next entry.")
    result = generator.section(wiki, "repo", "Flow", budget=100)
    assert result["status"] == "oversize" and result["markdown"] == ""
    assert result["continuation"]["arguments"]["offset"] == 0


def test_two_mechanism_blocks_can_be_paged_without_separating_qualifiers(wiki):
    mechanisms = [f'<a id="mechanism-m{i}"></a>\n### Entry {i}\n\n- Claim.\n\n- Boundary only when ready.\n'
                  for i in range(2)]
    body(wiki, "".join(mechanisms))
    first = generator.section(wiki, "repo", "Flow", budget=40)
    assert first["markdown"] == "## Flow\n" + mechanisms[0]
    second = generator.section(wiki, "repo", "Flow", offset=first["next_offset"], budget=40)
    assert second["markdown"] == mechanisms[1].rstrip()
    assert second["next_offset"] is None


@pytest.mark.parametrize("kwargs", [{"offset": -1}, {"offset": True}, {"budget": 0}, {"budget": False}])
def test_invalid_pagination_is_explicit_and_cannot_complete_a_read(wiki, kwargs):
    assert generator.outline(wiki, **kwargs)["error"] == "wiki_outline_invalid_pagination"
    assert generator.section(wiki, "repo", "Flow", **kwargs)["error"] == "wiki_section_invalid_pagination"


def test_blank_line_does_not_split_an_unfinished_condition_sentence(wiki):
    condition = "It is safe\n\nonly when " + "all conditions hold and " * 20 + "the pointer is owned."
    body(wiki, condition)
    result = generator.section(wiki, "repo", "Flow", budget=20)
    assert result["status"] == "oversize" and result["markdown"] == ""
