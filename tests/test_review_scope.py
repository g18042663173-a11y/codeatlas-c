from __future__ import annotations

import sqlite3

import pytest

from codeatlas import db as dbm
from codeatlas.experience import store
from codeatlas.indexer import build as indexer


def _copy(source):
    target = sqlite3.connect(":memory:")
    target.row_factory = sqlite3.Row
    source.backup(target)
    return target


def _draft(conn, scope):
    dbm.set_meta(conn, "evaluation_scope", scope)
    node = conn.execute(
        "SELECT * FROM node WHERE name='parse_value' AND is_definition=1"
    ).fetchone()
    card = store.add(
        conn,
        {
            "title": f"{scope} review case",
            "symptom": "fixture symptom",
            "root_cause": "fixture root cause",
            "fix_steps": "fixture next step",
            "verification": "fixture check",
            "evidence_tags": ["A1"],
        },
        artifact_scope=scope,
    )
    store.bind(conn, card, node["usr"], bound_by="scope-test")
    qa = store.qa_add(conn, card, "fixture QA", ["fixture point"], ["A1"])
    return card, qa["id"]


def test_agent_review_cannot_publish_formal_card(kb):
    conn = _copy(kb[0])
    card, qa = _draft(conn, "formal")
    provenance = dict(
        reviewer="judge-a", reviewer_type="agent", evaluation_scope="formal",
        model="judge-model", agent_id="judge-a", prompt_hash="prompt-v1",
        input_hash="input-v1",
    )
    store.qa_review(conn, qa, True, "agent checked", **provenance)
    with pytest.raises(ValueError, match="正式知识卡只能由 human"):
        store.review(conn, card, "approve", "agent approval", **provenance)
    assert store.get(conn, card)["status"] == "pending"


def test_every_formal_reviewer_needs_reproduction_and_current_confirmation(kb):
    conn = _copy(kb[0])
    card, qa = _draft(conn, "formal")
    store.qa_review(conn, qa, True, "human checked", reviewer="another-human")
    with pytest.raises(ValueError, match="复现实验"):
        store.review(conn, card, "approve", "try without reproduction",
                     reviewer="another-human")

    store.attach_experiments(
        conn, card, ["examples/reproductions/cjson-nesting/result.txt"]
    )
    store.qa_review(conn, qa, True, "checked reproduction too",
                    reviewer="another-human")
    with pytest.raises(ValueError, match="明确确认"):
        store.review(conn, card, "approve", "try without confirmation",
                     reviewer="another-human")

    bundle = store.review_bundle(conn, card)
    store.confirm_review(conn, card, bundle, "another-human")
    assert store.review(
        conn, card, "approve", "current bundle checked",
        reviewer="another-human", expected_bundle_hash=bundle,
    )["status"] == "approved"


def test_isolated_agent_card_never_crosses_formal_visibility(kb, tmp_path):
    conn = _copy(kb[0])
    card, qa = _draft(conn, "isolated_experiment")
    provenance = dict(
        reviewer="judge-a", reviewer_type="agent",
        evaluation_scope="isolated_experiment", model="judge-model",
        agent_id="judge-a", prompt_hash="prompt-v1", input_hash="input-v1",
    )
    store.qa_review(conn, qa, True, "agent checked", **provenance)
    store.review(conn, card, "approve", "experiment approval", **provenance)
    indexer.build(conn, dbm.get_meta(conn, "repo"), embedder_kind="null",
                  out_dir=str(tmp_path / "isolated"))
    assert conn.execute("SELECT visible FROM chunk WHERE uid=?", (f"exp:{card}",)).fetchone()[0] == 1

    dbm.set_meta(conn, "evaluation_scope", "formal")
    indexer.refresh_visibility(conn)
    assert conn.execute("SELECT visible FROM chunk WHERE uid=?", (f"exp:{card}",)).fetchone()[0] == 0
    assert store.approved_current(conn, card) == (False, "artifact_scope_not_visible")
