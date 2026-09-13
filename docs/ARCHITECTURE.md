# CodeAtlas 架构

## 设计目标

CodeAtlas 要解决的不是“让模型读更多代码”，而是“让一次代码诊断有可靠事实、明确权限和可复核出处”。核心链路保持本地单用户：

```text
固定仓库 + compile_commands.json
              │
              ▼
      libclang AST / 增量解析
              │
    node + certain/candidate edge
              │
      ┌───────┴────────┐
      ▼                ▼
  分层 Wiki         A 级代码块
      │                │
      └──────┬─────────┘
             ▼
symbol / BM25 / local vector / certain graph
             │
             ▼
     A/B/C 证据注册表或拒答
             │
             ▼
   受限 Agent（模型优先，可见回退）
             │
       用户显式保存草稿
             ▼
会话质量门禁 → pending 卡 → 精确绑定 → 人工审核
                                      │
                         parse 后自动失效 / 替代
```

## 0. 构建与查询版本

控制库保存 active 指针、人工资产和发布日志；每个 knowledge_set 保存独立的事实 SQLite/FTS、向量与 ID 映射、Wiki 和冻结源码。新版本在 staging 构建、完整校验后原子切换，构建中继续提供旧版查询。Agent、CLI 只读查询、Web 请求和整次正式评测固定单一快照。缓存与向量目录随快照隔离。

构建前备份旧数据库和知识目录。实际消费的外部头文件冻结为 VFS overlay；严格构建核对消费源码的 HEAD blob。激活受本地单写锁、base_active 和 governance_revision 约束。回滚生成新治理投影，不直接复活历史 B 卡。

## 1. 编译器事实层

解析输入必须记录 repository、Git revision 和编译参数。正式 cJSON/lwIP 指标只来自 compile database；目录扫描仍可用于诊断，但数据库会标记 `degraded=1`，不得混入正式报告。

| 关系 | 证据 | 用途 |
|---|---|---|
| `calls` | AST 可解析到具体函数声明时为 `certain` | 调用链、图扩展、影响分析 |
| `calls` | 函数指针、取地址、未解析目标为 `candidate` | 只作人工排查线索 |
| `includes` / `contains` | 预处理器与 AST | 文件依赖、知识组织 |
| `type_use` | AST `TYPE_REF` | 类型使用事实 |
| `field_access` | AST `MEMBER_REF_EXPR` | 字段访问事实 |
| `global_ref` | AST 全局变量引用 | 全局状态引用事实 |

后三类关系只在目标可解析且属于当前仓库时写入，并统一标记 `certain`。它们不是控制流或数据流分析；影响分析仍然只沿 `calls/certain` 反向遍历。

节点保存 USR、文件范围、签名和函数定义内容哈希。独立编译目标中的多个 `main` 会使用“USR + 文件路径”的内部身份，避免 fuzz 入口和测试入口互相覆盖；对普通声明仍保留 USR 去重，使头文件声明和源文件定义保持同一身份。

## 2. 分层代码知识

函数规则摘要头控制在 200 token 内。Wiki 自底向上生成，并覆盖所有纳入索引的 C/C++ 源文件：

- 文件页：职责与边界、入口/接口、certain 调用、显式条件/早返回/错误路径、include、类型/字段/全局引用，以及 candidate 与材料不足说明；没有函数定义的头文件也必须生成；
- 模块页：包含文件、关键入口和严格限定在当前模块文件集合内的跨文件调用；cJSON/lwIP 使用显式映射，根目录统一为 `root`；
- 仓库页：模块总览、高扇入入口、构建快照和事实边界。

每页保存 repository、revision、`build_config_hash`、输入哈希、源码范围和状态。文件页哈希包含函数定义哈希、摘要、分支、调用与引用事实；模块页包含子文件页哈希；仓库页包含子模块页哈希。因此实现或固定构建配置变化会按 `file → module → repo` 传播。构建结束必须通过全文件覆盖、Markdown 本地链接和 `sources` 回链校验；缺页或断链直接失败。可信模型输出只可重排已有事实块；新增谓词或条件即使配有真实引用也不接受。五类局部骨架（guard、branch_set、decider、error_path、state_write）保存条件和范围；隐式 else、嵌套条件、case 标签和循环组成可核查。switch fallthrough 与别名影响不做证明，非零返回也不直接判错。

## 3. 检索和证据等级

召回通道包括符号精确、FTS5/BM25、确定性 local-hash 字符/标识符向量和 certain 图扩展，使用 RRF 与可解释特征重排组合。离线中英术语归一只包含通用编程/网络词，不包含任务金标符号。向量是低权重补充通道，不能用任意近邻制造证据；完全没有词法、符号或可靠关系支撑时，系统仍然拒答。BGE-M3 只保留为可选消融，未达到语义 Recall +5pt 且整体 MRR 下降不超过 0.02 前不会进入默认方案。

| 等级 | 内容 | 是否可单独支撑结论 |
|---|---|---|
| A | 当前固定代码快照中的函数定义与编译器事实 | 仅支持事实本身，不自动证明自由叙述 |
| B | 人工批准、依赖和审核材料均有效的知识卡 | 仅限审核结论及其适用条件 |
| C | 规则生成或仅重排已有事实块的 Wiki 背景页 | 否 |

每条 A/B 引用进入统一证据注册表，并携带来源类型、repository、revision、USR、符号、文件范围和 stale 状态。没有 A/B 级证据时，在模型调用之前结束并返回拒答。

## 4. 会话与知识卡

公开会话只保存到 `conversation_session` / `conversation_message`，从不转成检索 chunk。导入时校验来源类型、许可证、仓库、固定 revision、内容哈希和敏感字段；提炼前再检查可复现输入、验证步骤和会话结构。审核者先设置“沉淀目标”，系统再生成候选列表；候选必须由人执行 `accept/edit/correct/skip`，其中 correct 保存前后 diff，skip 必须记录原因。

知识卡状态为：

```text
draft → pending → approved / rejected → stale → superseded
```

未确认候选与 pending 草稿写在隔离 drafts 目录，不进入索引。批准前必须至少有一条通过的 QA，并绑定一个带 `explains/warns/fixes/constrains` 关系的主锚点：

```text
repository + revision + USR + path + line range + definition_hash
```

卡片内容、锚点、依赖、实验及 QA 定义共同生成 review_bundle_hash。绑定或材料变化后旧 QA、确认失效；不同版本的审批请求返回冲突。知识卡先持久化 prepared 日志，再原子更新 Markdown，最后提交数据库和 committed 状态；异常恢复完整前态或后态。重建同时检查 canonical Markdown 与已提交日志，旧文件不能越过门禁。

卡片记录所有已解析实体和构建配置，以保守重验证覆盖宏、类型和下层函数变化；Wiki 记录文件事实、子页摘要依赖。传播使用 visited 工作列表，深度 2、30%/500 阈值触发全量重验证，事件保存依赖路径与前后哈希。当前新快照全量解析，依赖粒度仍偏粗。相同字节的源码/Wiki 可按内容哈希复用；尚未物理分离 Wiki 正文与位置引用对象。

## 5. 受限 Agent 与模型回退

Agent 支持 `auto | model | rule`：

- `auto`：检测到模型配置时优先走模型，否则显示 `model_not_configured` 并使用规则路径；
- `model`：强制尝试模型，但任何失败仍进入安全回退；
- `rule`：完全确定性的本地工具路径。

Agent 工具按三阶段记录：`discover → understand → verify`。概念、功能、排障类问题必须先执行 `search_evidence → wiki_outline → wiki_section`，之后才允许读源码；定位、调用链、影响分析可直接走符号与图。Wiki 不可用时允许源码降级，并记录 `wiki_unavailable`。模型每一步只能返回：

```json
{"action":"tool","tool":"search_evidence","arguments":{"query":"..."}}
{"action":"finish"}
```

白名单工具为 `search_evidence`、`wiki_outline`、`wiki_section`、`resolve_symbol`、`code_read`、`analyze_impact`，总尝试数最多 6，包含预检和失败；完整请求最多 8,000 估算输入 token、1,200 输出 token，provider 实际 token 另计。`code_read` 只读当前固定仓库内已解析的 C/H 源码，单次最多 300 行并拒绝路径穿越。每次结果进入统一证据注册表；运行轨迹保存阶段、请求/实际模式、fallback reason、模型名、prompt 版本、参数、结果、引用和耗时。

最终模型回答只能引用本次固定快照注册表中的 A/B/C 标签，仍要求至少有 A/B。引用合法、结构事实匹配和自由回答正确性分别记录，后者未经 rubric 人工复核只能显示待审。非法工具、额外参数、重复调用、无效 JSON、伪造引用、超时和接口异常都不会越过本地校验，而是回退到证据包。Agent 没有 shell、网络工具、源码写权限和审核权限。

模型配置只从环境读取：`LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`。能力接口只返回是否配置、模型名和脱敏服务 origin，不返回 Key。

## 6. 评测边界

- 117 道旧检索回归题中有 107 道自动结构题；其 gold 与 certain 图同源，报告必须拆分题型解释循环性。
- 40 道人工题（cJSON/lwIP 各 20）绑定固定源码或知识卡锚点、判定规则和审核理由，平衡定位、关系/功能、排障/影响与经验复用。
- 6 个闭环场景验证 Wiki-first、工具权限、A 级锚点、审核/QA/失效/替代、Markdown 重建和拒答；当前这些门禁均为 100%，原始会话入索引为 0。
- 默认本地向量下，cJSON/lwIP 10 道代码检索的完整方案 Recall@10 分别相对 BM25 提升 10pt/20pt，MRR 均上升；每套 15/20，总计 10 道经验复用题仍等待真实人工批准卡。
- 可选真实模型按 A（迭代 Agent+Code）、B（同一 Agent+固定 Skill+Code）、C（Agent+CodeAtlas+Code）对照，三组共用模型、预算和限制；先以 8 道类型均衡样本各跑 1 次冒烟，再对完整题集每题运行 3 次；安全与正确性保留全部试次，只有延迟和 token 可取中位数。新增同四路检索的源码/Wiki 平铺/Wiki 渐进阅读消融，顺序遵守率不代表理解增益。冒烟报告不可发布，没有完整报告时只显示“尚未运行”。

回答反馈只保存 `helpful/incorrect/incomplete`、可选错误证据标签和说明到本地待处理队列。反馈不会触发知识改写、索引更新、卡片状态改变或自动审核。

## 7. 双语料统一验收与发布状态

`acceptance-eval` 是现有解析、Wiki、任务评测和知识闭环之上的门禁层，不另造一条检索路径。`fast` 运行离线测试和 cJSON 正确性链路；`full` 再加入 lwIP 规模链路；`release` 不依赖本地语料数据库，而是校验已提交的 full 快照、实现内容哈希、题集/任务/闭环报告哈希与远端可访问性。

发布状态严格区分 `verified`、`locally_verified_not_published`、`publicly_verified`、`contradicted`、`unverifiable` 和 `stale`。实现内容哈希覆盖源码、测试、题集、公开合成会话、知识卡和 CI/复现脚本，但排除生成报告；因此报告单独提交后可以证明被测实现未改变，同时避免用一个已变化的 Git HEAD 冒充原始运行身份。Dashboard 只读取统一 JSON，不从网页启动测试、shell 或网络访问。

## 非目标

当前版本不包含团队中心、多用户权限、真实会话监听、IDE/MCP、自动修改代码或生产部署。它验证的是可信代码事实、受限 Agent 和人工经验治理能否在公开环境中完整工作。
