# cJSON 精选开发任务集（20 题） — 轻量任务评测

> **审核状态：清单标记已审核；仅作开发回归**。任务 gold 由人工维护；本报告展示可执行任务的验证结果，不替代自动消融报告。

> 已用于开发调整，不是独立留出集；召回通过不代表模型理解更好。

## 运行快照

| 项目 | 值 |
|---|---|
| 公开语料 | cJSON 公开源码固定快照 |
| 仓库 | `https://github.com/DaveGamble/cJSON` |
| Git revision | `fb16e5cf358798aabb049655975cde8427101056` |
| 解析模式 | compile database |
| 任务清单 | `eval/tasks_cjson.yaml` |
| 审核人 / 日期 | Guoshuaiqi / 2026-09-04 |
| 失败规则 | 运行时未命中固定锚点、provenance 不完整或门禁不满足即记录为失败，不调整 gold 隐藏失败。 |
| 审核状态 | draft 0 / approved 20 |
| 命令 | `codeatlas task-eval --db data/kb.db --tasks eval/tasks_cjson.yaml --embedder tfidf --out docs/TASK-EVAL-CJSON.md` |

## 汇总

### 检索任务（10 题；逐层消融）

| 配置 | Recall@10 | MRR@10 | 命中锚点的完整 A 级 provenance | 平均上下文 token | P50 / P95 |
|---|---:|---:|---:|---:|---:|
| 仅 BM25 | 90.0% | 0.439 | 90.0% | 3957 | 2 / 9ms |
| BM25 + 符号 | 80.0% | 0.585 | 80.0% | 4053 | 2 / 3ms |
| + 本地向量 | 80.0% | 0.568 | 80.0% | 4097 | 3 / 30ms |
| + certain 图扩展 | 90.0% | 0.611 | 90.0% | 5193 | 5 / 10ms |
| 完整无模型 | 100.0% | 0.711 | 100.0% | 5193 | 6 / 9ms |

### 结构与拒答任务（当前完整无模型方案）

| 能力 | 题数 | 指标 | 延迟 P50 / P95 |
|---|---:|---|---:|
| certain 影响集合 | 2 | P 1.0 / R 1.0 / F1 1.0 | 0 / 1ms |
| 无证据拒答 | 3 | 准确率 100.0% | 3 / 3ms |
| 已审核经验复用 | 5 | B 卡 Recall@10 0.0% / MRR 0.0 | 3 / 6ms |

### 开发回归检索门槛（非模型效果门槛）

- 完整方案相对 BM25-only：Recall@10 +10.0pt，MRR@10 +0.272。
- 门槛：Recall@10 至少 +10pt 且 MRR@10 不低于 -0.02；本轮：**通过**。

## 逐题结果

### locate-parse-options · 定位 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：cJSON_ParseWithOpts 在哪里定义，它是哪个解析入口？
- 预期锚点：cJSON_ParseWithOpts (cJSON.c:L1131)
- 源码：[permalink](https://github.com/DaveGamble/cJSON/blob/fb16e5cf358798aabb049655975cde8427101056/cJSON.c#L1131-L1144)；判定理由：公开 API 的实现位置是解析入口定位任务的明确源码锚点。
- 仅 BM25：命中=True，MRR=0.25，provenance 完整=True。
- 完整无模型：命中=True，MRR=1.0，provenance 完整=True。

### locate-delete · 定位 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：cJSON_Delete 的定义位置和释放入口在哪里？
- 预期锚点：cJSON_Delete (cJSON.c:L253)
- 源码：[permalink](https://github.com/DaveGamble/cJSON/blob/fb16e5cf358798aabb049655975cde8427101056/cJSON.c#L253-L276)；判定理由：递归释放函数可通过稳定的公开 API 名称和定义范围定位。
- 仅 BM25：命中=False，MRR=0.0，provenance 完整=False。
- 完整无模型：命中=True，MRR=1.0，provenance 完整=True。

### callers-parse-value · 调用链 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：哪些函数直接调用 parse_value？
- 预期锚点：cJSON_ParseWithLengthOpts (cJSON.c:L1147)；parse_array (cJSON.c:L1497)；parse_object (cJSON.c:L1662)
- 源码：[permalink](https://github.com/DaveGamble/cJSON/blob/fb16e5cf358798aabb049655975cde8427101056/cJSON.c#L1368-L1420)；判定理由：三个调用者均由固定快照中的 certain AST 调用边确认。
- 仅 BM25：命中=True，MRR=0.2，provenance 完整=True。
- 完整无模型：命中=True，MRR=0.333，provenance 完整=True。

### callers-add-item · 调用链 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：谁直接调用 cJSON_AddItemToObject？
- 预期锚点：apply_patch (cJSON_Utils.c:L807)；create_objects (test.c:L109)
- 源码：[permalink](https://github.com/DaveGamble/cJSON/blob/fb16e5cf358798aabb049655975cde8427101056/cJSON.c#L2119-L2122)；判定理由：一个工具实现和一个公开测试调用点可验证跨文件调用链检索。
- 仅 BM25：命中=True，MRR=0.143，provenance 完整=True。
- 完整无模型：命中=True，MRR=0.167，provenance 完整=True。

### semantic-parse-tree · 语义检索 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：JSON parser 如何处理 object values？
- 预期锚点：parse_value (cJSON.c:L1368)；parse_object (cJSON.c:L1662)；parse_string (cJSON.c:L824)
- 源码：[permalink](https://github.com/DaveGamble/cJSON/blob/fb16e5cf358798aabb049655975cde8427101056/cJSON.c#L1368-L1420)；判定理由：不直接给出符号名，检查自然语言是否能回到解析分派与对象/字符串处理实现。
- 仅 BM25：命中=True，MRR=1.0，provenance 完整=True。
- 完整无模型：命中=True，MRR=1.0，provenance 完整=True。

### semantic-serialize · 语义检索 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：JSON output buffer 如何在 serialization 时 grow？
- 预期锚点：print_value (cJSON.c:L1423)；print_object (cJSON.c:L1780)；print_string (cJSON.c:L1076)
- 源码：[permalink](https://github.com/DaveGamble/cJSON/blob/fb16e5cf358798aabb049655975cde8427101056/cJSON.c#L1423-L1494)；判定理由：不直接给出函数名，检查序列化语义查询是否命中值分派、对象和字符串输出实现。
- 仅 BM25：命中=True，MRR=1.0，provenance 完整=True。
- 完整无模型：命中=True，MRR=0.111，provenance 完整=True。

### locate-print-unformatted · 定位 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：cJSON_PrintUnformatted 的公开输出入口在哪里定义？
- 预期锚点：cJSON_PrintUnformatted (cJSON.c:L1312)
- 源码：[permalink](https://github.com/DaveGamble/cJSON/blob/fb16e5cf358798aabb049655975cde8427101056/cJSON.c#L1312-L1326)；判定理由：固定 revision 上无格式化输出 API 的唯一函数定义。
- 仅 BM25：命中=True，MRR=1.0，provenance 完整=True。
- 完整无模型：命中=True，MRR=1.0，provenance 完整=True。

### locate-object-item-sensitive · 定位 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：区分大小写查找对象成员的函数定义在哪里？
- 预期锚点：cJSON_GetObjectItemCaseSensitive (cJSON.c:L1988)
- 源码：[permalink](https://github.com/DaveGamble/cJSON/blob/fb16e5cf358798aabb049655975cde8427101056/cJSON.c#L1988-L1991)；判定理由：自然语言接口意图对应固定源码中的唯一公开实现。
- 仅 BM25：命中=True，MRR=0.5，provenance 完整=True。
- 完整无模型：命中=True，MRR=0.5，provenance 完整=True。

### locate-compare · 定位 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：cJSON 节点深度比较的入口 cJSON_Compare 在哪里实现？
- 预期锚点：cJSON_Compare (cJSON.c:L3072)
- 源码：[permalink](https://github.com/DaveGamble/cJSON/blob/fb16e5cf358798aabb049655975cde8427101056/cJSON.c#L3072-L3179)；判定理由：比较入口的源码范围在固定 revision 上唯一。
- 仅 BM25：命中=True，MRR=0.167，provenance 完整=True。
- 完整无模型：命中=True，MRR=1.0，provenance 完整=True。

### semantic-compare-tree · 语义检索 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：两棵 JSON 树怎样递归比较类型、数组元素和对象成员？
- 预期锚点：cJSON_Compare (cJSON.c:L3072)
- 源码：[permalink](https://github.com/DaveGamble/cJSON/blob/fb16e5cf358798aabb049655975cde8427101056/cJSON.c#L3072-L3179)；判定理由：不直接依赖完整函数名，检查树比较语义能否回到实现锚点。
- 仅 BM25：命中=True，MRR=0.125，provenance 完整=True。
- 完整无模型：命中=True，MRR=1.0，provenance 完整=True。

### impact-parse-value · 影响分析 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；目标：parse_value (cJSON.c:L1368)；深度：2
- certain gold：cJSON_ParseWithLengthOpts (cJSON.c:L1147)；parse_array (cJSON.c:L1497)；parse_object (cJSON.c:L1662)；parse_value (cJSON.c:L1368)；assert_parse_value (tests/parse_value.c:L44)；assert_print_value (tests/print_value.c:L31)；assert_print_object (tests/print_object.c:L27)；print_value_should_print_object (tests/print_value.c:L88)；assert_parse_object (tests/parse_object.c:L65)；parse_value_should_parse_string (tests/parse_value.c:L79)；assert_parse_array (tests/parse_array.c:L56)；print_value_should_print_string (tests/print_value.c:L77)；print_value_should_print_array (tests/print_value.c:L83)；print_value_should_print_false (tests/print_value.c:L67)；assert_not_array (tests/parse_array.c:L45)；parse_value_should_parse_false (tests/parse_value.c:L67)；parse_value_should_parse_object (tests/parse_value.c:L93)；print_value_should_print_true (tests/print_value.c:L62)；cJSON_ParseWithOpts (cJSON.c:L1131)；parse_value_should_parse_null (tests/parse_value.c:L55)；print_value_should_print_null (tests/print_value.c:L57)；cJSON_ParseWithLength (cJSON.c:L1232)；assert_print_array (tests/print_array.c:L27)；parse_value_should_parse_true (tests/parse_value.c:L61)；print_value_should_print_number (tests/print_value.c:L72)；parse_value_should_parse_number (tests/parse_value.c:L73)；assert_not_object (tests/parse_object.c:L53)；parse_value_should_parse_array (tests/parse_value.c:L87)
- 源码：[permalink](https://github.com/DaveGamble/cJSON/blob/fb16e5cf358798aabb049655975cde8427101056/cJSON.c#L1368-L1420)；判定理由：金标是固定 revision 与正式 compile database 下完整的 2 跳 certain 反向可达集，包含公开测试调用者与自递归。
- 结果：P=1.0 / R=1.0 / F1=1.0；candidate=0（独立展示=True）。

### impact-parse-options · 影响分析 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；目标：cJSON_ParseWithOpts (cJSON.c:L1131)；深度：2
- certain gold：cJSON_Parse (cJSON.c:L1227)；parse_with_opts_should_parse_utf8_bom (tests/parse_with_opts.c:L84)；LLVMFuzzerTestOneInput (fuzzing/cjson_read_fuzzer.c:L13)；parse_with_opts_should_require_null_if_requested (tests/parse_with_opts.c:L62)；cjson_functions_should_not_crash_with_null_pointers (tests/misc_tests.c:L384)；parse_with_opts_should_handle_empty_strings (tests/parse_with_opts.c:L39)；parse_with_opts_should_handle_incomplete_json (tests/parse_with_opts.c:L52)；parse_with_opts_should_return_parse_end (tests/parse_with_opts.c:L73)；parse_with_opts_should_handle_null (tests/parse_with_opts.c:L27)；main (fuzzing/afl.c:L85)；main (fuzzing/fuzz_main.c:L9)；cjson_get_object_item_case_sensitive_should_get_object_items (tests/misc_tests.c:L98)；file_test6_should_not_be_parsed (tests/parse_examples.c:L134)；cjson_parse_big_numbers_should_not_report_error (tests/misc_tests.c:L786)；cjson_delete_item_from_array_should_not_broken_list_structure (tests/misc_tests.c:L659)；cjson_get_object_item_should_not_crash_with_array (tests/misc_tests.c:L129)；cjson_get_object_item_should_get_object_items (tests/misc_tests.c:L67)；cjson_compare_should_compare_raw (tests/compare_tests.c:L121)；supports_full_hd (tests/readme_examples.c:L169)；merge_tests (tests/old_utils_tests.c:L168)；cjson_set_valuestring_should_return_null_if_strings_overlap (tests/misc_tests.c:L492)；cjson_should_not_parse_to_deeply_nested_jsons (tests/misc_tests.c:L209)；generate_merge_tests (tests/old_utils_tests.c:L192)；parse_test_file (tests/json_patch_tests.c:L32)；compare_from_string (tests/compare_tests.c:L27)；parse_file (tests/parse_examples.c:L31)；json_pointer_tests (tests/old_utils_tests.c:L52)；cjson_set_valuestring_to_object_should_not_leak_memory (tests/misc_tests.c:L693)；test12_should_not_be_parsed (tests/parse_examples.c:L182)；cjson_get_object_item_case_sensitive_should_not_crash_with_array (tests/misc_tests.c:L141)
- 源码：[permalink](https://github.com/DaveGamble/cJSON/blob/fb16e5cf358798aabb049655975cde8427101056/cJSON.c#L1131-L1144)；判定理由：该金标覆盖正式 compile database 中完整的 2 跳 certain 集，包括 API 包装、fuzz 入口和公开测试调用者。
- 结果：P=1.0 / R=1.0 / F1=1.0；candidate=0（独立展示=True）。

### refuse-kubernetes-tls · 无证据拒答 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：如何轮换 Kubernetes Ingress 的 TLS 证书？
- 语料范围参考：[permalink](https://github.com/DaveGamble/cJSON/blob/fb16e5cf358798aabb049655975cde8427101056/cJSON.c#L1-L20)；判定理由：Kubernetes 运维知识不属于 cJSON 公开源码范围，不能用无关 A 级片段拼凑答案。
- 结果：refused=True，A/B 证据=0。

### refuse-postgresql-slow-query · 无证据拒答 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：如何排查 PostgreSQL 的慢查询？
- 语料范围参考：[permalink](https://github.com/DaveGamble/cJSON/blob/fb16e5cf358798aabb049655975cde8427101056/cJSON.c#L1-L20)；判定理由：数据库排障知识不属于 cJSON 代码事实或已审核经验，系统应明确拒答。
- 结果：refused=True，A/B 证据=0。

### refuse-redis-cluster · 无证据拒答 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：Redis Cluster 跨槽事务失败应该如何排查？
- 语料范围参考：[permalink](https://github.com/DaveGamble/cJSON/blob/fb16e5cf358798aabb049655975cde8427101056/cJSON.c#L1-L20)；判定理由：分布式缓存排障不属于 cJSON 代码与已审核经验边界。
- 结果：refused=True，A/B 证据=0。

### exp-cjson-symptom · 已审核经验复用 · 失败

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：深层嵌套 JSON 输入触发解析失败时，历史审查记录描述了什么现象？
- 预期 B 卡：`cJSON 深层嵌套输入审查示例`；主锚点：parse_value (cJSON.c:L1368)
- 结果：命中=False，MRR=0.0，锚点完整=False。

### exp-cjson-dead-end · 已审核经验复用 · 失败

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：深层嵌套解析问题中，为什么只增大进程栈不是可靠修复？
- 预期 B 卡：`cJSON 深层嵌套输入审查示例`；主锚点：parse_value (cJSON.c:L1368)
- 结果：命中=False，MRR=0.0，锚点完整=False。

### exp-cjson-fix · 已审核经验复用 · 失败

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：已审核经验建议如何核对嵌套层级限制和失败返回？
- 预期 B 卡：`cJSON 深层嵌套输入审查示例`；主锚点：parse_value (cJSON.c:L1368)
- 结果：命中=False，MRR=0.0，锚点完整=False。

### exp-cjson-verify · 已审核经验复用 · 失败

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：深层嵌套输入审查应覆盖哪些边界用例？
- 预期 B 卡：`cJSON 深层嵌套输入审查示例`；主锚点：parse_value (cJSON.c:L1368)
- 结果：命中=False，MRR=0.0，锚点完整=False。

### exp-cjson-anchor · 已审核经验复用 · 失败

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：深层嵌套解析经验当前绑定的主函数和源码范围是什么？
- 预期 B 卡：`cJSON 深层嵌套输入审查示例`；主锚点：parse_value (cJSON.c:L1368)
- 结果：命中=False，MRR=0.0，锚点完整=False。

## 失败案例

> 真实能力失败 0；因没有当前 approved B 卡而被审核门禁阻塞 5。两类结果不会合并包装。

- **blocked_by_review · exp-cjson-symptom**：未命中当前 approved B 卡或主锚点 provenance 不完整
- **blocked_by_review · exp-cjson-dead-end**：未命中当前 approved B 卡或主锚点 provenance 不完整
- **blocked_by_review · exp-cjson-fix**：未命中当前 approved B 卡或主锚点 provenance 不完整
- **blocked_by_review · exp-cjson-verify**：未命中当前 approved B 卡或主锚点 provenance 不完整
- **blocked_by_review · exp-cjson-anchor**：未命中当前 approved B 卡或主锚点 provenance 不完整

## 口径与边界

- 定位、调用链和语义检索只检查 top-10 是否命中人工指定源码锚点；完整方案的命中还必须是含仓库、revision、USR、文件和行号的 A 级证据。
- 影响分析只对 certain 反向可达集计分；candidate 只作独立线索展示，绝不并入 F1。
- 拒答题要求 `refused=true` 且 `A/B=0`；全程显式传入 `llm_client=None`，没有模型调用。
- 本集只有 20 道人工维护任务，用于求职演示与失败复盘；它不具备统计显著性，不替代自动生成回归集的消融报告。
