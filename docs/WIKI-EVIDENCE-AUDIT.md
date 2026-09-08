# Wiki 评分输入审计（尚未重评）

2026-09-08。本次先检查评分器拿到了什么，而不是预设模型应当得更高分。

- 432 个冻结 Wiki 答案的引用编号映射没有变化。
- 范围检查发现 134 个试次需要复核（cJSON 62、lwIP 72）。这不是 134 个错误答案：大范围引用、拒答题和已有其他完整证据的情况需要分别判断。
- 原始工具事件能够恢复 81 个试次的 117 条被引用正文（cJSON 33/41、lwIP 48/76）。恢复只使用原始事件中保存的字节，匹配 repository、revision、快照、构建配置及代码范围或页面输入哈希，不读取今天生成的 Wiki。
- 直接恢复正文后的 lwIP 包有 10 个超过 8,000 估算 token；不得静默截断、提高预算或先付费再处理。
- 本轮尚未发送新的模型请求，未改答案、gold、旧评分和原始试次哈希。

## 已确认的漏正文案例

试次 `cc559c5a9f5b88237528403bc7acd6e237c82d12004498c54b63704d632afbaf`，题目 `ho-lwip-flow-dhcp-start-discover`。

答案引用原 `C1`（盲审后 `E25`），说明 `dhcp_inc_pcb_refcount` 涉及共享 PCB 的创建、绑定、连接和接收回调设置。原始 `wiki_section` 事件的“确定调用流程”正文包含以下四条调用边：

```text
dhcp_inc_pcb_refcount -> udp_new
dhcp_inc_pcb_refcount -> udp_bind
dhcp_inc_pcb_refcount -> udp_connect
dhcp_inc_pcb_refcount -> udp_recv
```

旧评分包却只保留 `page_id`、`input_hash` 等元数据；judge-b 和仲裁者明确因 E25 缺少可核查正文而判 `mismatched`。该判断受到评分输入缺失影响，不能当作知识层无效的证据，也不能直接把整个答案改为正确。

来源身份：lwIP `3d896ba0a37ff3ce73270ca5e230707fe47f60e3`；快照 `ks-156288ba49fa42bb`；页面 `files/src__core__ipv4__dhcp.c`；输入哈希 `37364636a18359ff`。旧快照 Markdown 66–69 行和原始事件相互吻合。固定源码 `src/core/ipv4/dhcp.c:272–296` 可独立核验该调用事实。

## 当前门禁

两套 Wiki 历史语义汇总均标记为 `pending_evidence_audit`，暂不用于效果主张；不是将所有旧答案判错。旧 JSON、评分和模型资格记录仍保留。知识卡 54 个旧评分的明确失效另见 [知识卡审计](REUSE-EVIDENCE-AUDIT.md)。

下一步须冻结逐项证据审计及所需重评范围，再在已授权的请求上限内执行。是否调整本轮“旧评分修正 / 新开发复验”的安排，等待用户明确选择；不会自动增加付费范围。

机器可核对通知：[cJSON](WIKI-CJSON-EVIDENCE-AUDIT.json)、[lwIP](WIKI-LWIP-EVIDENCE-AUDIT.json)。通知绑定历史报告文件哈希和原始 run hash；篡改或缺失不得恢复效果资格。

## 本轮实现检查点

全量 Python 383 项、UI 8 项通过。实现同行审查第二轮通过；下表只评价预算、恢复、选择性评分和正文传递的代码，不是项目效果指标。

| 正确性 | 完整性 | 范围控制 | 可测试性 | 综合 |
|---:|---:|---:|---:|---:|
| 8.5 | 8.0 | 9.0 | 8.0 | 8.4 |
