# 两张知识卡的最终待审材料

状态：`ai_assisted_draft / pending`。本文件仅集中提供审核内容，没有执行批准；需用户明确确认后才记录审核身份并完成 QA/发布。绑定依赖或修改任何字段后，以下 bundle 必须重新取得。

2026-09-06 已在固定源码 revision 上重新运行两套公开脚本，实际输出均与仓库内 `result.txt` 完全一致。下面的材料哈希由当前卡片内容、精确锚点、依赖清单、实验文件和 QA 定义共同计算；它不是人工批准凭据。

## cJSON · e-0f1743530a0e

- 现象：公开复现的深层嵌套输入超过限定深度后解析返回 NULL，不是生产事故。
- 结论：本实验失败来自 CJSON_NESTING_LIMIT 的主动保护，不能泛化为任意深层输入失败的唯一原因。
- 处理：调用方校验输入深度、处理 NULL；只有明确接受递归/资源风险并补齐边界测试，才调整编译期限制。
- 实验：`examples/reproductions/cjson-nesting/run.sh`；999/1000 成功，1001 为 NULL、parse_end=1000。
- 主锚点：cJSON.c:1368–1420，USR `c:cJSON.c@F@parse_value`，关系 `constrains`，commit `fb16e5cf358798aabb049655975cde8427101056`。
- 下层依赖覆盖：parse_array、parse_object、CJSON_NESTING_LIMIT、类型/配置等，当前保守记录 1129 项。四份公开实验文件哈希已纳入材料。
- QA：为什么超限后返回 NULL，调用方如何处理？期待核对下层递归前深度检查、调用方错误处理和配置变更风险。状态 pending。
- 材料哈希：`f5bb86c8520b3ff82747c88cd777e500a0c21d4ea5a67674135d1319481c7749`。

## lwIP · e-13d209f2a9b2

- 现象：公开多接口实验中 netif_list 改变，但 netif_default 不自动指向新接口。
- 结论：在本固定配置中，接口加入链表和选择默认接口是两种操作；不是复现了真实业务事故。
- 处理：netif_add 成功后按路由策略显式 netif_set_default，不假定最后加入的接口会覆盖旧默认接口。
- 实验：`examples/reproductions/lwip-netif/run.sh`；首接口 list=first/default=none，显式设置后 default=first；第二接口 list=second/default=first。
- 主锚点：src/core/netif.c:286–458，USR `c:@F@netif_add`，关系 `explains`，commit `3d896ba0a37ff3ce73270ca5e230707fe47f60e3`。
- 依赖覆盖：默认接口操作、相关宏/类型和构建配置，当前保守记录 6384 项。五份公开实验文件哈希已纳入材料。
- QA：为何添加接口不自动更新默认接口，调用方如何处理？期待核对链表操作与显式设置逻辑。状态 pending。
- 材料哈希：`a1ae3062b38857e8cd16552b4c4d0f23b38c8e260d1dd2d1858f48247ecad609`。

相对旧稿，本轮只重新绑定当前源码、补齐完整依赖与实验哈希、作废旧 QA 适用性；没有把自动检查写成人工审核，也没有让卡片进入 B 级检索。用户确认后仍需以相同审核身份依次通过当前 bundle 的 QA、确认该 bundle 并批准卡片；任一材料在中途变化都会触发冲突并要求重审。
