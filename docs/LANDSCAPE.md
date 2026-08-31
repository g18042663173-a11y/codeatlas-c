# 技术对标：这个项目在业界坐标系的什么位置

> 面试时被问"你了解 X 吗",能说出主流方案的名字、它和你做法的异同、
> 以及你为什么没选它 —— 这是深度信号。这份文档就是那张词汇表。
>
> ⚠️ 这个领域迭代很快，具体的模型名和工具版本请在使用前重新确认。
> 下面写的是**思路对标**，思路的保质期比工具长。

---

## 一句话定位

> **本质是 GraphRAG，但图不是 LLM 抽的，是编译器给的。**

这句话值得记住。GraphRAG 最大的软肋是实体和关系靠大模型从文本里抽取，
天然带噪声、不可验证、不可复现。本项目的图来自 Clang AST，是**编译器确认的事实**。
`certain / candidate` 这个双置信度设计，相当于给 GraphRAG 补了一个它原本没有的
**置信度维度**。

这不是模仿，是一个能站住的改进方向。

---

## 逐项对标

### 代码索引与符号标识

| 本项目 | 业界方案 | 关系 |
|---|---|---|
| libclang + `compile_commands.json` | clangd、clang-tidy、scip-clang | 同一套。编译数据库是 LLVM 官方标准 |
| clang USR 做符号唯一标识 | SCIP 的 moniker、LSIF、Kythe 的 VName | 思路一致，都是"跨文件稳定的符号标识符" |
| — | **SCIP**（Sourcegraph 现行标准，取代 LSIF） | 我没做跨语言 schema，只做了 C |
| — | **Kythe**（Google）、**Glean**（Meta） | 大厂内部的代码事实库，规模差几个数量级 |

**精确索引 vs 模糊索引**是这个领域的核心权衡，Sourcegraph 明确讨论过：

- **精确**（libclang / scip-clang）：需要能编译，慢，但符号解析准确
- **模糊**（tree-sitter / stack-graphs）：不需要编译环境，快，跨语言，但会有误报

本项目选精确路线，理由是 `certain` 边的可信度是整个设计的地基。
代价是对构建系统有依赖 —— 所以做了三级降级。

### 调用图与静态分析

| 本项目 | 学术/工业术语 |
|---|---|
| `certain` 边 | must-call / 编译器确认 |
| `candidate` 边 | **may-call**，over-approximation |
| 函数指针不解析、只隔离 | 没做 **points-to analysis** |
| — | **CHA**（类层次分析）、**RTA**（快速类型分析）—— 面向对象语言的调用图构建 |
| — | **Andersen**（子集约束，精确但 O(n³)）、**Steensgaard**（合并式，近线性但粗） |
| — | **SVF**（LLVM 上的现成指针分析框架） |

**必须能说清的三个词**：

- **Soundness（可靠性）**：不漏报。我的 `certain` 集是 unsound 的
  （函数指针调用不在里面），但我把它们显式标成了 `candidate`，没有假装完备。
- **Precision（精确性）**：不误报。我的 `certain` 集精确度高，因为它就是编译器的结论。
- **敏感度维度**：flow-sensitive（考虑语句顺序）、context-sensitive（区分调用上下文）、
  field-sensitive（区分结构体字段）。我的分析这三个都**不**敏感 —— 因为我根本没做指针分析。

> **被追问"为什么不做指针分析"时的答法**：
> "全程序指针分析的代价和这个项目的定位不匹配。而且我认为在这个场景下，
> 把不确定的东西**明确标出来**，比给一个不知道对不对的答案更有用 ——
> 影响分析一旦给出错误的回归范围，工具就失去信任了。
> 如果要做，嵌入式 C 里 90% 的回调是直接赋值（`netif->output = xxx`），
> 做一个字段敏感、流不敏感的目标集计算就能覆盖大部分，
> 不需要上 Andersen 那种完整方案。"

### 图查询

| 本项目 | 业界 |
|---|---|
| SQLite 递归 CTE | **Datalog**（CodeQL、Soufflé）本质是一回事 |
| 两表 + 引用计数 | 图数据库（Neo4j）、事实库（Glean） |

递归 CTE 就是手写的 Datalog 递归查询。CodeQL 把这套做成了 DSL，
让你用声明式语法写"找出所有从 A 可达 B 的路径"。
规模上去之后应该往那个方向走。

### 检索

| 本项目 | 业界 |
|---|---|
| BM25（SQLite FTS5） | Elasticsearch、Lucene，教科书标准 |
| 向量检索 | 双塔模型（bi-encoder）+ ANN 索引（HNSW / IVF） |
| **RRF 融合** | Elasticsearch、Vespa、Azure AI Search 的默认混合检索融合方式 |
| **重排** | cross-encoder（bge-reranker 系列）、Cohere Rerank、learning-to-rank |
| 图扩展进上下文 | **GraphRAG**（微软）、**LightRAG** |
| 摘要头 | GraphRAG 的 entity summary、aider 的 repo map、Contextual Retrieval |

**双塔 vs cross-encoder 是必须能讲清的**：

- **双塔**：query 和 doc 分别编码，编码时**互相看不见**。所以能预计算全部文档向量，
  在线只算一次 query 编码 + ANN 检索。快，但建模不了细粒度交互。
- **cross-encoder**：把 (query, doc) 拼起来过一遍模型，能建模交互，更准。
  但**无法预计算**，只能对候选集在线打分 —— 这就是它只能放在重排位置的原因。

本项目实现了 cross-encoder 接口但默认走特征线性重排，理由是零模型依赖 + 完全可解释
（见 `docs/DECISIONS.md` ADR-14）。

### 增量构建

| 本项目 | 业界 |
|---|---|
| 依赖闭包内容指纹 | **Bazel** 的内容寻址、Nix 的 derivation hash |
| 失效沿依赖传播 | **Salsa**（rust-analyzer 的增量框架）、query-based compiler |
| 边按 TU 引用计数回收 | 增量编译里的 fine-grained dependency tracking |

**rust-analyzer 的 Salsa** 是这个领域最值得看的实现：它把编译过程建模成
带记忆的查询图，改一个文件只重算受影响的查询节点。
我的做法是它的简化版 —— 粒度停在翻译单元，没有做到函数级。

---

## 本项目相对主流的真实差距

诚实清单，面试时主动说：

| 差距 | 主流做法 | 我的现状 |
|---|---|---|
| 向量模型是 TF-IDF+SVD（即 **LSA，1990 年代**） | 代码检索专用嵌入模型 | 已知短板，接口已留 |
| 无查询改写 | 查询扩展、**HyDE**、多查询生成 | 没做，语义题只有 15% 有这个原因 |
| 重排是启发式，非学习 | learning-to-rank、cross-encoder | 权重是先验拍的，不是训的 |
| 无函数指针目标推断 | points-to analysis | 显式隔离为 candidate |
| 规模验证到 13.5 万行 | 大厂到千万行级 | 递归 CTE 在更大规模未测 |
| 单语言（C） | SCIP 支持数十种语言 | 只做了 C |
| 无跨仓 / 无权限 / 无审计 | Sourcegraph、CodeQL 企业版 | 单用户本地 |

---

## 高频"你了解 X 吗"速查

| 被问到 | 一句话接住 |
|---|---|
| **SCIP / LSIF** | Sourcegraph 的代码索引格式，LSIF 是前代、SCIP 是现行。它用 moniker 做跨仓符号标识，和我用 clang USR 一个思路，区别是它有标准化的跨语言 schema，我只做了 C |
| **CodeQL** | 把代码建成关系型事实库，用 Datalog 查询。我的递归 CTE 本质是手写 Datalog，它做成了 DSL 和完整的查询库 |
| **Kythe / Glean** | Google 和 Meta 的代码事实库，思路一致但规模差几个数量级 |
| **GraphRAG** | 微软的图增强 RAG。我的方案就是它，区别是图来自编译器而不是 LLM 抽取，所以可验证 |
| **tree-sitter** | 增量语法解析器，快、跨语言、不需要编译环境，但只有语法没有语义 —— resolve 不了同名符号和宏，所以我选了 libclang |
| **Salsa / rust-analyzer** | 查询式增量框架，我的增量是它的简化版，粒度停在翻译单元 |
| **HNSW / FAISS** | ANN 索引。我数据量小（千级 chunk），numpy 暴力检索够用，接口留了 |
| **RRF** | 倒数排名融合，Elasticsearch 的混合检索默认方案。选它是因为免调参、无量纲 |
| **cross-encoder** | 重排用的交互式模型，比双塔准但无法预计算，只能对候选集在线打分 |
| **HyDE** | 先让模型生成一个假想答案再去检索，缓解 query-document 语义鸿沟。我没做，是语义题偏弱的原因之一 |

---

## 怎么用这份文档

**不要背。** 背出来的对标一问就穿。

正确用法是：挑三个和你项目最相关的（我建议 **SCIP、GraphRAG、tree-sitter**），
去读它们的官方文档或博客各半小时，形成自己的理解。
剩下的知道名字和一句话定位就够了 —— 被问到能说
"这个我了解得不深，只知道它是做 X 的" 比硬答强得多。
