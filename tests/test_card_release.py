from __future__ import annotations

import json
from pathlib import Path

from codeatlas import db
from codeatlas.experience import release, store


ROOT = Path(__file__).resolve().parents[1]
CARD = ROOT / "knowledge/cards/e-0f1743530a0e.md"


def test_runtime_publication_identity_does_not_change_review_bundle(tmp_path):
    source_payload, source_text = store._parse_markdown(CARD)
    rendered = release._runtime_markdown(source_text, "pub-release-local")
    target = tmp_path / "card.md"
    target.write_text(rendered, encoding="utf-8")
    installed, _ = store._parse_markdown(target)
    assert installed["record"]["publication_op_id"] == "pub-release-local"
    assert (installed["record"]["review_bundle_hash"]
            == source_payload["record"]["review_bundle_hash"])


def test_latest_equivalent_local_publication_wins_over_old_stale(tmp_path):
    conn = db.connect(":memory:")
    target = tmp_path / "cards" / "e-0f1743530a0e.md"
    _payload, source = store._parse_markdown(CARD)
    approved = release._runtime_markdown(source, "pub-release-current")
    stale_payload = json.loads(source.split("---", 2)[1])
    stale_payload.pop("checksum")
    stale_payload["record"]["status"] = "stale"
    stale_payload["record"]["publication_op_id"] = "pub-stale-old"
    stale_payload["checksum"] = store._hash(stale_payload)
    stale = "---\n" + json.dumps(stale_payload, ensure_ascii=False, indent=2) \
        + "\n---" + source.split("---", 2)[2]
    for op, created, text in (
        ("pub-stale-old", "2026-09-06T00:00:00+00:00", stale),
        ("pub-release-current", "2026-09-07T00:00:00+00:00", approved),
    ):
        files = json.dumps([{"path": str(target), "before": None, "after": text}])
        conn.execute(
            "INSERT INTO publication_journal VALUES(?, 'committed', ?, ?, ?)",
            (op, files, created, created),
        )
    conn.commit()
    found = release._installed_publication(
        conn, target, card_id="e-0f1743530a0e",
        review_bundle_hash="6f624a45bf16d7a76876b8c33acaa8fa4c6fc269435e1dd7ac550a30aa95d56c",
    )
    assert found == ("pub-release-current", approved)
