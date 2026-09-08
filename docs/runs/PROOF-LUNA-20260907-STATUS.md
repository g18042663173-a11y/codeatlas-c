# Luna 真实效果实验状态（2026-09-08 收口）

## 结论

510 个 Luna 答案保持冻结，题目与 gold 未改。知识卡 54 个旧评分停用，两套 Wiki 也确认存在引用正文遗漏，历史语义汇总暂不用于效果判断。134 个 Wiki 范围疑点尚需逐项核对，不等于 134 个答错。详见 [Wiki 审计](../WIKI-EVIDENCE-AUDIT.md) 与 [知识卡审计](../REUSE-EVIDENCE-AUDIT.md)。

当前状态：

- `collection_complete=true`
- `judge_qualification=true`（Sol/V4B；Terra/V4A 失败）
- `experiments_complete=true`
- `claims_complete=false`（知识复用旧评分停用，Wiki 汇总待证据审计）
- `reviews_complete=false`（原有 485 个终局判断保留作历史记录，不代表当前评分输入已通过完整性复核）
- `portfolio_ready=false`
- `public_repro=false`

答案采集已完成；知识卡与 Wiki 的评分输入缺陷须核查、修复并复核后，才能判断收益或退化。“只剩一项”及“431 个均具有效证据”的表述均已撤回。本轮网关请求新增 0 次，原请求记录为 3,125 次。

## 冻结答案批次

- campaign：`campaign-85926a33953647dd8e6d1ef72d902b18`
- plan hash：`99456cccbfa5b5f06873cd92c6c5bd8d917b1cc5e31a3d1c76a67d05dedd27e3`
- 被测模型：`gpt-5.6-luna`
- endpoint 身份：`https://codex.y2025g.top/v1`
- 开发冒烟：24/24
- 正式答案：486/486
- 总试次：510/510 completed，0 uncertain
- Wiki cJSON run hash：`25a00c1dc5a4416b95d0c4a1336841d4c2e4ba3166bb1dd8843dfbc56566641e`
- Wiki lwIP run hash：`79ff17fd9893328e097fb85466d2006b7cd06edf2b6359a5734cfbad96e5ad81`
- 知识复用 run hash：`606fb74d35b7cdf4810ff312ba9fdb992f0e61b455ddc03394a23869fe9e7d1b`

API Key 只从本机供应商配置临时注入；精简报告、Git、数据库和命令输出均不保存凭据。

## V4 评分资格

V4A、V4B 各含 12 个机制、66 个判断点，并在调用前由两份隔离源码核验确认。门槛没有在看到结果后修改。

| 路径 | 模型 / 资格集 | 结果 | 关键指标 |
|---|---|---|---|
| 主路径 | Terra / V4A | 失败 | judge-a point 96.97%，但出现 1 次 contradicted→met；judge-b missed recall 71.43%，且 1 次不安全引用判 supported |
| 密封后备 | Sol / V4B | 通过 | 两个 session point 96.97%、met recall 94.44%、missed/contradiction recall 100%，禁止断言、操纵和不安全引用门禁均通过 |

Terra 完整返回但语义门禁失败后才打开 Sol。没有调用 Astra，没有降低阈值，也没有复用 V3 成绩。

## 正式评分与恢复链

| attempt | 作用 | 结果 |
|---|---|---|
| `v4b-sol` | 原始完整盲包 | 404 个输入超过 8k；保留失败批次，不计效果 |
| `v4b-sol.semantic-review-v1` | 保留显式引用与全部源码/实验真值的有界投影 | 双评 969/972 有效；127 个分歧中 126 个仲裁有效；3 个独立评分格式无效，1 个仲裁超预算 |
| `format-gap-recovery-v1` | 新 session 校准并补 3 个独立评分空位 | 补位成功；新仲裁 session 未通过 V4B，立即停止 |
| `format-gap-recovery-v2` | 复用已通过校准的补位评分和原仲裁 session | 只发送 3 个缩减后的仲裁请求；2 个解决，1 个合法语义未决 |

V4 总新增请求 1,394 次，低于预登记硬上限 1,522；全局账本也低于 5,000。有效评分从未重跑，失败或未决结果从未删除。

唯一未决项是 cJSON 的 `cJSON_DetachItemFromObject`：答案提到调用 `cJSON_GetObjectItem`，但没有明确“默认大小写不敏感”这一限定。两份独立评分分别判为 `met` 与 `missing`，已校准仲裁仍返回 `unresolved`。系统没有继续换模型寻找期望判定。

## 效果结果

### Wiki

- lwIP 原 216 个评分的历史汇总现为 `pending_evidence_audit`；旧差值不用于当前效果主张。
- cJSON 原 215 个终局、1 个未决记录均保留；整体汇总同样为 `pending_evidence_audit`，不补算、不改分。
- provider usage 并非所有试次都完整返回，因此 token 效率主张保持未测。

### 会话知识策划

两个短合成案例、六道题、三种材料共 54 个答案已采集。旧评分所报的三组 0% 正确率、完整度和差值受引用错配污染，均不得作效果结论。52 个带引用答案的编号映射有误，且对应引用只携带元数据；另有 3 个知识卡答案沿用了仅生产阶段使用的 T 标签。修复后 54 个冻结答案的引用映射与正文完整性离线检查通过，原始答案哈希不变。原实测输入 token 多 5.70% 属于调用用量记录，但不能凭这一数字推断模型质量下降。

### 维护

当前实现重新执行 24/24 个固定上游场景；16 个语义决策为 16/16，漏放 0、误失效 0，本机依赖判断约 0.83 秒。仅主锚点为 13/16，全部失效为 8/16。该实验没有测完整重建和真人复审成本，因此只支持冻结场景上的失效/恢复正确性，不支持普遍维护收益或摊销结论。

## 可公开的表述边界

可以说：

- 实现了编译器事实、版本化 Wiki、知识卡人工发布、失效传播和受限 Agent；
- 在固定 cJSON/lwIP 上完成 510 个 Luna 答案与 486 个正式双评流程；
- Sol 通过密封评分资格；三类实验和失败证据均已落盘；
- 维护冻结场景为 16/16，旧 40 题仍是 30/40，当前卡片验收是 10/10。

不能说：

- Wiki 已经被证明稳定提升回答正确率；
- 结构化知识卡优于或劣于原始会话、普通摘要（旧语义评分已停用）；
- 本项目已达到公开复现或生产级效果。

精简结果见：

- [cJSON Wiki 对照](../curated/PROOF-WIKI-CJSON-V4.md)
- [lwIP Wiki 对照](../curated/PROOF-WIKI-LWIP-V4.md)
- [知识复用对照](../curated/PROOF-KNOWLEDGE-REUSE-V4.md)
- [统一验收](../../ACCEPTANCE-CJSON-LWIP.md)

## 回归

- Python：366 passed，3 warnings
- UI：8 passed
- 当前正式知识卡：2 张；当前版本经验题 10/10
- 旧开发题：30/40，未改旧 gold
