# CodeAtlas

> 2026-09-09 收尾状态：历史 510 个 Luna 答案、题目、gold 和报告全部保留。新一轮 12 个公开知识案例已完成 324/324 个 Luna 答案试次，其中 318 次正常返回、6 次传输失败；Sol 通过密封资格集并完成 648 个双盲评分位置与 82 次必要仲裁，但 14 个试次仍因评分结构错误、网络失败或语义分歧没有终局，因此新知识卡收益保持 `insufficient_evidence`。106 组 usage 完整的配对中，知识卡输入 token 相对原会话增加 4.21%，没有观察到压缩收益。Wiki 零付费覆盖清点仍只有 cJSON 38 个合格未曝光机制，低于预登记的每库 40 个，正式 Wiki 实验停在 `coverage_blocked`。详见[正式知识卡审计](docs/runs/curated/PROOF-KNOWLEDGE-REUSE-V2-12CASE-AUDIT.md)、[覆盖报告](docs/VALUE-PROOF-COVERAGE.md)与[证明协议](docs/design/VALUE-PROOF-PROTOCOL.md)。

| 新一轮收益验证状态 | 值 | 含义 |
|---|---|---|
| `engineering_complete` | `true` | 阅读契约、机制 Wiki、12 个公开案例和离线门禁已实现并通过回归 |
| `protocol_ready` | `false` | 知识卡协议已执行；Wiki 留出覆盖仍未达到冻结门槛 |
| `experiments_complete` | `false` | 知识卡 324 个答案已执行，但 14 个评分未决；Wiki 正式批次未启动 |
| `benefit_supported` | `false` | 新知识卡实验仍是证据不足且未观察到 token 压缩；Wiki 尚未正式运行 |

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
- 分层知识：为每个纳入索引的 C/C++ 源文件生成文件页，再汇总模块页和仓库页；页面覆盖职责、入口、certain 调用、显式分支/错误路径、类型/字段/全局引用与 candidate 边界。C++ 方法按函数入库，虚调用只保留静态绑定目标。每次构建校验页面覆盖、Markdown 链接和 `sources` 回链，失去依赖的旧页会变成 `stale` 并退出检索。
- 版本化证据：A 级代码引用返回 repository、revision、USR、符号、文件范围和定义哈希；B 级只来自已审核且锚点仍有效的知识卡；C 级 Wiki 只能作为背景。
- 经验治理：会话先设置目标，再确认候选。批准要求精确锚点、完整依赖、绑定当前材料哈希的 QA 和人工确认。Markdown 保存内容，发布日志保存提交事实；重建必须同时验证两者，不能把任意旧 Markdown 重新发布。回滚代码时仍应用当前审核、失效和替代状态。
- 渐进 Agent：概念、功能和排障题只有在成功取得 Wiki 目录与完整章节后才算完成前置阅读，再用源码核验；空页、失败调用或破碎条件句不会解锁后续阶段。定位、调用链和影响题可直接走结构化工具。最多 6 次调用，只开放 `search_evidence`、`wiki_outline`、`wiki_section`、`resolve_symbol`、`code_read`、`analyze_impact`。工具契约同时生成提示词与本地校验；历史结果按完整 JSON 块取舍，不截断条件或引用。
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

每套 20 题含 10 道代码检索、2 道影响分析、3 道拒答和 5 道经验复用。两套代码检索均为 10/10，影响与拒答通过。旧 10 道经验题绑定早期示例卡，继续保留 `blocked_by_review` 和 30/40，不事后改 gold；这不代表当前没有正式 B 卡。用户已确认的两张卡完成 QA、精确锚定和发布，与它们对应的新版验收为 10/10，`current_release_complete=true`。486 个正式模型答案已完成修订后的复核，未决为 0；统一报告得到本地 `portfolio_ready=true`，表示材料和复核完整，不代表模型效果门禁通过或已经公开复现。

仓库已加入两条可复现实验：cJSON 用固定的 999/1000/1001 层输入核对解析边界（输入不会跟随宏一起移动），lwIP 覆盖首个/第二个接口加入时 `netif_list`、`netif_default` 与返回值。实验脚本会校验固定 revision，并将输出与受控结果逐行比对。两张知识卡已按固定 `review_bundle_hash` 由用户集中确认，QA 记录为 `Guoshuaiqi / human / formal / passed`，并分别发布到已验证的 active 快照。审核正文、依赖、锚点、实验或 QA 任一变化都会使这次确认失效。完整材料见 [正式知识卡审核包](docs/review/CARD-REVIEW-20260906.md)。

六个固定的 Agent / 知识卡场景也已重跑：工具轨迹合法率、A 级锚点完整率、审核/失效门禁正确率、无证据拒答准确率均为 100%，原始会话进入检索的数量为 0。详见：

- [cJSON 人工任务报告](docs/TASK-EVAL-CJSON.md)
- [lwIP 人工任务报告](docs/TASK-EVAL-LWIP.md)
- [cJSON 六场景闭环](docs/WORKFLOW-EVAL-CJSON.md)
- [lwIP 六场景闭环](docs/WORKFLOW-EVAL-LWIP.md)
- [双语料统一验收](docs/ACCEPTANCE-CJSON-LWIP.md)
- [机制型 Wiki 离线重建验证](docs/VALUE-PROOF-WIKI-VALIDATION.md)
- [12 个新公开知识案例](examples/reproductions/knowledge-sources-v2/README.md)
- [三类价值证明协议](docs/design/VALUE-PROOF-PROTOCOL.md)
- [Luna 答案与 V4 评分收口状态](docs/runs/PROOF-LUNA-20260907-STATUS.md)
- [cJSON Wiki 效果对照](docs/runs/curated/PROOF-WIKI-CJSON-V5.md)
- [lwIP Wiki 效果对照](docs/runs/curated/PROOF-WIKI-LWIP-V5.md)
- [知识复用效果对照](docs/runs/curated/PROOF-KNOWLEDGE-REUSE-V5.md)
- [真实上游维护变异结果](docs/PROOF-MAINTENANCE-UPSTREAM-V5.md)
- [维护变异补充单元契约（CC0 合成）](docs/PROOF-MAINTENANCE.md)
- [cJSON 自动回归消融](docs/EVAL-CJSON.md)
- [lwIP 自动回归消融](docs/EVAL-LWIP.md)

当前结果只在本地验证，尚未推送或做外部干净下载复现，因此状态是 `locally_verified_not_published`。只有实现内容、题集和报告哈希仍与 full 快照一致，当前 commit 已在远端且公开报告 URL 可读取时，`release` 才会标记 `publicly_verified`。

旧检索回归集共 117 题（107 道自动结构题、10 道人工语义题），和新的 40 道正式人工任务分开报告。自动结构题的 gold 来自同一张 `certain` 图，因此不能拿来证明自然语言泛化能力。

40 道开发回归不能证明模型理解提升。510 个 Luna 答案保持冻结，Terra/V4A 失败后 Sol/V4B 通过。修正证据后，486 个正式答案全部完成复核。两仓库等权的 Wiki 主对照正确率差值为 +5.84pt，95% 区间 [-2.28,+14.17]pt，不能确认稳定提升；必要要点完整度差值为 +12.50pt，区间 [+4.82,+19.56]pt，是本批固定任务的次指标观察。知识卡对照严格正确为 7/18，原会话为 5/18，但只有两个案例，区间跨零，卡片输入 token 还增加 5.70%。独立于语义评分，432 个 Wiki 试次有 115 个降级为证据包，其中 95 个为规划 JSON 解析失败；这些原始执行记录仍保留，模型效果门禁没有通过。[修订结果与边界](docs/REVIEW-REPAIR-STATUS.md)。

真实上游维护实验已在固定 cJSON/lwIP 副本上运行 24/24 个预登记场景，执行和报告完整性通过。首轮全仓指纹策略暴露了无关变化全部触发复审的问题；改为审核时声明“结论实际消费的函数、宏和配置”后，8 个有效变化全部重验证，8 个无关变化全部保留，语义决策为 16/16（漏放 0、误失效 0）。对照中，仅主锚点哈希为 13/16，漏放 2 个且误失效 1 个；全部失效为 8/16，误失效 8 个。本机本次 16 个场景的依赖指纹计算约 0.83 秒。发布中断和治理回滚场景实际调用了项目的持久化发布日志并完成恢复；该实验仍未测完整解析/索引重建和真人复核成本，因此证明的是这组冻结变更上的失效决策与恢复正确性，不是普遍的维护收益。

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

codeatlas eval knowledge-reuse \
  --manifest eval/knowledge_reuse_v2.yaml \
  --answer-key eval/knowledge_reuse_v2.answers.yaml \
  --out /tmp/codeatlas-reuse-v2.json     # 无 Key：校验 12 案例/36 题，不执行答案
codeatlas eval proof inventory          # 零付费清点未曝光机制；不足时退出码 1
codeatlas eval proof dry-run            # 只算开发/正式请求上界，不读取 Key 或授权
codeatlas eval maintenance --executor upstream \
  --manifest eval/upstream_maintenance.yaml \
  --out docs/PROOF-MAINTENANCE-UPSTREAM-V5.json
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

# 用 LangGraph 词表讲解同一条流水线；默认不依赖 langgraph 包
codeatlas graph explain
codeatlas graph run "cJSON_Delete 在哪里定义" --db data/kb.db

# 已入库的文件 / 模块 / include / 跨文件调用（不重新解析）
codeatlas map --db data/kb.db
codeatlas map file cJSON.c --db data/kb.db
```

对照表见 [框架词表](docs/design/FRAMEWORK-MAPPING.md)。本仓库 Python 模块怎么拆见 [CODEMAP](docs/CODEMAP.md)；追问边界见 [失败边界](docs/FAILURE-BOUNDARY.md)。可选 `pip install -e ".[langgraph]"` 后可用 `--backend langgraph` 编译同一张图；检索、拒答和快照仍走本地实现。

真实证明实验使用 OpenAI-compatible 接口。下面只给出占位配置；Key 必须由运行者在当前终端临时设置，不写入仓库、报告、数据库或日志：

```bash
export LLM_PROVIDER="openai-compatible"
export LLM_BASE_URL="https://provider.example/v1"
export LLM_MODEL="model-id"
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
codeatlas eval proof review --campaign data/proof/campaign-v1 \
  --calibration eval/review_calibration_v4a.yaml \
  --attempt judge-attempt --judge-model model-id --calibration-only
# 仅当冻结校准通过，才允许同一 attempt 进入正式评分
codeatlas eval proof report --campaign data/proof/campaign-v1
```

证明 campaign 将开发冒烟和正式留出试次分开：冒烟只检查接口、工具与上下文是否完整，不是正式题的前缀，也不进入效果统计。正式 Wiki 对照 432 个答案试次，经验对照 54 个答案试次。所有回答、双评分和必要仲裁共用持久化 SQLite 账本；每个答案及评分角色使用独立上游 session，失败和不确定调用保留且不自动重跑，额度耗尽可以断点续跑。每次完整模型输入最多 8,000 估算 token，输出最多 1,200 token；实际 provider token 另计。本轮冻结答案使用 Luna，Terra/V4A 主评资格失败后，Sol/V4B 密封后备通过；评分与恢复链的完整边界见 [实验状态](docs/runs/PROOF-LUNA-20260907-STATUS.md)。

报告把锚点召回、标签合法与答案 rubric 分开；未审准确率为 null，不按零分展示。`eval review <原报告> <评分JSON> <新报告JSON>` 导入带来源的逐点评审并同步 JSON、Markdown；AI 评审只能标为 `ai_reviewed`，不会冒充人审，原始试次也不覆盖。`eval reading` 在相同代码检索底座上比较无 Wiki、Wiki 自由阅读和渐进阅读，输出中性盲审包；旧名 `wiki_flat` 不代表全文平铺。对照采用固定 seed 的成对交错调度，先平均同题重复，再按机制等权汇总并 bootstrap。服务未给出完整 token 时记未测，经验题节省不计入代码题收益。

锚点命中提升不能解锁答案增益门禁。效果结论绑定独立语义 gold、完整三次运行、通过资格集的双 agent 盲审和必要仲裁；校准只对实际 provider、model、agent、prompt 与输入哈希有效。这些结果标为 `ai_reviewed`，不冒充人审。源码、实现、题集或批准卡变化会使报告过期。旧 486 个正式答案均已形成终局；新 12 案例/36 题已完成 324 个 Luna 答案、648 个 Sol 双盲评分位置和 82 个仲裁。初评缺口实际为 7 个 JSON/schema 错误与 6 个网络失败，仲裁缺口为 2 个网络失败，连同语义分歧后共有 14 个试次未决；版本化恢复角色未完成资格响应，且单一替补角色不能安全补齐同一道题的两个独立评分槽，因此没有反复重试挑结果。正式运行所用 v1 评分投影还存在“同文件即关联”的过宽映射，相关引用支持判断不用于效果证明；未来运行已改为要求源码行范围重叠的 v2。正式审计只给出正确率上下界和完整 usage 配对，不能生成确定收益结论。

## 本机交互演示

```bash
codeatlas serve --db data/kb.db --data-dir data
# 浏览器打开 http://127.0.0.1:8000
```

柔和蓝色的单页工作台展示代码事实、三级 Wiki、证据检索、Agent 时间线、会话沉淀、知识卡审核和验收结果。会话区完整呈现“质量评估 → 沉淀目标 → 候选 → accept/edit/correct/skip → pending”；回答可提交 `helpful/incorrect/incomplete` 反馈和错误证据标签，反馈只进入本地待处理队列，不会自动改知识或索引。网页只能导入与当前仓库和 revision 匹配的内置 CC0 合成会话，不能读取任意本地路径，也不能执行 shell、改源码或自行批准知识卡。

## 项目边界

当前版本不做多用户团队服务、生产权限审计、自动监听真实会话、IDE/MCP 插件或自动修改代码。模型是可选的规划与表达层，不是事实来源、审核者或发布者。两张公开 B 卡已完成用户审核并可由发布包重建；历史 486 个正式答案复核已完成且未决为 0，但主收益仍没有得到支持。新的 12 案例知识卡批次已真实执行，但因 14 个评分未决且 token 未下降，主张仍是证据不足；严格 Wiki 留出实验因固定 cJSON 覆盖不足停在 `coverage_blocked`。项目可以展示工程机制、评测纪律与负面结果，不能包装成企业级平台、生产效果或已证明优于直接读代码。

更多说明：[架构](docs/ARCHITECTURE.md) · [评测口径](docs/EVAL.md) · [Agent 与知识卡](docs/AGENT_WORKFLOW.md) · [面试 STAR](docs/INTERVIEW_STAR.md)
