"""求职演示必须隔离源库，并实际证明审核和拒答边界。"""
from __future__ import annotations

from pathlib import Path

import pytest

from codeatlas import portfolio_demo


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "experience" / "public-cjson-review-example.jsonl"


def _db_path(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2]


def test_portfolio_demo_isolated_and_proves_review_gate(kb, tmp_path):
    conn, _ = kb
    source = _db_path(conn)
    source_experience_count = conn.execute("SELECT COUNT(*) FROM experience").fetchone()[0]

    result = portfolio_demo.run(
        source, work_dir=str(tmp_path / "portfolio"), experience_file=str(SAMPLE),
        embedder_kind="null", require_cjson_snapshot=False,
    )

    assert result["demo_db"] != source
    assert Path(result["demo_db"]).is_file()
    assert conn.execute("SELECT COUNT(*) FROM experience").fetchone()[0] == source_experience_count
    p = result["evidence"]["provenance"]
    assert p["symbol"] == "cJSON_ParseWithOpts"
    assert p["usr"] and p["path"] and p["line_start"]
    assert result["experience"]["pending_visible"] is False
    assert result["experience"]["approved_visible"] is True
    assert result["experience"]["citation"]["level"] == "B"
    assert result["refusal"]["refused"] is True
    assert result["refusal"]["ab_evidence"] == 0
    assert result["llm_mode"] == "disabled"
    assert "模拟人工审核" in portfolio_demo.render_markdown(result)


def test_portfolio_demo_llm_error_falls_back_to_evidence_pack(kb, tmp_path):
    conn, _ = kb

    class BrokenLLM:
        def answer(self, *_args, **_kwargs):
            raise TimeoutError("demo timeout")

    result = portfolio_demo.run(
        _db_path(conn), work_dir=str(tmp_path / "portfolio"), experience_file=str(SAMPLE),
        embedder_kind="null", llm_client=BrokenLLM(), require_cjson_snapshot=False,
    )
    assert result["llm_mode"] == "fallback_to_evidence_pack"
    assert result["locate"]["answer"] is None
    assert result["locate"]["evidence_pack"]


def test_portfolio_demo_rejects_non_cjson_snapshot_when_requested(kb, tmp_path):
    conn, _ = kb
    with pytest.raises(portfolio_demo.PortfolioDemoError, match="固定 cJSON 快照"):
        portfolio_demo.run(
            _db_path(conn), work_dir=str(tmp_path / "portfolio"), experience_file=str(SAMPLE),
            embedder_kind="null", require_cjson_snapshot=True,
        )
