"""Durable multi-file prepare/commit journal; prepared bytes are never authority."""
from __future__ import annotations
import json
import os
import uuid
from pathlib import Path
from datetime import datetime, timezone


def atomic_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".prepared-" + uuid.uuid4().hex)
    try:
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def prepare(conn, files: list[tuple[Path, str]], op_id: str | None = None) -> str:
    op_id = op_id or "pub-" + uuid.uuid4().hex
    existing = conn.execute("SELECT files_json,state FROM publication_journal WHERE op_id=?", (op_id,)).fetchone()
    if existing:
        expected = [(str(path.resolve()), text) for path, text in files]
        actual = [(entry["path"], entry["after"]) for entry in json.loads(existing[0])]
        if actual != expected or existing[1] == "rolled_back":
            raise ValueError("publication operation conflicts with prior payload/state")
        return op_id
    entries = [{"path": str(p.resolve()), "before": p.read_text() if p.exists() else None,
                "after": text} for p, text in files]
    conn.execute("INSERT INTO publication_journal VALUES(?,?,?, ?,NULL)",
                 (op_id, "prepared", json.dumps(entries, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
    conn.commit()  # WAL durable before touching any authoritative file.
    for item in entries:
        atomic_text(Path(item["path"]), item["after"])
    return op_id


def commit(conn, op_id):
    from . import db
    updated = conn.execute("UPDATE publication_journal SET state='committed',committed_at=? WHERE op_id=? AND state='prepared'",
                 (datetime.now(timezone.utc).isoformat(), op_id))
    if not updated.rowcount:
        return
    db.set_meta(conn, "governance_revision", str(int(db.get_meta(conn, "governance_revision", "0")) + 1))
    conn.commit()  # Same transaction as card projection writes.


def recover(conn):
    repaired = []
    for row in conn.execute("SELECT * FROM publication_journal ORDER BY created_at,op_id").fetchall():
        if row["state"] == "rolled_back":
            continue
        for entry in json.loads(row["files_json"]):
            path = Path(entry["path"])
            desired = entry["after"] if row["state"] == "committed" else entry["before"]
            if desired is None:
                path.unlink(missing_ok=True)
            elif not path.exists() or path.read_text() != desired:
                atomic_text(path, desired)
        if row["state"] == "prepared":
            conn.execute("UPDATE publication_journal SET state='rolled_back' WHERE op_id=?", (row["op_id"],))
            repaired.append(row["op_id"])
    conn.commit()
    return repaired
