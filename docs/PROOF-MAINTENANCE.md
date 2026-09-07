# CodeAtlas 维护变异评测

Predeclared external oracle; synthetic fixtures do not verify upstream behavior. No automatic shell/model invocation.

状态：`executed`；清单 SHA-256：`5b18f029319ad73c38c92b8c6f420f786020d05c4c8ad9b5d77a40de051d780c`。
未执行和编译失败不计作有效语义变异；未实测时间与成本保持 null。

| 场景 | 分类 | 预期复审 | 实际结果 | current / 主锚点 / 全失效 |
|---|---|---|---|---|
| cjson-nesting-limit | effective | True | completed | revalidate / keep / revalidate |
| cjson-child-ownership | effective | True | completed | revalidate / keep / revalidate |
| cjson-buffer-growth | effective | True | completed | revalidate / keep / revalidate |
| cjson-failure-return | effective | True | completed | revalidate / revalidate / revalidate |
| cjson-comment-only | unrelated_to_claim | False | completed | keep / keep / revalidate |
| cjson-unrelated-helper | unrelated_to_claim | False | completed | revalidate / keep / revalidate |
| cjson-line-move | unrelated_to_claim | False | completed | keep / keep / revalidate |
| cjson-unused-macro | unrelated_to_claim | False | completed | revalidate / keep / revalidate |
| cjson-syntax-error | failure | None | compile_failed | revalidate / keep / 未测 |
| cjson-unknown-type | failure | None | compile_failed | revalidate / keep / 未测 |
| cjson-missing-include | failure | None | compile_failed | revalidate / keep / 未测 |
| cjson-invalid-config | failure | None | compile_failed | revalidate / keep / 未测 |
| lwip-packet-refcount | effective | True | completed | revalidate / keep / revalidate |
| lwip-default-interface | effective | True | completed | revalidate / keep / revalidate |
| lwip-interface-flag | effective | True | completed | revalidate / keep / revalidate |
| lwip-checksum-config | effective | True | completed | revalidate / revalidate / revalidate |
| lwip-comment-only | unrelated_to_claim | False | completed | keep / keep / revalidate |
| lwip-unrelated-helper | unrelated_to_claim | False | completed | revalidate / keep / revalidate |
| lwip-line-move | unrelated_to_claim | False | completed | keep / keep / revalidate |
| lwip-unused-macro | unrelated_to_claim | False | completed | revalidate / keep / revalidate |
| lwip-syntax-error | failure | None | compile_failed | revalidate / keep / 未测 |
| lwip-unknown-type | failure | None | compile_failed | revalidate / keep / 未测 |
| lwip-missing-include | failure | None | compile_failed | revalidate / keep / 未测 |
| lwip-invalid-config | failure | None | compile_failed | revalidate / keep / 未测 |

## 结果证据卡

- 独立场景：24；有效语义场景：16；故障/编译失败：8。
- 当前保守策略：漏放有效变更 0/8；无关变更误失效 4/8；决策正确 12/16。
- 仅主锚点哈希：漏放有效变更 6/8；无关变更误失效 0/8。
- 全部失效：漏放有效变更 0/8；无关变更误失效 8/8。
- 真值复核：`unresolved`；当前是 CC0 合成契约，不是上游源码变异或人工专家审核。
- 摊销：`None`；原因：`quality_not_comparable`。

摊销只有在质量可比、计量单位一致且每次查询节省为正时才计算。
当前结果只能说明策略偏安全但误失效仍高，不能证明维护收益或构建提速。
