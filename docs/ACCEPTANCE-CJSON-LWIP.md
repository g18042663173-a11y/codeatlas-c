# CodeAtlas cJSON / lwIP 双语料验收

> 模式：`full`；基础能力：**通过**；旧 40 题：**30/40 保留**；当前卡 10 题：**完成**；作品集：**未就绪**；公开状态：`locally_verified_not_published`。

## 可复现身份

- CodeAtlas：`e31071601c26d4f4d3ec2491e20c79b4c26a5e82`；branch `feat/portfolio-core-evidence`；dirty=True
- 实现内容哈希：`9043b96c551291ed0a4c05d5c5bac99e8145fed641a6bebee109b987f2beb1d3`
- 生成时间：`2026-09-08T04:36:02+00:00`

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
- 可用于效果汇总的复核终局：`216/486`（待证据审计不等于判错）
- 正式知识卡人工发布：`completed`
- 可选人工答案复核：`not_run`（不冒充 AI 双审）
- 所有主张均有终态：`false`
- 所有预登记收益均得到支持：`false`（不作为作品集就绪的硬门槛）
- 公开复现：`not_completed`

- `wiki-cjson`：`completed`；复核 `ai_reviewed`。
- `wiki-lwip`：`pending_evidence_audit`；复核 `unresolved`。
  评分输入存在引用映射或正文完整性问题，原语义数字暂不用于效果判断；见 `docs/WIKI-LWIP-EVIDENCE-AUDIT.json`。
- `knowledge-reuse`：`invalid_review`；复核 `unresolved`。
  评分输入存在引用映射或正文完整性问题，原语义数字暂不用于效果判断；见 `docs/REUSE-EVIDENCE-AUDIT.json`。
- `maintenance`：`completed`；复核 `deterministic_oracle`。
- 主张 `wiki-cjson`：`insufficient_evidence`。
- 主张 `wiki-lwip`：`unresolved`。
- 主张 `knowledge-reuse`：`unresolved`。
- 主张 `maintenance`：`insufficient_evidence`。
- Wiki 主对照（两仓库等权）：`not_measured`；模型答案与复核未完整时不显示差值。
