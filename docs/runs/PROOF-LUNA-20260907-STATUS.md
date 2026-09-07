# Luna 真实效果实验状态（2026-09-08 收口）

## 结论

510 个 Luna 答案保持冻结，没有重新生成，也没有修改题目或 gold。Terra 在全新的 V4A 资格集上失败；按预登记规则启用 Sol，Sol 在互不重叠的 V4B 上通过。486 个正式答案随后完成两次独立评分和必要仲裁，但 cJSON 有 1 个限定条件判断在仲裁后仍为语义未决。

当前状态：

- `collection_complete=true`
- `judge_qualification=true`（Sol/V4B；Terra/V4A 失败）
- `experiments_complete=true`
- `claims_complete=true`（四项均为 `insufficient_evidence`）
- `reviews_complete=false`（485/486 形成终局语义评分）
- `portfolio_ready=false`
- `public_repro=false`

这不是“实验没跑”，而是实验跑完后没有形成可发布的正向效果证明。

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

- lwIP 216/216 完成评审：源码组正确率 13.89%，Wiki 自由阅读 18.06%，渐进阅读 19.44%。主对照按机制配对的差值为 +6.25pt，95% 区间 `[0.00, 13.54]`，不足以支持“稳定提升”。
- cJSON 215/216 形成终局评分；因为主对照包含 1 个未决，整体差值保持未测，不用部分结果补齐结论。
- provider usage 并非所有试次都完整返回，因此 token 效率主张保持未测。

### 会话知识策划

两个短合成案例、六道题、三种材料共 54 个答案均完成评审。三组严格正确率都为 0；完整度分别为原始会话 65.08%、通用摘要 68.49%、结构化卡片 65.93%。结构化卡片相对原会话 +0.85pt，区间跨零；相对通用摘要 -2.57pt。可测输入 token 比原会话多 5.70%。这批结果不支持知识卡效果收益。

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
- 结构化知识卡优于原始会话或普通摘要；
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
