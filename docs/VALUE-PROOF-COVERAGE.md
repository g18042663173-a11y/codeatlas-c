# CodeAtlas Wiki 收益实验覆盖清点

- 状态：`coverage_blocked`
- 覆盖门禁：`未通过`
- 清点哈希：`cf07279e2e77a5b8169a69f6aa42f387dadf971c2b4cccd1984a86d95a747d60`

| 语料 | 合格未曝光机制 | 目标 | 已选 | 状态 |
|---|---:|---:|---:|---|
| cjson | 38 | 40 | 38 | `coverage_blocked` |
| lwip | 530 | 40 | 40 | `candidate_ready_for_source_review` |

## 判定边界

Candidates are not questions or effect evidence. Two source reviews are required before protocol_ready; insufficient coverage is never filled by splitting functions.

该结果只回答是否有足够的未曝光公开入口进入双份源码复核。它不是题集、模型效果或费用授权。覆盖不足时不会拆分函数、复用旧题或增加语料凑数。
