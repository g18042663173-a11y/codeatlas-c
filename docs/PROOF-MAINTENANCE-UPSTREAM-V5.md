# CodeAtlas cJSON / lwIP 真实上游维护实验

> 状态：`executed`；执行 24/24；阻塞 0；固定上游归档=`true`。

## 结果

- 与预登记 outcome 一致：24/24。
- 当前保守依赖决策正确：16/16；有效变更漏放 0/8；无关变更误失效 0/8；依赖指纹耗时 825.437 ms。
- 主锚点基线决策正确：13/16；全部失效基线：8/16。
- 24 场景全部实际执行：`true`。
- outcome：`{"artifact_tamper": 2, "compile_failed": 2, "completed": 16, "governance_rolled_back": 2, "publication_interrupted": 2}`。

| 场景 | 语料 | 分类 | 执行状态 | outcome | 决策 |
|---|---|---|---|---|---|
| upstream-cjson-effective-depth-condition | cjson | effective | ready | completed | revalidate |
| upstream-cjson-effective-nesting-config | cjson | effective | ready | completed | revalidate |
| upstream-cjson-effective-parse-value-dispatch | cjson | effective | ready | completed | revalidate |
| upstream-cjson-effective-failure-position | cjson | effective | ready | completed | revalidate |
| upstream-cjson-unrelated-version | cjson | unrelated_to_claim | ready | completed | keep |
| upstream-cjson-unrelated-print-buffer | cjson | unrelated_to_claim | ready | completed | keep |
| upstream-cjson-unrelated-number-parser | cjson | unrelated_to_claim | ready | completed | keep |
| upstream-cjson-unrelated-case-comparison | cjson | unrelated_to_claim | ready | completed | keep |
| upstream-cjson-failure-compile | cjson | failure | ready | compile_failed | — |
| upstream-cjson-failure-artifact-tamper | cjson | failure | ready | artifact_tamper | — |
| upstream-cjson-failure-publication | cjson | failure | ready | publication_interrupted | — |
| upstream-cjson-failure-governance | cjson | failure | ready | governance_rolled_back | — |
| upstream-lwip-effective-state-reset | lwip | effective | ready | completed | revalidate |
| upstream-lwip-effective-single-netif-config | lwip | effective | ready | completed | revalidate |
| upstream-lwip-effective-default-list-operation | lwip | effective | ready | completed | revalidate |
| upstream-lwip-effective-init-failure-return | lwip | effective | ready | completed | revalidate |
| upstream-lwip-unrelated-link-up | lwip | unrelated_to_claim | ready | completed | keep |
| upstream-lwip-unrelated-admin-up | lwip | unrelated_to_claim | ready | completed | keep |
| upstream-lwip-unrelated-hostname | lwip | unrelated_to_claim | ready | completed | keep |
| upstream-lwip-unrelated-client-data | lwip | unrelated_to_claim | ready | completed | keep |
| upstream-lwip-failure-compile | lwip | failure | ready | compile_failed | — |
| upstream-lwip-failure-artifact-tamper | lwip | failure | ready | artifact_tamper | — |
| upstream-lwip-failure-publication | lwip | failure | ready | publication_interrupted | — |
| upstream-lwip-failure-governance | lwip | failure | ready | governance_rolled_back | — |

## 边界

Real patches run only in git-archived temporary copies of pinned public upstream commits. The dependency adapter evaluates the reviewed card function/macro manifest; publication faults execute the production durable prepare/commit/recover journal, but this is not a timed full parse/index rebuild. No production Wiki, database, or network is mutated.

这里的当前策略按仓库现有保守依赖边界重算；主锚点与全部失效只是对照。源码补丁、编译、运行断言与四类故障控制都是真实执行；快照/卡片/Wiki 阶段验证的是本地事务证据，不冒充完整解析和索引重建计时。24 个场景执行完成不等于维护收益已经成立：没有同单位的知识生产、真人复审与查询节省，因此摊销价值仍为未测。
