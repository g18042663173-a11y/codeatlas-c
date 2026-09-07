# ADR-002：知识快照与发布恢复

状态：已实施并以隔离 fixture 和双语料验证。来源：[手册未定义·本次新增]。

实现边界：CAS 复用相同源码/Wiki 文件字节；Wiki 输入哈希不含全局 commit，正文与引用会重新导出。尚未将正文和引用拆成独立物理 CAS 对象。正式冻结验证被消费源码与 HEAD blob 一致，系统头等外部输入复制后通过 Clang VFS overlay 固定；完整 Git 工作区洁净检查仍由正式语料/发布验收执行。

控制库保留人工资产与运行审计，每个 knowledge_set 独立保存冻结源码、事实 SQLite/FTS、Wiki、向量与映射。开始查询时固定 QueryContext；整个 Agent 运行不重新读取 active。构建中继续查询上一版并显示版本。

构建顺序：备份旧资产 → 冻结源码和编译输入 → staging 构建 → 检查诊断、Wiki、依赖、索引和文件哈希 → 持久化产物 → 单事务 compare-and-swap active 与 governance_revision。失败不切换；同机单写锁串行发布；冲突重新投影当前卡片。回滚旧源码也必须使用当前治理状态，不能复活被撤销/替代的卡。

不可变正文采用内容哈希复用；SQLite/FTS 选择每集独立副本，避免每个 SQL 漏写集合过滤。源文件标识使用仓库相对路径。输入包含 revision、tree hash、build hash、生成批次、解析/模型/prompt 版本；dirty 工作树不能只报原 commit。

Markdown 保存内容；控制库 publication_journal 保存提交判定。每次多文件变更有幂等 op_id：prepared 持久化 → 文件 fsync/rename → 数据库提交与 committed。恢复时未提交操作回滚文件，已提交操作补齐文件；prepared 不进入重建。历史文件不自动删除。

首个集合验证成功后启用新读路径；没有 active 时返回 not_ready。旧 parse/wiki/index 只修改 staging。迁移保留会话、卡片、QA、审核、运行及反馈主键。

测试覆盖：构建中查询、切换中 Agent、构建失败、CAS 冲突、回滚最新治理、审批多文件崩溃窗口和重建。
