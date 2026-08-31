"""重排（Reranking）—— 检索链路上原本缺失的一环。

【为什么需要它】
主流检索是 召回 → 融合 → **重排** → 生成。
本项目原来只做到融合就直接拼上下文了，症状很典型：
Recall 不低（0.68）但 MRR 只有 0.45 —— **该找的都找到了，只是没排在前面**。
这正是重排要解决的问题。

融合和重排的分工：
  RRF 融合   只看"各路排名"，不看内容。它不知道这个 chunk 到底是不是定义、
             是不是查询里点名的那个符号、扇入高不高。
  重排       拿到候选之后，用**内容特征**重新打分。

【为什么用特征线性打分，而不是 cross-encoder】
Cross-encoder（如 bge-reranker）效果更好，本模块也留了接口。但默认实现是
手写特征的线性加权，理由有三：

  1. **零模型依赖**，符合本项目"无 key 无模型也能跑通"的一贯约束。
  2. **完全可解释**：能打印出每个特征对最终分数的贡献。
     排错时你知道为什么某个结果排上来了，黑盒模型给不了这个。
  3. 代码检索的强信号是**结构性**的（是不是定义、是不是精确同名、扇入多少），
     这些特征人工就能写对，不需要模型去学。

【必须说清的局限】
这是**启发式**重排，不是 learning-to-rank。权重是我按先验拍的，不是训出来的。
要训需要带相关性标注的点击/标注数据，本项目没有。
—— 面试被问到"权重怎么定的"，答案就是这句，不要编成"调参得到"。
"""
from __future__ import annotations

import logging
import math
import re
import sqlite3
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

RE_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
RE_FILE = re.compile(r"\b[\w/.-]+\.(?:c|h)\b", re.I)

# 特征权重。全部为正，含义是"这个信号有多值得信"。
# ★ 这些数是按先验拍的，不是训出来的 —— 见模块 docstring 的局限说明。
WEIGHTS = {
    # 融合分权重要显著大于其它特征：内容特征是**打破平局**用的，
    # 不是用来推翻召回排序的。第一版没拉开差距，特征噪声直接盖过了融合信号。
    "fusion": 6.0,
    "exact_symbol": 2.5,  # 查询里点名的标识符与本块符号名完全相等
    # ★ 前缀匹配权重为负，这是反直觉但正确的：
    #   问 cJSON_Delete 时命中 cJSON_DeleteItemFromObject，说明这是**另一个**符号。
    #   第一版我给了 +0.8，结果它把真实调用者挤出前十，Recall 从 68.1% 掉到 62.7%
    #   —— 等于把本项目要解决的"同名符号混淆"又引了回来。
    "prefix_symbol": -0.5,
    "is_definition": 1.0,  # 定义优于声明
    "term_cover": 1.5,    # 查询词在标题/正文中的覆盖率
    "file_match": 1.2,    # 查询点名了文件，本块正好在该文件
    "evidence": 0.8,      # A 级 > B 级 > C 级
    "importance": 0.6,    # log(扇入)，被调用得多的函数更可能是问的对象
    # 跳数衰减权重要小：融合分里已经含了 decay^hop，这里再重罚一次是双重惩罚。
    "hop_decay": 0.3,
    "len_penalty": -0.4,  # 超长块稀释信号
}


@dataclass
class Scored:
    """一条重排结果，带完整的特征分解 —— 这是"可解释"的落点。"""

    citation: dict
    score: float
    features: dict[str, float] = field(default_factory=dict)

    def explain(self) -> str:
        parts = sorted(self.features.items(), key=lambda kv: -abs(kv[1]))
        body = "  ".join(f"{k}={v:+.2f}" for k, v in parts if abs(v) > 1e-6)
        return f"{self.score:+.3f} | {body}"


# ------------------------------------------------------------------ 特征提取

def _tokens(q: str) -> tuple[set[str], set[str], set[str]]:
    """返回 (标识符, 文件名, 普通词)。"""
    idents = {t for t in RE_IDENT.findall(q)}
    files = set(RE_FILE.findall(q))
    words = set(re.findall(r"[A-Za-z_]\w+|[\u4e00-\u9fff]{2,}", q.lower()))
    return idents, files, words


def _symbol_of(title: str) -> str:
    """从引用标题里取符号名。code 块是 "name (path)"，摘要头是 "name 摘要头(hopN)"。"""
    return re.split(r"[\s(]", (title or "").strip(), maxsplit=1)[0]


def _hop_of(title: str) -> int:
    m = re.search(r"hop(\d+)", title or "")
    return int(m.group(1)) if m else 0


def extract_features(conn: sqlite3.Connection, cit: dict, q: str,
                     fusion_norm: float) -> dict[str, float]:
    idents, files, words = _tokens(q)
    sym = _symbol_of(cit.get("title"))
    f: dict[str, float] = {"fusion": fusion_norm}

    # 精确 / 前缀符号匹配
    if sym and sym in idents:
        f["exact_symbol"] = 1.0
    elif sym and any(sym.startswith(i) or i.startswith(sym) for i in idents):
        f["prefix_symbol"] = 1.0

    # 文件名点名
    src = cit.get("source_ref") or ""
    if files and any(fn in src for fn in files):
        f["file_match"] = 1.0

    # 证据等级
    f["evidence"] = {"A": 1.0, "B": 0.7, "C": 0.3}.get(cit.get("level"), 0.0)

    # 图扩展跳数衰减（hop0 不衰减，越远越低）
    hop = _hop_of(cit.get("title"))
    f["hop_decay"] = 1.0 / (1.0 + hop)

    # 需要查库的特征
    node_id = cit.get("node_id")
    if node_id:
        row = conn.execute(
            """SELECT n.is_definition, COALESCE(s.fan_in,0) fan_in
                 FROM node n LEFT JOIN summary_head s ON s.node_id = n.id
                WHERE n.id = ?""", (node_id,)).fetchone()
        if row:
            f["is_definition"] = float(row["is_definition"] or 0)
            f["importance"] = math.log1p(row["fan_in"] or 0) / math.log(20)

    uid = cit.get("uid")
    if uid:
        row = conn.execute(
            "SELECT title, text FROM chunk WHERE uid = ?", (uid,)).fetchone()
        if row:
            blob = f"{row['title']} {row['text'][:1500]}".lower()
            if words:
                f["term_cover"] = sum(1 for w in words if w in blob) / len(words)
            n_tok = len(row["text"]) / 4
            if n_tok > 600:
                f["len_penalty"] = min((n_tok - 600) / 1200, 1.0)
    return f


# ------------------------------------------------------------------ 重排器

class FeatureReranker:
    """默认实现：手写特征 + 线性加权。无模型依赖，完全可解释。"""

    name = "feature"

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        self.w = dict(WEIGHTS)
        if weights:
            self.w.update(weights)

    def rank(self, conn: sqlite3.Connection, q: str,
             citations: list[dict]) -> list[Scored]:
        if not citations:
            return []
        # 融合分归一化到 [0,1]，避免不同查询之间量纲不可比
        scores = [c.get("score", 0.0) for c in citations]
        lo, hi = min(scores), max(scores)
        span = (hi - lo) or 1.0

        out: list[Scored] = []
        for c in citations:
            fusion_norm = (c.get("score", 0.0) - lo) / span
            feats = extract_features(conn, c, q, fusion_norm)
            contrib = {k: self.w.get(k, 0.0) * v for k, v in feats.items()}
            out.append(Scored(citation=c, score=sum(contrib.values()),
                              features=contrib))
        out.sort(
            key=lambda s: (
                -s.score,
                str(s.citation.get("uid") or s.citation.get("title") or ""),
            )
        )
        return out


class CrossEncoderReranker:
    """可选实现：cross-encoder 打分，效果更好但需要下模型。

    与 FeatureReranker 的关键区别：双塔（向量召回）是把 query 和 doc 分别编码后
    算相似度，**编码时彼此看不见**；cross-encoder 把 (query, doc) 拼在一起过一遍模型，
    能建模细粒度交互，所以更准，但代价是无法预计算、只能对候选集在线打分 ——
    这正是它只能放在重排位置、不能放在召回位置的原因。
    """

    def __init__(self, model: str = "BAAI/bge-reranker-base") -> None:
        from sentence_transformers import CrossEncoder  # 延迟导入
        self.model = CrossEncoder(model)
        self.name = model

    def rank(self, conn: sqlite3.Connection, q: str,
             citations: list[dict]) -> list[Scored]:
        if not citations:
            return []
        texts = []
        for c in citations:
            row = conn.execute("SELECT title, text FROM chunk WHERE uid=?",
                               (c.get("uid"),)).fetchone()
            texts.append(f"{row['title']}\n{row['text'][:1200]}" if row
                         else (c.get("title") or ""))
        scores = self.model.predict([(q, t) for t in texts])
        out = [Scored(citation=c, score=float(s), features={"cross_encoder": float(s)})
               for c, s in zip(citations, scores)]
        out.sort(
            key=lambda s: (
                -s.score,
                str(s.citation.get("uid") or s.citation.get("title") or ""),
            )
        )
        return out


# 结构性查询的意图信号。命中这些词说明用户问的是**图上的关系**，
# 而不是"这段代码是什么"。
RE_STRUCTURAL = re.compile(
    r"谁调用|调用了|被调用|调用链|上层函数|下游|影响|波及|依赖|牵连|回归范围|"
    r"who\s+calls|call(?:er|ee)s?\s+of|impact|affected\s+by|depends?\s+on",
    re.I)


def should_rerank(q: str) -> bool:
    """★ 按查询意图决定要不要做内容重排。

    这是实测逼出来的设计，不是拍脑袋：
    第一版对所有查询一律重排，结果 Recall 从 68.1% 掉到 63.9%。
    拆开看，**变差的 11 题全是结构性查询**（"改 X 会波及谁"这类），
    候选集 35~44 条。

    机制：这类问题的正确答案，是调用图上距离较远、
    但**内容上和 X 完全不像**的函数。而内容特征（精确同名、词覆盖率、
    是否定义）恰恰会把种子函数自己和它的近邻顶上去，把真正的 2 跳答案挤出前十。

    结论：**内容相似度在结构性查询上是错误的排序信号**，
    正确的信号是图距离 —— 而图距离已经编码在融合分里（decay^hop）。
    所以这里的正确做法不是调权重，是**不要重排**。
    """
    return not RE_STRUCTURAL.search(q)


def get_reranker(kind: str = "feature"):
    """工厂。none → 不重排（消融对照组用）。"""
    if kind in ("none", "off", ""):
        return None
    if kind == "feature":
        return FeatureReranker()
    try:
        return CrossEncoderReranker(kind if "/" in kind else "BAAI/bge-reranker-base")
    except Exception as e:
        log.warning("cross-encoder 加载失败，降级为特征重排：%s", e)
        return FeatureReranker()


def apply(conn: sqlite3.Connection, q: str, citations: list[dict],
          reranker, *, top_k: int = 0, explain: bool = False) -> tuple[list[dict], list]:
    """对引用列表重排，返回 (新引用列表, 打分明细)。

    ★ tag 会按新顺序重新编号 —— 否则生成侧引用的 [A1] 和实际第一条对不上。
    """
    if reranker is None or not citations:
        return citations, []
    if not should_rerank(q):
        # 结构性查询：融合分里的图距离已经是正确排序，内容重排只会破坏它
        return citations, []

    ranked = reranker.rank(conn, q, citations)
    if top_k:
        ranked = ranked[:top_k]

    counter: dict[str, int] = {}
    out = []
    for s in ranked:
        c = dict(s.citation)
        lvl = c.get("level", "A")
        counter[lvl] = counter.get(lvl, 0) + 1
        c["tag"] = f"{lvl}{counter[lvl]}"
        c["rerank_score"] = round(s.score, 4)
        if explain:
            c["rerank_explain"] = s.explain()
        out.append(c)
    return out, ranked
