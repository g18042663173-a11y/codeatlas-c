# CodeAtlas 正式知识卡集中审核包（2026-09-06）

状态：`approved_by_user_confirmation`。用户于 2026-09-06 明确确认本文件绑定的两个审核包；系统随后按统一门禁完成 QA、确认、批准和发布。

## 审核边界

- 两例都来自固定公开源码和仓库内可执行复现实验，不是生产事故。
- AI 只整理材料；最终审核身份必须由用户明确确认后写为 `human`。
- 确认绑定当前 `review_bundle_hash`。正文、依赖、锚点、实验或 QA 任一变化后，旧确认自动失效。
- 原始会话不进入检索；只有完成 QA、确认和批准的卡片才成为 B 级证据。

## 1. cJSON 嵌套深度边界

卡片：`e-0f1743530a0e`  
当前审核包：`6f624a45bf16d7a76876b8c33acaa8fa4c6fc269435e1dd7ac550a30aa95d56c`

### 已确认内容

- 现象：公开复现实验中，深层嵌套 JSON 在超过限定深度后返回 `NULL`。
- 根因：固定版本的 `parse_array` / `parse_object` 在递归前检查
  `input_buffer->depth >= CJSON_NESTING_LIMIT`；本例不是内存耗尽或进程栈不足的证据。
- 处理：调用方校验深度并处理 `NULL`；只有接受递归和资源风险并补齐边界测试时，才调整编译期限制。
- 验证：固定输入深度 999、1000、1001 分别得到 `ok`、`ok`、`NULL`；失败例记录
  `parse_end=1000`、`input_len=2003`。

### 源码与依赖

- 主锚点：`c:cJSON.c@F@parse_value`，`cJSON.c:1368-1420`，关系 `constrains`。
- 明确函数依赖：`parse_array`、`parse_object`、`cJSON_ParseWithOpts`、
  `cJSON_ParseWithLengthOpts`。
- 明确宏依赖：`CJSON_NESTING_LIMIT`；同时绑定本次构建配置和上述函数的结构化骨架。
- 关键事实：`parse_value` 分派数组/对象；两个下层解析函数执行深度保护；
  `cJSON_ParseWithOpts` 委托给 `cJSON_ParseWithLengthOpts`，后者在失败路径更新 `return_parse_end`。

复现入口：`examples/reproductions/cjson-nesting/run.sh`。本次重跑与已冻结 `result.txt` 一致。

### QA

问题：为什么超过 `CJSON_NESTING_LIMIT` 后解析返回 `NULL`，调用方应如何处理？

期望要点：

1. `parse_array` / `parse_object` 在递归前检查嵌套深度。
2. 调用方处理 `NULL`；调整编译期限制前补齐固定边界测试并评估资源风险。

## 2. lwIP 接口链表与默认接口分离

卡片：`e-13d209f2a9b2`  
当前审核包：`9fb6b15935d33ef15cfb1305f841f294f5ec2ec036a778843f15cf14e3ae4f08`

### 已确认内容

- 现象：固定多接口配置中，`netif_add` 成功后 `netif_list` 更新，但 `netif_default` 不自动指向新接口。
- 根因：加入接口链表与选择默认接口是两个动作；不能把 `netif_add` 当作默认路由选择操作。
- 处理：`netif_add` 成功后按路由策略显式调用 `netif_set_default`；新增接口不应无条件覆盖既有默认接口。
- 验证：首个接口加入后 `list=first/default=none`；显式设置后 `default=first`；
  追加第二个接口后 `list=second/default=first`。

### 源码与依赖

- 主锚点：`c:@F@netif_add`，`src/core/netif.c:286-458`，关系 `explains`。
- 明确实体依赖：`netif_add`、`netif_set_default`、`netif_list`、`netif_default`；
  同时绑定本次构建配置、直接类型/字段/全局引用和结构化骨架。
- 关键事实：`netif_add` 在初始化成功后把接口前插到 `netif_list`；
  `netif_set_default` 才写入 `netif_default`。
- 限定条件：结论针对仓库固定的多接口测试配置；`LWIP_SINGLE_NETIF` 等配置变化会触发复审。

复现入口：`examples/reproductions/lwip-netif/run.sh`。本次重跑与已冻结 `result.txt` 一致；
编译出现两个未使用参数警告，不影响断言与程序退出码。

### QA

问题：`netif_add` 为什么不会自动更新 `netif_default`，调用方应怎么做？

期望要点：

1. `netif_add` 负责成功初始化并把接口加入 `netif_list`。
2. 调用方根据路由策略显式调用 `netif_set_default`，不要假设最后加入者自动成为默认接口。

## 执行结果

按上述两个固定审核包哈希完成：

1. 两条 QA 均记录为 `Guoshuaiqi / human / formal / passed`；
2. 两个审核包均有当前 human 确认，确认后哈希未变化；
3. 两张卡均为 `approved_current=true`，已原子发布 Markdown；
4. 两个经验 chunk 均为可见的 B 级证据。

当前 active 快照及检索核验：

- cJSON：`ks-2745704368ee44e0`；卡片在对应问题的 Top-10 中排名 10，引用包含仓库、revision、USR、源码范围和定义哈希。
- lwIP：`ks-156288ba49fa42bb`；卡片在对应问题的 Top-10 中排名 10，引用字段同样完整。
- 两个 active 快照均为 `ready`。一次历史 lwIP 快照曾发生越过治理投影的写入，已保留并隔离为 `failed`，不会进入当前查询。

旧 40 题开发清单仍绑定早期人工示例卡标题，且部分 lwIP 问题属于旧材料语义；因此本次重跑仍为 30/40。该结果保留为评测契约不一致，不能通过事后改旧 gold 把它包装成 40/40。两张正式卡的检索与引用改由独立、版本化且与确认内容一致的验收验证。
