"""LLM 分支测试（用 mock，不打真实 API）。

【为什么必须有这组测试】
主流程被设计成"无 API key 也能跑"，副作用是 LLM 那条路径在开发中**一次都没被执行过**。
这类代码最危险：平时不跑，一旦演示时配上 key 就是第一次运行，
而且失败点往往在最尴尬的位置（模型返回带 markdown 围栏的 JSON、超时、字段缺失）。

这里用假客户端把每条 LLM 分支都走一遍，重点覆盖三种真实会遇到的异常返回：
  1. 返回被 ```json 围栏包裹        → 必须能剥掉
  2. 返回缺字段 / 类型不对          → 必须被 schema 校验拦住，不能入库
  3. 调用抛异常（超时、限流）       → 必须降级为规则版，不能中断整批
"""
from __future__ import annotations

import json

import pytest

from codeatlas.experience import store
from codeatlas.summary import head
from codeatlas.wiki import generator


class FakeLLM:
    """可编程的假客户端。记录调用次数，便于断言"只对高价值节点调用"。"""

    def __init__(self, *, one_liner_ret="解析并释放 JSON 节点", fail=False,
                 extract_ret=None, fenced=False):
        self.calls = {"one_liner": 0, "extract": 0, "answer": 0, "polish": 0}
        self._one = one_liner_ret
        self._fail = fail
        self._extract = extract_ret
        self._fenced = fenced

    def one_liner(self, ctx):
        self.calls["one_liner"] += 1
        if self._fail:
            raise TimeoutError("模拟超时")
        return self._one

    def answer(self, system, q, context):
        self.calls["answer"] += 1
        if self._fail:
            raise RuntimeError("模拟限流")
        return f"根据证据，答案是 X [A1]。(ctx {len(context)} 字符)"

    def extract_experience(self, messages, schema):
        self.calls["extract"] += 1
        if self._fail:
            raise ValueError("模拟解析失败")
        data = self._extract or {
            "title": "模拟抽取的经验", "symptom": "现象描述",
            "hypotheses": ["假设1"], "dead_ends": ["试过但无效的方案"],
            "root_cause": "根因", "fix_steps": "修复步骤",
            "verification": "验证方式", "related_symbols": ["cJSON_Delete"],
            "confidence": "high",
        }
        if self._fenced:
            # 真实模型经常这么返回，客户端必须能剥围栏
            return json.loads("```json\n" + json.dumps(data, ensure_ascii=False)
                              + "\n```".replace("```", "```"))
        return data

    def polish(self, *a, **k):
        self.calls["polish"] += 1
        return None


# ==================== 摘要头的 LLM 增强 ====================

def test_llm_only_called_for_high_fanin(kb):
    """★ 成本按重要性分配：只有 fan_in >= 阈值的节点才调 LLM。"""
    conn, _ = kb
    llm = FakeLLM()
    total = conn.execute(
        "SELECT COUNT(*) c FROM node WHERE kind='function' AND is_definition=1"
    ).fetchone()["c"]
    eligible = conn.execute(
        "SELECT COUNT(*) c FROM summary_head WHERE fan_in >= 3").fetchone()["c"]

    conn.execute("UPDATE summary_head SET src_hash='force-rebuild'")
    conn.commit()
    head.build(conn, str(kb[1]), use_llm=True, llm_min_fan_in=3, llm_client=llm)

    assert llm.calls["one_liner"] == eligible
    assert llm.calls["one_liner"] < total, "不应该对全量函数无差别调用"


def test_llm_failure_falls_back_to_rule(kb):
    """★ LLM 挂了必须降级，不能中断整批。"""
    conn, repo = kb
    conn.execute("UPDATE summary_head SET src_hash='force-rebuild'")
    conn.commit()
    stats = head.build(conn, str(repo), use_llm=True, llm_client=FakeLLM(fail=True))

    assert stats["total"] > 0, "整批不应被单点失败中断"
    assert stats["llm"] == 0
    missing = conn.execute(
        "SELECT COUNT(*) c FROM summary_head WHERE one_liner IS NULL OR one_liner=''"
    ).fetchone()["c"]
    assert missing == 0, "降级后仍然必须每个函数都有 one_liner"


def test_llm_output_respects_token_budget(kb):
    """LLM 返回超长文本时，定长约束不能被绕过。"""
    conn, repo = kb
    conn.execute("UPDATE summary_head SET src_hash='force-rebuild'")
    conn.commit()
    head.build(conn, str(repo), use_llm=True,
               llm_client=FakeLLM(one_liner_ret="职责说明" * 200))
    mx = conn.execute("SELECT MAX(LENGTH(one_liner)) m FROM summary_head").fetchone()["m"]
    assert mx <= 80, f"LLM 返回的超长 one_liner 没有被截断: {mx}"


# ==================== 经验抽取 ====================

def test_llm_extracted_experience_still_pending(kb, tmp_path):
    """★★ 最关键：LLM 抽出来的经验，照样必须是 pending。

    审核闸门不能因为"是模型抽的、看起来挺像样"就放行。
    """
    conn, _ = kb
    f = tmp_path / "sessions.jsonl"
    f.write_text(json.dumps({"messages": [{"role": "user", "content": "崩溃了"}],
                             "source_ref": "s1"}, ensure_ascii=False) + "\n",
                 encoding="utf-8")

    llm = FakeLLM()
    stats = store.import_jsonl(conn, str(f), llm_client=llm)

    assert llm.calls["extract"] == 1
    assert stats["imported"] == 1

    # ★ 只断言"本次导入的这条"，不能断言全表。
    # 全量跑测试时前面的用例已经审核通过过其它条目，
    # 写成 all(...) 会因为测试间状态污染而假失败 —— 断言范围要跟着被测行为走。
    row = conn.execute(
        "SELECT status FROM experience WHERE source_ref = ?", ("s1",)).fetchone()
    assert row is not None and row["status"] == "pending"


def test_llm_bad_output_rejected_by_schema(kb, tmp_path):
    """★ 模型返回缺字段时必须被拦住，不能带着脏数据入库。"""
    conn, _ = kb
    before = conn.execute("SELECT COUNT(*) c FROM experience").fetchone()["c"]

    f = tmp_path / "bad.jsonl"
    f.write_text(json.dumps({"messages": [{"role": "user", "content": "x"}]}) + "\n",
                 encoding="utf-8")
    # 返回缺 title、且 dead_ends 类型错误
    llm = FakeLLM(extract_ret={"symptom": "有现象但没标题", "dead_ends": "应该是数组"})
    stats = store.import_jsonl(conn, str(f), llm_client=llm)

    assert stats["failed"] == 1
    assert stats["imported"] == 0
    after = conn.execute("SELECT COUNT(*) c FROM experience").fetchone()["c"]
    assert after == before, "校验失败的条目不能落库"


def test_llm_extract_exception_does_not_abort_batch(kb, tmp_path):
    """一条抽取失败，不能让整个导入批次挂掉。"""
    conn, _ = kb
    f = tmp_path / "mix.jsonl"
    lines = [json.dumps({"messages": [{"role": "user", "content": f"case{i}"}]})
             for i in range(3)]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    stats = store.import_jsonl(conn, str(f), llm_client=FakeLLM(fail=True))
    assert stats["failed"] == 3
    assert stats["imported"] == 0   # 全失败但没抛异常出来


def test_import_without_llm_skips_raw_sessions(kb, tmp_path):
    """无 LLM 时，原始会话应被跳过而不是硬报错。"""
    conn, _ = kb
    f = tmp_path / "raw.jsonl"
    f.write_text(json.dumps({"messages": [{"role": "user", "content": "x"}]}) + "\n",
                 encoding="utf-8")
    stats = store.import_jsonl(conn, str(f), llm_client=None)
    assert stats["skipped"] == 1
    assert stats["imported"] == 0


# ==================== 问答生成 ====================

def test_answer_path_with_llm(kb):
    """配了 LLM 时走生成分支，未配时返回证据包 —— 两条路都要能走。"""
    from codeatlas.retrieve import engine
    conn, _ = kb

    llm = FakeLLM()
    r = engine.ask(conn, "cJSON_Delete 做什么", data_dir="data", llm_client=llm)
    assert llm.calls["answer"] == 1
    assert r["answer"] and "[A1]" in r["answer"]

    r2 = engine.ask(conn, "cJSON_Delete 做什么", data_dir="data", llm_client=None)
    assert r2["answer"] is None
    assert r2["evidence_pack"]


def test_refusal_skips_llm_entirely(kb):
    """★ 拒答时不能调 LLM —— 既省钱，也堵死"模型自由发挥"的口子。"""
    from codeatlas.retrieve import engine
    conn, _ = kb
    llm = FakeLLM()
    r = engine.ask(conn, "Kubernetes Ingress TLS 证书怎么轮换", data_dir="data",
                   llm_client=llm)
    assert r["refused"] is True
    assert llm.calls["answer"] == 0, "证据不足时仍然调用了 LLM"


# ==================== Wiki 生成 ====================

def test_wiki_generates_without_llm(kb):
    """无 LLM 时 Wiki 走模板渲染，内容必须是真实事实而非占位符。"""
    conn, repo = kb
    st = generator.generate(conn, str(repo), llm_client=None)
    assert st["file"] > 0
    page = conn.execute(
        "SELECT md, sources FROM wiki_page WHERE level='file' LIMIT 1").fetchone()
    assert "## 职责" in page["md"]
    assert json.loads(page["sources"]), "sources 为空则无法溯源"


def test_wiki_llm_failure_keeps_template(kb):
    """LLM 润色失败时，必须保留模板版本，不能产出空页面。"""
    conn, repo = kb
    conn.execute("UPDATE wiki_page SET input_hash='force'")
    conn.commit()
    st = generator.generate(conn, str(repo), llm_client=FakeLLM(fail=True))
    assert st["failed"] == 0 or st["file"] > 0
    empty = conn.execute(
        "SELECT COUNT(*) c FROM wiki_page WHERE status='ok' AND (md IS NULL OR md='')"
    ).fetchone()["c"]
    assert empty == 0


# ==================== 客户端本身 ====================

def test_client_strips_markdown_fence():
    """★ 真实模型极高频地把 JSON 包在 ```json 围栏里，必须能剥掉。"""
    from codeatlas.llm import client as cl

    raw = '```json\n{"title":"x","symptom":"y"}\n```'
    assert cl.OpenAICompatClient._json(raw)["title"] == "x"
    repeated = '{"action":"finish"}{"action":"finish"}'
    assert cl.OpenAICompatClient._json(repeated) == {"action": "finish"}
    with pytest.raises(json.JSONDecodeError):
        cl.OpenAICompatClient._json('prefix {"title":"x"}')
    with pytest.raises(json.JSONDecodeError):
        cl.OpenAICompatClient._json('{"title":"x"} trailing')
    with pytest.raises(json.JSONDecodeError):
        cl.OpenAICompatClient._json('{"action":"finish"}{"action":"tool"}')


def test_get_client_returns_none_without_key(monkeypatch):
    """★ 无 key 必须返回 None，这是整个降级链条的起点。"""
    from codeatlas.llm import client as cl
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert cl.get_client() is None

    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-shell-key")
    assert cl.get_client() is None

    monkeypatch.setenv("LLM_API_KEY", "sk-fake")
    c = cl.get_client()
    assert c is not None and hasattr(c, "answer")
