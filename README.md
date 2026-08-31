# CodeAtlas

> 面向大型 C 工程的 AI 知识库：Clang AST 代码知识图谱 + 定长检索摘要头 + 混合检索 + 人机协同审核

> **公开边界：**这是基于公开开源语料构建的个人可复现工程，不是华为官方产品，
> 不包含华为内部代码、数据、文档或生产指标。

读一个十万行级的 C 仓库，靠 grep 逐行找效率太低；直接把代码丢给大模型，又会遇到
上下文不足、同名符号混淆、答案无法追溯。CodeAtlas 把代码**离线编译成可查询的事实图谱**，
在线用四路混合检索回答问题，**每个结论都带证据引用，证据不足时拒绝作答**。

```
C 工程 ──libclang──▶ 知识图谱(certain/candidate) ──▶ 摘要头 ──▶ BM25 + 向量索引
                                                                      │
                    四路召回 ─▶ RRF 融合 ─▶ 图多跳扩展 ─▶ 证据分级 ─▶ 带引用回答
                                                                      ▲
        会话/工单 ──▶ LLM 抽取 ──▶ pending ──【人工审核】──▶ approved ─┘
```

## 文档

| 文件 | 内容 |
|---|---|
| [`docs/BRIEFING.md`](docs/BRIEFING.md) | **项目交接说明** —— 交给 AI 协助写简历时粘贴这份 |
| [`docs/PLAN.md`](docs/PLAN.md) | 交付清单、剩余工作、掌握程度自测 |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | 13 条设计决策（ADR）+ 备选方案否决理由 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 完整架构设计 |
| [`docs/LANDSCAPE.md`](docs/LANDSCAPE.md) | 技术对标：SCIP / GraphRAG / CodeQL 等主流方案的位置关系 |
| [`docs/EVAL.md`](docs/EVAL.md) | 评测索引、口径与复现环境 |
| [`docs/EVAL-LWIP.md`](docs/EVAL-LWIP.md) | lwIP 58 题消融报告（自动生成） |
| [`docs/EVAL-CJSON.md`](docs/EVAL-CJSON.md) | cJSON 59 题消融报告（自动生成） |

## 快速开始

```bash
pip install -e ".[dev]"
bash demo.sh          # 主语料 lwIP（135k 行），约 1 分钟跑完全流程
bash demo.sh cjson    # 小语料 cJSON，约 15 秒，快速验证
pytest -q             # 76 项测试
```

`demo.sh` 依次执行：解析 → 增量验证 → 摘要头 → 索引 → 分层文档 → 影响分析 → 消融实验。
**README 里所有数字都由它产出。**

单步执行：

```bash
codeatlas parse corpus/lwip --compile-db corpus/lwip   # 默认增量，--force 全量
codeatlas summary [--use-llm]
codeatlas index --embedder tfidf
codeatlas ask "netif 是怎么注册的"
codeatlas impact netif_add --depth 3
codeatlas serve                                        # http://127.0.0.1:8000
```

**不需要 API key、不需要 GPU、不需要下载模型**，以上命令全部可跑通。
此时系统退化为 BM25 + 图检索，`ask` 返回带引用的证据包而非自然语言回答。
配置 `LLM_API_KEY` 后才启用 LLM 生成与经验抽取。

## 实测结果

### 检索消融（两个语料，各约 60 题）

**lwIP**（135k 行 TCP/IP 协议栈，回调密集）

| 配置 | Recall@10 | MRR@10 | 上下文 token |
|---|---:|---:|---:|
| 仅 BM25 | 64.4% | 0.471 | 2,924 |
| BM25 + 符号精确 | 64.4% | 0.443 | 2,924 |
| BM25 + 符号 + 向量 (RRF) | 70.0% | 0.452 | 3,740 |
| + 图多跳（取函数体） | 71.9% | 0.451 | 5,379 |
| + 图多跳（取摘要头） | **74.5%** | 0.456 | 5,685 |
| + 特征重排 | 74.5% | 0.456 | 5,685 |

> lwIP 上重排数字不变，是因为它 58 题里有 46 题是结构性查询，
> 按意图路由**全部跳过重排** —— 这反而验证了路由生效。
> 重排的收益体现在 cJSON 题集（含 10 道人工语义题）上：65.8% → 66.4%，MRR 0.449 → 0.460。

> 以上数字由 `bash demo.sh` 在 2026-08-31 的 Linux/Clang 环境重跑；
> 完整生成报告见 [`docs/EVAL-LWIP.md`](docs/EVAL-LWIP.md)。
> 演示脚本固定 lwIP `3d896ba`、cJSON `fb16e5c` 语料提交与 `PYTHONHASHSEED=1`；
> 两份题集的 SHA-256 分别以 `ff4f25a2`、`67793cff` 开头。

**cJSON**（3.5k 行，作为小语料对照）

| 配置 | Recall@10 | 上下文 token |
|---|---|---|
| 仅 BM25 | 55.9% | 2,637 |
| + 向量 (RRF) | 58.1% | 3,860 |
| + 图多跳（取函数体） | 59.2% | 5,537 |
| + 图多跳（取摘要头） | 65.8% | 4,936 |
| **+ 特征重排** ★ | **66.4%**（MRR 0.449→0.460） | 4,936 |

> `bash demo.sh cjson` 复现；完整生成报告见
> [`docs/EVAL-CJSON.md`](docs/EVAL-CJSON.md)。

**结论要说准确**：摘要头带来的召回提升在两个语料上一致（lwIP +2.6、cJSON +6.6 个点），
但**上下文 token 的变化方向不一致** —— cJSON 上降 10.9%，lwIP 上反而升 5.7%。

这不是矛盾，是同一个机制的两种表现：摘要头把单节点成本从几百 token 压到 ~90，
**在固定预算下能装进更多节点**。
小语料里可扩展的节点本来就不够，预算装不满，总量就降下来了；
大语料能把预算填满，总量自然持平。

**单位 token 的召回效率**（每千 token 的 Recall 百分点）：
cJSON 从 10.7 提升到 13.3；lwIP 从 13.4 到 13.1，基本持平 ——
大语料上收益全部体现在绝对召回，而不是省 token。
这两个数字方向不同，但指向同一个机制，**如实写出来比只报好看的那个更可信**。

### 分题型消融：一个必须自己先说的方法论问题

总分会骗人。自动生成题的 gold 取自 certain 边，而图扩展走的也是 certain 边 ——
**"图扩展提升召回"这个结论有循环论证的嫌疑**。所以必须拆开看（cJSON，59 题）：

| 题型 | 无图扩展 | 图-摘要头 | 增益 | 金标来自图？ |
|---|---|---|---|---|
| 间接影响(2跳) | 11.7% | 43.2% | **+31.5pt** | 是（循环） |
| 影响面 | 61.7% | 74.4% | +12.7pt | 是（循环） |
| 被调关系 | 65.8% | 76.9% | +11.1pt | 是（循环） |
| 调用链 | 98.0% | 100.0% | +2.0pt | 是（循环） |
| 符号定位 | 100.0% | 100.0% | **+0.0pt** | **否** |
| 语义理解 | 15.0% | 15.0% | **+0.0pt** | **否** |

重排那一行的增益只有 +0.6pt，但**拿到它的过程比数字重要**：
第一版一律重排让 Recall 掉了 5.4 个点，根因是"内容相似度在结构性查询上是错误的排序信号"。
完整过程见 `docs/DECISIONS.md` ADR-14。

**结论要说准确**：图扩展的收益**全部**集中在结构性查询上，
两类非循环题型的增益恰好为零。

这不是缺陷，是符合机制的结果 —— 图扩展提供的本来就是**结构信息**，
它没有理由改善自然语言语义理解。所以正确的表述是：

- ✅ **"图多跳扩展使结构性查询（谁调用了 X / 改 X 影响谁 / 2 跳影响）召回提升 11~31 个点"**
- ❌ ~~"混合检索使整体召回提升 12 个点"~~ —— 这个说法经不起追问

`codeatlas eval` 会自动输出这张表，报告里也会写明哪些题型是循环的。
**让评测工具自己揭短，比等着被人指出来强。**

**语义理解只有 15%** 是当前最实在的短板。成因是向量通道用的是 TF-IDF+SVD，
不具备真正的语义能力。换 `bge-small-zh-v1.5` 后这一行应该会明显改善 ——
这是一个**可验证的假设**，不是辩解。

金标来源：自动生成题的 gold 直接取自编译器确认的 AST 事实（客观可复现），
语义题为人工标注（带主观性，是本评测主要局限）。两套完整报告见
[`docs/EVAL-LWIP.md`](docs/EVAL-LWIP.md) 与
[`docs/EVAL-CJSON.md`](docs/EVAL-CJSON.md)。

### 增量解析

2026-08-31 在 WSL2 Linux 本地 ext4、Clang 18 环境重跑 lwIP 124 个翻译单元：

| 场景 | 重解析 TU 数 | 耗时 |
|---|---|---|
| 首次全量 | 124 | 29.6–35.7s |
| 无任何变更 | 0 | **0.05–0.06s** |

时间受硬件、文件系统和杀毒软件影响，公开简历只引用“无变更时复用全部 124 个 TU”的
机制事实，不把单机耗时外推为生产性能。本轮未重跑单个 `.c` / `.h` 修改基准，旧数字不再作为公开主张。

指纹算的是**依赖闭包**（TU 内所有仓库内头文件的内容哈希 + 编译参数哈希），
不是单文件哈希 —— 否则改头文件不会失效依赖方。

### 解析规模

| | cJSON | lwIP | mbedtls |
|---|---|---|---|
| 源码规模 | 3.5k 行 | **135k 行** | 87k 行 |
| 翻译单元 | 6 | 124 | 65 |
| 编译诊断错误 | 1 | 5 | 84 |
| 全量解析耗时 | 6.4s | 29.6–35.7s | 未在本轮重跑 |
| 图节点 | 259 | **6,397** | 2,744 |
| 函数定义 | 156 | **1,167** | 888 |
| 逻辑边 | 650 | **11,753** | 4,944 |
| 确定边 certain | 630 | 11,471 | 4,564 |
| 候选边 candidate | 20 | 282 | 380 |
| — 函数指针 | 6 | **84** | 6 |
| — 取地址 | 8 | **166** | 5 |
| — 未解析 | 12 | 32 | 369 |
| candidate 占调用边 | 4.6% | **9.7%** | 27.2% |
| USR 去重命中 | 552 | **147,068** | 48,305 |
| 摘要头平均 token | 34.8 | 89.3 | 63.2 |

**主语料选 lwIP** 而不是 cJSON，是因为 D1（区分 certain/candidate）的前提是
"C 工程里函数指针密集"。cJSON 头文件里函数指针声明数为 0，
用它验证等于**拿反例证明论点**。lwIP 是回调驱动的协议栈，头文件里 127 处函数指针声明，
candidate 里函数指针 + 取地址占 250/282 —— 这才是这个设计要解决的场景。

mbedtls 保留作对照：它的 candidate 有 369/380 是 `unresolved`（条件编译宏导致），
和 lwIP 的分布完全不同，说明 candidate 占比本身反映的是代码风格，不能横向比较。

`codeatlas impact netif_add --depth 3` 实测：确定影响面 7 个函数 / 6 个文件。

## 五个核心设计

每条设计的完整论证、备选方案否决理由、以及面试追问应答，见 **[`docs/DECISIONS.md`](docs/DECISIONS.md)**。

### D1 事实与推断分离（certain / candidate）

libclang 能 resolve 到具体 `FUNCTION_DECL` 的调用记为 `certain`；函数指针调用、取地址、
宏展开后才成立的调用记为 `candidate` 并标注 `reason`。
**candidate 不参与调用链查询、变更影响分析和检索上下文**，只在单独一栏展示。

`graph/traverse.py` 里每条 SQL 都显式带 `AND confidence = 'certain'`，不依赖默认值。

> 理由：影响分析的价值完全建立在准确性上。一个会给出错误回归范围的工具，
> 被发现说谎一次就再没人用 —— 它比没有工具更糟，因为它让人基于错误信息做决策。

### D2 定长检索摘要头（≤200 token）

每个函数额外生成：一句话职责 + 被调(≤12) + 调用者(≤8) + 头文件 + 关键类型 + **已审核的踩坑点**。
图多跳扩展时**只取摘要头，不取函数体**，解决"扩得更远"与"塞得进去"的矛盾。

实测平均 34.8 token、最大 155，全部在 200 上限内（有断言测试）。
超限时按 pitfalls → key_types → headers → callers → callees 顺序逐项裁剪。

成本策略：规则版对全量函数生成（免费、秒级）；`--llm` 只对 `fan_in ≥ 3` 的高价值函数
改写摘要 —— **成本按节点重要性分配，不是无差别全量调用**。

### D3 确定性程序管边界，模型只管表达

节点、边、生命周期、schema 全部由 Python 控制。LLM 只做两件事：写自然语言摘要、
从会话抽结构化经验。模型输出必须过 JSON Schema 校验，不合格重试，超限降级为规则版。
**模型永远不能写入事实层。**

### D4 人工审核硬闸门

LLM 抽取的经验一律 `status='pending'`，**审核前在检索中完全不可见** ——
不是靠上层过滤，而是 `chunk.visible=0` 的行根本不进 FTS5 索引。

> 理由：代码事实错了下次解析会覆盖；经验错了会被检索、被信、被写进新代码，
> 再成为下一条经验的输入。错误经验会自我放大，比没有经验库更糟。

### D5 证据分级与拒答

| 级别 | 来源 | 可作结论依据 |
|---|---|---|
| A | 编译器确认的代码事实 | 是 |
| B | 人工审核通过的经验 | 是 |
| C | LLM 生成的 Wiki | 仅背景 |
| D | candidate 关系、未审核内容 | **不进上下文** |

上下文中 A/B 级为 0 时拒绝下结论，只返回排查线索。生成侧强制每句带 `[A1]`/`[B2]` 引用编号。

## 检索流程

```
1. 路由        含 C 标识符/文件名 → symbol 模式，符号召回权重 ×1.5
2. 三路召回    符号精确 / BM25(FTS5) / 向量           各 top-30
3. RRF 融合    score(d) = Σ wᵢ / (60 + rankᵢ(d))
4. 图多跳扩展  沿 certain 边扩 2 跳，decay=0.5，★ 只取摘要头
5. 证据拼装    源码 40% / 摘要头 25% / 经验 20% / Wiki 15%
6. 重排        特征线性打分，可解释；★ 结构性查询按意图跳过
7. 生成        强制引用；A/B 级为 0 → 拒答
```

**为什么用 RRF 而不是加权分数相加**：BM25 分数无界、cosine 有界，量纲不同、分布随查询漂移，
线性加权需要按语料调参。RRF 只用排名，无量纲、免调参、对异常分数鲁棒。

**为什么不用 Neo4j**：单机十万级节点，SQLite 递归 CTE 毫秒级返回（P50 12ms），
且图 / 全文索引 / 业务数据在同一事务内，不用处理跨库一致性。
遍历逻辑全部封装在 `graph/traverse.py`，要换只改一个文件。

## 命令

```
codeatlas init                                     建库
codeatlas parse <repo> [--compile-db DIR] [--force]  解析（默认增量）
codeatlas summary [--use-llm]                      生成摘要头
codeatlas index [--embedder null|tfidf|BAAI/...]   建索引
codeatlas wiki [--level file|module|repo|all]      生成分层 Wiki
codeatlas ask "问题" [--no-graph|--no-vector|--no-bm25]
codeatlas impact <symbol> [--depth 3]              变更影响分析
codeatlas exp import <file.jsonl>                  导入经验（落 pending）
codeatlas exp pending / review <id> approve|reject 审核
codeatlas eval [--gen N] [--out REPORT]            消融实验 → 指定报告
codeatlas stats [--as-json]                        统计
codeatlas serve [--port 8000]                      Web 界面
```

## 测试

```bash
pytest -q        # 76 passed
```

覆盖：双置信度约束、**遍历绝不走 candidate**、USR 去重、摘要头 token 预算、
**审核闸门（pending 检索命中必须为 0）**、拒答、无 LLM/无向量降级、
FTS 注入防护、幂等性、RRF 数学性质、Wiki 断点续写。

## 目录

```
src/codeatlas/
├── db.py                  DDL 与连接
├── parser/
│   ├── compile_db.py      compile_commands.json 加载 + 三级降级
│   └── ast_walker.py      ★ AST 遍历，certain/candidate 判定
├── graph/traverse.py      ★ 递归 CTE 多跳 + 影响分析
├── summary/head.py        ★ 定长摘要头
├── indexer/build.py       分块 + FTS5 + 可插拔向量
├── retrieve/engine.py     ★ 四路召回 + RRF + 图扩展 + 证据分级
├── experience/store.py    ★ 审核闸门
├── wiki/generator.py      分层 Wiki + 断点续写
├── eval/run.py            ★ 评测与消融
├── server/app.py          FastAPI + 单页前端
└── llm/client.py          OpenAI 兼容，无 key 时返回 None

docs/
├── ARCHITECTURE.md        完整架构设计
├── DECISIONS.md           ★ 设计决策 + 面试追问应答
├── EVAL.md                评测索引与统一口径
├── EVAL-LWIP.md           lwIP 评测报告（自动生成）
└── EVAL-CJSON.md          cJSON 评测报告（自动生成）
```

带 ★ 的六个文件是核心，其余是胶水。**先读这六个。**

## 已知局限

诚实地讲，当前版本还有这些问题（详见 `docs/DECISIONS.md` 末尾）：

- **增量解析只覆盖 C 文件粒度**，宏定义变更导致的跨 TU 语义变化未做精确追踪
- **评测集 59 题偏小**，分六类后每类 10 题左右，置信区间宽；且只评检索未评生成质量
- **`unresolved` 类 candidate 占比过高**（mbedtls 上 369/380），基本都是条件编译宏导致，应进一步细分
- **向量通道用的是 TF-IDF+SVD 降级实现**，不是真正的语义嵌入模型，该行数字偏保守
- **符号精确召回未测出增益**（57.5% → 57.3%）—— 因为 FTS5 配了 `tokenchars '_'`，
  BM25 本身已做到标识符精确匹配。它的价值在同名符号消歧场景，cJSON 语料太干净测不出来。
  这一行没有因为数字不好看就删掉
- 人工审核成本高；单用户、无权限、无审计

## 许可

MIT
