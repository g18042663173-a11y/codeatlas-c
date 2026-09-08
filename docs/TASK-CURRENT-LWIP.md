# lwIP 当前正式知识卡验收集（5 题） — 轻量任务评测

> **审核状态：清单标记已审核；仅作开发回归**。任务 gold 由人工维护；本报告展示可执行任务的验证结果，不替代自动消融报告。

> 已用于开发调整，不是独立留出集；召回通过不代表模型理解更好。

## 运行快照

| 项目 | 值 |
|---|---|
| 公开语料 | lwIP 公开源码与公开可复现实验固定快照 |
| 仓库 | `https://github.com/lwip-tcpip/lwip` |
| Git revision | `3d896ba0a37ff3ce73270ca5e230707fe47f60e3` |
| 解析模式 | compile database |
| 任务清单 | `eval/tasks_current_lwip.yaml` |
| 审核人 / 日期 | Guoshuaiqi / 2026-09-06 |
| 失败规则 | 当前发布卡未命中、不是 approved B 级证据或主锚点 provenance 不完整即失败；不得回写旧 20 题 gold。 |
| 审核状态 | draft 0 / approved 5 |
| 命令 | `codeatlas task-eval --db data/lwip.db --tasks eval/tasks_current_lwip.yaml --embedder tfidf --out docs/TASK-CURRENT-LWIP.md` |

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
| 已审核经验复用 | 5 | B 卡 Recall@10 100.0% / MRR 0.118 | 34 / 73ms |

### 开发回归检索门槛（非模型效果门槛）

- 完整方案相对 BM25-only：Recall@10 +0.0pt，MRR@10 +0.000。
- 门槛：Recall@10 至少 +10pt 且 MRR@10 不低于 -0.02；本轮：**失败**。

## 逐题结果

### current-lwip-root-cause · 已审核经验复用 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-06；问题：netif_add 加入接口后 netif_default 仍为空的真实根因是什么？
- 预期 B 卡：`沉淀 netif_add 与默认接口选择分离的可复现知识`；主锚点：netif_add (src/core/netif.c:L286)
- 结果：命中=True，MRR=0.111，锚点完整=True。

### current-lwip-condition · 已审核经验复用 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-06；问题：首个和第二个接口调用 netif_add 时，netif_list 与 netif_default 分别应满足什么条件？
- 预期 B 卡：`沉淀 netif_add 与默认接口选择分离的可复现知识`；主锚点：netif_add (src/core/netif.c:L286)
- 结果：命中=True，MRR=0.143，锚点完整=True。

### current-lwip-reject-false-cause · 已审核经验复用 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-06；问题：能否认为 netif_add 会自动把最后加入的接口设为默认接口？如何排除这个错误解释？
- 预期 B 卡：`沉淀 netif_add 与默认接口选择分离的可复现知识`；主锚点：netif_add (src/core/netif.c:L286)
- 结果：命中=True，MRR=0.111，锚点完整=True。

### current-lwip-fix · 已审核经验复用 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-06；问题：netif_add 成功后，调用方应怎样使用 netif_set_default 完成默认接口选择？
- 预期 B 卡：`沉淀 netif_add 与默认接口选择分离的可复现知识`；主锚点：netif_add (src/core/netif.c:L286)
- 结果：命中=True，MRR=0.1，锚点完整=True。

### current-lwip-verification · 已审核经验复用 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-06；问题：如何运行 lwIP netif_add 复现实验，并验证 list、default 和返回值？
- 预期 B 卡：`沉淀 netif_add 与默认接口选择分离的可复现知识`；主锚点：netif_add (src/core/netif.c:L286)
- 结果：命中=True，MRR=0.125，锚点完整=True。

## 失败案例

> 真实能力失败 0；因没有当前 approved B 卡而被审核门禁阻塞 0。两类结果不会合并包装。

本轮没有失败任务；结果仍只适用于本报告记录的固定公开快照与小规模人工题集。

## 口径与边界

- 定位、调用链和语义检索只检查 top-10 是否命中人工指定源码锚点；完整方案的命中还必须是含仓库、revision、USR、文件和行号的 A 级证据。
- 影响分析只对 certain 反向可达集计分；candidate 只作独立线索展示，绝不并入 F1。
- 拒答题要求 `refused=true` 且 `A/B=0`；全程显式传入 `llm_client=None`，没有模型调用。
- 本集只有 5 道人工维护任务，用于求职演示与失败复盘；它不具备统计显著性，不替代自动生成回归集的消融报告。
