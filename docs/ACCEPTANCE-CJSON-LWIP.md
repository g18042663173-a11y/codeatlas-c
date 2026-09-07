# CodeAtlas cJSON / lwIP 双语料验收

> 模式：`full`；基础能力：**通过**；40 题：**未完成**；作品集：**未就绪**；公开状态：`locally_verified_not_published`。

## 可复现身份

- CodeAtlas：`1b1f1d8bd66bf05fc15f1f62d7a97715e77fc3b4`；branch `feat/portfolio-core-evidence`；dirty=True
- 实现内容哈希：`a2f68948a21bfcac63153cc60a65b022ad8720899812cb43c993b453acacee3a`
- 生成时间：`2026-09-06T14:46:10+00:00`

## 双语料矩阵

| 语料 | 角色 | 基础能力 | 固定源码/compile DB | 代码事实/Wiki | 20 题 | 知识闭环 | 正式卡 |
|---|---|---:|---:|---:|---:|---:|---:|
| cjson | correctness | 通过 | 通过 | 通过 | 失败 | 通过 | 通过 |
| lwip | scale | 通过 | 通过 | 通过 | 失败 | 通过 | 通过 |

### cjson 任务结果

- 总任务：15/20；真实能力失败 0；审核阻塞 5。
- Recall@10：BM25 90.0% → 完整方案 100.0%；MRR 0.711。

### lwip 任务结果

- 总任务：15/20；真实能力失败 0；审核阻塞 5。
- Recall@10：BM25 80.0% → 完整方案 100.0%；MRR 0.814。

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

- 真实模型 A/B/C：`not_run`；未达发布门槛时不以规则结果替代模型数字。
- 合成测试审核只验证门禁，不作为真实经验复用效果。

- `cjson`：`not_run`；发布门禁 未通过。
- `lwip`：`not_run`；发布门禁 未通过。

## 三类价值证明

- 工程底座：`passed`
- 预登记实验执行：`not_completed`
- 双审／独立真值复核：`not_completed`
- 正式知识卡人工发布：`completed`
- 实验答案另行人工复核（非发布门槛）：`not_completed`
- 所有主张均有终态：`false`
- 所有预登记收益均得到支持：`false`（不作为作品集就绪的硬门槛）
- 公开复现：`not_completed`

- `wiki-cjson`：`not_run`；复核 `ai_reviewed`。
- `wiki-lwip`：`not_run`；复核 `ai_reviewed`。
- `knowledge-reuse`：`not_run`；复核 `not_run`。
- `maintenance`：`completed`；复核 `deterministic_oracle`。
- 主张 `wiki-cjson`：`not_measured`。
- 主张 `wiki-lwip`：`not_measured`。
- 主张 `knowledge-reuse`：`not_measured`。
- 主张 `maintenance`：`insufficient_evidence`。
- Wiki 主对照（两仓库等权）：`not_measured`；模型答案与复核未完整时不显示差值。
