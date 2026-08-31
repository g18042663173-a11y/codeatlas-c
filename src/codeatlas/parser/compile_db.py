"""compile_commands.json 的加载与编译参数清洗。

三级降级策略：
  1. 有 compile_commands.json  → 用真实编译上下文（最准）
  2. 无，但给了 --include 目录 → 用手工 flags 扫描全部 .c
  3. 都没有                    → 裸解析，只带默认 include 路径
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)
_BUILTIN: list[str] = []

# 需要剔除的参数：这些是"生成产物"相关的，libclang 不需要且可能报错
_DROP_WITH_ARG = {"-o", "-MF", "-MT", "-MQ"}
_SOURCE_EXT = {".c", ".cc", ".cpp", ".cxx", ".m"}


def builtin_include_args() -> list[str]:
    """补上 freestanding 头目录（stddef.h / stdarg.h / stdbool.h 等）。

    ★ 高频坑：libclang 作为**库**被调用时，拿不到 clang 驱动程序才知道的
      resource-dir，于是每个 #include <stdio.h> 的翻译单元都会报一次
      "'stddef.h' file not found"。症状很有迷惑性 ——
      表现为"每个文件恰好 1 个错误"，看着像小毛病，
      实际是整条标准库头链断了：AST 照样产出，但类型信息不全，
      能 resolve 的调用变少，**certain 边被系统性少算**。

    探测顺序：
      1. pip libclang wheel 自带的 clang/*/include（多数 wheel 不带）
      2. 系统 LLVM 安装的 lib/clang/*/include
      3. GCC 的 include 目录 —— 这些 freestanding 头是编译器无关的，
         用 GCC 的喂给 clang 完全可行，是最实用的兜底
    """
    import glob as _glob
    from clang import cindex

    cands: list[str] = []
    try:
        base = os.path.dirname(cindex.conf.get_filename())
        cands += [os.path.join(base, "clang", "*", "include"),
                  os.path.join(base, "..", "clang", "*", "include")]
    except Exception:
        pass
    cands += ["/usr/lib/llvm-*/lib/clang/*/include",
              "/usr/lib/clang/*/include",
              "/usr/local/lib/clang/*/include",
              "/usr/lib/gcc/*/*/include"]

    for pat in cands:
        for hit in sorted(_glob.glob(pat)):
            if os.path.exists(os.path.join(hit, "stddef.h")):
                return [f"-I{os.path.normpath(hit)}"]
    return []


@dataclass
class CompileUnit:
    """一个待解析的翻译单元。"""

    source: str                      # 源文件绝对路径
    args: list[str] = field(default_factory=list)
    directory: str = "."             # 相对 -I 依赖的工作目录


def clean_args(raw_args: list[str], source_file: str) -> list[str]:
    """剔除 -c / -o xxx / -M* 依赖生成 / 源文件本身，其余全部保留。

    这段逻辑看着琐碎，但漏一个 -o 就会让 libclang 把输出文件名当成源文件。
    """
    args = list(raw_args)
    out: list[str] = []
    src_base = os.path.basename(source_file)
    i = 1  # args[0] 是编译器名

    while i < len(args):
        a = args[i]
        if a in _DROP_WITH_ARG:
            i += 2
            continue
        if a == "-c" or a.startswith("-M"):
            i += 1
            continue
        if os.path.basename(a) == src_base:
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def from_compile_db(db_dir: str | Path) -> list[CompileUnit]:
    """模式 1：从 compile_commands.json 读取。"""
    d = Path(db_dir)
    if d.suffix == ".json":
        d = d.parent
    f = d / "compile_commands.json"
    if not f.exists():
        raise FileNotFoundError(
            f"{f} 不存在。生成方式：\n"
            "  CMake    : cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -B build\n"
            "  Makefile : bear -- make"
        )

    entries = json.loads(f.read_text(encoding="utf-8", errors="replace"))
    units: list[CompileUnit] = []
    for e in entries:
        src = e["file"]
        directory = e.get("directory", str(d))
        if not os.path.isabs(src):
            src = os.path.join(directory, src)
        raw = e.get("arguments") or e.get("command", "").split()
        units.append(CompileUnit(source=os.path.abspath(src),
                                 args=clean_args(raw, src) + _BUILTIN,
                                 directory=directory))
    log.info("compile_commands.json 覆盖 %d 个翻译单元", len(units))
    return units


def from_scan(repo: str | Path, includes: list[str] | None = None,
              defines: list[str] | None = None) -> list[CompileUnit]:
    """模式 2/3：目录扫描降级方案。

    没有编译数据库时用它。解析质量会下降（宏未定义、头文件找不到），
    因此 stats 会记录 degraded=1，README 里必须如实说明。
    """
    root = Path(repo).resolve()
    inc = [f"-I{Path(p).resolve()}" for p in (includes or [])]
    if not inc:
        # 兜底：把仓库根和常见 include 目录都加上
        inc = [f"-I{root}"]
        for name in ("include", "inc", "src", "headers"):
            p = root / name
            if p.is_dir():
                inc.append(f"-I{p}")
    dfn = [f"-D{d}" for d in (defines or [])]

    units: list[CompileUnit] = []
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() in _SOURCE_EXT and p.is_file():
            if any(part in {"build", ".git", "test", "tests"} for part in p.parts):
                continue
            units.append(CompileUnit(source=str(p),
                                     args=[*inc, *dfn, "-std=c11", *_BUILTIN],
                                     directory=str(root)))
    log.info("目录扫描发现 %d 个源文件（降级模式）", len(units))
    return units


def load(repo: str | Path, compile_db: str | Path | None = None,
         includes: list[str] | None = None,
         defines: list[str] | None = None) -> tuple[list[CompileUnit], bool]:
    """统一入口。返回 (翻译单元列表, 是否处于降级模式)。"""
    global _BUILTIN
    if not _BUILTIN:
        _BUILTIN = builtin_include_args()
        if _BUILTIN:
            log.info("注入 clang builtin 头目录: %s", _BUILTIN[0])
        else:
            log.warning("未找到 clang builtin 头目录，标准库类型可能解析不全")
    if compile_db:
        try:
            return from_compile_db(compile_db), False
        except FileNotFoundError as e:
            log.warning("编译数据库不可用，降级为目录扫描：%s", e)
    return from_scan(repo, includes, defines), True
