"""C++ TUs reuse the C fact/refuse path. Virtual dispatch stays static."""
from __future__ import annotations

from pathlib import Path

import pytest

from codeatlas import agent, db as dbm
from codeatlas.graph.structure import file_card, repo_outline
from codeatlas.graph.traverse import impact, resolve_symbol
from codeatlas.indexer import build as indexer
from codeatlas.parser import ast_walker, compile_db
from codeatlas.parser.languages import is_readable_source, scan_language_args
from codeatlas.retrieve import engine
from codeatlas.summary import head

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "mini_cpp"


@pytest.fixture(scope="module")
def cxx_kb(tmp_path_factory):
    d = tmp_path_factory.mktemp("cxx-kb")
    conn = dbm.connect(d / "kb.db")
    units, degraded = compile_db.load(str(CORPUS), None, [str(CORPUS)], [])
    assert degraded
    walker = ast_walker.AstWalker(str(CORPUS))
    errors = 0
    for unit in units:
        diag = walker.walk_unit(unit.source, unit.args, unit.directory)
        assert diag >= 0
        errors += max(diag, 0)
    ast_walker.persist(conn, walker.result)
    dbm.set_meta(conn, "repo", str(CORPUS))
    dbm.set_meta(conn, "repository_id", "tests/fixtures/mini_cpp")
    dbm.set_meta(conn, "revision", "fixture")
    conn.commit()
    head.build(conn, str(CORPUS))
    indexer.build(conn, str(CORPUS), out_dir=str(d))
    yield conn, walker.result, units, errors, str(d)
    conn.close()


def test_scan_uses_cxx_standard_only_for_cpp(tmp_path):
    (tmp_path / "a.cpp").write_text("int f() { return 1; }\n", encoding="utf-8")
    (tmp_path / "b.c").write_text("int g(void) { return 1; }\n", encoding="utf-8")
    units, _ = compile_db.load(str(tmp_path), None, [str(tmp_path)], [])
    cpp = next(unit for unit in units if unit.source.endswith("a.cpp"))
    c_unit = next(unit for unit in units if unit.source.endswith("b.c"))
    assert scan_language_args("a.cpp") == ["-x", "c++", "-std=c++17"]
    assert "-std=c++17" in cpp.args and "-x" in cpp.args
    assert "-std=c11" in c_unit.args
    assert "-std=c++17" not in c_unit.args


def test_mini_cpp_outline_include_and_class(cxx_kb):
    conn, _result, units, errors, _data = cxx_kb
    assert {Path(unit.source).name for unit in units} == {"widget.cpp", "driver.cpp"}
    assert errors == 0
    outline = repo_outline(conn)
    includes = {(edge["src"], edge["dst"]) for edge in outline["includes"]}
    assert ("widget.cpp", "widget.hpp") in includes
    assert ("driver.cpp", "widget.hpp") in includes
    card = file_card(conn, "widget.cpp")
    names = {item["name"] for item in card["symbols"]}
    assert "helper" in names
    assert "Widget::reset" in names
    assert any(item["kind"] == "class" and item["name"] == "Widget"
               for item in file_card(conn, "widget.hpp")["symbols"])
    assert {"src": "Counter", "dst": "Widget"} in file_card(conn, "widget.hpp")["inherits"]


def _callees(conn, caller: str) -> set[str]:
    rows = resolve_symbol(conn, caller, kinds=("function",))
    assert rows, caller
    caller_id = next(row["id"] for row in rows if row["is_definition"])
    return {
        row["name"]
        for row in conn.execute(
            """SELECT n.name FROM edge e JOIN node n ON n.id=e.dst
                WHERE e.src=? AND e.kind='calls' AND e.confidence='certain'""",
            (caller_id,),
        )
    }


def test_mini_cpp_calls_are_statically_bound(cxx_kb):
    conn, result, _units, _errors, _data = cxx_kb
    use = _callees(conn, "use_widget")
    assert "helper" in use
    assert "Widget::reset" in use
    assert "via_base" in use
    assert "Widget::apply" in use
    via = _callees(conn, "via_base")
    assert "Widget::size" in via
    assert "Counter::size" not in via
    counter = _callees(conn, "Counter::size")
    assert "Widget::size" in counter
    reasons = {
        row["reason"]
        for row in conn.execute(
            """SELECT e.reason FROM edge e JOIN node n ON n.id=e.src
                WHERE e.kind='calls' AND e.confidence='candidate' AND n.name='Widget::apply'"""
        )
    }
    assert "fn_pointer" in reasons
    assert all(
        edge["confidence"] in {"certain", "candidate"}
        for edge in result.edges.values()
    )


def test_short_name_impact_and_readable_code_read(cxx_kb):
    conn, _result, _units, _errors, _data = cxx_kb
    rows = resolve_symbol(conn, "reset")
    assert any(row["name"] == "Widget::reset" for row in rows)
    result = impact(conn, next(row["id"] for row in rows if row["name"] == "Widget::reset"))
    callers = {item["name"] for hop in result["by_hop"].values() for item in hop}
    assert "use_widget" in callers
    payload = agent._code_read(conn, {"path": "widget.cpp", "line_start": 7, "line_end": 10})
    assert "reset" in payload["text"]
    assert is_readable_source("widget.hpp")
    with pytest.raises(agent.AgentError):
        agent._code_read(conn, {"path": "widget.cpp.bak", "line_start": 1, "line_end": 2})


def test_ask_hits_qualified_method_and_file_route(cxx_kb):
    conn, _result, _units, _errors, data_dir = cxx_kb
    assert engine.route("Widget::reset") == "symbol"
    assert engine.route("driver.cpp 里谁调用 helper") == "symbol"
    asked = engine.ask(conn, "Widget::reset", data_dir=data_dir)
    assert asked.get("refused") is not True
    titles = " ".join(item.get("title") or "" for item in asked.get("citations") or [])
    assert "Widget::reset" in titles or "reset" in titles
