# 评测口径与复现

当前结果与限制以 [统一验收 JSON/Markdown](ACCEPTANCE-CJSON-LWIP.md) 为准。
模型效果的实验设计和评分契约见 [三类价值证明协议](design/VALUE-PROOF-PROTOCOL.md)。

## 各类测试证明什么

- 内置 C fixture 与本地假 HTTP 服务：证明代码契约和异常处理，不证明真实模型能力。
- 117 题检索回归：结构题主要由同一代码图生成，只能用于版本回归。
- cJSON/lwIP 各 20 道开发任务：人工定义锚点和判定规则，但已经用于调参，不是独立留出。
- 六场景隔离演示：证明审核、QA、失效、替代和重建门禁；模拟批准不能代替真实用户审核。
- 真实模型 A/B/C：比较整套系统。经验信息量不等，因此经验分数不能证明策划增益。
- 知识层消融：三组代码检索投影相同，只改变 Wiki 可用性和阅读方式，用于检验知识层价值。
- 会话同源对照：两个案例的源码、命令、输出和会话事实相同，只改变材料组织方式。
- 维护变异：24 个真实上游场景检验当前失效/发布策略；另有 24 个 CC0 合成契约作为单元补充，二者分开报告。

## 当前结果

固定 revision 的 cJSON/lwIP 都完成 compile database 解析，严重诊断为 0。
现有 40 道开发任务为 30/40：20 道检索、4 道影响、6 道拒答通过；旧清单中的 10 道经验复用绑定早期示例卡身份和题义，仍显示审核阻塞。主数据库现在已有两张用户正式批准的 B 卡，并以另一个与卡片内容一致的验收记录验证 Top-10 和版本引用；为避免看过结果后改 gold，旧 40 题不回写成 40/40。
历史两道检索失败已修复，不能继续写成当前失败；具体 Recall/MRR 与运行身份从下面的报告读取，不保留另一套手写指标表。

24 个真实上游维护场景已执行完整。首轮全仓指纹会让无关变化全部触发复审；改为审核材料声明结论实际消费的函数、宏和配置后，8 个有效变化全部重验证，8 个无关变化全部保留，语义决策正确 16/16，漏放和误失效均为 0，本轮依赖指纹计算约 0.83 秒。仅主锚点哈希正确 13/16，全部失效正确 8/16。统一验收仍把普遍维护收益标为 `insufficient_evidence`：该实验验证了冻结场景上的决策和故障恢复，但未测完整解析、索引重建、真人复审和摊销成本。

48 道 Wiki 题、6 道经验题及 V4A/V4B 资格集均冻结，510 个 Luna 答案不变。引用编号和正文修订后，cJSON 新评分完成 216/216；知识复用完成 53/54，1 个因评分请求超时未决；lwIP 的 5 个完整输入超 8k，尚未派发。旧报告保留，不用漏传证据的旧分数推断效果。见 [本轮结果](REVIEW-REPAIR-STATUS.md)、[Wiki 审计](WIKI-EVIDENCE-AUDIT.md) 与 [知识卡审计](REUSE-EVIDENCE-AUDIT.md)。
无 Key 的工程通过、人工审核、模型效果是三个独立状态，不能互相替代。

## 运行

```bash
pytest -q
bash demo.sh cjson
bash demo.sh lwip
codeatlas acceptance-eval --mode fast
codeatlas acceptance-eval --mode full
codeatlas acceptance-eval --mode release --use-existing
codeatlas eval knowledge-reuse --out docs/PROOF-KNOWLEDGE-REUSE.json
codeatlas eval maintenance --executor upstream \
  --manifest eval/upstream_maintenance.yaml \
  --out docs/PROOF-MAINTENANCE-UPSTREAM-V5.json
codeatlas eval maintenance --execute-synthetic --out docs/PROOF-MAINTENANCE.json
codeatlas eval calibrate-reviewer --submission path/to/calibration-submission.json

# 可选真实模型；只能由用户在本机配置新的 Key 后运行。
# 首轮是开发回归，不自动获得效果声明资格。
codeatlas eval model --db data/kb.db --tasks eval/tasks_cjson.yaml \
  --data-dir data --out docs/MODEL-ABC-CJSON.md --abc --runs 3 --seed 17
codeatlas eval reading --db data/kb.db --tasks eval/tasks_cjson.yaml \
  --runs 3 --seed 17 --out docs/MODEL-READING-CJSON.json
```

校准提交必须包含至少两个彼此隔离的 agent review；每个 review 都携带完整 12 项判断，以及 reviewer 的 provider、model、agent、prompt hash 和校准盲包 input hash。校准凭据只对这些实际评分身份有效，不能由另一模型或另一提示词复用。

两种模型对照均保留全部试次、固定 seed 的成对调度、原始回答及来源，生成不含方案标签的评审包。经验同源对照先在题内平均三次重复，再在同一案例内平均三个追问，最后以两个案例为 bootstrap 单位；54 个回答不被当作 54 个独立样本。
`--answer-key` 绑定固定题集和带来源的语义要点；`eval review` 可导入逐点评审并另存 JSON/Markdown。双 agent 复核记录为 `ai_reviewed`，不写成人工审核；分歧保留为 `unresolved`，只有第三个独立 agent 绑定两份原评分哈希后才能仲裁。
没有评审的准确率/完整度显示待审；缺失 usage 显示未测，不按零成本计算。
效果声明不再使用“命中金标源码”代替答案正确性。规则结果和假模型只验证流程。

`eval/corpora.yaml` 声明 cJSON 官方 GitHub 与 lwIP 的 Savannah 官方源/GitHub 镜像关系。
正式报告绑定 revision、构建输入、题集、实现、dirty 状态、命令和生成时间；
公开声明从当前 JSON 读取。未提交结果仅本地可验证，404 或哈希漂移不得标为公开已验证。

## 结果文件

- [双语料统一验收](ACCEPTANCE-CJSON-LWIP.md)
- [cJSON 开发任务](TASK-EVAL-CJSON.md) / [lwIP 开发任务](TASK-EVAL-LWIP.md)
- [cJSON 自动回归](EVAL-CJSON.md) / [lwIP 自动回归](EVAL-LWIP.md)
- [cJSON 六场景](WORKFLOW-EVAL-CJSON.md) / [lwIP 六场景](WORKFLOW-EVAL-LWIP.md)
- [cJSON Wiki 效果对照](runs/curated/PROOF-WIKI-CJSON-V4.md)
- [lwIP Wiki 效果对照](runs/curated/PROOF-WIKI-LWIP-V4.md)
- [会话同源对照](runs/curated/PROOF-KNOWLEDGE-REUSE-V4.md)
- [真实上游维护变异](PROOF-MAINTENANCE-UPSTREAM-V5.md)
- [合成维护单元契约](PROOF-MAINTENANCE.md)

两张正式卡已发布，当前验收 10/10；旧开发集保留 30/40。真实效果评审存在知识复用引用映射缺陷和一项 Wiki 未决，`reviews_complete=false`、`claims_complete=false`。统一报告明确区分已采集答案、有效评分和因缺陷停用的评分。
