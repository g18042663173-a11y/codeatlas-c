"""C / C++ 扩展名与扫描默认语言。compile_commands 仍以编译器实参为准。"""
from __future__ import annotations

from pathlib import Path

C_SOURCE_EXT = {".c", ".m"}
CXX_SOURCE_EXT = {".cc", ".cpp", ".cxx", ".c++"}
SOURCE_EXT = C_SOURCE_EXT | CXX_SOURCE_EXT
HEADER_EXT = {".h", ".hh", ".hpp", ".hxx", ".inc"}
READABLE_EXT = SOURCE_EXT | HEADER_EXT


def suffix_of(path: str | Path) -> str:
    return Path(path).suffix.lower()


def is_cxx_source(path: str | Path) -> bool:
    return suffix_of(path) in CXX_SOURCE_EXT


def is_readable_source(path: str | Path) -> bool:
    return suffix_of(path) in READABLE_EXT


def scan_language_args(path: str | Path) -> list[str]:
    """Directory-scan defaults. A compile database keeps its own -std / -x."""
    if is_cxx_source(path):
        return ["-x", "c++", "-std=c++17"]
    return ["-std=c11"]
