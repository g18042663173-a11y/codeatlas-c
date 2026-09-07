# 框架补全审计（2026-09-05）

来源：用户确认的《CodeAtlas 框架补全与验收修订方案》。学习手册及提示词包是历史设计参考，不修改原件。

| 项目 | 改造前实现 | 本轮处理 |
|---|---|---|
| 版本切换 | `cli.parse`、`wiki.generator.generate`、`indexer.build.build` 分别覆盖 | 不可变知识快照与原子 active 切换 |
| Wiki 增量 | `_file_input_hash` 含全仓 commit | 生成输入与引用版本分离 |
| 失效 | `experience.store._anchors_current` 只验主函数原文 | 依赖清单、构建配置、保守传播 |
| 控制流 | `AstWalker._record_branch` 只记短源码范围 | 五类结构事实与受限陈述 |
| 引用与评分 | `_valid_answer` 只验标签；`_abc_grade` 命中锚点即正确 | 引用合法、事实一致、答案复核分离 |
| 审核 | 只要求存在 passed QA | 内容版本绑定、可恢复发布日志 |
| 预算 | C 预检不计工具次数；A/B 中间结果删正文 | 统一请求预算与全部尝试轨迹 |
| 已有能力 | 40 题、固定版本、Wiki-first 六工具、会话策划、网页 | 保留、回归验证 |

基线：30/40；代码检索两库各 10/10；10 题 blocked_by_review；模型未运行。这个基线测量锚点召回，不能作为自由模型回答正确率。

改造前真实链路：`cli.parse → incremental.purge_tu → ast_walker.persist → stale_summary_and_chunks → experience.stale_check`；之后分别运行 `summary → wiki.generate → indexer.build`。

主要存储：SQLite `node/edge/edge_source/branch_fact`、`wiki_page`、`chunk/chunk_fts`；外部 `vectors.npy/vec_ids.json`；治理表 `conversation_* / curation_candidate / experience* / agent_*`；知识卡 Markdown。改造前无统一 knowledge_set。
