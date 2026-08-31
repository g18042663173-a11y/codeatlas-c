# CodeAtlas — 大型 C 工程 AI 知识库 MVP 架构设计

> v0.1 · 本地优先 / 单用户 / 求职作品级 MVP
> 项目名可自行替换（CodeAtlas / ClangAtlas / BaseKB 均可），下文统一用 `codeatlas`

---

## 0. 一句话定位

**把一个十万行级 C 工程，离线编译成「代码知识图谱 + 分层 Wiki + 已审核经验库」，在线用「符号精确 + BM25 + 向量 + 图多跳」四路混合检索回答开发问题，并给出可追溯的证据引用。**

与你朋友那份实习摘要的关系：

| 摘要里的东西 | 本 MVP 的处置 |
|---|---|
| ClangWiki（代码知识库） | **保留为主干**，完整实现 |
| Secall（会话知识库） | **压缩为一个子模块**「经验库」，只保留 导入→抽取→审核→索引 这条链 |
| LLM-Wiki | 按 Karpathy 原始 pattern 实现（见 §8），不依赖 nashsu/llm_wiki 这个 Tauri 桌面应用 |
| 企业级权限 / 项目隔离 / 审计 | **明确不做**，写进 README 的"后续演进" |

### 关于 nashsu/llm_wiki 这个仓库

它是 Karpathy 提出的 LLM-Wiki 方法论的一个桌面端实现（Tauri + React）。你**不要去改它、也不要依赖它**，它是文档知识库、不是代码知识库，技术栈（Rust/TS）也和你要写的（Python）不匹配。

你真正要借鉴的是它背后的那套 pattern，只有 4 条：

1. **三层结构**：`raw/`（原始事实，只读不改）→ `wiki/`（LLM 生成，可重写）→ `schema.md`（规则约束）
2. **三个操作**：Ingest（写入）/ Query（检索）/ Lint（体检修复）
3. **index.md 做目录，log.md 做操作流水**，Wiki 页面用 YAML frontmatter 记 `sources[]` 做溯源
4. **人负责策展，模型负责维护** — 模型永远不能直接改 `raw/`

这 4 条就是你在面试里讲"什么是 LLM-Wiki"的全部内容。

---

## 1. 范围裁剪（这是本文档最重要的一节）

### ✅ 做（MVP 必须有）

| # | 能力 | 为什么必须有 |
|---|---|---|
| 1 | libclang 解析 C 工程 → 节点/边入库 | 项目地基，也是最硬的技术点 |
| 2 | **certain / candidate 双置信度边** | 唯一能把你和"套壳 RAG"区分开的设计 |
| 3 | 检索摘要头（summary head） | 你自己原话里的核心想法，落地成本低、演示效果好 |
| 4 | 四路混合检索 + RRF 融合 | 面试必问"为什么不只用向量"，这是答案 |
| 5 | 图多跳扩展（沿 certain 边扩 1–2 跳） | 兑现"多跳检索"这个说法 |
| 6 | 变更影响分析 `impact()` | 演示效果最好的一个功能，30 秒能讲清价值 |
| 7 | 经验库：导入 → LLM 抽取 → **人工审核** → 才可检索 | "非单纯 RAG"的落点 |
| 8 | 证据分级 A/B/C/D + 证据不足时拒绝下结论 | 拉开可信度差距，面试加分项 |
| 9 | 分层 Wiki 生成（file → module → repo，3 层够了） | LLM-Wiki 的落地形态 |
| 10 | **固定评测集 + 消融实验表** | 简历上数字的唯一合法来源，**千万别省** |

### ❌ 明确不做（写进 README「非目标」）

- 增量解析 / 文件监听（首版全量重建，接受 10 分钟）
- Neo4j / 独立图数据库（SQLite 递归 CTE 足够，见 §6.4）
- 多用户、权限、审计、项目隔离
- 社区发现 / Louvain（对回答质量几乎无贡献，纯装饰）
- 流式输出、前端花哨可视化
- 4 层 Wiki（"叶子—信道—子系统—仓库"压成 3 层）
- 跨系统符号打通、外部会话格式统一

> **裁剪原则**：简历项目的评分点是 *「设计取舍是否有道理 + 数字是否可信 + 能否讲清楚」*，不是功能数量。功能越多，AI 生成的代码越容易糊、你越讲不清、面试越危险。

---

## 2. 五个核心设计决策（面试就讲这五条）

**D1. 事实与推断分离 —— certain vs candidate**

libclang 从 AST 拿到的调用边标 `certain`；函数指针、宏展开后才成立的调用、同名符号猜测标 `candidate`。
**candidate 边默认不进调用链、不进影响分析、不进检索上下文**，只在 UI 单独一栏展示并标注"需人工确认"。

> 为什么重要：基带 C 代码里函数指针和宏极多，如果不区分，一个错误的调用边会让"影响分析"给出完全错误的回归范围 —— 这类工具一旦被发现说谎一次，就没人用了。

**D2. 检索摘要头 —— 用 200 token 替代 2000 token 的 JSON**

结构化调用关系导出成 JSON 非常冗长，直接塞给模型会瞬间打爆上下文。
所以每个函数额外生成一个 ≤200 token 的定长摘要头：一句话职责 + 被调函数（≤12）+ 调用者（≤8）+ 关键头文件 + 关键宏/类型 + **已审核的踩坑点**。
图多跳扩展时**只取摘要头，不取函数体** —— 这是能扩到 2 跳还不爆上下文的关键。

**D3. 确定性程序管边界，模型只管表达**

节点/边/生命周期/schema 全部由 Python 代码控制，LLM 只做两件事：写自然语言摘要、从会话里抽结构化经验。
模型输出必须过 JSON Schema 校验 + 章节完整性校验，不合格就重试，重试超限就降级为规则生成。
**模型永远不能写入 `raw/`。**

**D4. 人工审核是硬闸门，不是可选项**

LLM 抽出的经验条目一律 `status=pending`，**审核前检索命中率为 0**（这条要写测试用例验证）。
只有 `approved` 的条目才升级为 B 级证据进入检索。这是"专家经验库 ≠ RAG"的唯一实质区别。

**D5. 证据分级 + 拒答**

| 级别 | 来源 | 是否可作为结论依据 |
|---|---|---|
| **A** | 编译器确认的代码事实（AST 节点、certain 边、源码原文） | 是 |
| **B** | 人工审核通过的经验条目 | 是 |
| **C** | LLM 生成且通过校验的 Wiki 段落 | 仅作背景，需标注 |
| **D** | candidate 关系、未审核内容 | **不进上下文** |

上下文中 A/B 级证据为 0 时，**不输出确定性结论**，只输出"证据不足 + 建议排查方向 + 相关符号线索"。

---

## 3. 系统总览

```
┌──────────────────────── 离线流水线（CLI，可重入）─────────────────────────┐
│                                                                          │
│  C 工程 ──cmake──▶ compile_commands.json                                 │
│                          │                                               │
│                          ▼                                               │
│                   ① libclang AST 遍历                                    │
│                          │                                               │
│         ┌────────────────┼────────────────┐                              │
│         ▼                ▼                ▼                              │
│   certain 边       节点(函数/结构体/    candidate 边                      │
│   (AST 确认)        宏/全局/文件)      (函数指针/宏/词法)                  │
│         └────────────────┼────────────────┘                              │
│                          ▼                                               │
│                   ② 图构建 + 度数/深度统计                                │
│                          ▼                                               │
│                   ③ 摘要头生成（规则 + LLM 增强）                          │
│                          ▼                                               │
│         ┌────────────────┼────────────────┐                              │
│         ▼                ▼                ▼                              │
│    ④ BM25(FTS5)    ⑤ 向量索引       ⑥ 分层 Wiki 生成                     │
│                                       (file→module→repo，断点续写)        │
│                                                                          │
│  会话/工单/文档 ──▶ ⑦ LLM 结构化抽取 ──▶ pending ──[人工审核]──▶ approved  │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                              SQLite (kb.db) + vectors.npy + wiki/*.md
                                    │
┌───────────────────────── 在线服务（FastAPI）─────────────────────────────┐
│  /ask       四路召回 → RRF → 图多跳扩展(摘要头) → 证据分级拼装 → 带引用回答 │
│  /impact    反向 BFS(仅 certain) → 影响面 + 回归建议 + candidate 单列      │
│  /symbol    符号卡片：定义/签名/调用链/相关经验/Wiki                       │
│  /wiki      分层 Wiki 浏览                                               │
│  /review    审核队列（通过/拒绝/编辑）                                    │
│  /stats     全量统计（简历数字来源）                                      │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                      单页前端（3 个 Tab：问答 / 图谱 / 审核）
```

---

## 4. 数据模型（SQLite DDL，可直接给 AI）

单文件 `data/kb.db`。**图不用图数据库**，用两张表 + 递归 CTE。

```sql
-- ========== 图层 ==========
CREATE TABLE node (
  id            TEXT PRIMARY KEY,   -- sha1(USR) 或 sha1(repo::path::kind::name::line)
  kind          TEXT NOT NULL,      -- repo|file|function|struct|field|enum|typedef|macro|global|module|wiki|experience
  name          TEXT NOT NULL,
  usr           TEXT,               -- clang USR，跨文件唯一标识（同名符号消歧的关键）
  repo          TEXT,
  path          TEXT,
  line_start    INTEGER,
  line_end      INTEGER,
  signature     TEXT,
  is_definition INTEGER DEFAULT 0,  -- 0=声明 1=定义
  is_static     INTEGER DEFAULT 0,
  extra         TEXT                -- JSON: 条件编译宏、存储类、返回类型等
);
CREATE INDEX ix_node_name ON node(name);
CREATE INDEX ix_node_kind ON node(kind);
CREATE INDEX ix_node_path ON node(path);
CREATE INDEX ix_node_usr  ON node(usr);

CREATE TABLE edge (
  id         INTEGER PRIMARY KEY,
  src        TEXT NOT NULL,
  dst        TEXT NOT NULL,
  kind       TEXT NOT NULL,   -- calls|includes|contains|uses_type|rw_global|documents|about
  confidence TEXT NOT NULL,   -- certain | candidate      ★ D1 核心字段
  reason     TEXT,            -- ast | fn_pointer | macro_expand | name_match
  evidence   TEXT,            -- JSON {file, line, col}
  UNIQUE(src, dst, kind, confidence)
);
CREATE INDEX ix_edge_src ON edge(src, kind, confidence);
CREATE INDEX ix_edge_dst ON edge(dst, kind, confidence);

-- ========== 检索摘要头（★ D2 核心表）==========
CREATE TABLE summary_head (
  node_id      TEXT PRIMARY KEY,
  one_liner    TEXT,     -- ≤40 字中文一句话职责
  callees      TEXT,     -- JSON 数组，≤12，仅 certain
  callers      TEXT,     -- JSON 数组，≤8，仅 certain
  headers      TEXT,     -- JSON 数组，≤8
  key_types    TEXT,     -- JSON 数组
  key_macros   TEXT,     -- JSON 数组（条件编译相关）
  pitfalls     TEXT,     -- JSON 数组，来自 approved 经验条目
  fan_in       INTEGER,
  fan_out      INTEGER,
  generated_by TEXT,     -- rule | llm
  token_len    INTEGER,  -- 断言 ≤ 200，超了截断
  src_hash     TEXT      -- 源码 hash，变了才重生成
);

-- ========== 检索层 ==========
CREATE TABLE chunk (
  rowid          INTEGER PRIMARY KEY,   -- FTS5 external content 需要整型 rowid
  uid            TEXT UNIQUE,
  node_id        TEXT,
  kind           TEXT,   -- code | wiki | experience | doc
  title          TEXT,
  text           TEXT,
  source_ref     TEXT,   -- "path/to/f.c#L120-L188"
  evidence_level TEXT,   -- A|B|C|D    ★ D5
  visible        INTEGER DEFAULT 1     -- pending 经验为 0，审核后置 1
);
CREATE VIRTUAL TABLE chunk_fts USING fts5(
  title, text, content='chunk', content_rowid='rowid', tokenize='unicode61'
);
-- 向量存 data/vectors.npy (float32, L2 归一化) + data/vec_ids.json 做行号→uid 映射

-- ========== 经验库（★ D4）==========
CREATE TABLE experience (
  id            TEXT PRIMARY KEY,
  title         TEXT,
  symptom       TEXT,   -- 问题现象
  hypotheses    TEXT,   -- JSON 数组：诊断假设
  dead_ends     TEXT,   -- JSON 数组：无效尝试（这一项最有价值，别丢）
  root_cause    TEXT,
  fix_steps     TEXT,
  verification  TEXT,   -- 验证方式
  source_type   TEXT,   -- session | issue | doc | manual
  source_ref    TEXT,
  status        TEXT DEFAULT 'pending',   -- draft|pending|approved|rejected
  reviewer_note TEXT,
  model         TEXT,
  prompt_ver    TEXT,
  created_at    TEXT,
  reviewed_at   TEXT
);
CREATE TABLE experience_link (
  exp_id TEXT, node_id TEXT, relation TEXT, confidence TEXT,
  PRIMARY KEY (exp_id, node_id)
);

-- ========== Wiki ==========
CREATE TABLE wiki_page (
  id         TEXT PRIMARY KEY,
  level      TEXT,     -- file | module | repo
  node_id    TEXT,
  title      TEXT,
  md         TEXT,
  sources    TEXT,     -- JSON 数组，LLM-Wiki 的 frontmatter sources[]，溯源用
  status     TEXT,     -- ok | stale | failed
  version    INTEGER DEFAULT 1,
  input_hash TEXT,     -- 输入变了才重生成
  updated_at TEXT
);
CREATE TABLE gen_task (   -- 断点续写：长任务崩了能接着跑
  id TEXT PRIMARY KEY, target_id TEXT, stage TEXT,
  state TEXT,  -- pending|running|done|failed
  attempt INTEGER DEFAULT 0, last_error TEXT, updated_at TEXT
);
```

---

## 5. 离线流水线（每个阶段 = 一条 CLI = 一个可测单元）

```bash
codeatlas init                                    # 建库 + 目录骨架
codeatlas parse    --repo ./corpus/x --db build/compile_commands.json
codeatlas graph    build                          # 度数、闭包、孤儿检查
codeatlas summary  build [--llm]                  # 摘要头：先规则，--llm 才增强
codeatlas index    build                          # 分块 + FTS5 + 向量
codeatlas wiki     gen --level file|module|repo [--resume]
codeatlas exp      import --file sessions/*.jsonl
codeatlas exp      review                         # 交互式审核（也可走 Web）
codeatlas ask      "PUSCH 解调失败可能有哪些原因"
codeatlas impact   --symbol nr_pusch_decode --depth 3
codeatlas eval     run                            # ★ 出简历数字
codeatlas serve    --port 8000
```

**每个阶段都必须可重入**：重复执行结果一致（幂等），中途 Ctrl+C 后再跑能接上（靠 `gen_task` + `input_hash`）。

### ① parse 阶段的 3 个坑（提前告诉 AI）

1. **compile_commands.json 拿不到**：不需要完整编译成功，只要 `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -B build` 配置通过就有这个文件。配置也失败时，降级方案：用 `bear -- make` 或手工构造最小 flags（`-I` 列表 + `-D` 宏）。
2. **头文件被重复解析**：一个 `.h` 被 200 个 `.c` include，会产生 200 份重复节点。**必须用 USR 去重**，这就是 `node.usr` 存在的理由。
3. **libclang 版本与系统 clang 不匹配**：显式 `clang.cindex.Config.set_library_file()` 指向 `libclang.so`，写进 `.env`。

### ③ summary 阶段：先规则，后 LLM

规则版（不花钱、秒级）：`one_liner` 直接用函数注释首行或函数名分词；callees/callers/headers 全部从图里查。
`--llm` 版：只对 **fan_in ≥ 3 的高价值函数**（通常占全量 10–15%）调 LLM 改写 `one_liner`，其余保留规则版。
> 这条设计本身就是一个面试点：**成本按节点重要性分配，而不是无差别全量调用**。

---

## 6. 在线检索与问答（核心算法）

### 6.1 主流程

```python
def ask(q: str, budget_tokens: int = 8000):
    # 1. 查询路由
    mode = "symbol" if RE_C_IDENT.search(q) or q.endswith((".c", ".h")) else "nl"

    # 2. 三路召回，各取 top-30
    r1 = exact_symbol_recall(q)          # node.name / usr / path 精确 + 前缀匹配
    r2 = bm25_recall(q)                  # chunk_fts MATCH ... ORDER BY bm25()
    r3 = vector_recall(q)                # cosine top-k，无 embedding 时返回 []

    # 3. RRF 融合（symbol 模式下 r1 权重 ×1.5）
    fused = rrf([r1, r2, r3], k=60, weights=[1.5 if mode=="symbol" else 1.0, 1.0, 1.0])

    # 4. 图多跳扩展 —— ★ 只取摘要头
    seeds = fused[:8]
    expanded = graph_expand(seeds, hops=2, decay=0.5,
                            edge_kinds=["calls", "includes", "contains"],
                            confidence="certain")     # ★ D1：candidate 不进
    heads = [summary_head(n) for n in expanded]       # ★ D2：不取函数体

    # 5. 证据拼装（按预算分配）
    ctx = assemble(
        code_chunks = 0.40 * budget,   # A 级
        summary_heads = 0.25 * budget, # A 级
        experiences = 0.20 * budget,   # B 级，仅 status='approved'
        wiki = 0.15 * budget,          # C 级
    )

    # 6. 生成 —— ★ D5
    if count_evidence(ctx, levels=["A", "B"]) == 0:
        return insufficient_evidence_response(fused)   # 不下结论，只给线索
    return llm_answer(q, ctx, require_citation=True)
```

### 6.2 RRF 融合

```python
def rrf(rank_lists, k=60, weights=None):
    scores = defaultdict(float)
    for w, lst in zip(weights or [1.0]*len(rank_lists), rank_lists):
        for rank, doc_id in enumerate(lst, start=1):
            scores[doc_id] += w / (k + rank)
    return sorted(scores, key=scores.get, reverse=True)
```
> 为什么用 RRF 而不是加权分数相加：BM25 分数和 cosine 相似度**量纲不同、分布不同**，直接线性加权需要调参且不稳定；RRF 只用排名，无量纲、免调参、对异常分数鲁棒。这是面试标准答案。

### 6.3 引用格式（生成侧约束）

system prompt 里强制：每个结论句末尾必须带 `[A1]` / `[B2]` / `[C3]` 编号，编号对应上下文里的证据块。回答后端做一次正则校验，无引用的结论句直接标灰或触发重试。

### 6.4 图多跳 = SQLite 递归 CTE（不需要 Neo4j）

```sql
-- 从 :root 出发，沿 certain 调用边正向扩 :maxhop 跳
WITH RECURSIVE reach(id, hop) AS (
    SELECT :root, 0
    UNION
    SELECT e.dst, r.hop + 1
    FROM edge e JOIN reach r ON e.src = r.id
    WHERE e.kind = 'calls'
      AND e.confidence = 'certain'
      AND r.hop < :maxhop
)
SELECT DISTINCT id, MIN(hop) AS hop FROM reach GROUP BY id ORDER BY hop;
```
反向影响分析只需把 `e.src`/`e.dst` 对调。

---

## 7. 变更影响分析（演示效果最好的功能）

```
输入：符号名 或 文件路径
输出：
  ├─ 直接调用者          （1 跳，certain）
  ├─ 间接影响            （2–3 跳，certain，按跳数分组）
  ├─ 受影响头文件        （反向 includes 闭包）
  ├─ 受影响模块/目录     （聚合去重）
  ├─ 建议回归范围        （受影响模块 ∪ 相关 approved 经验里的验证步骤）
  └─ ⚠️ 潜在影响（candidate）  单独一栏，标注"函数指针/宏推断，需人工确认"
```

**演示话术**（30 秒）：
> "改这个函数会影响什么？grep 只能告诉你哪些文件出现过这个名字，告诉不了你 2 跳外的调用者、也告诉不了你哪些头文件的使用方要跟着改。而且我把函数指针推断出来的关系单独隔离了 —— 因为这类关系不可靠，混进去会给出错误的回归范围。"

---

## 8. 分层 Wiki 生成（LLM-Wiki 落地形态）

### 目录结构（就是 Karpathy 的三层 pattern）

```
kb/
├── purpose.md          # 这个知识库为谁、解决什么问题（LLM 每次生成都读）
├── schema.md           # 页面类型、必需章节、命名规则（约束模型输出）
├── raw/                # ★ 只读，模型不得写入
│   ├── ast/            # parse 阶段导出的原始事实 JSON
│   └── sessions/       # 导入的会话/工单原文
└── wiki/
    ├── index.md        # 目录（导航入口）
    ├── log.md          # 操作流水（谁在什么时候重建了什么）
    ├── files/          # 叶子层：单文件文档
    ├── modules/        # 模块层
    └── repo.md         # 仓库层总览
```

### 自底向上 + 断点续写

```
files/*.md   ← 输入：文件内所有函数的 summary_head + 文件级 include 关系
modules/*.md ← 输入：该模块下所有 files/*.md 的摘要 + 跨文件调用边
repo.md      ← 输入：所有 modules/*.md 的摘要
```

每层生成前先查 `input_hash`，没变就跳过（**这是省 token 的关键，也是"增量"的最小实现**）。
每页生成后做 **章节完整性校验**：`schema.md` 规定的必需章节（职责 / 关键数据结构 / 对外接口 / 调用关系 / 已知问题）缺一个就重试，重试 3 次仍失败则 `status='failed'` 并记入 `log.md`，不阻塞整批。

每页 frontmatter 必须有：
```yaml
---
level: module
sources: ["files/pusch_decode.md", "node:sha1abc...", "exp:e-0012"]
generated_at: 2026-08-24T10:00:00
model: deepseek-chat
input_hash: 8f3a...
---
```
`sources[]` 就是溯源能力的实现 —— 任何一句 Wiki 结论都能回溯到具体的代码节点或经验条目。

---

## 9. 经验库与人工审核闭环

```
原始输入（会话 JSONL / 工单 / 文档）
   │
   ├─ 预处理：会话去除无效分支，只保留"有效对话路径"（用户确认过的那条链）
   │
   ▼
LLM 结构化抽取 → JSON Schema 校验 → experience(status='pending')
   │                                  chunk(visible=0, level='B')
   │
   ▼
审核界面：逐条 [通过] / [编辑后通过] / [拒绝]
   │
   ▼
approved → chunk.visible=1，进入检索；
         → 反向写入相关函数的 summary_head.pitfalls
```

**抽取用的 JSON Schema**（直接给 AI 用）：

```json
{
  "title": "≤20字",
  "symptom": "问题现象，客观描述",
  "hypotheses": ["排查时提出的假设"],
  "dead_ends": ["试过但无效的方案 —— 这项最有价值，务必抽取"],
  "root_cause": "根因，无法确定则填 null",
  "fix_steps": "修复步骤",
  "verification": "如何验证修复生效",
  "related_symbols": ["涉及的函数/结构体名"],
  "confidence": "high | medium | low"
}
```

**必须写的两个测试**（这是 D4 的证明）：
1. `test_pending_not_retrievable`：插入一条 pending 经验，`ask()` 命中数必须为 0
2. `test_approved_becomes_retrievable`：同一条置为 approved 后，`ask()` 必须命中且证据级别为 B

> 简历里可以写："自动生成的知识条目审核前正式检索命中为 0（有测试用例保证）" —— 这句话比任何形容词都有说服力。

---

## 10. API 接口

```
POST /api/ask              {q, budget?}          → {answer, citations[], evidence_levels, latency_ms}
GET  /api/symbol/{name}    ?repo=                → 符号卡片：定义/签名/调用链/相关经验/Wiki 链接
GET  /api/graph/callchain  ?symbol=&depth=&dir=  → {nodes[], edges[], truncated}
GET  /api/impact           ?symbol=|?file=       → 见 §7
GET  /api/wiki/tree                              → 三层目录
GET  /api/wiki/{page_id}                         → {md, sources[], status}
POST /api/experience/import                      → {imported, pending}
GET  /api/experience/pending                     → 审核队列
POST /api/experience/{id}/review  {action, note} → approve|reject|edit
GET  /api/stats                                  → ★ 简历数字：节点数/边数/certain-candidate比/chunk数/wiki页数/审核通过率
GET  /healthz
```

---

## 11. 目录结构

```
codeatlas/
├── README.md                   # ★ 决定简历项目成败的文件，见 §16
├── pyproject.toml
├── .env.example
├── docs/
│   ├── ARCHITECTURE.md         # 本文档
│   ├── DECISIONS.md            # ADR：D1–D5 每条一页，写清"为什么不选另一个方案"
│   └── EVAL.md                 # 评测方法与结果表
├── src/codeatlas/
│   ├── cli.py                  # typer
│   ├── config.py
│   ├── db.py                   # 连接 + DDL + 迁移
│   ├── parser/
│   │   ├── compile_db.py       # compile_commands.json 加载/降级
│   │   ├── ast_walker.py       # ★ libclang 遍历，产出 certain
│   │   ├── lexical.py          # 函数指针/宏 → candidate
│   │   └── usr.py              # USR 归一化与去重
│   ├── graph/
│   │   ├── build.py
│   │   ├── traverse.py         # 递归 CTE 封装
│   │   └── impact.py
│   ├── summary/head.py         # ★ 摘要头
│   ├── index/
│   │   ├── chunker.py
│   │   ├── bm25.py             # FTS5
│   │   └── vectors.py          # 可插拔 embedder，含 null 降级
│   ├── retrieve/
│   │   ├── router.py
│   │   ├── recall.py
│   │   ├── fusion.py           # RRF
│   │   ├── expand.py           # 图多跳
│   │   └── assemble.py         # ★ 证据分级与预算分配
│   ├── llm/
│   │   ├── client.py           # OpenAI 兼容，重试/超时/成本统计
│   │   ├── prompts/            # 每个 prompt 一个 .md，带版本号
│   │   └── validate.py         # JSON Schema + 章节校验
│   ├── wiki/generator.py
│   ├── experience/{extract,review}.py
│   ├── eval/{dataset.py,run.py}    # ★
│   └── server/{app.py,routes/}
├── web/index.html              # 单页，3 Tab，vanilla JS，不要框架
├── eval/questions.yaml         # ★ 40 题固定评测集
└── tests/                      # pytest，目标 ≥ 40 项
```

---

## 12. 技术选型与降级策略

| 层 | 首选 | 降级方案（配置开关，跑不通就切） |
|---|---|---|
| 语言 | Python 3.11 | — |
| 解析 | libclang (clang.cindex) | tree-sitter-c（无类型信息，全部边降为 candidate，**并在 README 说明**） |
| 存储/图 | SQLite + 递归 CTE | — |
| 关键词 | SQLite FTS5 + bm25() | — |
| 向量 | bge-small-zh-v1.5（~100MB, CPU 可跑） | **null embedder**：返回空召回，系统退化为 BM25+图，**必须能跑通** |
| 向量索引 | numpy 平坦索引（<10 万块够用） | usearch（若量级上去） |
| LLM | OpenAI 兼容（DeepSeek / Qwen / GLM / Ollama） | 全部 prompt 加 `--dry-run`，无 key 时走规则版 |
| Web | FastAPI + Jinja/静态单页 | — |
| CLI | typer + rich | — |

**降级策略本身是加分项**：`--no-llm --no-vector` 必须能完整跑通全流程。面试官/HR 拿到你的仓库，`pip install && codeatlas demo` 能在**没有任何 API key** 的情况下跑出结果 —— 这一点比多做两个功能重要得多。

---

## 13. 语料选择（别小看，选错会卡两天）

分两级：

**L0 冒烟语料（跑测试用）**：`cJSON`（单文件 C，秒级解析）—— 所有单测跑在这上面，CI 友好。

**L1 主语料（出数字用）**，按优先级尝试，**给每个 2 小时上限，超时就切下一个**：

| 优先级 | 项目 | 规模 | 说明 |
|---|---|---|---|
| 1 | **OpenAirInterface5G / openair1** | 十万行级 C | 通信基带，故事最对口；CMake 原生支持；**只需 configure 出 compile_commands.json，不必编译成功** |
| 2 | **mbedtls** | ~10 万行 C | CMake 干净，几乎必成功；保底选项，简历改写成"大型 C 工程" |
| 3 | libuv / nng | 几万行 C | 再保底 |

> 建议：**L1 用 OAI，同时保留 mbedtls 的一套结果**，README 里两组数字都放。这既证明工具的通用性，又保住了"基带"这个故事线。

---

## 14. 评测方案（★ 简历数字的唯一合法来源）

`eval/questions.yaml`，**手工标 40 题**（一天能标完，别偷懒）：

| 类型 | 题数 | 示例 |
|---|---|---|
| 符号定位 | 15 | "nr_pusch_decode 定义在哪、签名是什么" |
| 调用链 / 影响 | 10 | "谁调用了 X？改 X 会影响哪些文件" |
| 语义理解 | 10 | "上行解调的整体流程" |
| 经验类 | 5 | "编译报 undefined reference to X 怎么排查" |

每题标注 `gold_refs`（正确答案应命中的 node_id / chunk uid 列表）。

**指标**：Recall@5、MRR@10、答案引用覆盖率、P50/P95 延迟、单次问答 token 成本。

**消融表（这张表就是你的简历）**：

| 配置 | Recall@5 | MRR@10 | 平均上下文 token |
|---|---|---|---|
| 仅 BM25 | — | — | — |
| BM25 + 向量 | — | — | — |
| BM25 + 向量 + RRF | — | — | — |
| **+ 图多跳（全量函数体）** | — | — | — |
| **+ 图多跳（摘要头）** ★ | — | — | **↓ 大幅下降** |

最后一行的对比是整个项目最有说服力的一句话：**"引入摘要头后，Recall@5 从 X% 提升到 Y%，同时平均上下文长度下降 Z%"**。

---

## 15. 里程碑（AI 辅助，约 12–14 天）

| 阶段 | 天 | 交付 | 验收标准 |
|---|---|---|---|
| M0 骨架 | 1 | CLI + DDL + cJSON 跑通 | `codeatlas parse` 出节点边，测试通过 |
| M1 解析 | 2–3 | libclang + certain/candidate | L1 语料解析成功，统计数字出来 |
| M2 图+影响 | 2 | 递归 CTE + impact | `impact --symbol X` 输出正确，有单测 |
| M3 摘要头 | 1 | summary_head | 100% 函数有头，token_len ≤200 |
| M4 检索 | 2–3 | 四路+RRF+扩展+分级 | `ask` 带引用回答，无 key 也能跑 |
| M5 经验库 | 2 | 抽取+审核+索引 | §9 两个测试通过 |
| M6 Wiki | 1–2 | 三层生成+断点续写 | 中断重启能续，章节校验通过 |
| M7 评测+前端+README | 2 | 消融表 + 单页 + README | 消融表出数字，README 有架构图和 GIF |

**每个阶段必须能独立演示**。M4 结束时项目已经可以写进简历了，M5–M7 是加分。

---

## 16. 交给 AI Agent 的正确姿势

### 通用前置 prompt（每次会话开头都贴）

```
项目：CodeAtlas，Python 3.11，SQLite 单文件存储，本地优先单用户。
架构文档见 docs/ARCHITECTURE.md，严格遵守以下不可协商约束：
1. edge 表必须有 confidence 字段（certain/candidate），candidate 边不得进入
   调用链查询、影响分析、检索上下文。
2. 图多跳扩展只允许取 summary_head，禁止取函数体。
3. LLM 生成的 experience 初始 status 必须是 pending，pending 内容
   在检索中必须完全不可见。
4. --no-llm --no-vector 模式下全流程必须能跑通。
5. 每个 CLI 阶段必须幂等、可中断续跑。
每完成一个模块，同时写 pytest 测试；不要跨阶段一次性写完。
现在只做【阶段 N】：<粘贴下面对应任务>
```

### 逐阶段任务（一次只给一个）

1. **建骨架**：pyproject、typer CLI 空壳、`db.py` 执行 §4 全部 DDL、`init` 命令、连通性测试
2. **compile_db 加载器**：解析 compile_commands.json，处理相对路径/response file，含 3 种降级路径 + 测试
3. **AST walker**：libclang 遍历产出 node/edge(certain)，USR 去重，cJSON 上验证节点数正确
4. **lexical 候选边**：函数指针赋值、宏内调用 → candidate 边，与 certain 严格分离 + 测试
5. **graph/traverse + impact**：递归 CTE 封装，正反向 BFS，深度截断，impact 输出结构 + 测试
6. **summary_head**：规则版先做，token_len 断言 ≤200，`--llm` 只对 fan_in≥3 的节点调用
7. **index**：chunker + FTS5 + 可插拔 embedder（含 null 实现）+ numpy 向量索引
8. **retrieve**：router / 三路召回 / RRF / 图扩展 / 证据分级拼装 / 拒答逻辑，逐个函数写测试
9. **experience**：JSON Schema 抽取 + pending 闸门 + 审核 API，§9 两个测试必须通过
10. **wiki generator**：三层自底向上 + gen_task 断点续写 + 章节校验
11. **eval**：questions.yaml 加载、指标计算、消融开关、Markdown 表格输出
12. **server + web**：FastAPI 路由 + 单页 3 Tab

### 三条铁律

- **一次一个阶段**，不要说"帮我把整个项目写了"。AI 一次生成 3000 行你 review 不动，最后自己讲不清楚 = 面试当场翻车。
- **每个阶段结束你自己跑一遍测试、读一遍核心函数**。你必须能徒手在白板上画出 §6.1 那个流程。
- **DECISIONS.md 自己写，不要让 AI 写**。这份文件是面试官判断"是你做的还是 AI 做的"的主要依据。

---

## 17. 简历怎么写

**一行版（技能栏）**
> CodeAtlas：面向十万行级 C 工程的 AI 知识库，Clang AST 代码图谱 + 四路混合检索 + 人机协同审核

**三行版（项目栏）**
> **CodeAtlas — 大型 C 工程 AI 辅助开发知识库**｜Python / libclang / SQLite(FTS5) / FastAPI
> 基于 Clang AST 构建代码知识图谱（N 万节点 / M 万关系），严格区分编译器确认关系与词法推断关系，后者不参与调用链与变更影响分析；设计定长「检索摘要头」压缩调用上下文，支持图多跳扩展而不撑爆上下文窗口。
> 融合符号精确检索、BM25、向量语义与图关系扩展并以 RRF 排序，引入 A/B/C/D 证据分级与拒答机制；经验知识经 LLM 抽取后需人工审核方可进入检索。自建 40 题评测集，消融实验显示摘要头方案使 Recall@5 提升 X%、平均上下文长度下降 Y%。

### 面试必问 8 题及答法要点

| 问题 | 答法核心 |
|---|---|
| 为什么不直接用向量 RAG？ | C 符号名精确匹配是向量的弱项（`pusch_decode` vs `pusch_decode_ue` 语义几乎相同但完全是两个东西）；且 RAG 答不了"谁调用了它"这类结构性问题 |
| 为什么不用 Neo4j？ | 单机十万级节点，SQLite 递归 CTE 完全够；少一个部署依赖，clone 即用；数据量上去再换，接口已经封装在 `traverse.py` |
| certain / candidate 怎么判定？ | AST 的 CallExpr 且能 resolve 到 definition = certain；函数指针赋值、宏展开后才成立、纯名字匹配 = candidate |
| 摘要头解决什么问题？ | 结构化调用关系 JSON 太长，2 跳扩展直接爆上下文；定长 200 token 的头让"扩得更远"和"塞得进去"同时成立（有消融数据支撑） |
| 人工审核是不是多余？ | 恰恰相反 —— 无审核的经验库会把错误经验放大传播，比没有更糟；我用测试用例保证 pending 内容检索命中为 0 |
| 幻觉怎么处理？ | 证据分级 + 强制引用 + A/B 级证据为 0 时拒答，只给排查线索 |
| 效果怎么衡量？ | 40 题固定评测集，Recall@5 / MRR@10 / 引用覆盖率，四组消融对比 |
| 有什么没做好？ | 全量重建无增量、评测集偏小且我自己标注有主观性、Wiki 生成耗时长、审核成本高 —— **坦率说局限比吹牛加分** |

---

## 18. 风险与砍功能优先级

| 风险 | 应对 |
|---|---|
| OAI 配不出 compile_commands.json | 2 小时上限，切 mbedtls |
| libclang 装不上 / 版本乱 | `.env` 指定 `LIBCLANG_PATH`，README 写清三大平台装法 |
| LLM 生成 Wiki 太慢/太贵 | 只对 fan_in≥3 节点调用 + input_hash 跳过 + 断点续写 |
| 时间不够 | 按此顺序砍：Wiki 生成 → 前端美化 → 向量检索 → 经验库 |
| **绝不能砍** | certain/candidate 区分、摘要头、评测消融表、README |

> 极端情况下，只有 §2 的 D1 + D2 + D5 三条 + §14 的消融表，这个项目依然是一份很能打的简历项目。
> 反过来，功能做了十个但没有评测数字、自己讲不清取舍 —— 那就是又一个"AI 写的 RAG demo"。
