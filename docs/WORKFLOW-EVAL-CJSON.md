# CodeAtlas Agent 与会话知识闭环评测

> 固定 6 个公开场景；无 API Key、无外部模型、隔离 SQLite 副本。

## 运行快照

- 仓库：`https://github.com/DaveGamble/cJSON`
- revision：`fb16e5cf358798aabb049655975cde8427101056`
- 清单：`eval/workflow_cjson.yaml`
- 审核模式：`synthetic_test_review`（仅验证门禁）

## 门禁指标

| 指标 | 结果 |
|---|---:|
| 工具轨迹合法率 | 100.0% |
| 概念题 Wiki-first 顺序遵守率 | 100.0% |
| A 级锚点完整率 | 100.0% |
| 知识卡审核/过期/替代门禁正确率 | 100.0% |
| Markdown 重建一致率 | 100.0% |
| 无证据拒答准确率 | 100.0% |
| 原始会话进入检索数量 | 0 |
| Agent 延迟 p50 / p95 | 14 / 37 ms |

## 固定场景

- `session-import`：通过；public source/license/hash validated; duplicate rejected; raw session chunks=0
- `agent-trace`：通过；impact=3 read-only calls; wiki-first=True; draft is explicit
- `pending-gate`：通过；pending card has no B-level retrieval visibility
- `approval-anchor`：通过；USR + range + definition hash bound before B evidence appeared
- `stale-isolation`：通过；definition hash mismatch hid stale B evidence; supersede=True; markdown rebuild=True
- `no-evidence-refusal`：通过；insufficient and unrelated requests refused without card persistence

## 口径与边界

- 只度量可验证的工具权限、源码锚点、审核/过期隔离和拒答，不评估模型文案质量。
- `stale-isolation` 在隔离副本中模拟 definition hash 变化，不修改公开源码或源知识库。
- 原始会话未进入 FTS；评测导入的三个会话均为 CC0 公开合成资产。
