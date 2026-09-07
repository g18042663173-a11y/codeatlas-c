# cJSON 当前正式知识卡验收集（5 题） — 轻量任务评测

> **审核状态：清单标记已审核；仅作开发回归**。任务 gold 由人工维护；本报告展示可执行任务的验证结果，不替代自动消融报告。

> 已用于开发调整，不是独立留出集；召回通过不代表模型理解更好。

## 运行快照

| 项目 | 值 |
|---|---|
| 公开语料 | cJSON 公开源码与公开可复现实验固定快照 |
| 仓库 | `https://github.com/DaveGamble/cJSON` |
| Git revision | `fb16e5cf358798aabb049655975cde8427101056` |
| 解析模式 | compile database |
| 任务清单 | `eval/tasks_current_cjson.yaml` |
| 审核人 / 日期 | Guoshuaiqi / 2026-09-06 |
| 失败规则 | 当前发布卡未命中、不是 approved B 级证据或主锚点 provenance 不完整即失败；不得回写旧 20 题 gold。 |
| 审核状态 | draft 0 / approved 5 |
| 命令 | `codeatlas task-eval --db data/kb.db --tasks eval/tasks_current_cjson.yaml --embedder tfidf --out docs/TASK-CURRENT-CJSON.md` |

## 汇总

### 检索任务（0 题；逐层消融）

| 配置 | Recall@10 | MRR@10 | 命中锚点的完整 A 级 provenance | 平均上下文 token | P50 / P95 |
|---|---:|---:|---:|---:|---:|
| 仅 BM25 | 0.0% | 0.0 | 0.0% | 0 | 0 / 0ms |
| BM25 + 符号 | 0.0% | 0.0 | 0.0% | 0 | 0 / 0ms |
| + 本地向量 | 0.0% | 0.0 | 0.0% | 0 | 0 / 0ms |
| + certain 图扩展 | 0.0% | 0.0 | 0.0% | 0 | 0 / 0ms |
| 完整无模型 | 0.0% | 0.0 | 0.0% | 0 | 0 / 0ms |

### 结构与拒答任务（当前完整无模型方案）

| 能力 | 题数 | 指标 | 延迟 P50 / P95 |
|---|---:|---|---:|
| certain 影响集合 | 0 | P 0.0 / R 0.0 / F1 0.0 | 0 / 0ms |
| 无证据拒答 | 0 | 准确率 0.0% | 0 / 0ms |
| 已审核经验复用 | 5 | B 卡 Recall@10 100.0% / MRR 0.642 | 5 / 6ms |

### 开发回归检索门槛（非模型效果门槛）

- 完整方案相对 BM25-only：Recall@10 +0.0pt，MRR@10 +0.000。
- 门槛：Recall@10 至少 +10pt 且 MRR@10 不低于 -0.02；本轮：**失败**。

## 逐题结果

### current-cjson-root-cause · 已审核经验复用 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-06；问题：cJSON 嵌套深度到达 CJSON_NESTING_LIMIT 时，parse_value 失败的真实根因是什么？
- 预期 B 卡：`沉淀 cJSON 嵌套深度边界的可复现排障知识`；主锚点：parse_value (cJSON.c:L1368)
- 结果：命中=True，MRR=0.111，锚点完整=True。

### current-cjson-condition · 已审核经验复用 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-06；问题：排查 cJSON 嵌套限制时，低于、等于和超过 CJSON_NESTING_LIMIT 分别要验证什么边界条件？
- 预期 B 卡：`沉淀 cJSON 嵌套深度边界的可复现排障知识`；主锚点：parse_value (cJSON.c:L1368)
- 结果：命中=True，MRR=1.0，锚点完整=True。

### current-cjson-reject-false-cause · 已审核经验复用 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-06；问题：cJSON 深层输入解析失败能否直接归因于内存不足？如何用嵌套限制实验排除这个错误解释？
- 预期 B 卡：`沉淀 cJSON 嵌套深度边界的可复现排障知识`；主锚点：parse_value (cJSON.c:L1368)
- 结果：命中=True，MRR=0.1，锚点完整=True。

### current-cjson-fix · 已审核经验复用 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-06；问题：确认是 CJSON_NESTING_LIMIT 后，应该怎样处理输入或构建配置，并避免把限制简单删除？
- 预期 B 卡：`沉淀 cJSON 嵌套深度边界的可复现排障知识`；主锚点：parse_value (cJSON.c:L1368)
- 结果：命中=True，MRR=1.0，锚点完整=True。

### current-cjson-verification · 已审核经验复用 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-06；问题：如何复现并验证 cJSON 嵌套深度边界，解析结果和 parse_end 应观察什么？
- 预期 B 卡：`沉淀 cJSON 嵌套深度边界的可复现排障知识`；主锚点：parse_value (cJSON.c:L1368)
- 结果：命中=True，MRR=1.0，锚点完整=True。

## 失败案例

> 真实能力失败 0；因没有当前 approved B 卡而被审核门禁阻塞 0。两类结果不会合并包装。

本轮没有失败任务；结果仍只适用于本报告记录的固定公开快照与小规模人工题集。

## 口径与边界

- 定位、调用链和语义检索只检查 top-10 是否命中人工指定源码锚点；完整方案的命中还必须是含仓库、revision、USR、文件和行号的 A 级证据。
- 影响分析只对 certain 反向可达集计分；candidate 只作独立线索展示，绝不并入 F1。
- 拒答题要求 `refused=true` 且 `A/B=0`；全程显式传入 `llm_client=None`，没有模型调用。
- 本集只有 5 道人工维护任务，用于求职演示与失败复盘；它不具备统计显著性，不替代自动生成回归集的消融报告。
