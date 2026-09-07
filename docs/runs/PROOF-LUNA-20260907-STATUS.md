# Luna 真实效果实验状态（2026-09-07）

## 结论

答案采集已经完整完成，效果评分尚未完成。当前状态必须保持：

- `collection_complete=true`
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

截至本报告生成时，答案采集与校准合计：

- 请求 1,707 次：1,673 completed，34 failed，0 uncertain
- 失败类型：23 TimeoutError、8 URLError、2 UpstreamHTTPError、1 RemoteDisconnected
- 已完成响应返回的 usage 合计：6,246,566 input tokens、407,456 output tokens、6,654,022 total tokens
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

失败 batch 全部保留。新评分 attempt 使用独立 batch、session、judge model、worker 数和 `review_runtime_hash`，并绑定原始 campaign plan hash、三个 run hash 与盲审 packet hash；不会覆盖旧评分或原始答案。

## 暴露的框架问题

1. 原实现把答案采集运行时与评分运行时整体绑定，无法在保留原始答案的前提下发布新版评分协议。本轮已增加 versioned review attempt，使 `run_hash` 与评分协议独立绑定。
2. 评分校准对细粒度点状态和引用状态要求 12/12，但实际 judge 对以下边界不稳定：
   - 答案语义正确但无显式引用时，是否降低总体 decision；
   - `missed` 与显式反向断言的区分；
   - 真实文件位置配错误推论与伪造文件内容的区分；
   - 正确保留未决边界时 `pass` 与 `acceptable_refusal` 的区分。
3. 两个不同 judge 均未通过当前校准，因此不能继续自动评分并生成收益数字。继续换模型直到通过会造成选择偏差。

## 验证

- `tests/test_campaign_review.py`：17 passed
- `tests/test_proof_campaign.py`（排除 CLI help 渲染单例）：16 passed
- `tests/ui_workspace.test.cjs`：8 passed
- CLI 已通过真实 `prepare / preflight / run / report / review` 路径执行
- Python 全量回归本次未完成：macOS 读取 `.venv/site-packages/rich/theme.py` 时出现 `[Errno 60] Operation timed out`；这不是断言失败，但在环境恢复前不能写成本轮全量通过

## 下一步门槛

在冻结新的评分方案前，不再发送正式 judge 请求。需要先把校准从“单次整对象完全一致”改成可审计的分层门禁，重新做独立 gold 复核，并在新的留出校准集上预登记通过标准。只有校准、双评分和必要仲裁完成后，才能生成 Wiki/知识卡效果结论并把 `experiments_complete` 改为 true。

旧 40 道开发回归仍保持 30/40；本批实验不会回写旧 gold。当前没有提交、推送或公开复现。
