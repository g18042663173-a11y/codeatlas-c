# 框架词表对照：CodeAtlas ↔ LangChain / LangGraph

CodeAtlas 的检索、拒答、快照和评测仍然是本地确定性实现。这一层只把同一条流水线画成面试官熟悉的 **State / Node / Edge / checkpoint**，方便对外讲解和个人背诵。不要把 `pip install langgraph` 写成项目已经换成 LangGraph。

运行：

```bash
codeatlas graph explain
codeatlas graph run "cJSON_Delete 在哪里定义" --db data/kb.db
# 可选：pip install -e ".[langgraph]"
codeatlas graph run "cJSON_Delete 在哪里定义" --db data/kb.db --backend langgraph
```

## 为什么可以“往上凑”，但不能整仓重写

| 做法 | 结果 |
|---|---|
| 用 LangChain `RetrievalQA` / 默认 RAG 链换掉 `engine.ask` | 丢掉 certain-only 图扩展、A/B/C 分级、无 A/B 拒答、快照钉住。已测 Recall/拒答数字对不上。 |
| 用 LangGraph 重写 Agent 循环 | 工具白名单、Wiki-first 顺序、最多 6 次调用会变成框架默认 ReAct，本地校验变弱。 |
| 加一层图门面，节点调用现有函数 | 面试能用他们的词提问；评测、拒答、快照不变。 |

设计记录里曾经明确不把 LangGraph 当运行时。现在补的是**对照层**，不是推翻那条约束。

## 一张图

```text
START → route → retrieve → grade ─┬─ no A/B → refuse → END
                                  └─ has A/B → act → generate → END
```

对应命令：`codeatlas graph explain`。

## 八股词表

| 他们说的 | CodeAtlas 里是什么 | 你可以怎么答 |
|---|---|---|
| State | `query / route / evidence / decision / events / answer / trace` | 图状态就是一次提问的中间结果，不是对话历史本身。 |
| Node | `route` `retrieve` `grade` `act` `generate` `refuse` | 每个节点调用仓库里已经存在的函数，不在框架里重写检索。 |
| Edge / conditional edge | `grade → refuse \| act` | 条件边就是 `ab_evidence == 0` 时拒答。 |
| Retriever | `engine.route` + 符号 / BM25 / 向量 / 已审经验 | 多路召回，不是单一 vector store。 |
| Ensemble + RRF | `engine.rrf`，`RRF_K=60` | 融的是排名，不是分数直接相加。 |
| GraphRAG | `graph_expand` 沿 **libclang certain** 边扩 hop | 名字容易撞车：这里不是文档社区图。 |
| Reranker | `retrieve/rerank.py` 特征重排 | 融合之后才看“是不是定义 / 同名 / 扇入”。 |
| Document grader | `ab_evidence` / `refused` | 没有 A/B 就不进生成。C 级 Wiki 只能当背景。 |
| ToolNode / ReAct | `agent._rule_plan` + 6 个只读工具 | 有阶段：discover / understand / verify，不是无限工具环。 |
| checkpoint / MemorySaver | `snapshots.pin` + `knowledge_set_id` | 检查点钉的是源码快照，不是 chat transcript。 |
| callback / tracing | `trace` 节点名 + Agent events | 用来讲清走过哪条边，不替代评测报告。 |

## 建议背的 4 句

1. **检索不是向量库**：符号精确命中、BM25、本地向量、已审核经验先分别召回，再用 RRF 融合，然后只沿 certain 调用边扩 2 跳。
2. **生成前先 grade**：没有 A 级代码事实或 B 级已审经验就拒答；Wiki 是 C 级，不能单独支撑结论。
3. **Agent 是受限图，不是开放 ReAct**：最多 6 次只读工具，概念题先读 Wiki，定位/影响题走结构化工具。
4. **checkpoint 是知识快照**：一次提问钉住一个 `knowledge_set`，代码变了旧卡会 stale，不会在运行中途换版本。

## 简历和口头怎么说

可以写/说：

> 检索与 Agent 按 LangGraph 同构的 State/Node/条件边编排；实现是本地确定性流水线，可选 LangGraph 只做同一张图的编译门面。

不要写：

> 基于 LangChain / LangGraph 构建 RAG 应用。

数字仍然只引用 `engine.ask` 和既有评测报告，不要声称“换成框架后指标上升”。
