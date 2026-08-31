"""分块 + 索引构建。

三类 chunk：
  code       A 级 —— 函数源码
  experience B 级 —— 已审核经验（pending 的 visible=0，检索不可见）
  wiki       C 级 —— LLM 生成的 Wiki 段落

向量部分是可插拔的：没有 embedding 模型时用 NullEmbedder，
整个系统退化为 BM25 + 图检索，但必须能完整跑通（架构约束 #4）。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

CODE_CHUNK_MAX_LINES = 160


class NullEmbedder:
    """无模型降级实现。返回空向量，向量召回直接返回 []。"""

    name = "null"
    dim = 0

    def encode(self, texts: list[str]):
        return None


class TfidfSvdEmbedder:
    """本地 TF-IDF + SVD (LSA) 语义通道。不联网、不下模型、几秒建好。

    定位：BM25 之外的第二条语义通道。它不如 BGE 这类预训练模型，
    但能捕捉到词形不同、共现相关的召回（BM25 完全做不到），
    且在离线/内网环境里是唯一可用的向量方案 —— 基带研发环境通常就是这种。
    模型（vocab + SVD 投影）随索引一起持久化，检索时直接加载。
    """

    def __init__(self, dim: int = 192) -> None:
        self.name = f"tfidf-svd-{dim}"
        self.dim = dim
        self._vec = None
        self._svd = None

    def fit(self, texts: list[str]):
        import numpy as np
        from sklearn.decomposition import TruncatedSVD
        from sklearn.feature_extraction.text import TfidfVectorizer
        # 同时用词级和字符级 n-gram：前者抓自然语言，后者抓 C 标识符的子串
        self._vec = TfidfVectorizer(
            analyzer="char_wb", ngram_range=(3, 5), max_features=60000,
            sublinear_tf=True, lowercase=True)
        X = self._vec.fit_transform(texts)
        n = min(self.dim, max(2, min(X.shape) - 1))
        self._svd = TruncatedSVD(n_components=n, random_state=0)
        V = self._svd.fit_transform(X).astype("float32")
        return self._norm(V)

    @staticmethod
    def _norm(V):
        import numpy as np
        d = np.linalg.norm(V, axis=1, keepdims=True)
        return V / np.maximum(d, 1e-9)

    def encode(self, texts: list[str]):
        if self._vec is None:
            return None
        return self._norm(self._svd.transform(self._vec.transform(texts)).astype("float32"))

    def save(self, out_dir):
        import pickle
        with open(Path(out_dir) / "tfidf_model.pkl", "wb") as f:
            pickle.dump({"vec": self._vec, "svd": self._svd, "dim": self.dim}, f)

    @classmethod
    def load(cls, out_dir):
        import pickle
        f = Path(out_dir) / "tfidf_model.pkl"
        if not f.exists():
            return None
        with open(f, "rb") as fh:
            d = pickle.load(fh)
        e = cls(d["dim"]); e._vec, e._svd = d["vec"], d["svd"]
        return e


class SentenceTransformerEmbedder:
    """可选：bge-small-zh-v1.5 约 100MB，CPU 可跑。"""

    def __init__(self, model: str = "BAAI/bge-small-zh-v1.5") -> None:
        from sentence_transformers import SentenceTransformer  # 延迟导入
        self.model = SentenceTransformer(model)
        self.name = model
        self.dim = self.model.get_sentence_embedding_dimension()

    def encode(self, texts: list[str]):
        import numpy as np
        v = self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(v, dtype="float32")


def get_embedder(kind: str = "null", data_dir: str = "data"):
    if kind in ("null", "none", ""):
        return NullEmbedder()
    if kind == "tfidf":
        return TfidfSvdEmbedder.load(data_dir) or TfidfSvdEmbedder()
    try:
        return SentenceTransformerEmbedder(kind if "/" in kind else "BAAI/bge-small-zh-v1.5")
    except Exception as e:
        log.warning("加载 embedding 模型失败，降级为 null：%s", e)
        return NullEmbedder()


def _read_span(repo: Path, path: str, a: int, b: int) -> str:
    f = repo / path
    if not f.exists():
        return ""
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    b = min(b, a + CODE_CHUNK_MAX_LINES)
    return "\n".join(lines[a - 1:b])


def _identifier_expand(text: str) -> str:
    """把 nr_pusch_decode 这类标识符额外拆成子词，提升 BM25 召回。

    unicode61 tokenchars '_' 会把整个标识符当一个 token，
    用户搜 "pusch" 时匹配不上，所以补一份拆词。
    """
    ids = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", text))
    parts = set()
    for i in ids:
        for p in re.split(r"[_]+|(?<=[a-z0-9])(?=[A-Z])", i):
            if len(p) >= 3:
                parts.add(p.lower())
    return " ".join(sorted(parts))


def build(conn: sqlite3.Connection, repo: str, *, embedder_kind: str = "null",
          out_dir: str = "data") -> dict:
    """重建全部索引。幂等：先清空 chunk 再重建。"""
    from ..summary import head as head_mod

    repo_p = Path(repo)
    # ★ external content FTS5 表不能用 DELETE FROM 清空（会让索引与内容表脱节，
    #   后续操作直接报 "database disk image is malformed"），必须用特殊命令。
    conn.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('delete-all')")
    conn.execute("DELETE FROM chunk")

    rows: list[dict] = []

    # ---- A 级：函数源码 ----
    for fn in conn.execute(
            "SELECT * FROM node WHERE kind='function' AND is_definition=1"):
        body = _read_span(repo_p, fn["path"], fn["line_start"], fn["line_end"])
        if not body.strip():
            continue
        h = head_mod.get(conn, fn["id"])
        head_txt = head_mod.render(h, fn["name"]) if h else ""
        rows.append(dict(
            uid=f"code:{fn['id']}", node_id=fn["id"], kind="code",
            title=f"{fn['name']} ({fn['path']})",
            text=f"{head_txt}\n{fn['signature'] or ''}\n{body}\n{_identifier_expand(fn['name'])}",
            source_ref=f"{fn['path']}#L{fn['line_start']}-L{fn['line_end']}",
            evidence_level="A", visible=1))

    # ---- B 级：经验条目（★ pending 的 visible=0，检索完全不可见）----
    for ex in conn.execute("SELECT * FROM experience"):
        visible = 1 if ex["status"] == "approved" else 0
        text = "\n".join(filter(None, [
            ex["symptom"], ex["root_cause"], ex["fix_steps"], ex["verification"],
            " ".join(json.loads(ex["dead_ends"] or "[]")),
        ]))
        rows.append(dict(
            uid=f"exp:{ex['id']}", node_id=None, kind="experience",
            title=ex["title"], text=text, source_ref=ex["source_ref"] or "",
            evidence_level="B", visible=visible))

    # ---- C 级：Wiki ----
    for w in conn.execute("SELECT * FROM wiki_page WHERE status='ok'"):
        rows.append(dict(uid=f"wiki:{w['id']}", node_id=w["node_id"], kind="wiki",
                         title=w["title"], text=w["md"] or "",
                         source_ref=f"wiki/{w['id']}", evidence_level="C", visible=1))

    conn.executemany(
        """INSERT INTO chunk(uid,node_id,kind,title,text,source_ref,evidence_level,visible)
           VALUES(:uid,:node_id,:kind,:title,:text,:source_ref,:evidence_level,:visible)""",
        rows)
    # external content FTS5：手动同步，只索引 visible 的
    conn.execute(
        "INSERT INTO chunk_fts(rowid,title,text) "
        "SELECT rowid,title,text FROM chunk WHERE visible=1")
    conn.commit()

    # ---- 向量索引（可选）----
    emb = get_embedder(embedder_kind, out_dir)
    vec_n = 0
    if not isinstance(emb, NullEmbedder):
        import numpy as np
        vis = conn.execute(
            "SELECT rowid, uid, title, text FROM chunk WHERE visible=1 ORDER BY rowid").fetchall()
        texts = [f"{r['title']}\n{r['text'][:1200]}" for r in vis]
        if isinstance(emb, TfidfSvdEmbedder):
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            mat = emb.fit(texts)          # 语料自建，无需下载
            emb.save(out_dir)
        else:
            mat = emb.encode(texts)
        if mat is not None and len(mat):
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            np.save(Path(out_dir) / "vectors.npy", mat)
            (Path(out_dir) / "vec_ids.json").write_text(
                json.dumps([r["rowid"] for r in vis]), encoding="utf-8")
            vec_n = len(mat)

    return {
        "chunks": len(rows),
        "visible": sum(1 for r in rows if r["visible"]),
        "hidden_pending": sum(1 for r in rows if not r["visible"]),
        "vectors": vec_n,
        "embedder": emb.name,
    }


def refresh_visibility(conn: sqlite3.Connection) -> None:
    """经验条目审核状态变更后，同步 chunk 可见性与 FTS 索引。

    ★ 这是 D4「审核闸门」在检索层的落点。
    """
    conn.execute("""
        UPDATE chunk SET visible = (
            SELECT CASE WHEN ex.status='approved' THEN 1 ELSE 0 END
              FROM experience ex WHERE 'exp:' || ex.id = chunk.uid)
         WHERE kind='experience'""")
    conn.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('delete-all')")
    conn.execute("INSERT INTO chunk_fts(rowid,title,text) "
                 "SELECT rowid,title,text FROM chunk WHERE visible=1")
    conn.commit()
