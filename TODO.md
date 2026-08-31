# 进度

## 已完成（可运行 + 有测试，76 项全绿）

| 阶段 | 内容 | 关键验收 |
|---|---|---|
| M0 | 骨架：DDL / CLI / 幂等重建 | — |
| M1 | libclang 解析，certain/candidate 双置信度，USR 去重，三级降级 | lwIP 124 TU / 5 诊断错误 |
| M2 | 递归 CTE 多跳 + 影响分析，candidate 隔离 | 遍历绝不走 candidate（有测试） |
| M3 | 定长摘要头 ≤200 token，规则版全量 + LLM 增强钩子 | 平均 62 token，最大 171 |
| M4 | 四路召回 + RRF + 图扩展 + 证据分级 + 拒答 | lwIP Recall@10 64.6% → 73.5% |
| M5 | 经验库：schema 校验 + pending 闸门 + 审核同步 | 审核前命中为 0（有测试） |
| M6 | 分层文档生成 + gen_task 断点续写 + 章节校验 | 中断可续 |
| M7 | 评测：lwIP 58 题、cJSON 59 题 + 六组消融 | docs/EVAL-LWIP.md / docs/EVAL-CJSON.md |
| M8 | FastAPI + 单页前端 | 三 Tab |
| M9 | 增量解析：依赖闭包指纹 + 边引用计数回收 | 增量与全量逐边一致（有测试） |
| M10 | LLM 分支 mock 测试 | 13 项，覆盖围栏/超时/schema 失败 |

## 待做（按投入产出排序）

### P0 — 投递前必须做（约 1 天）
- [ ] **配真实 API key 跑通 LLM 路径**。目前 13 项 mock 测试兜底，但从未打过真实 API。
      跑 `codeatlas summary --use-llm` 和 `codeatlas wiki`，评估生成质量。
      验收：若 one_liner 明显优于规则版则保留 "LLM 增强" 表述；否则改称"分层文档生成"，
      不要在简历里叫 LLM-Wiki。
- [ ] **README 加 2~3 张截图**（impact 输出、Web 界面、消融表）。招聘者只扫一眼。

### P1 — 有时间就做（各 1~2 天）
- [ ] 换真实嵌入模型 `BAAI/bge-small-zh-v1.5` 重跑消融，替换 tfidf 那一行数字
      验收：EVAL.md 里 embedder 字段改成真实模型名，数字重新生成
- [ ] candidate 细分：把 `unresolved` 拆成 宏展开 / 声明缺失 / 解析错误
      验收：mbedtls 上 369 条 unresolved 能被分成三类并各自报数
- [ ] 评测集扩到 120 题，补负例标注（当前只标了"应召回什么"）

### P2 — 加分项
- [ ] 生成质量评测（人工打分或 LLM-as-judge）
- [ ] 宏定义变更的跨 TU 精确失效追踪（当前靠头文件哈希兜底）
- [ ] 同名符号消歧的专项评测（证明符号通道在什么条件下有价值）

## 明确不做
多用户 / 权限 / 审计 / 项目隔离 —— 超出个人项目范围，写在"后续演进"即可。
