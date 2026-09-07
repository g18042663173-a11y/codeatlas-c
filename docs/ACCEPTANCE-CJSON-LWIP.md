# CodeAtlas cJSON / lwIP 双语料验收

> 模式：`full`；基础能力：**通过**；旧 40 题：**30/40 保留**；当前卡 10 题：**完成**；作品集：**未就绪**；公开状态：`locally_verified_not_published`。

## 可复现身份

- CodeAtlas：`57c9db65e87f093fd9bf5ade5aa5d49c60ae3e24`；branch `feat/portfolio-core-evidence`；dirty=True
- 实现内容哈希：`350b078b72eb0f4a36d271aa0896efde5620a24344a591db0a19bf58828bbadd`
- 生成时间：`2026-09-07T17:51:59+00:00`

## 双语料矩阵

| 语料 | 角色 | 基础能力 | 固定源码/compile DB | 代码事实/Wiki | 旧 20 题 | 当前卡 5 题 | 知识闭环 | 正式卡 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| cjson | correctness | 通过 | 通过 | 通过 | 失败 | 通过 | 通过 | 通过 |
| lwip | scale | 通过 | 通过 | 通过 | 失败 | 通过 | 通过 | 通过 |

### cjson 任务结果

- 总任务：15/20；真实能力失败 0；审核阻塞 5。
- Recall@10：BM25 90.0% → 完整方案 100.0%；MRR 0.711。
- 当前发布卡验收：5/5；该结果不回写旧 gold。

### lwip 任务结果

- 总任务：15/20；真实能力失败 0；审核阻塞 5。
- Recall@10：BM25 80.0% → 完整方案 100.0%；MRR 0.814。
- 当前发布卡验收：5/5；该结果不回写旧 gold。

## 公开声明验证

| 声明 | 状态 | 实际 |
|---|---|---|
| cjson-official-source | `verified` | {"checkout_url": "https://github.com/DaveGamble/cJSON", "official_url": "https://github.com/DaveGamble/cJSON", "source_relation": "official"} |
| lwip-upstream-and-mirror | `verified` | {"checkout_url": "https://github.com/lwip-tcpip/lwip", "official_url": "https://git.savannah.nongnu.org/git/lwip.git", "source_relation": "mirror"} |
| cjson-task-count | `verified` | 20 |
| lwip-task-count | `verified` | 20 |
| lwip-two-failures | `contradicted` | 0 |
| codeatlas-report-publication | `locally_verified_not_published` | dirty_or_untracked |

## 模型边界

- 旧整套 A/B/C 报告：`not_run`；它与下方三类价值证明分开，未运行时不以规则结果替代模型数字。
- 合成测试审核只验证门禁，不作为真实经验复用效果。

- `cjson`：`not_run`；发布门禁 未通过。
- `lwip`：`not_run`；发布门禁 未通过。

## 三类价值证明

- 工程底座：`passed`
- 预登记实验执行：`completed`
- 双审／独立真值复核：`485/486 终局`
- 正式知识卡人工发布：`completed`
- 可选人工答案复核：`not_run`（不冒充 AI 双审）
- 所有主张均有终态：`true`
- 所有预登记收益均得到支持：`false`（不作为作品集就绪的硬门槛）
- 公开复现：`not_completed`

- `wiki-cjson`：`pending_review`；复核 `unresolved`。
- `wiki-lwip`：`completed`；复核 `ai_reviewed`。
- `knowledge-reuse`：`completed`；复核 `ai_reviewed`。
- `maintenance`：`completed`；复核 `deterministic_oracle`。
- 主张 `wiki-cjson`：`insufficient_evidence`。
- 主张 `wiki-lwip`：`insufficient_evidence`。
- 主张 `knowledge-reuse`：`insufficient_evidence`。
- 主张 `maintenance`：`insufficient_evidence`。
- Wiki 主对照（两仓库等权）：`not_measured`；模型答案与复核未完整时不显示差值。
