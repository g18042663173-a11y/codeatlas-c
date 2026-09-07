# Luna 真实效果实验状态（2026-09-07）

## 结论

答案采集和新版评分校准都已实际运行；新版校准未达到预登记门槛，因此正式答案评分按协议没有启动。当前状态必须保持：

- `collection_complete=true`
- `judge_calibration_complete=true`
- `judge_calibration_passed=false`
- `formal_answer_review_started=false`
- `experiments_complete=false`
- `effect_claim=unresolved`
- `portfolio_ready=false`

不能把本报告解释为 Wiki 或知识卡已经带来正向收益。

## 冻结答案批次

- campaign：`campaign-85926a33953647dd8e6d1ef72d902b18`
- plan hash：`99456cccbfa5b5f06873cd92c6c5bd8d917b1cc5e31a3d1c76a67d05dedd27e3`
- 被测模型：`gpt-5.6-luna`
- OpenAI-compatible endpoint：`https://codex.y2025g.top/v1`
- 调度：4 workers，固定 1 次传输重试
- 开发冒烟：24/24；工具规划 12/12；非拒答引用 18/18；拒答 6/6
- 正式答案：486/486
- 总试次：510/510 completed，0 uncertain

正式盲审包保持以下不可变身份：

- cJSON Wiki：`run_hash=25a00c1dc5a4416b95d0c4a1336841d4c2e4ba3166bb1dd8843dfbc56566641e`
- lwIP Wiki：`run_hash=79ff17fd9893328e097fb85466d2006b7cd06edf2b6359a5734cfbad96e5ad81`
- 知识复用：`run_hash=606fb74d35b7cdf4810ff312ba9fdb992f0e61b455ddc03394a23869fe9e7d1b`

## 请求账本

截至新版分层校准完成时，答案采集与评分请求合计：

- 请求 1,731 次：1,697 completed，34 failed，0 uncertain
- 失败类型：23 TimeoutError、8 URLError、2 UpstreamHTTPError、1 RemoteDisconnected
- 冻结答案采集已测得 6,246,566 input tokens、407,456 output tokens、6,654,022 total tokens；新增评分校准未形成完整的统一 provider usage 汇总
- 服务未提供可核验价格，因此成本保持未测，不从 token 推造费用

所有失败尝试均保留，未通过重跑选择较好答案。API Key 只注入临时运行环境，不进入计划、数据库、报告或仓库。

## 评分批次

| attempt | judge | workers | 结果 | 原因 | 校准请求 | 最低正确数 |
|---|---|---:|---|---|---:|---:|
| legacy | Luna | 1 | unresolved | 一条校准请求在固定重试后传输失败 | 25 | 未形成完整评分 |
| transport-recovery-1 | Luna | 4 | unresolved | 校准内容未达 12/12 | 24 | 2/12 |
| sol-judge-1 | Sol | 4 | unresolved | judge-b sticky 会话连续超时 | 34 | 未形成完整评分 |
| sol-judge-2 | Sol | 2 | unresolved | 传输完整，但校准内容未达 12/12 | 24 | 5/12 |
| sol-judge-guided-1 | Sol | 2 | unresolved | 显式 citation/point/refusal 政策后仍未达 12/12 | 24 | 3/12 |
| luna-layered-v3 | Luna | 2 | unresolved | 分层校准未达预登记门槛 | 24 | judge-a 2/12、45/66 point；judge-b 3/12、50/66 point |

失败 batch 全部保留。新评分 attempt 使用独立 batch、session、judge model、worker 数和 `review_runtime_hash`，并绑定原始 campaign plan hash、三个 run hash 与盲审 packet hash；不会覆盖旧评分或原始答案。

新版 `luna-layered-v3` 的 batch ID 为 `3a43a2accb4bd67131693d58fe4174ef7691ed6e8c4f01ac13c93536a09b004e`，结果哈希为 `97e32024e65cdd6930bb99d4a5bb385f4e5197cda7d3e129bb8b3413ca068fc0`。两名 judge 的逐层结果为：

- judge-a：point accuracy 68.18%，met recall 42.42%，missed recall 92.31%，contradiction recall 95.00%，forbidden flag 91.67%。
- judge-b：point accuracy 75.76%，met recall 57.58%，missed recall 92.31%，contradiction recall 95.00%，forbidden flag 91.67%。
- 两者均正确识别评分操纵，没有把不安全引用判成 supported；但 point accuracy、met recall 和 forbidden flag 未达冻结阈值，因此总门禁失败。
- 校准请求携带原问题、候选答案、全部必要要点、禁止断言和冻结源码摘录；失败不是因为盲包缺少要点定义。

## 暴露的框架问题

1. 原实现把答案采集运行时与评分运行时整体绑定，无法在保留原始答案的前提下发布新版评分协议。本轮已增加 versioned review attempt，使 `run_hash` 与评分协议独立绑定。
2. 旧评分把多个字段压成整对象 12/12，夸大了单点错误。本轮已经改为 point、forbidden、语义引用和操纵检测的独立门禁，并由本地真值表派生 verdict/completeness。
3. 分层后 Luna 仍对以下边界不稳定：
   - 答案语义正确但无显式引用时，是否降低总体 decision；
   - `missed` 与显式反向断言的区分；
   - 真实文件位置配错误推论与伪造文件内容的区分；
   - 正确保留未决边界时 `pass` 与 `acceptable_refusal` 的区分。
4. 两个 Luna 独立 session 均未通过冻结的 v3 校准，因此不能继续自动评分并生成收益数字。继续在同一 holdout 上改提示、降阈值或换模型直到通过都会污染资格判断。

## 验证

- v3 校准 gold：12 个新 candidate、66 个 point、67 条证据绑定，经两名互不可见的 source reviewer 对同一 input hash 独立批准，状态仅为 `ai_reviewed`
- v3 评分协议定向回归：36 passed
- `tests/ui_workspace.test.cjs`：8 passed
- CLI 已通过真实 `prepare / preflight / run / report / review` 路径执行
- 在不受 iCloud 占位影响的干净本地副本中重新创建环境并运行
  `PYTHONPATH=src .venv/bin/pytest -q -rs --disable-warnings`：最终 347 passed，3 warnings，150.02 秒；统一 full 验收内的前一版同套测试为 346 passed

## 下一步门槛

本轮“把评分校准实际做完”的结果是明确的负门禁：校准请求已发完、失败证据已保留、正式 486 个答案没有被一个不合格 judge 打分。若继续效果实验，必须另建未被本轮结果污染的新 qualification holdout，并预先指定新的评分者，或改为真实人工双审；不能复用 v3 调参后再把它称为留出验证。只有新校准、双评分和必要仲裁完整完成后，才能生成 Wiki/知识卡效果结论并把 `experiments_complete` 改为 true。

旧 40 道开发回归仍保持 30/40；本批实验不会回写旧 gold。当前实现已本地提交为
`ea2afa9`，尚未推送或完成公开复现。
