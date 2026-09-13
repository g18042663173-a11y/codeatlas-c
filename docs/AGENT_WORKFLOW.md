# Agent 与会话知识闭环

CodeAtlas 的 Agent 是本地、只读、可审计的代码诊断器；知识卡是人工治理的长期资产。二者共享证据，但权限严格分开。

```text
问题
 ├─ 概念 / 功能 / 排障 → search → Wiki outline → Wiki section → source verify
 └─ 定位 / 调用链 / 影响 → symbol / certain graph → source verify
                              │
                              ▼
                 A/B/C 证据注册表或拒答

公开会话 → 质量门禁 → 沉淀目标 → 候选 accept/edit/correct/skip
                                      │
                                      ▼
                        pending Markdown（不入索引）
                                      │
                   主锚点 + 关系 + QA + 人工审核
                                      ▼
 approved B 卡 → 定义变化 stale → 新卡批准后 superseded
                                      │
                                      ▼
                         Markdown 主源可完整重建
```

## Agent 权限

- 最多 6 次工具调用，事件阶段为 `discover / understand / verify`。
- 白名单只有 `search_evidence`、`wiki_outline`、`wiki_section`、`resolve_symbol`、`code_read`、`analyze_impact`。
- `code_read` 只能访问当前固定仓库内已解析的 C/C++ 源码（`.c/.cc/.cpp/.h/.hpp` 等），单次最多 300 行；路径穿越、任意本地文件、shell、网络和源码写入都不可用。
- 概念/功能/排障题必须 Wiki-first；没有当前 Wiki 时允许直接源码降级，并记录 `wiki_unavailable`。定位和结构题可以直接走符号或图。
- 最终回答只能引用本轮注册的 A/B/C 标签；A/B 为 0 时在模型调用前拒答。模型无效 JSON、越权工具、伪造引用或超时都会记录 fallback reason。

## 知识治理

- 原始会话不生成检索 chunk。导入校验公开来源、许可证、repo@revision、内容哈希、复现输入、验证步骤和敏感字段。
- 候选未确认前不会生成卡片；`correct` 保留修改 diff，`skip` 必须给出重复、无价值、证据不足或错误等原因。
- 卡片包括现象、假设、无效尝试、根因、步骤、验证、证据轮次、A/B 标签和一个精确主源码锚点。
- 批准需要 `repository + revision + USR + range + definition_hash + relation` 和至少一条已通过 QA。模型不能绑定、审核或批准。
- `approved/stale/superseded` 的事实来源是 `knowledge/cards/<id>.md`；SQLite/FTS/向量是派生投影。`card rebuild` 会先删除卡片投影，再从 Markdown 恢复锚点、QA、审核和替代历史。

## 可复现验收

```bash
bash demo.sh workflow
# 或
codeatlas workflow-eval --db data/kb.db --out docs/WORKFLOW-EVAL-CJSON.md
```

固定 6 场景当前结果：Wiki-first、工具合法、A 级锚点、审核/QA/失效/替代、Markdown 重建和拒答均为 100%，原始会话进入检索为 0。评测只操作隔离 SQLite 与 Markdown 目录。

可选真实模型对照：

```bash
codeatlas eval model --db data/kb.db --tasks eval/tasks_cjson.yaml \
  --data-dir data --out docs/MODEL-SMOKE-CJSON.md --abc --runs 1 --limit 8
# 冒烟通过且真实卡已审核后，再去掉 --limit 并使用 --runs 3
```

没有真实 Key/报告时，网页只显示“尚未运行”，不会用规则或假 HTTP 服务冒充模型效果。

用户可给 Agent 回答提交 `helpful / incorrect / incomplete`、可选错误证据标签和说明。反馈只进入本地待处理队列，不会直接修改知识卡、索引或审核状态。
