# CodeAtlas

CodeAtlas 是一个面向复杂 C 工程的可信 AI 研发辅助原型。它先用编译器建立代码事实，再让检索和 Agent 在这些事实之上工作：回答能回到固定版本的源码，经验要经过人工审核，代码改变后旧经验会自动失效；没有可靠证据时直接拒答。

它是公开、单机、可复现的求职作品集，不包含内部代码、真实业务会话或历史汇报中的未复现实验数字。

设计借鉴了 [Karpathy 的 LLM Wiki 模式](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)：把部分临时理解沉淀成持久知识。CodeAtlas 针对 C 工程增加编译器事实、固定快照和人工发布边界；不是 `nashsu/llm_wiki` 的 fork 或依赖，也不声称完整复现了模型自主综合与维护。原文的 raw/wiki/schema 三层，不等于这里的文件/模块/仓库三级页面。[来源与差异](docs/design/LLM-WIKI-LINEAGE.md)

## 目前做到什么

查询以不可变 `knowledge_set` 为单位：先冻结源码和实际编译输入，再在 staging 完成事实、Wiki、FTS 和向量构建。校验成功才切换 active；构建失败仍查旧版，一次 Agent 运行不会中途换版本。首次迁移会备份数据库与 Markdown。

```text
compile_commands.json + 固定 Git revision
                    │
                    ▼
 libclang：符号 / USR / 定义哈希 / 代码关系
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
 certain 编译器事实      candidate 排查线索
          │             （不进入确定结论）
          ▼
 SQLite 图谱 + 分层 Wiki + FTS5 / 本地向量
          │
          ▼
 符号 + BM25 + 向量 + certain 图扩展
          │
          ▼
 A/B/C 证据包 ──► 受限 Agent ──► 回答或拒答
                         │
                         ▼
 公开会话 → pending 知识卡 → 人工绑定与审核
                         │
             代码变化 ──┴──► stale / superseded
```

- 代码事实：提取函数、类型、字段、全局变量，以及 `calls`、`includes`、`contains`、`type_use`、`field_access`、`global_ref`。能被编译器确认的关系标为 `certain`；函数指针、取地址和未解析调用保留为 `candidate`。
- 分层知识：为每个纳入索引的 `.c/.h` 文件生成文件页，再汇总模块页和仓库页；页面覆盖职责、入口、certain 调用、显式分支/错误路径、类型/字段/全局引用与 candidate 边界。每次构建校验页面覆盖、Markdown 链接和 `sources` 回链，失去依赖的旧页会变成 `stale` 并退出检索。
- 版本化证据：A 级代码引用返回 repository、revision、USR、符号、文件范围和定义哈希；B 级只来自已审核且锚点仍有效的知识卡；C 级 Wiki 只能作为背景。
- 经验治理：会话先设置目标，再确认候选。批准要求精确锚点、完整依赖、绑定当前材料哈希的 QA 和人工确认。Markdown 保存内容，发布日志保存提交事实；重建必须同时验证两者，不能把任意旧 Markdown 重新发布。回滚代码时仍应用当前审核、失效和替代状态。
- 渐进 Agent：概念、功能和排障题先读 Wiki，再用源码核验；定位、调用链和影响题可直接走结构化工具。最多 6 次调用，只开放 `search_evidence`、`wiki_outline`、`wiki_section`、`resolve_symbol`、`code_read`、`analyze_impact`。非法 JSON、路径越界、伪造引用、超时或接口异常都会留下明确原因并安全回退。
- 无 Key 可运行：解析、Wiki、检索、影响分析、审核、失效和全部离线评测都不依赖模型。API Key 只从环境变量读取，不进入数据库、日志、运行轨迹或网页。

引用合法不代表回答正确。结构化陈述必须匹配已保存的事实；模型自由叙述即使引用真实源码，也只能标为待人工复核。两种结果分开显示、分开评测。

当前知识卡依赖采用“所有已解析实体 + 构建配置”的保守清单，会多触发复审；尚未把最小语义依赖集当成已解决问题。新快照全量解析，Wiki 按输入哈希复用并刷新引用；不宣传最小增量构建或语义等价判定。[本轮实现审计](docs/design/IMPLEMENTATION-AUDIT.md)

## 已复现结果

两套语料都固定到了公开 commit，并通过 `compile_commands.json` 解析；当前正式重跑中严重编译诊断为 0。
cJSON 直接使用 Dave Gamble 的官方 GitHub 仓库；lwIP 的官方开发源是 GNU Savannah，测试拉取使用 `lwip-tcpip/lwip` GitHub 镜像。二者的来源关系、commit 和本地路径统一声明在 `eval/corpora.yaml`，不由脚本临时猜测。

| 语料 | 编译单元 | Wiki（文件 / 模块 / 仓库） | 20 题结果 | 完整方案 vs BM25 | 结构与拒答 |
|---|---:|---:|---:|---|---|
| cJSON `fb16e5c` | 28 | 34 / 3 / 1 | 15/20 | Recall@10 100.0% vs 90.0%，+10pt；MRR +0.271 | 影响 F1 1.0；拒答 100% |
| lwIP `3d896ba` | 124 | 266 / 18 / 1 | 15/20 | Recall@10 100.0% vs 80.0%，+20pt；MRR +0.397 | 影响 F1 1.0；拒答 100% |

每套 20 题都含 10 道代码检索、2 道影响分析、3 道拒答和 5 道经验复用。当前两套代码检索均为 10/10，影响与拒答全部通过，真实检索失败为 0；旧任务报告中的 10 道经验复用题仍标记为 `blocked_by_review`，但这不再表示主数据库缺少 B 卡。两张正式卡已由用户确认并完成 QA、精确锚定和发布；旧任务清单冻结的是早期示例卡标题，其中部分 lwIP 问题也不属于本次默认接口案例。为避免看到结果后改 gold，旧结果保留为 30/40，并单独验证两张当前卡的 Top-10 检索与完整版本引用。统一状态仍是 `foundation_passed=true`、`task_complete=false`、`portfolio_ready=false`。

仓库已加入两条可复现实验：cJSON 用固定的 999/1000/1001 层输入核对解析边界（输入不会跟随宏一起移动），lwIP 覆盖首个/第二个接口加入时 `netif_list`、`netif_default` 与返回值。实验脚本会校验固定 revision，并将输出与受控结果逐行比对。两张知识卡已按固定 `review_bundle_hash` 由用户集中确认，QA 记录为 `Guoshuaiqi / human / formal / passed`，并分别发布到已验证的 active 快照。审核正文、依赖、锚点、实验或 QA 任一变化都会使这次确认失效。完整材料见 [正式知识卡审核包](docs/review/CARD-REVIEW-20260906.md)。

六个固定的 Agent / 知识卡场景也已重跑：工具轨迹合法率、A 级锚点完整率、审核/失效门禁正确率、无证据拒答准确率均为 100%，原始会话进入检索的数量为 0。详见：

- [cJSON 人工任务报告](docs/TASK-EVAL-CJSON.md)
- [lwIP 人工任务报告](docs/TASK-EVAL-LWIP.md)
- [cJSON 六场景闭环](docs/WORKFLOW-EVAL-CJSON.md)
- [lwIP 六场景闭环](docs/WORKFLOW-EVAL-LWIP.md)
- [双语料统一验收](docs/ACCEPTANCE-CJSON-LWIP.md)
- [三类价值证明协议](docs/design/VALUE-PROOF-PROTOCOL.md)
- [会话同源对照（当前未运行模型）](docs/runs/PROOF-KNOWLEDGE-REUSE-20260906-v4.md)
- [真实上游维护变异结果](docs/runs/PROOF-MAINTENANCE-UPSTREAM-20260906-v4.md)
- [维护变异补充单元契约（CC0 合成）](docs/PROOF-MAINTENANCE.md)
- [cJSON 自动回归消融](docs/EVAL-CJSON.md)
- [lwIP 自动回归消融](docs/EVAL-LWIP.md)

当前统一报告是本地未提交运行结果，因此状态只能是 `locally_verified_not_published`。只有实现内容、题集和报告哈希仍与 full 快照一致，工作区干净、当前 commit 已在远端且公开报告 URL 可读取时，`release` 才会标记 `publicly_verified`。

旧检索回归集共 117 题（107 道自动结构题、10 道人工语义题），和新的 40 道正式人工任务分开报告。自动结构题的 gold 来自同一张 `certain` 图，因此不能拿来证明自然语言泛化能力。

这 40 道人工任务也已用于调试与检索调整，属于开发回归集，不是独立留出集。上表只能证明这批固定任务的召回和门禁表现，不能证明加入 Wiki 后模型更理解代码。[三类价值证明协议](docs/design/VALUE-PROOF-PROTOCOL.md) 将 Wiki、会话组织和维护成本分成三个实验；48 道留出题、6 道经验题和 12 个校准样例已经过双 agent 源码核验，但历史未曝光仍只能作为声明，不能由哈希证明。真实模型答案及其双 agent 盲审尚未运行，新实验也不会自动把旧的十道审核阻塞改成通过。

真实上游维护实验已在固定 cJSON/lwIP 副本上运行 24/24 个预登记场景，执行和报告完整性通过。首轮全仓指纹策略暴露了无关变化全部触发复审的问题；改为审核时声明“结论实际消费的函数、宏和配置”后，8 个有效变化全部重验证，8 个无关变化全部保留，语义决策为 16/16（漏放 0、误失效 0）。对照中，仅主锚点哈希为 13/16，漏放 2 个且误失效 1 个；全部失效为 8/16，误失效 8 个。本机本次 16 个场景的依赖指纹计算约 0.81 秒。发布中断和治理回滚场景实际调用了项目的持久化发布日志并完成恢复；该实验仍未测完整解析/索引重建和真人复核成本，因此证明的是这组冻结变更上的失效决策与恢复正确性，不是普遍的维护收益。

## 快速运行

```bash
python -m pip install -e ".[dev]"
pytest -q                         # 仓库内置小型 C fixture；不下载语料、不调用模型

bash demo.sh cjson                # 固定 cJSON：解析、Wiki、索引、消融、20 题人工评测
bash demo.sh lwip                 # 固定 lwIP：同一套正式流程
bash demo.sh workflow             # cJSON + Agent/知识卡 6 场景隔离评测

codeatlas acceptance-eval --mode fast    # PR：测试 + cJSON + cJSON 知识闭环
codeatlas acceptance-eval --mode full    # 定时/手动：cJSON + lwIP 全链路
codeatlas acceptance-eval --mode release # 发布前：只验证已提交 full 快照和公开 URL

codeatlas eval knowledge-reuse          # 无 Key 时冻结并校验两例同源材料
codeatlas eval maintenance --executor upstream \
  --manifest eval/upstream_maintenance.yaml \
  --out docs/PROOF-MAINTENANCE-UPSTREAM.json
```

`acceptance-eval` 同时原子生成 Markdown 与 JSON；任一固定 revision、compile database、构建哈希、Wiki 完整性、40 题门禁、知识闭环或声明校验失败都会返回非零退出码。`release` 输出到独立的 `docs/RELEASE-VERIFICATION.*`，不会覆盖作为依据的 full 报告。

单独运行人工任务：

```bash
codeatlas snapshot build corpus/cJSON corpus/cJSON/compile_commands.json --db data/kb.db --activate
codeatlas snapshot status --db data/kb.db
# build 不带 --activate 只准备新版；snapshot rollback <id> 以当前治理状态重投影旧事实

codeatlas task-eval --db data/kb.db --tasks eval/tasks_cjson.yaml \
  --data-dir data --out docs/TASK-EVAL-CJSON.md --require-approved

codeatlas task-eval --db data/lwip.db --tasks eval/tasks_lwip.yaml \
  --data-dir data/lw --out docs/TASK-EVAL-LWIP.md --require-approved
```

## Agent 与模型

不配置模型时，`auto` 会明确显示 `model_not_configured` 并执行同一组本地只读工具：

```bash
codeatlas session import examples/conversations/cjson-nesting-review.json --db data/kb.db
codeatlas session assess cjson-nesting-review --db data/kb.db
codeatlas agent run "审查 parse_value 的改动影响范围" \
  --session-id cjson-nesting-review --mode auto --db data/kb.db
```

真实证明实验固定使用 OpenCode Go 的 OpenAI-compatible 接口。Key 只在当前终端设置；开始前还需要在账户侧确认 `Use balance` 已关闭，CodeAtlas 无法替你读取或修改这项计费开关：

```bash
export LLM_PROVIDER="opencode-go"
export LLM_BASE_URL="https://opencode.ai/zen/go/v1"
export LLM_MODEL="deepseek-v4-flash"
export LLM_API_KEY="..."
# 可选硬预算；价格必须取当前服务的公开计费口径
export LLM_MAX_REQUESTS="250"
export LLM_MAX_COST_USD="5"
export LLM_INPUT_USD_PER_MILLION="..."
export LLM_OUTPUT_USD_PER_MILLION="..."

codeatlas agent run "parse_value 的影响范围" --mode auto --db data/kb.db

# 先生成同源摘要/知识卡；双审冻结前不能进入经验对照
codeatlas eval knowledge-produce
codeatlas eval knowledge-material-review \
  eval/frozen/knowledge_reuse-v4.pending.json path/to/material-reviews.json

# 统一证明 campaign：24 个独立开发冒烟试次 + 486 个正式答案试次
codeatlas eval proof prepare data/proof/campaign-v1 --max-requests 5000
codeatlas eval proof run --campaign data/proof/campaign-v1 --profile smoke
codeatlas eval proof run --campaign data/proof/campaign-v1 --profile full --resume
codeatlas eval proof review --campaign data/proof/campaign-v1 --out docs/PROOF-REVIEWS.json
codeatlas eval proof report --campaign data/proof/campaign-v1
```

证明 campaign 将开发冒烟和正式留出试次分开：冒烟只检查接口、工具与上下文是否完整，不是正式题的前缀，也不进入效果统计。正式 Wiki 对照 432 个答案试次，经验对照 54 个答案试次。所有回答、双评分和必要仲裁共用持久化 SQLite 账本；每个答案及评分角色使用独立上游 session，失败和不确定调用保留且不自动重跑，套餐耗尽可以断点续跑。按最坏六次答案调用、双评分、全量仲裁和三名评分角色的 12 项校准计算，campaign 会把 4554 次离线调用上界写入 `plan.json`；知识材料生产与复核另计。准确性、安全和规划率统计全部原始试次，只有延迟与 token 可取分布或中位数。每次完整模型输入最多 8,000 估算 token，输出最多 1,200 token；实际 provider token 另计。接口与套餐边界以 [OpenCode Go 官方文档](https://opencode.ai/docs/go/) 为准。

报告把锚点召回、标签合法与答案 rubric 分开；未审准确率为 null，不按零分展示。`eval review <原报告> <评分JSON> <新报告JSON>` 导入带来源的逐点评审并同步 JSON、Markdown；AI 评审只能标为 `ai_reviewed`，不会冒充人审，原始试次也不覆盖。`eval reading` 在相同代码检索底座上比较无 Wiki、Wiki 自由阅读和渐进阅读，输出中性盲审包；旧名 `wiki_flat` 不代表全文平铺。对照采用固定 seed 的成对交错调度，先平均同题重复，再按机制等权汇总并 bootstrap。服务未给出完整 token 时记未测，经验题节省不计入代码题收益。

锚点命中提升不能再解锁答案增益门禁。模型效果声明还需 `--answer-key` 绑定的独立语义金标、完整三次运行、通过校准的双 agent 盲审和必要的第三方仲裁；校准报告必须绑定当前 12 个样例以及实际评分 agent 的 provider、model、agent 和 prompt hash，不能换一个模型后复用旧凭据。这些结果始终标为 `ai_reviewed`，不会冒充人审。正式知识卡仍由用户单独确认，旧 40 题或 8 题冒烟不具备效果发布资格。源码/实现/题集/批准卡哈希变化会使报告过期；无 Key 只显示 `not_run`，不以规则结果代替。真实模型尚未运行。

## 本机交互演示

```bash
codeatlas serve --db data/kb.db --data-dir data
# 浏览器打开 http://127.0.0.1:8000
```

柔和蓝色的单页工作台展示代码事实、三级 Wiki、证据检索、Agent 时间线、会话沉淀、知识卡审核和验收结果。会话区完整呈现“质量评估 → 沉淀目标 → 候选 → accept/edit/correct/skip → pending”；回答可提交 `helpful/incorrect/incomplete` 反馈和错误证据标签，反馈只进入本地待处理队列，不会自动改知识或索引。网页只能导入与当前仓库和 revision 匹配的内置 CC0 合成会话，不能读取任意本地路径，也不能执行 shell、改源码或自行批准知识卡。

## 项目边界

当前版本不做多用户团队服务、生产权限审计、自动监听真实会话、IDE/MCP 插件或自动修改代码。模型是可选的规划与表达层，不是事实来源、审核者或发布者。下一阶段是由用户完成公开 B 卡审核，并在套餐额度可用且不启用余额扣费的前提下运行正式模型 campaign；此前不把经验复用、模型效果或维护摊销收益写成已完成指标。

更多说明：[架构](docs/ARCHITECTURE.md) · [评测口径](docs/EVAL.md) · [Agent 与知识卡](docs/AGENT_WORKFLOW.md) · [面试 STAR](docs/INTERVIEW_STAR.md)
