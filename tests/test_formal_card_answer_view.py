"""Only API response presentation changes; persisted formal content is immutable."""
from pathlib import Path

from fastapi.testclient import TestClient

from codeatlas import db
from codeatlas.experience import store
import codeatlas.server.app as server


def test_api_formal_view_does_not_mutate_db_bundle_or_published_bytes(tmp_path, monkeypatch):
    connection = db.connect(tmp_path / "fixture.db")
    db.set_meta(connection, "knowledge_dir", str(tmp_path / "knowledge"))
    eid = store.add(connection, {"title": "Condition matters", "symptom": "Failed.",
                                "hypotheses": ["Flag?"], "dead_ends": ["Not a leak."],
                                "root_cause": "Only when flag is set.", "fix_steps": "Clear flag.",
                                "verification": "Test both states; not yet run.",
                                "evidence_tags": ["A1"], "evidence_turns": [1]})
    # Test fixture state only; the API under test may not approve or write it.
    connection.execute("UPDATE experience SET status='approved' WHERE id=?", (eid,))
    bundle = store.review_bundle(connection, eid)
    connection.execute("UPDATE experience SET review_bundle_hash=? WHERE id=?", (bundle, eid))
    connection.commit()
    before_db = list(connection.iterdump())
    before_files = {p: p.read_bytes() for p in (tmp_path / "knowledge").rglob("*.md")}
    monkeypatch.setattr(server, "conn", lambda: db.read_only(tmp_path / "fixture.db"))
    client = TestClient(server.app)
    for item in (client.get(f"/api/card/{eid}").json(), client.get("/api/cards").json()["items"][0]):
        assert item["current_review_bundle_hash"] == bundle
        assert item["review_bundle_hash"] == bundle
        assert item["answer_view"]["source_bundle_hash"] == bundle
        assert item["answer_view"]["status"] == "compression_infeasible"
        assert "source_session_unavailable" in item["answer_view"]["reasons"]
        assert "Only when flag is set." in item["answer_view"]["text"]
    assert list(connection.iterdump()) == before_db
    assert {p: p.read_bytes() for p in before_files} == before_files
    connection.execute("UPDATE experience SET status='pending' WHERE id=?", (eid,))
    connection.commit()
    assert "answer_view" not in client.get(f"/api/card/{eid}").json()
    connection.close()


def test_formal_view_toggle_keeps_review_controls_and_escapes_text():
    html = (Path(server.__file__).parent / "static/index.html").read_text()
    assert "x.status==='approved'?x.answer_view:null" in html
    assert 'data-card-view="compact"' in html and 'data-card-view="full" hidden' in html
    assert "esc(view.text)" in html and "function toggleCardView(select)" in html
    assert "compression_infeasible" in html and "expected_bundle_hash" in html
    assert "data-action=\"review\"" in html
