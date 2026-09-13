"""File/module map reads existing certain edges; candidate stays aside."""
from __future__ import annotations

from codeatlas.graph.modules import module_of
from codeatlas.graph.structure import file_card, repo_outline
from codeatlas.graph.traverse import impact, resolve_symbol
from codeatlas.wiki.generator import _module_of


def test_module_of_matches_pinned_corpora():
    assert module_of("cJSON.c", "corpus/cJSON") == "root"
    assert module_of("tests/parse_test.c", "DaveGamble/cJSON") == "tests"
    assert module_of("src/core/ipv4/ip4.c", "lwip-tcpip/lwip") == "core/ipv4"
    assert module_of("src/core/init.c", "lwip") == "core"
    assert module_of("src/apps/http/httpd.c", "corpus/lwip") == "apps/http"
    assert module_of("src/include/lwip/opt.h", "lwip") == "include"
    assert module_of("parser/ast_walker.py", "codeatlas") == "parser"
    assert _module_of("src/apps/http/httpd.c", "lwip") == "apps/http"


def test_mini_c_outline_and_file_card(kb):
    conn, _ = kb
    outline = repo_outline(conn)
    names = {module["name"] for module in outline["modules"]}
    assert "root" in names
    includes = {(edge["src"], edge["dst"]) for edge in outline["includes"]}
    assert ("mini.c", "mini.h") in includes
    assert ("driver.c", "mini.h") in includes
    assert all(
        edge["src"] != edge["dst"] for edge in outline["cross_file_calls"]
    )

    card = file_card(conn, "mini.c")
    assert card["path"] == "mini.c"
    assert card["module"] == "root"
    assert "mini.h" in card["includes_out"]
    assert any(item["name"] == "cJSON_Delete" for item in card["symbols"])
    assert any(item["src"] == "driver.c" for item in card["calls_in"])
    by_basename = file_card(conn, "mini.c")
    assert by_basename["path"] == card["path"]
    try:
        file_card(conn, "not-a-real-file.c")
    except KeyError as exc:
        assert "未找到文件" in str(exc)
    else:
        raise AssertionError("missing file must raise")


def test_impact_modules_use_shared_map(kb):
    conn, _ = kb
    from codeatlas.graph.modules import repository_label

    rows = resolve_symbol(conn, "cJSON_Delete")
    assert rows
    result = impact(conn, rows[0]["id"], max_hop=2)
    repo = repository_label(conn)
    for path in result["affected_files"]:
        assert module_of(path, repo) in result["affected_modules"]
    assert result["affected_modules"]
    assert "." not in result["affected_modules"]
