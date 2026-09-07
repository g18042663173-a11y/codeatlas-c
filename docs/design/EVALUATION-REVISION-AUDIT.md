# 评测合理性修订：实现与验证

2026-09-05；分支 `feat/portfolio-core-evidence`，未提交、未推送。

## 本轮修改

- `eval/protocol.py`：固定 seed 成对交错调度、完整试次检查、服务 usage 完整性、语义金标文件和结果保留契约。
- `eval/reading_eval.py`：三组共用隔离的代码 FTS/向量投影，Wiki 与 B 卡不暗中进入检索；每次运行隔离。无 Wiki 或 Key 时不运行模型。
- `eval/model_eval.py`：A/B/C 都记录迭代结果与异常，使用相同调度、用量与审核格式；新增独立盲审包。
- `eval/rubric.py`：锚点只计召回；答案须人审，未审显示 null。支持逐点评分、错误陈述检查、按题成对区间。对照/答案/证据变更不能复用旧评分，整批导入不半生效。
- 主对照分开：ABC 比较整套系统；知识层主比较自由 Wiki vs 无 Wiki，渐进阅读的两项差异另报。不把读文档顺序当成理解增益。
- `retrieve/engine.py`：不可见向量在排名前排除，不再占据有效结果名额。`agent.py` 与模型规划提示明确 Wiki 是否可用，flat 模式不会因没有 Wiki 而被强制算作规则降级。
- `llm/client.py`：失败请求也计入尝试；服务遗漏 token 字段或超时时不按零成本计。只记录用量与异常类型，不记录 Key。
- `task_eval.py`：旧 40 题标为开发回归；Markdown 的经验失败分类与 JSON 一致，有卡但检索失败不再一律归咎于审核阻塞。
- README、STAR、评测文档修正旧口径；补充 LLM Wiki 原始模式与同名应用的区别，没有改两个排除旧稿。

## 实际验证

```bash
PYTHONPATH=src /tmp/codeatlas-portfolio-venv/bin/python -m pytest -q
/opt/homebrew/opt/node@24/bin/node --test tests/ui_workspace.test.cjs
```

结果：175 passed；前端 8 passed。原有 151 项回归保留，新增 24 项合理性回归。三项既有 FastAPI/Starlette 弃用警告未影响测试。本轮没有调用收费接口。

新增用例包含：引用真但结论错、锚点提升不算答案提升、经验 token 不能掩盖代码成本、缺失 usage、重复试次配对、旧结果覆盖拒绝、逐点评审原子性、评审后修改协议冲突、共用检索投影、隔离副本、阅读报告经 CLI 评分往返，以及 Wiki 收益与渐进策略收益分离。假模型和 synthetic_test_review 只验证机制，不进入正式效果报告。

两套 `task-eval --require-approved --embedder local` 已重新运行，仍各为 15/20：真实能力失败 0、审核阻塞 5。随后 `acceptance-eval --mode full --use-existing` 重新校验当前数据库和报告，基础能力通过、总任务未完成、作品集未就绪，退出 1 符合严格门禁。该命令不重新解析全库，不能称为本轮重新跑了完整构建流水线。

cJSON/lwIP 的 `MODEL-READING-*.json/.md` 均通过显式清空 Key 的命令生成 `not_run`，没有伪造正确率。快照为 `ks-9962806b203d4c32` 与 `ks-72e8fcec2e064629`。

## 未完成的效果证据

独立语义留出金标的人工冻结、两张卡的用户批准、真实模型试次与逐项盲审尚未完成。
同等信息量的原始会话 vs 策划卡对照、过度失效成本与构建/人工维护摊销也尚未量化。协议明确列为边界，不用当前 ABC 或安全用例替代。

因此，本轮交付的是合理的实验和评分机制，不是“已证明 Wiki 优于直接读代码”的结果。

## 保留性校验

`git diff --check` 通过。以下 SHA-256 与修改前相同：

- `eval/tasks_cjson.yaml`：`2758b81110f499690b3ab5acb03b8bbc598d72bef2cbc7250ffabe2c51a3142c`
- `eval/tasks_lwip.yaml`：`39932189415eb5e65a4380f0af1df269d92a8aec9700680a1afa680d42d8874d`
- `README 2.md`：`009ce0722b1dce006ac5a0fc53d3d36ae64c2647170d329e9b6a8921e3e5f25c`
- `docs/ARCHITECTURE 2.md`：`50248054eee2e42064f1de273ed60c8dae2186fde9a78c0cb54cb78f36ee314c`

[实验协议](EVALUATION-VALIDITY.md) · [LLM Wiki 来源核查](LLM-WIKI-LINEAGE.md) · [统一验收](../ACCEPTANCE-CJSON-LWIP.md)
