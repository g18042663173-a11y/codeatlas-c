"""One module name for Wiki, impact, and the structure map."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).with_name("modules.yaml")


@lru_cache(maxsize=1)
def _corpora() -> list[dict[str, Any]]:
    raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return list(raw.get("corpora") or [])


def module_of(path: str, repository: str = "") -> str:
    """Stable public module map for pinned corpora; directory fallback otherwise."""
    if not path:
        return "root"
    rid = (repository or "").lower()
    for corpus in _corpora():
        match = str(corpus.get("match") or "")
        if match and match not in rid:
            continue
        if "/" not in path and corpus.get("root_if_flat"):
            return str(corpus["root_if_flat"])
        prefixes = sorted(
            corpus.get("prefixes") or [],
            key=lambda item: len(str(item.get("prefix") or "")),
            reverse=True,
        )
        for item in prefixes:
            prefix = str(item.get("prefix") or "")
            if not prefix or not path.startswith(prefix):
                continue
            module = str(item.get("module") or prefix.rstrip("/"))
            if item.get("nest_next"):
                tail = path[len(prefix):].split("/", 1)[0]
                return f"{module}/{tail}" if tail else module
            return module
        return path.rsplit("/", 1)[0] if "/" in path else "root"
    return path.rsplit("/", 1)[0] if "/" in path else "root"


def repository_label(conn) -> str:
    from .. import db as dbm

    return dbm.get_meta(conn, "repository_id", dbm.get_meta(conn, "repo", ""))
