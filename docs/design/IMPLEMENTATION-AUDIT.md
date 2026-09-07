# 框架改造交付审计 · 2026-09-05

本文保留先前快照改造的历史验收；后续评测修订与最新测试见 [评测合理性审计](EVALUATION-REVISION-AUDIT.md)。

本轮只改本地 `feat/portfolio-core-evidence`，不提交、不推送。原有脏工作区保留，两个旧稿与 40 题 gold 的 SHA-256 未变。备份位于 `data/kb-sets/migration-backup` 和 `data/lwip-sets/migration-backup`。内部录音、图片和会话未导入公开资产。

## 改造与证据

| 问题 | 实现 | 验证 |
|---|---|---|
| 构建覆盖查询版本 | `snapshots.py` 冻结源码及消费头文件；独立 SQLite/FTS、向量、Wiki；校验后切 active | 旧连接跨切换仍读旧源码；失败保留 active；正式两库 0 严重诊断 |
| 宏、类型、配置变化未失效 | `dependencies.py` 完整保守卡片依赖；文件/子页 Wiki 依赖；确定性传播日志 | 外部宏变更、返回常量、环和阈值、QA 更新、回滚不得恢复旧 B |
| 真引用配假结论 | `contracts.py` 分离引用校验、精确结构化断言和自由回答待审 | 假 USR 被拒；真实引用配错误释放结论不能计为正确 |
| 审核后换材料、半发布 | bundle 覆盖卡片/锚点/依赖/实验/QA；日志 prepared/committed；恢复和幂等 | 旧 QA 不复用；历史 Markdown 回放被拒；多文件中断恢复 |
| Agent 与对照不公平 | 6 次尝试，300 行源码，8,000/1,200 请求预算；A/B 正文可见，C 预检计数 | 第六次合法工具可汇总；失败调用计数；原始试次保留 |
| 锚点命中冒充理解正确 | rubric 盲审导入；主指标、分类和门禁统一重算 | 非 gold 拒答不计正确；规划分母不排除错误拒答；模型报告绑定 active 身份 |
| 旧审核 UI 覆盖新门禁 | 清除旧函数覆盖，分离绑定、QA、当前材料批准 | 浏览器已显示材料哈希、依赖数、独立按钮；新增前端源契约测试 |

本地回归：`pytest -q` **331 passed**；前端控制器 `8 passed`。其中 HTTP 协议测试只绑定本机临时端口，不调用收费服务；没有因为缺少外部语料或模型而跳过核心测试。

## 本轮实测

以 [统一报告](../ACCEPTANCE-CJSON-LWIP.md) 及其 JSON 为完整门禁来源，下面是同轮任务结果：

| 语料 | TU | 文件/模块/仓库 Wiki | 任务通过 | 真实失败 | 审核阻塞 | BM25 → 完整 Recall@10 | MRR |
|---|---:|---|---:|---:|---:|---|---|
| cJSON | 28 | 34 / 3 / 1 | 15/20 | 0 | 5 | 90% → 100% | 0.440 → 0.711 |
| lwIP | 124 | 266 / 18 / 1 | 15/20 | 0 | 5 | 80% → 100% | 0.417 → 0.814 |

两套 6 场景隔离评测均通过，原始会话入索引为 0。自动化批准者为 synthetic_test_review，不计作真实经验效果。两条公开实验重新运行，cJSON 的 999/1000 成功、1001 返回 NULL；lwIP 的接口加入与默认接口设置结果和受控输出一致。两张正式卡已按固定审核包由用户确认、完成 QA 并发布；材料见 [正式知识卡审核包](../review/CARD-REVIEW-20260906.md)。

旧自动回归消融也保留完整结果：[cJSON](../EVAL-CJSON.md)、[lwIP](../EVAL-LWIP.md)。例如 cJSON 的被调关系和语义子类在图扩展方案中存在下降，不能把精选 10 题的全命中包装为所有查询都改善。没有改 gold 隐藏失败。

## 尚未证明或采用保守实现的部分

- 新快照全量解析，卡片依赖为所有已解析实体加配置；不是最小语义消费集，会过度失效。传播日志选择重验证策略，不证明程序语义等价。
- Wiki input_hash 不包含全局 commit；引用独立刷新，相同文件字节可 CAS 复用。正文/引用尚未拆成两个物理 CAS 对象。
- 骨架是当前构建配置下的显式局部观察；未启用分支、别名和 switch fallthrough 不做 certain 语义证明。不是完整 CFG 或数据流分析。
- 自由回答正确性需要按固定 rubric 盲审。Luna 的 510 个答案试次已采集，但评分 judge 未通过冻结资格校准，正式盲审没有启动；阅读顺序遵守和未审答案都不代表理解增益。
- 两张正式卡仍 pending，QA 和最终确认未由用户完成。本次方案讨论不构成内容批准。
- 24 个真实上游维护场景已执行完整。审核声明的函数/宏消费依赖在 16 个语义场景达到 16/16，漏放和误失效均为 0；发布中断和治理回滚使用生产持久化日志完成恢复。该结果不包含完整构建和真人复核成本，不能证明摊销价值。
- 432 个 Wiki 答案试次和 54 个经验试次尚未调用真实模型；历史报告在实现变化后为 `stale`，不会进入当前效果结论。

因此基础工程、人工审核、真实模型效果分开验收。当前 `task_complete=false`、`portfolio_ready=false`；本地未提交结果不能显示公开已验证。

## 常用入口

```bash
codeatlas snapshot build corpus/cJSON corpus/cJSON/compile_commands.json --db data/kb.db --activate
codeatlas snapshot status --db data/kb.db
codeatlas snapshot rollback <historical-id> --db data/kb.db
codeatlas acceptance-eval --mode full
codeatlas acceptance-eval --mode full --use-existing
codeatlas eval reading --db data/kb.db --tasks eval/tasks_cjson.yaml
codeatlas serve --db data/kb.db --data-dir data
```

`acceptance-eval` 因 10 道审核阻塞退出 1 是预期严格门禁，不等于构建失败；不要改成返回“总体通过”。旧 `parse/wiki/index` 在快照启用后不会覆盖 active，正式入口已改为 `snapshot build --activate`。无 Key 时不执行付费接口。

## 同行代码审阅

| 正确性 | 完整性 | 范围控制 | 可测试性 | 综合 |
|---:|---:|---:|---:|---:|
| 8.0 | 8.0 | 8.5 | 8.5 | 8.25 |

第 3 轮通过，无阻塞。审阅后网页实测清理了旧覆盖函数，并补版本化引用/工具标签一致性、评测接口固定快照及旧报告过期隔离回归；最终全套回归覆盖这些补验。此评分是代码审阅，不是模型效果评分。

设计依据：[初始差距审计](00-gap-audit.md)、[依赖失效 ADR](ADR-001-invalidation-propagation.md)、[快照发布 ADR](ADR-002-knowledge-set-atomic-switch.md)、[来源对照](CHANGELOG-design.md)。
