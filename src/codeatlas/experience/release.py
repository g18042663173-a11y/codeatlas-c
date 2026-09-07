"""Portable, hash-bound releases for human-approved public knowledge cards."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import db as dbm
from ..contracts import digest
from ..publication import atomic_text
from . import store


class CardReleaseError(ValueError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _inside(root: Path, value: str | Path) -> Path:
    root = root.resolve()
    path = (root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    if path != root and root not in path.parents:
        raise CardReleaseError(f"path escapes project root: {value}")
    return path


def _rows(conn: sqlite3.Connection, query: str, values=()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, values).fetchall()]


def _verify_experiments(record: dict[str, Any], project_root: Path) -> list[dict[str, str]]:
    manifest = json.loads(record.get("experiment_manifest") or "[]")
    if not manifest:
        raise CardReleaseError("formal release requires reproducible experiment artifacts")
    normalized = []
    for entry in manifest:
        path = _inside(project_root, entry["path"])
        if not path.is_file() or _sha256_bytes(path.read_bytes()) != entry["sha256"]:
            raise CardReleaseError(f"experiment artifact hash mismatch: {entry['path']}")
        normalized.append({"path": path.relative_to(project_root.resolve()).as_posix(),
                           "sha256": entry["sha256"]})
    return normalized


def _validate_human_gate(payload: dict[str, Any], confirmations: list[dict[str, Any]]) -> None:
    record = payload["record"]
    bundle = record.get("review_bundle_hash")
    if (record.get("status") != "approved" or record.get("artifact_scope") != "formal"
            or not bundle):
        raise CardReleaseError("only approved formal cards with a review bundle may be released")
    qas = payload.get("qas") or []
    if not any(qa.get("status") == "passed" and qa.get("bundle_hash") == bundle
               and qa.get("reviewer_type") == "human"
               and qa.get("evaluation_scope") == "formal" for qa in qas):
        raise CardReleaseError("current human-reviewed QA is missing")
    reviews = payload.get("reviews") or []
    if not any(review.get("action") == "approve" and review.get("reviewer_type") == "human"
               and review.get("evaluation_scope") == "formal" for review in reviews):
        raise CardReleaseError("formal human approval is missing")
    if not any(row.get("bundle_hash") == bundle and row.get("reviewer_type") == "human"
               for row in confirmations):
        raise CardReleaseError("current human confirmation is missing")


def export_release(conn: sqlite3.Connection, exp_id: str, *, project_root: str | Path,
                   release_dir: str | Path = "knowledge") -> dict[str, Any]:
    """Export one already-published card without changing its reviewed content."""
    root = Path(project_root).resolve()
    row = conn.execute("SELECT * FROM experience WHERE id=?", (exp_id,)).fetchone()
    if row is None:
        raise CardReleaseError(f"knowledge card not found: {exp_id}")
    valid, reason = store.approved_current(conn, exp_id)
    if not valid:
        raise CardReleaseError(f"card is not currently approved: {reason}")
    canonical = Path(row["canonical_path"] or "")
    if not canonical.is_file():
        raise CardReleaseError("canonical Markdown is missing")
    payload, markdown = store._parse_markdown(canonical)
    if payload["record"].get("review_bundle_hash") != row["review_bundle_hash"]:
        raise CardReleaseError("database and Markdown review bundle differ")
    confirmations = _rows(
        conn, "SELECT exp_id,bundle_hash,reviewer,reviewer_type,confirmed_at "
              "FROM review_confirmation WHERE exp_id=? ORDER BY confirmed_at", (exp_id,))
    _validate_human_gate(payload, confirmations)
    journal = conn.execute("SELECT * FROM publication_journal WHERE op_id=?",
                           (row["publication_op_id"],)).fetchone()
    if journal is None or journal["state"] != "committed" or not journal["committed_at"]:
        raise CardReleaseError("committed publication journal is missing")
    if not any(entry.get("after") == markdown for entry in json.loads(journal["files_json"])):
        raise CardReleaseError("canonical Markdown differs from publication journal")
    experiments = _verify_experiments(payload["record"], root)
    release_root = _inside(root, release_dir)
    card_path = release_root / "cards" / f"{exp_id}.md"
    package_path = release_root / "releases" / f"{exp_id}.json"
    atomic_text(card_path, markdown)
    package = {
        "schema_version": 1,
        "card_id": exp_id,
        "review_bundle_hash": row["review_bundle_hash"],
        "markdown_path": card_path.relative_to(root).as_posix(),
        "markdown_sha256": _sha256_bytes(markdown.encode()),
        "repository": payload.get("anchors", [{}])[0].get("repository"),
        "revision": payload.get("anchors", [{}])[0].get("revision"),
        "source_publication": {"op_id": journal["op_id"],
                               "committed_at": journal["committed_at"]},
        "confirmations": confirmations,
        "experiments": experiments,
    }
    package["checksum"] = digest(package)
    rendered = json.dumps(package, ensure_ascii=False, indent=2) + "\n"
    atomic_text(package_path, rendered)
    return {"card_id": exp_id, "release": str(package_path), "markdown": str(card_path),
            "release_sha256": _sha256_bytes(rendered.encode()),
            "review_bundle_hash": row["review_bundle_hash"]}


def load_release(path: str | Path, *, project_root: str | Path) -> tuple[dict[str, Any], str]:
    root = Path(project_root).resolve()
    package_path = _inside(root, path)
    package = json.loads(package_path.read_text(encoding="utf-8"))
    checksum = package.pop("checksum", None)
    if package.get("schema_version") != 1 or checksum != digest(package):
        raise CardReleaseError("release package checksum differs")
    package["checksum"] = checksum
    markdown_path = _inside(root, package["markdown_path"])
    markdown = markdown_path.read_text(encoding="utf-8")
    if _sha256_bytes(markdown.encode()) != package["markdown_sha256"]:
        raise CardReleaseError("released Markdown hash differs")
    payload, _ = store._parse_markdown(markdown_path)
    if (payload["record"].get("id") != package["card_id"]
            or payload["record"].get("review_bundle_hash") != package["review_bundle_hash"]):
        raise CardReleaseError("released Markdown identity differs")
    _validate_human_gate(payload, package.get("confirmations") or [])
    if _verify_experiments(payload["record"], root) != package["experiments"]:
        raise CardReleaseError("release experiment manifest differs")
    return package, markdown


def _runtime_markdown(markdown: str, publication_op_id: str) -> str:
    """Bind an immutable release to the local publication journal.

    ``publication_op_id`` is deliberately outside ``review_bundle_hash``: it is
    an installation fact, not reviewed knowledge.  A clean checkout therefore
    gets a new, local journal identity without invalidating the human review.
    """
    parts = markdown.split("---", 2)
    if len(parts) != 3 or parts[0].strip():
        raise CardReleaseError("released Markdown has invalid frontmatter")
    payload = json.loads(parts[1])
    payload.pop("checksum", None)
    payload["record"]["publication_op_id"] = publication_op_id
    payload["checksum"] = store._hash(payload)
    return "---\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n---" + parts[2]


def _installed_publication(conn: sqlite3.Connection, target: Path, *, card_id: str,
                           review_bundle_hash: str) -> tuple[str, str] | None:
    """Return the latest equivalent local publication, if already installed."""
    for row in conn.execute(
        "SELECT op_id,files_json FROM publication_journal "
        "WHERE state='committed' ORDER BY created_at DESC,op_id DESC"
    ).fetchall():
        for entry in json.loads(row["files_json"]):
            if Path(entry["path"]).resolve() != target.resolve() or not entry.get("after"):
                continue
            try:
                parts = entry["after"].split("---", 2)
                payload = json.loads(parts[1])
                checksum = payload.pop("checksum", None)
                valid = checksum == store._hash(payload)
                record = payload.get("record") or {}
            except (IndexError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if (valid and record.get("id") == card_id
                    and record.get("status") == "approved"
                    and record.get("review_bundle_hash") == review_bundle_hash
                    and record.get("publication_op_id") == row["op_id"]):
                return row["op_id"], entry["after"]
            # This is the newest publication for the target.  Older entries
            # cannot be authoritative after journal recovery.
            break
        else:
            continue
        break
    return None


def import_release(conn: sqlite3.Connection, path: str | Path, *,
                   project_root: str | Path, rebuild_index: bool = False) -> dict[str, Any]:
    """Project a tracked formal release into an already-built corpus database."""
    root = Path(project_root).resolve()
    package, markdown = load_release(path, project_root=root)
    payload, _ = store._parse_markdown(_inside(root, package["markdown_path"]))
    anchor = (payload.get("anchors") or [None])[0]
    if not anchor:
        raise CardReleaseError("formal release has no primary anchor")
    repository = dbm.get_meta(conn, "repository_id", dbm.get_meta(conn, "repo", ""))
    revision = dbm.get_meta(conn, "revision", "")
    if package["repository"] != repository or package["revision"] != revision:
        raise CardReleaseError("release repository/revision differs from database")
    node = conn.execute(
        "SELECT definition_hash FROM node WHERE usr=? AND kind='function' AND is_definition=1",
        (anchor["usr"],),
    ).fetchone()
    if node is None or node["definition_hash"] != anchor["definition_hash"]:
        raise CardReleaseError("release primary anchor is not current")
    knowledge_root = store._knowledge_root(conn)
    target = knowledge_root / "cards" / f"{package['card_id']}.md"
    # ``store.rebuild`` is normally a governed mutation that republishes the
    # current snapshot immediately.  A release import must restore its human
    # confirmations before that publication, otherwise the intermediate card
    # is conservatively marked stale.  Execute the underlying rebuild under the
    # same writer lock and publish governance exactly once after all checks pass.
    from .. import publication, snapshots
    with snapshots.writer_lock(conn):
        publication.recover(conn)
        installed = _installed_publication(
            conn, target, card_id=package["card_id"],
            review_bundle_hash=package["review_bundle_hash"],
        )
        if installed:
            op_id, runtime_markdown = installed
            atomic_text(target, runtime_markdown)
        else:
            op_id = "pub-release-" + digest({
                "release": package["checksum"],
                "target": str(target.resolve()),
                "installed_at": datetime.now(timezone.utc).isoformat(),
            })[:24]
            runtime_markdown = _runtime_markdown(markdown, op_id)
            publication.prepare(conn, [(target, runtime_markdown)], op_id)
            publication.commit(conn, op_id)
        rebuild_impl = getattr(store.rebuild, "__wrapped__", store.rebuild)
        rebuilt = rebuild_impl(conn, rebuild_index=False)
        for confirmation in package["confirmations"]:
            conn.execute(
                "INSERT OR REPLACE INTO review_confirmation"
                "(exp_id,bundle_hash,reviewer,reviewer_type,confirmed_at) VALUES(?,?,?,?,?)",
                (confirmation["exp_id"], confirmation["bundle_hash"], confirmation["reviewer"],
                 confirmation["reviewer_type"], confirmation["confirmed_at"]),
            )
        revision = int(dbm.get_meta(conn, "governance_revision", "0")) + 1
        dbm.set_meta(conn, "governance_revision", str(revision))
        conn.commit()
    valid, reason = store.approved_current(conn, package["card_id"])
    if not valid:
        raise CardReleaseError(f"rebuilt release is not current: {reason}")
    if rebuild_index:
        if dbm.get_meta(conn, "snapshot_enabled") == "1":
            raise CardReleaseError(
                "snapshot databases must rebuild and activate a new knowledge set; "
                "the control database cannot publish an in-place index"
            )
        from ..indexer import build as index_build
        index_build.build(conn, dbm.get_meta(conn, "repo", "."),
                          embedder_kind="null", out_dir=str(knowledge_root.parent))
    return {"card_id": package["card_id"], "approved_current": True,
            "snapshot_rebuild_required": dbm.get_meta(conn, "snapshot_enabled") == "1",
            "review_bundle_hash": package["review_bundle_hash"], **rebuilt}
