"""Local immutable knowledge sets. The control DB is the atomic active pointer."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import shlex
import hashlib
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .contracts import digest

_locks = {}
_held = threading.local()
GOV_TABLES = ("experience", "experience_anchor", "experience_link", "experience_review",
              "experience_qa", "knowledge_dependency", "review_confirmation")
FACT_TABLES = ("node", "edge", "edge_source", "branch_fact", "semantic_fact", "entity_fingerprint",
               "summary_head", "wiki_page")


class SnapshotError(ValueError):
    pass


def database_path(conn) -> Path:
    value = conn.execute("PRAGMA database_list").fetchone()[2]
    if not value:
        raise SnapshotError("persistent control database required")
    return Path(value).resolve()


@contextmanager
def writer_lock(conn):
    key = str(database_path(conn))
    lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_held, "keys", set())
        if key in held:
            yield
            return
        path = Path(key + ".writer.lock")
        with path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            _held.keys = held | {key}
            try:
                yield
            finally:
                _held.keys = held
                fcntl.flock(handle, fcntl.LOCK_UN)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _git(root, *args):
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args], capture_output=True, text=True, timeout=15)
    return result.stdout.strip() if result.returncode == 0 else ""


def _copy_tables(source, target, tables):
    for table in tables:
        target.execute(f"DELETE FROM {table}")
        rows = source.execute(f"SELECT * FROM {table}").fetchall()
        if rows:
            cols = rows[0].keys()
            target.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                               [tuple(r) for r in rows])


def _project(control, facts):
    from . import dependencies
    from .experience import store
    _copy_tables(control, facts, GOV_TABLES)
    for card in facts.execute("SELECT id FROM experience WHERE status='approved'").fetchall():
        valid, reason = store.approved_current(facts, card[0])
        if not valid:
            facts.execute("UPDATE experience SET status='stale',stale_reason=? WHERE id=?", (reason, card[0]))
            facts.execute("INSERT INTO invalidation_event(knowledge_set_id,target,trigger_id,rule,dependency_path,created_at) VALUES(?,?,?,?,?,?)",
                          (db.get_meta(facts, "knowledge_set_id"), card[0], reason, "conservative_dependency_revalidation", json.dumps([reason, card[0]]), _now()))
    facts.commit()
    from .dependencies import record_wiki
    record_wiki(facts)


def _seal(directory: Path, conn, manifest: dict):
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    objects = directory.parent / "objects"
    reused, total = 0, 0
    for folder in (directory / "source", directory / "wiki", directory / "external"):
        if not folder.exists():
            continue
        for path in folder.rglob("*"):
            if not path.is_file() or path.is_symlink() or path.name in ("compile_commands.json", "vfs.json"):
                continue
            content_hash = digest(path.read_bytes())
            obj = objects / content_hash[:2] / content_hash
            obj.parent.mkdir(parents=True, exist_ok=True)
            total += 1
            if obj.exists():
                if digest(obj.read_bytes()) != content_hash:
                    raise SnapshotError("content object corrupted")
                reused += 1
            else:
                shutil.copyfile(path, obj)
                obj.chmod(0o444)
            path.unlink()
            os.link(obj, path)
    manifest["content_objects"] = {"count": total, "reused": reused}
    manifest["artifacts"] = {str(p.relative_to(directory)): digest(p.read_bytes())
                             for p in sorted(directory.rglob("*")) if p.is_file()
                             and p.name != "manifest.json" and not p.name.endswith(("-wal", "-shm", ".lock"))}
    # Manifest publication itself is fsync + rename.
    from .publication import atomic_text
    atomic_text(directory / "manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))


def verify(directory: Path) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text())
    for rel, expected in manifest["artifacts"].items():
        path = (directory / rel).resolve()
        if directory.resolve() not in path.parents or not path.is_file() or digest(path.read_bytes()) != expected:
            raise SnapshotError(f"artifact hash mismatch: {rel}")
    with readonly(directory / "facts.db") as facts:
        if facts.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SnapshotError("SQLite integrity failure")
        if facts.execute("SELECT COUNT(*) FROM chunk WHERE visible=1 AND kind='session'").fetchone()[0]:
            raise SnapshotError("raw conversation indexed")
        from .wiki.generator import integrity
        if not integrity(facts)["ok"]:
            raise SnapshotError("Wiki incomplete")
        import numpy as np
        ids = json.loads((directory / "vec_ids.json").read_text())
        vectors = np.load(directory / "vectors.npy", mmap_mode="r", allow_pickle=False)
        expected = [r[0] for r in facts.execute("SELECT rowid FROM chunk WHERE visible=1 ORDER BY rowid")]
        if ids != expected or vectors.shape[0] != len(ids):
            raise SnapshotError("vector mapping does not match SQLite snapshot")
    return manifest


def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


@dataclass
class QueryContext:
    conn: sqlite3.Connection
    knowledge_set_id: str
    directory: Path
    manifest: dict

    @property
    def provenance(self):
        return {key: self.manifest.get(key) for key in
                ("knowledge_set_id", "source_content_hash", "build_config_hash", "revision", "implementation_hash")}


@contextmanager
def pin(control):
    active = db.get_meta(control, "active_knowledge_set")
    if not active:
        raise SnapshotError("not_ready: build and activate a knowledge snapshot first")
    row = control.execute("SELECT directory,manifest_json FROM knowledge_set WHERE id=?", (active,)).fetchone()
    directory = Path(row[0])
    # Active artifacts are verified before switching; never run migrations on a read path.
    facts = readonly(directory / "facts.db")
    try:
        yield QueryContext(facts, active, directory, json.loads(row[1]))
    finally:
        facts.close()


def status(control):
    active = db.get_meta(control, "active_knowledge_set")
    details, events = {}, []
    if active:
        row = control.execute("SELECT directory,manifest_json FROM knowledge_set WHERE id=?", (active,)).fetchone()
        details = json.loads(row[1])
        facts = readonly(Path(row[0]) / "facts.db")
        try:
            events = [dict(r) for r in facts.execute("SELECT * FROM invalidation_event ORDER BY id DESC LIMIT 12")]
        finally:
            facts.close()
    return {"active": active, "invalidation": details.get("invalidation", {}), "recent_changes": events,
            "source_content_hash": details.get("source_content_hash"), "build_config_hash": details.get("build_config_hash"),
            "governance_revision": int(db.get_meta(control, "governance_revision", "0")),
            "state": "ready" if db.get_meta(control, "active_knowledge_set") else "not_ready",
            "sets": [dict(r) for r in control.execute(
                "SELECT id,state,base_active,governance_revision,created_at,error FROM knowledge_set ORDER BY created_at DESC")]}


def _freeze_build_inputs(source, frozen, compile_path, directory, strict):
    from .parser import compile_db as cdb
    from clang import cindex
    units, degraded = cdb.load(str(source), str(compile_path), [], [])
    if degraded or not units:
        raise SnapshotError("formal compile database required")
    external = {}
    commands = []
    for unit in units:
        src = Path(unit.source).resolve()
        if source not in src.parents:
            raise SnapshotError("compile database source outside repository")
        args = []
        for arg in unit.args:
            if arg.startswith("@"):
                raise SnapshotError("response files require explicit expansion before snapshot build")
            # Relocate path tokens, not arbitrary substrings in macro/string values.
            if arg == str(source) or arg.startswith(str(source) + os.sep):
                arg = str(frozen) + arg[len(str(source)):]
            elif arg.startswith("-I" + str(source)):
                arg = "-I" + str(frozen) + arg[len("-I" + str(source)):]
            args.append(arg)
        old_dir = Path(unit.directory).resolve()
        if old_dir != source and source not in old_dir.parents:
            raise SnapshotError("compile working directory outside repository; normalize the database first")
        work = frozen / old_dir.relative_to(source)
        target = frozen / src.relative_to(source)
        # Discovery pass only. Final accepted parse uses the frozen external VFS.
        cwd = os.getcwd()
        try:
            os.chdir(work)
            tu = cindex.Index.create().parse(str(target), args=args)
        finally:
            os.chdir(cwd)
        if any(d.severity >= 3 for d in tu.diagnostics):
            raise SnapshotError("severe compiler diagnostics during dependency discovery")
        for inclusion in tu.get_includes():
            path = Path(inclusion.include.name).resolve()
            if frozen == path or frozen in path.parents:
                if strict:
                    rel = str(path.relative_to(frozen))
                    expected = _git(source, "rev-parse", "HEAD:" + rel)
                    raw = path.read_bytes()
                    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
                    if not expected or expected != blob:
                        raise SnapshotError("untracked or modified consumed header: " + rel)
                continue
            # Only consumed bytes are copied; SDK roots are never recursively copied.
            raw = path.read_bytes()
            external[str(path)] = digest(raw)
            destination = directory / "external" / digest(raw)
            destination.parent.mkdir(exist_ok=True)
            if not destination.exists():
                destination.write_bytes(raw)
        if strict:
            rel = str(src.relative_to(source))
            raw = target.read_bytes()
            expected = _git(source, "rev-parse", "HEAD:" + rel)
            blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            if not expected or expected != blob:
                raise SnapshotError("untracked or modified translation unit: " + rel)
        commands.append({"directory": str(work), "file": str(target), "arguments": ["clang", *args, "-c", str(target)]})
    for path, expected in external.items():
        if digest(Path(path).read_bytes()) != expected:
            raise SnapshotError("external dependency changed during freeze")
    from .publication import atomic_text
    overlay = directory / "external" / "vfs.json"
    atomic_text(overlay, json.dumps({"version": 0, "roots": [
        {"type": "file", "name": path, "external-contents": str(directory / "external" / value)}
        for path, value in sorted(external.items())]}))
    canonical = json.dumps(commands, sort_keys=True).replace(str(frozen), "${REPO}")
    config = digest({"commands": canonical, "external": external})
    for command in commands:
        command["arguments"] += ["-ivfsoverlay", str(overlay)]
    atomic_text(frozen / "compile_commands.json", json.dumps(commands))
    return external, config


def build(control, repo: str, compile_db: str, *, activate_after=False, strict=True) -> dict:
    from .cli import parse, _repository_id
    from .summary import head
    from .wiki import generator
    from .indexer import build as indexer
    source = Path(repo).resolve()
    compile_path = Path(compile_db).resolve()
    if not compile_path.is_file():
        raise SnapshotError("formal compile database required")
    revision = _git(source, "rev-parse", "HEAD") or db.get_meta(control, "revision", "unversioned")
    # Do not trust Git's index stat cache (or an IDE-held index lock). Each
    # consumed input is compared directly to HEAD's blob in _freeze_build_inputs.
    dirty = not strict
    if strict and (dirty or revision == "unversioned"):
        raise SnapshotError("formal corpus must be fixed and clean")
    identifier = "ks-" + uuid.uuid4().hex[:16]
    root = database_path(control).with_suffix("").with_name(database_path(control).stem + "-sets")
    directory = root / identifier
    with writer_lock(control):
        directory.mkdir(parents=True)
        # Non-destructive first migration backup, including all current drafts/cards.
        backup = root / "migration-backup"
        if not backup.exists():
            backup.mkdir()
            dest = sqlite3.connect(backup / "control.db")
            control.backup(dest)
            dest.close()
            from .experience.store import _knowledge_root
            cards = _knowledge_root(control)
            if cards.exists():
                shutil.copytree(cards, backup / "knowledge")
        base = db.get_meta(control, "active_knowledge_set")
        governance = int(db.get_meta(control, "governance_revision", "0"))
        control.execute("INSERT INTO knowledge_set VALUES(?,?,?,?,?,?,?,NULL)",
                        (identifier, "building", str(directory), base, governance, "{}", _now()))
        control.commit()
    try:
        frozen = directory / "source"
        # Never follow out-of-repository symlinks or copy Git credentials/object databases.
        shutil.copytree(source, frozen, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.o", "*.a", "*.so", "*.dylib"))
        for link in frozen.rglob("*"):
            if link.is_symlink():
                if frozen not in link.resolve().parents:
                    raise SnapshotError(f"source symlink escapes repository: {link.relative_to(frozen)}")
        source_files = {str(p.relative_to(frozen)): digest(p.read_bytes()) for p in sorted(frozen.rglob("*"))
                        if p.is_file() and p.suffix in (".c", ".h", ".inc")}
        for rel, expected in source_files.items():
            if digest((source / rel).read_bytes()) != expected:
                raise SnapshotError("source changed while freezing")
        commands = json.loads(compile_path.read_text())
        for command in commands:
            src = Path(command["file"])
            if not src.is_absolute():
                src = Path(command["directory"]) / src
            if source not in src.resolve().parents:
                raise SnapshotError("compile database source outside repository")
        external, config = _freeze_build_inputs(source, frozen, compile_path, directory, strict)
        facts = db.connect(directory / "facts.db")
        db.set_meta(facts, "snapshot_building", "1")
        db.set_meta(facts, "snapshot_config_hash", config)
        facts.commit()
        facts.close()
        parse(str(frozen), db=str(directory / "facts.db"), compile_db=str(frozen / "compile_commands.json"),
              include=[], define=[], force=True, incremental=False)
        facts = db.connect(directory / "facts.db")
        stats = json.loads(db.get_meta(facts, "parse_stats", "{}"))
        if stats.get("diag_error") or stats.get("tu_failed") or not stats.get("tu_ok"):
            raise SnapshotError("severe compiler diagnostics; previous active retained")
        db.set_meta(facts, "repository_id", _repository_id(str(source)))
        db.set_meta(facts, "revision", revision)
        db.set_meta(facts, "knowledge_set_id", identifier)
        db.set_meta(facts, "source_content_hash", digest(source_files))
        db.set_meta(facts, "build_config_hash", config)
        facts.commit()
        with writer_lock(control):
            _project(control, facts)
        head.build(facts, str(frozen))
        if base:
            previous = control.execute("SELECT directory FROM knowledge_set WHERE id=?", (base,)).fetchone()
            old = readonly(Path(previous[0]) / "facts.db")
            try:
                _copy_tables(old, facts, ("wiki_page",))
                facts.commit()
            finally:
                old.close()
        wiki = generator.generate(facts, str(frozen), out_dir=str(directory / "wiki"))
        invalidation = {"mode": "initial_full_validation", "changed_count": 0, "affected_count": 0}
        if base:
            from .dependencies import audit_changes
            old = readonly(Path(previous[0]) / "facts.db")
            try:
                invalidation = audit_changes(old, facts)
            finally:
                old.close()
        index = indexer.build(facts, str(frozen), embedder_kind="local", out_dir=str(directory))
        implementation = digest({str(p.relative_to(Path(__file__).parent)): digest(p.read_bytes())
                                 for p in Path(__file__).parent.rglob("*.py")})
        manifest = {"schema_version": 1, "knowledge_set_id": identifier, "revision": revision,
                    "repository": db.get_meta(facts, "repository_id"), "source_content_hash": digest(source_files),
                    "source_files": source_files, "dirty_source": dirty, "build_config_hash": config,
                    "compile_database_hash": digest(compile_path.read_bytes()), "implementation_hash": implementation,
                    "governance_revision": governance, "created_at": _now(), "wiki": wiki, "index": index,
                    "dependency_policy": "conservative_all_parsed_entities", "invalidation": invalidation,
                    "external_dependencies": external,
                    "external_dependency_policy": "consumed_bytes_frozen_vfs"}
        _seal(directory, facts, manifest)
        facts.close()
        verify(directory)
        with writer_lock(control):
            control.execute("UPDATE knowledge_set SET state='validated',manifest_json=? WHERE id=?",
                            (json.dumps(manifest, ensure_ascii=False), identifier))
            control.commit()
        if activate_after:
            activate(control, identifier)
        return status(control)
    except Exception as exc:
        with writer_lock(control):
            control.rollback()
            control.execute("UPDATE knowledge_set SET state='failed',error=? WHERE id=?", (f"{type(exc).__name__}: {exc}", identifier))
            control.commit()
        raise


def activate(control, identifier: str, *, rollback=False):
    from .indexer import build as indexer
    with writer_lock(control):
        row = control.execute("SELECT * FROM knowledge_set WHERE id=?", (identifier,)).fetchone()
        if not row or row["state"] not in ("validated", "active", "historical"):
            raise SnapshotError("only validated sets can activate")
        base = db.get_meta(control, "active_knowledge_set")
        if not rollback and row["base_active"] != base:
            raise SnapshotError("active changed during build; explicit rollback/rebuild required")
        directory = Path(row["directory"])
        manifest = verify(directory)
        governance = int(db.get_meta(control, "governance_revision", "0"))
        # Never mutate published artifacts: rollback/review changes create a new projection set.
        if row["state"] in ("active", "historical") or governance != row["governance_revision"]:
            new_id = "ks-" + uuid.uuid4().hex[:16]
            new_directory = directory.parent / new_id
            shutil.copytree(directory, new_directory)
            facts = db.connect(new_directory / "facts.db")
            # Every set is self-contained; never query an unverified ancestor path.
            old_repo = db.get_meta(facts, "repo")
            new_repo = str(new_directory / "source")
            db.set_meta(facts, "repo", new_repo)
            facts.execute("UPDATE node SET repo=?", (new_repo,))
            for table in ("edge_source", "branch_fact", "semantic_fact"):
                facts.execute(f"UPDATE {table} SET tu=REPLACE(tu,?,?)", (old_repo, new_repo))
            _project(control, facts)
            db.set_meta(facts, "knowledge_set_id", new_id)
            facts.commit()
            indexer.build(facts, db.get_meta(facts, "repo"), embedder_kind="local", out_dir=str(new_directory))
            manifest.update(knowledge_set_id=new_id, governance_revision=governance,
                            parent_set=identifier, created_at=_now())
            _seal(new_directory, facts, manifest)
            facts.close()
            identifier, directory = new_id, new_directory
            control.execute("INSERT INTO knowledge_set VALUES(?,?,?,?,?,?,?,NULL)",
                            (identifier, "validated", str(directory), base, governance, json.dumps(manifest), _now()))
            control.commit()
            verify(directory)
        facts = readonly(directory / "facts.db")
        try:
            control.execute("BEGIN IMMEDIATE")
            # Legacy governance commands validate anchors against the same activated facts.
            _copy_tables(facts, control, FACT_TABLES)
            for key in ("repo", "repository_id", "revision", "build_config_hash", "source_content_hash",
                        "parse_stats", "degraded", "build_config_summary"):
                db.set_meta(control, key, db.get_meta(facts, key, ""))
            db.set_meta(control, "active_knowledge_set", identifier)
            db.set_meta(control, "snapshot_enabled", "1")
            control.execute("UPDATE knowledge_set SET state='historical' WHERE state='active'")
            control.execute("UPDATE knowledge_set SET state='active' WHERE id=?", (identifier,))
            control.commit()
        except Exception:
            control.rollback()
            raise
        finally:
            facts.close()
        from .experience import store
        store.stale_check(control)
    return status(control)
