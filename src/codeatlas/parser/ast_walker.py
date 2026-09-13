"""libclang AST 遍历：把 C/C++ 翻译单元抽成 node / edge。

【本文件是全项目的地基，两个关键设计写在这里】

D1 事实与推断分离
    CALL_EXPR 能被编译器 resolve 到具体 FUNCTION_DECL  → certain (reason=ast)
    通过变量/字段/参数调用（函数指针）                  → candidate (reason=fn_pointer)
    完全 resolve 不了（宏展开、声明缺失）               → candidate (reason=unresolved)
    函数名被取地址但未直接调用                          → candidate (reason=address_taken)

    candidate 边照样入库，但在调用链、影响分析、检索上下文中一律排除。
    基带这类代码里函数指针极多，如果混进 certain，影响分析会给出错误的回归范围。

USR 全局去重
    一个头文件会被 N 个翻译单元 include，同一个函数声明会被遍历 N 次。
    clang 的 USR (Unified Symbol Resolution) 对同一符号跨 TU 稳定，
    所以 node.id = sha1(USR)。static 函数的 USR 自带文件路径，天然不会串。
"""
from __future__ import annotations

import hashlib
import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from itertools import islice
from contextvars import ContextVar

from clang import cindex

log = logging.getLogger(__name__)

CK = cindex.CursorKind
_ROOT = ContextVar("codeatlas_parser_root", default=None)
_CONFIG = ContextVar("codeatlas_parser_config", default="unknown")


def _ck(*names: str) -> tuple:
    return tuple(kind for name in names if (kind := getattr(CK, name, None)) is not None)


_CALLABLE_KINDS = _ck(
    "FUNCTION_DECL", "CXX_METHOD", "CONSTRUCTOR", "DESTRUCTOR", "CONVERSION_FUNCTION",
)
_RECORD_KINDS = _ck("STRUCT_DECL", "UNION_DECL", "CLASS_DECL", "CLASS_TEMPLATE")
_TYPE_KINDS = _RECORD_KINDS + _ck("ENUM_DECL", "TYPEDEF_DECL")
_NAMESPACE_PARENTS = _ck("TRANSLATION_UNIT", "NAMESPACE", "LINKAGE_SPEC")


def _identity_path(path):
    root = _ROOT.get()
    return repo_relpath(path, root) if root else os.path.abspath(path)
# ★ 不加这个 flag，宏定义和 #include 指令完全不会出现在 AST 里
PARSE_OPTS = cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD

_KIND_MAP = {
    CK.FUNCTION_DECL: "function",
    CK.STRUCT_DECL: "struct",
    CK.UNION_DECL: "struct",
    CK.ENUM_DECL: "enum",
    CK.TYPEDEF_DECL: "typedef",
    CK.MACRO_DEFINITION: "macro",
    CK.FIELD_DECL: "field",
}
if hasattr(CK, "CLASS_DECL"):
    _KIND_MAP[CK.CLASS_DECL] = "class"


def nid(*parts: str) -> str:
    """稳定节点 ID。"""
    return hashlib.sha1("::".join(parts).encode("utf-8")).hexdigest()[:20]


def node_id_for(cur: cindex.Cursor) -> str:
    if cur.kind == CK.MACRO_DEFINITION and cur.location.file:
        # The ordinal distinguishes same-name redefinitions without line-offset identity.
        import re
        from pathlib import Path
        prefix = Path(cur.location.file.name).read_bytes()[:cur.extent.start.offset].decode("utf-8", "replace")
        ordinal = len(re.findall(r"(?m)^\s*#\s*define\s+" + re.escape(cur.spelling) + r"\b", prefix))
        return nid("macro", _identity_path(cur.location.file.name), cur.spelling, str(ordinal), _CONFIG.get())
    usr = cur.get_usr()
    if usr:
        # A compile database can contain several executable targets. Clang gives
        # every external ``main`` the same USR, even when the definitions belong
        # to independent fuzz/test programs. Path-scope this special entry point
        # so one target cannot overwrite another and inherit its call edges.
        loc = cur.location
        if (cur.kind == CK.FUNCTION_DECL and cur.spelling == "main"
                and loc.file is not None):
            return nid("usr-path", usr, _identity_path(loc.file.name))
        return nid("usr", usr)
    loc = cur.location
    fname = loc.file.name if loc.file else "?"
    return nid("loc", _identity_path(fname), cur.kind.name, cur.spelling or "?", str(loc.line))


def file_id(path: str) -> str:
    return nid("file", _identity_path(path))


def repo_relpath(path: str, repo: str) -> str:
    """Return a stable repository-relative path on every operating system."""
    return os.path.relpath(os.path.abspath(path), repo).replace(os.sep, "/")


@dataclass
class ParseResult:
    nodes: dict[str, dict] = field(default_factory=dict)
    edges: dict[tuple, dict] = field(default_factory=dict)
    branch_facts: dict[str, dict] = field(default_factory=dict)
    semantic_facts: dict[str, dict] = field(default_factory=dict)
    build_configs: set[str] = field(default_factory=set)
    stats: Counter = field(default_factory=Counter)

    def add_node(self, node: dict) -> None:
        """定义优先：后来的定义可以覆盖先前的声明。"""
        old = self.nodes.get(node["id"])
        if old is None:
            self.nodes[node["id"]] = node
        elif node.get("is_definition") and not old.get("is_definition"):
            self.nodes[node["id"]] = node
        else:
            self.stats["node_dedup"] += 1

    def add_edge(self, src: str, dst: str, kind: str, confidence: str,
                 reason: str = "", evidence: str = "", tu: str = "") -> None:
        # 逻辑边按 (src,dst,kind,confidence) 去重；
        # 来源 TU 单独累积到 sources，落库时写进 edge_source 关联表。
        key = (src, dst, kind, confidence)
        if key in self.edges:
            self.stats["edge_dedup"] += 1
            if tu:
                self.edges[key]["sources"].add(tu)
            return
        self.edges[key] = dict(src=src, dst=dst, kind=kind, confidence=confidence,
                               reason=reason, evidence=evidence,
                               sources={tu} if tu else set())
        self.stats[f"edge_{confidence}"] += 1

    def add_branch_fact(self, fact: dict) -> None:
        self.branch_facts[fact["id"]] = fact
        self.stats[f"branch_{fact['kind']}"] += 1


class AstWalker:
    """遍历一批翻译单元，累积到同一个 ParseResult。"""

    def __init__(self, repo: str, result: ParseResult | None = None) -> None:
        self.repo = os.path.abspath(repo)
        _ROOT.set(self.repo)
        self.config_hash = "unknown"
        self.build_config_override = None
        self.result = result or ParseResult()
        self.index = cindex.Index.create()
        self._seen_files: set[str] = set()
        self._cur_tu: str = ""          # 当前正在解析的翻译单元
        self.last_tu = None             # 供增量模块采集 include 依赖

    # ---------------- 对外入口 ----------------

    def walk_unit(self, source: str, args: list[str], directory: str = ".") -> int:
        """解析一个 TU。返回该 TU 的严重诊断数量。"""
        cwd = os.getcwd()
        _ROOT.set(self.repo)
        from ..contracts import digest
        self.config_hash = self.build_config_override or digest([a.replace(self.repo, "${REPO}") for a in args])
        _CONFIG.set(self.config_hash)
        self.result.build_configs.add(self.config_hash)
        try:
            self._cur_tu = os.path.abspath(source)
            os.chdir(directory)  # compile_commands 里的 -I 常是相对路径
            tu = self.index.parse(source, args=args, options=PARSE_OPTS)
        except cindex.TranslationUnitLoadError as e:
            log.warning("解析失败 %s: %s", source, e)
            self.result.stats["tu_failed"] += 1
            return -1
        finally:
            os.chdir(cwd)

        self.last_tu = tu
        errors = [d for d in tu.diagnostics if d.severity >= 3]
        self.result.stats["tu_ok"] += 1
        self.result.stats["diag_error"] += len(errors)

        self._collect_includes(tu, source)
        self._walk_cursor(tu.cursor, source)
        return len(errors)

    # ---------------- 内部 ----------------

    def _in_repo(self, path: str | None) -> bool:
        if not path:
            return False
        return os.path.abspath(path).startswith(self.repo)

    def _ensure_file_node(self, path: str) -> str:
        ap = os.path.abspath(path)
        fid = file_id(ap)
        if ap not in self._seen_files:
            self._seen_files.add(ap)
            self.result.add_node(dict(
                id=fid, kind="file", name=os.path.basename(ap), usr=None,
                repo=self.repo, path=repo_relpath(ap, self.repo),
                line_start=None, line_end=None, signature=None,
                is_definition=1, is_static=0, extra=None,
            ))
        return fid

    def _collect_includes(self, tu: cindex.TranslationUnit, source: str) -> None:
        """#include 关系。只记录仓库内的头文件。"""
        for inc in tu.get_includes():
            src_path = inc.source.name if inc.source else None
            dst_path = inc.include.name if inc.include else None
            if not (self._in_repo(src_path) and self._in_repo(dst_path)):
                continue
            a = self._ensure_file_node(src_path)
            b = self._ensure_file_node(dst_path)
            # #include 是编译器实际展开的，属于确定事实
            self.result.add_edge(a, b, "includes", "certain", reason="preprocessor", tu=self._cur_tu)

    def _walk_cursor(self, cursor: cindex.Cursor, source: str) -> None:
        for cur in cursor.walk_preorder():
            loc = cur.location
            path = loc.file.name if loc.file else None
            if not self._in_repo(path):
                continue

            kind = _KIND_MAP.get(cur.kind)

            if cur.kind in _CALLABLE_KINDS:
                self._handle_function(cur, path)
            elif cur.kind == CK.VAR_DECL and self._is_global(cur):
                self._add_decl(cur, "global", path)
            elif kind and cur.kind != CK.FIELD_DECL:
                if cur.kind == getattr(CK, "CLASS_DECL", None) and not cur.spelling:
                    continue
                self._add_decl(cur, kind, path)
                if cur.kind in _RECORD_KINDS:
                    self._collect_bases(cur)
            elif cur.kind == CK.FIELD_DECL:
                self._handle_field(cur, path)

    @staticmethod
    def _is_global(cur: cindex.Cursor) -> bool:
        parent = cur.semantic_parent
        return parent is not None and parent.kind in _NAMESPACE_PARENTS

    def _add_decl(self, cur: cindex.Cursor, kind: str, path: str) -> str:
        node = dict(
            id=node_id_for(cur), kind=kind, name=cur.spelling or "<anon>",
            usr=cur.get_usr() or None, repo=self.repo,
            path=repo_relpath(path, self.repo),
            line_start=cur.extent.start.line, line_end=cur.extent.end.line,
            signature=(cur.type.spelling if cur.type else None),
            is_definition=int(cur.is_definition()), is_static=0, extra=None,
        )
        self.result.add_node(node)
        self.result.add_edge(self._ensure_file_node(path), node["id"], "contains", "certain",
                             reason="ast", tu=self._cur_tu)
        self.result.stats[f"node_{kind}"] += 1
        return node["id"]

    def _handle_field(self, cur: cindex.Cursor, path: str) -> None:
        parent = cur.semantic_parent
        if parent is None or parent.kind not in _RECORD_KINDS:
            return
        fid = self._add_decl(cur, "field", path)
        self.result.add_edge(node_id_for(parent), fid, "contains", "certain",
                             reason="ast", tu=self._cur_tu)

    def _collect_bases(self, derived: cindex.Cursor) -> None:
        specifier = getattr(CK, "CXX_BASE_SPECIFIER", None)
        if specifier is None:
            return
        for child in derived.get_children():
            if child.kind != specifier:
                continue
            base = child.referenced
            if base is None and child.type is not None:
                try:
                    base = child.type.get_declaration()
                except (ValueError, AttributeError):
                    base = None
            if (base is None or base.kind not in _RECORD_KINDS
                    or not self._in_repo(base.location.file.name if base.location.file else None)):
                continue
            self.result.add_edge(
                node_id_for(derived), node_id_for(base), "inherits", "certain",
                reason="ast", tu=self._cur_tu,
            )

    @staticmethod
    def _callable_name(cur: cindex.Cursor) -> str:
        name = cur.spelling or cur.displayname or "<anon>"
        parent = cur.semantic_parent
        if parent is None or parent.kind not in _RECORD_KINDS or not parent.spelling:
            return name
        if name.startswith(parent.spelling + "::"):
            return name
        return f"{parent.spelling}::{name}"

    @staticmethod
    def _skip_callable(cur: cindex.Cursor) -> bool:
        parent = cur.semantic_parent
        return bool(parent is not None and parent.kind in _RECORD_KINDS and not parent.spelling)

    def _handle_function(self, cur: cindex.Cursor, path: str) -> None:
        if self._skip_callable(cur):
            return
        is_static = cur.storage_class == cindex.StorageClass.STATIC
        if hasattr(cur, "is_static_method") and cur.is_static_method():
            is_static = True
        extra = None
        if cur.kind != CK.FUNCTION_DECL:
            extra = f'{{"cxx_kind":"{cur.kind.name}"}}'
        node = dict(
            id=node_id_for(cur), kind="function", name=self._callable_name(cur),
            usr=cur.get_usr() or None, repo=self.repo,
            path=repo_relpath(path, self.repo),
            line_start=cur.extent.start.line, line_end=cur.extent.end.line,
            signature=cur.type.spelling if cur.type else None,
            is_definition=int(cur.is_definition()), is_static=int(is_static), extra=extra,
        )
        self.result.add_node(node)
        self.result.add_edge(self._ensure_file_node(path), node["id"], "contains", "certain",
                             reason="ast", tu=self._cur_tu)
        parent = cur.semantic_parent
        if parent is not None and parent.kind in _RECORD_KINDS and parent.spelling:
            self.result.add_edge(node_id_for(parent), node["id"], "contains", "certain",
                                 reason="ast", tu=self._cur_tu)
        self.result.stats["node_function"] += 1
        if cur.is_definition():
            self.result.stats["node_function_def"] += 1
            self._scan_body(cur, node["id"], path)
            from .semantics import extract
            for fact in extract(cur, node["id"], node["path"], self._cur_tu, self.config_hash):
                self.result.semantic_facts[fact["id"]] = fact

    def _scan_body(self, fn: cindex.Cursor, caller_id: str, path: str) -> None:
        """在函数体里找调用边 —— D1 的判定逻辑全在这里。"""
        rel = repo_relpath(path, self.repo)
        for child in fn.get_children():
            self._walk_expr(child, caller_id, rel, callee_pos=False)

    def _walk_expr(self, cur: cindex.Cursor, caller_id: str, rel: str,
                   callee_pos: bool) -> None:
        """显式父节点递归。

        ★ 不能用 walk_preorder + semantic_parent 判断"是否处于被调位置"：
          对表达式节点，semantic_parent 返回的是所在函数/TU 这类语义上下文，
          不是语法父节点。用它会把每个正常调用的函数名又记一条 address_taken，
          candidate 集直接被污染成噪声。
        """
        k = cur.kind

        if k in (CK.IF_STMT, CK.SWITCH_STMT, CK.CONDITIONAL_OPERATOR, CK.RETURN_STMT):
            self._record_branch(cur, caller_id, rel)

        if k == CK.CALL_EXPR:
            self._classify_call(cur, caller_id, rel)
            # 第 0 个 child 是被调表达式，其余是实参
            for i, ch in enumerate(cur.get_children()):
                self._walk_expr(ch, caller_id, rel, callee_pos=(i == 0))
            return

        if k == CK.DECL_REF_EXPR:
            if not callee_pos:
                # Function references become candidate calls; compiler-resolved
                # repository globals become a separate certain fact.
                self._classify_reference(cur, caller_id, rel)
            return

        if k == CK.MEMBER_REF_EXPR:
            self._classify_member_access(cur, caller_id, rel)

        if k == CK.TYPE_REF:
            self._classify_type_use(cur, caller_id, rel)
            return

        # 其余节点：callee_pos 需要穿透 UNEXPOSED_EXPR / 隐式转换等包装层
        for ch in cur.get_children():
            self._walk_expr(ch, caller_id, rel, callee_pos=callee_pos)

    def _record_branch(self, cur: cindex.Cursor, function_id: str, rel: str) -> None:
        """Record only the source span the compiler exposed.

        These rows deliberately do not claim reachability or full control-flow
        semantics.  They are C-level background facts for Wiki pages.
        """
        kind_map = {
            CK.IF_STMT: "if", CK.SWITCH_STMT: "switch",
            CK.CONDITIONAL_OPERATOR: "conditional", CK.RETURN_STMT: "early_return",
        }
        kind = kind_map[cur.kind]
        # IF_STMT tokens include the whole body. Materialising them all for every
        # nested branch becomes quadratic on large functions, so only retain the
        # short evidence prefix shown in the Wiki.
        tokens = [token.spelling for token in islice(cur.get_tokens(), 80)]
        text = " ".join(tokens[:80])
        if cur.kind == CK.RETURN_STMT and any(
            marker in text.lower() for marker in ("null", "false", "err", "-1", "error")
        ):
            kind = "error_return"
        start, end = cur.extent.start.line, cur.extent.end.line
        source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        fact_id = nid("branch", function_id, rel, str(start), str(end), kind, source_hash)
        self.result.add_branch_fact({
            "id": fact_id, "function_id": function_id, "tu": self._cur_tu,
            "path": rel, "line_start": start, "line_end": end, "kind": kind,
            "condition_text": text[:500], "source_hash": source_hash,
        })

    def _classify_call(self, cur: cindex.Cursor, caller_id: str, rel: str) -> None:
        ref = cur.referenced
        ev = f'{{"file":"{rel}","line":{cur.location.line}}}'

        if ref is not None and ref.kind in _CALLABLE_KINDS:
            # ✅ 编译器确认：能 resolve 到具体函数/方法声明（含静态绑定的虚调用）
            self.result.add_edge(caller_id, node_id_for(ref), "calls", "certain",
                                 reason="ast", evidence=ev, tu=self._cur_tu)
            self.result.stats["call_certain"] += 1
            return

        if ref is not None and ref.kind in (CK.VAR_DECL, CK.FIELD_DECL, CK.PARM_DECL):
            # ⚠️ 通过变量/结构体字段/参数调用 → 函数指针
            # ★ 必须为这个"指针载体"建节点：局部变量和形参不在常规节点采集范围内，
            #   不建的话这条边会在 persist 的孤儿清理里被静默删掉，
            #   而函数指针恰恰是 candidate 里最有价值的一类。
            self.result.add_edge(caller_id, self._ensure_reference_node(ref), "calls", "candidate",
                                 reason="fn_pointer", evidence=ev, tu=self._cur_tu)
            self.result.stats["call_candidate_fnptr"] += 1
            return

        # ⚠️ 完全 resolve 不了：宏展开后的调用、声明缺失、解析错误
        name = cur.spelling or self._callee_name(cur)
        if name:
            # 建一个占位节点，否则这条边会在落库时被外键清理掉。
            # "这里调用了一个解析不到的东西"本身就是有价值的信息（多半是宏或缺声明）
            ph = nid("unresolved", name)
            self.result.add_node(dict(
                id=ph, kind="unresolved", name=name, usr=None, repo=self.repo,
                path=None, line_start=None, line_end=None, signature=None,
                is_definition=0, is_static=0, extra='{"note":"未能解析的调用目标"}'))
            self.result.add_edge(caller_id, ph, "calls", "candidate",
                                 reason="unresolved", evidence=ev, tu=self._cur_tu)
        self.result.stats["call_candidate_unresolved"] += 1

    @staticmethod
    def _callee_name(cur: cindex.Cursor) -> str:
        """CALL_EXPR.spelling 在 resolve 失败时常为空，从被调子表达式里捞名字。"""
        for ch in cur.get_children():
            if ch.spelling:
                return ch.spelling
            for gch in ch.get_children():
                if gch.spelling:
                    return gch.spelling
        toks = [t.spelling for t in cur.get_tokens()]
        return toks[0] if toks else ""

    def _ensure_reference_node(self, ref: cindex.Cursor) -> str:
        """Ensure a compiler-resolved reference target survives edge persistence."""
        rid = node_id_for(ref)
        loc = ref.location
        path = loc.file.name if loc.file else None
        rel = (repo_relpath(path, self.repo)
               if path and self._in_repo(path) else None)
        if ref.kind == CK.VAR_DECL and self._is_global(ref):
            kind = "global"
        else:
            kind = {
                CK.PARM_DECL: "param", CK.FIELD_DECL: "field",
                CK.STRUCT_DECL: "struct", CK.UNION_DECL: "struct",
                CK.ENUM_DECL: "enum", CK.TYPEDEF_DECL: "typedef",
                **({CK.CLASS_DECL: "class"} if hasattr(CK, "CLASS_DECL") else {}),
            }.get(ref.kind, "local_var")
        self.result.add_node(dict(
            id=rid, kind=kind, name=ref.spelling or "<anon>", usr=ref.get_usr() or None,
            repo=self.repo, path=rel, line_start=loc.line, line_end=loc.line,
            signature=ref.type.spelling if ref.type else None,
            is_definition=int(ref.is_definition()), is_static=0,
            extra='{"note":"编译器解析的引用目标"}'))
        return rid

    def _classify_reference(self, cur: cindex.Cursor, caller_id: str, rel: str) -> None:
        ref = cur.referenced
        if ref is None or not self._in_repo(ref.location.file.name if ref.location.file else None):
            return
        evidence = f'{{"file":"{rel}","line":{cur.location.line}}}'
        if ref.kind in _CALLABLE_KINDS:
            # A function name outside the callee position is an address-taken hint,
            # not proof that an indirect call reaches that function.
            self.result.add_edge(caller_id, node_id_for(ref), "calls", "candidate",
                                 reason="address_taken", tu=self._cur_tu,
                                 evidence=evidence)
            self.result.stats["call_candidate_addrtaken"] += 1
        elif ref.kind == CK.VAR_DECL and self._is_global(ref):
            self.result.add_edge(
                caller_id, self._ensure_reference_node(ref), "global_ref", "certain",
                reason="ast", evidence=evidence, tu=self._cur_tu,
            )
            self.result.stats["global_ref_certain"] += 1

    def _classify_member_access(self, cur: cindex.Cursor, caller_id: str, rel: str) -> None:
        ref = cur.referenced
        if ref is None or ref.kind != CK.FIELD_DECL:
            return
        if not self._in_repo(ref.location.file.name if ref.location.file else None):
            return
        self.result.add_edge(
            caller_id, self._ensure_reference_node(ref), "field_access", "certain",
            reason="ast", tu=self._cur_tu,
            evidence=f'{{"file":"{rel}","line":{cur.location.line}}}',
        )
        self.result.stats["field_access_certain"] += 1

    def _classify_type_use(self, cur: cindex.Cursor, caller_id: str, rel: str) -> None:
        ref = cur.referenced
        if ref is None or ref.kind not in _TYPE_KINDS:
            return
        if not self._in_repo(ref.location.file.name if ref.location.file else None):
            return
        self.result.add_edge(
            caller_id, self._ensure_reference_node(ref), "type_use", "certain",
            reason="ast", tu=self._cur_tu,
            evidence=f'{{"file":"{rel}","line":{cur.location.line}}}',
        )
        self.result.stats["type_use_certain"] += 1


# ---------------- 落库 ----------------

def _definition_hash(node: dict) -> str | None:
    """Hash the exact definition span used as a reviewed source anchor.

    The hash deliberately covers only the function definition, not its surrounding
    line number. A function can move without invalidating a reviewed knowledge card;
    a body/signature edit cannot.
    """
    if node.get("kind") != "function" or not node.get("is_definition"):
        return None
    repo, path = node.get("repo"), node.get("path")
    start, end = node.get("line_start"), node.get("line_end")
    if not repo or not path or not start or not end:
        return None
    try:
        raw = os.path.join(repo, path)
        with open(raw, "rb") as source_file:
            lines = source_file.read().splitlines(keepends=True)
    except OSError:
        return None
    if start < 1 or end < start or start > len(lines):
        return None
    return hashlib.sha256(b"".join(lines[start - 1:end])).hexdigest()


def persist(conn, result: ParseResult) -> None:
    from .. import db as dbm
    from ..contracts import digest
    if not dbm.get_meta(conn, "build_config_hash"):
        dbm.set_meta(conn, "build_config_hash", digest(sorted(result.build_configs)))
    conn.executemany(
        "INSERT OR REPLACE INTO semantic_fact VALUES(:id,:function_id,:kind,:payload_json,:confidence,:path,:line_start,:line_end,:tu)",
        result.semantic_facts.values())
    nodes = []
    for raw in result.nodes.values():
        node = dict(raw)
        node["definition_hash"] = _definition_hash(node)
        nodes.append(node)
    conn.executemany(
        "INSERT OR REPLACE INTO node"
        "(id,kind,name,usr,repo,path,line_start,line_end,signature,definition_hash,"
        "is_definition,is_static,extra)"
        " VALUES(:id,:kind,:name,:usr,:repo,:path,:line_start,:line_end,:signature,"
        ":definition_hash,:is_definition,:is_static,:extra)",
        nodes,
    )
    edges = list(result.edges.values())
    conn.executemany(
        "INSERT OR IGNORE INTO edge(src,dst,kind,confidence,reason,evidence)"
        " VALUES(:src,:dst,:kind,:confidence,:reason,:evidence)",
        edges,
    )
    # 回填 edge_source：逻辑边 → 观察到它的每个 TU
    pairs = []
    for e in edges:
        if not e["sources"]:
            continue
        row = conn.execute(
            "SELECT id FROM edge WHERE src=? AND dst=? AND kind=? AND confidence=?",
            (e["src"], e["dst"], e["kind"], e["confidence"])).fetchone()
        if row:
            pairs += [(row[0], tu) for tu in e["sources"]]
    conn.executemany(
        "INSERT OR IGNORE INTO edge_source(edge_id, tu) VALUES(?,?)", pairs)
    if result.branch_facts:
        conn.executemany(
            """INSERT OR REPLACE INTO branch_fact
               (id,function_id,tu,path,line_start,line_end,kind,condition_text,source_hash)
               VALUES(:id,:function_id,:tu,:path,:line_start,:line_end,:kind,:condition_text,:source_hash)""",
            list(result.branch_facts.values()),
        )
    # 孤儿边清理。★ 这里必须报数：曾经因为漏建局部变量节点，
    # 导致全部 fn_pointer 边被静默删光，candidate 集看起来"很干净"其实是空的。
    orphan = conn.execute(
        "SELECT COUNT(*) c FROM edge WHERE dst NOT IN (SELECT id FROM node)").fetchone()[0]
    if orphan:
        log.warning("清理孤儿边 %d 条（目标节点不存在）", orphan)
        result.stats["edge_orphan_dropped"] = orphan
    conn.execute("DELETE FROM edge WHERE dst NOT IN (SELECT id FROM node)")
    from ..dependencies import normalized
    for node in nodes:
        source_hash = node.get("definition_hash")
        if node.get("path") and node.get("line_start"):
            try:
                with open(os.path.join(node["repo"], node["path"]), encoding="utf-8", errors="replace") as stream:
                    span = "".join(stream.readlines()[node["line_start"]-1:node["line_end"]])
                semantic = digest(normalized(span))
            except OSError:
                semantic = "unknown"
        else:
            semantic = "unknown"
        skeleton = [f["payload_json"] for f in result.semantic_facts.values() if f["function_id"] == node["id"]]
        conn.execute("INSERT OR REPLACE INTO entity_fingerprint VALUES(?,?,?,?,?,?)",
                     (node["id"], digest(node.get("signature")), semantic, digest(skeleton),
                      dbm.get_meta(conn, "build_config_hash"), int(semantic != "unknown")))
    conn.commit()
