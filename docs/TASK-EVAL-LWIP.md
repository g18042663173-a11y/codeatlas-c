# lwIP 精选开发任务集（20 题） — 轻量任务评测

> **审核状态：清单标记已审核；仅作开发回归**。任务 gold 由人工维护；本报告展示可执行任务的验证结果，不替代自动消融报告。

> 已用于开发调整，不是独立留出集；召回通过不代表模型理解更好。

## 运行快照

| 项目 | 值 |
|---|---|
| 公开语料 | lwIP 公开源码固定快照 |
| 仓库 | `https://github.com/lwip-tcpip/lwip` |
| Git revision | `3d896ba0a37ff3ce73270ca5e230707fe47f60e3` |
| 解析模式 | compile database |
| 任务清单 | `eval/tasks_lwip.yaml` |
| 审核人 / 日期 | Guoshuaiqi / 2026-09-04 |
| 失败规则 | 运行时未命中固定锚点、provenance 不完整或门禁不满足即记录为失败，不调整 gold 隐藏失败。 |
| 审核状态 | draft 0 / approved 20 |
| 命令 | `codeatlas task-eval --db data/lwip.db --tasks eval/tasks_lwip.yaml --embedder tfidf --out docs/TASK-EVAL-LWIP.md` |

## 汇总

### 检索任务（10 题；逐层消融）

| 配置 | Recall@10 | MRR@10 | 命中锚点的完整 A 级 provenance | 平均上下文 token | P50 / P95 |
|---|---:|---:|---:|---:|---:|
| 仅 BM25 | 80.0% | 0.417 | 80.0% | 3678 | 8 / 12ms |
| BM25 + 符号 | 90.0% | 0.65 | 90.0% | 3750 | 18 / 35ms |
| + 本地向量 | 100.0% | 0.784 | 100.0% | 3804 | 20 / 149ms |
| + certain 图扩展 | 100.0% | 0.814 | 100.0% | 5386 | 23 / 42ms |
| 完整无模型 | 100.0% | 0.814 | 100.0% | 5386 | 27 / 44ms |

### 结构与拒答任务（当前完整无模型方案）

| 能力 | 题数 | 指标 | 延迟 P50 / P95 |
|---|---:|---|---:|
| certain 影响集合 | 2 | P 1.0 / R 1.0 / F1 1.0 | 0 / 0ms |
| 无证据拒答 | 3 | 准确率 100.0% | 18 / 29ms |
| 已审核经验复用 | 5 | B 卡 Recall@10 0.0% / MRR 0.0 | 21 / 32ms |

### 开发回归检索门槛（非模型效果门槛）

- 完整方案相对 BM25-only：Recall@10 +20.0pt，MRR@10 +0.397。
- 门槛：Recall@10 至少 +10pt 且 MRR@10 不低于 -0.02；本轮：**通过**。

## 逐题结果

### locate-netif-add · 定位 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：netif_add 在哪里定义，它负责哪段网络接口初始化？
- 预期锚点：netif_add (src/core/netif.c:L286)
- 源码：[permalink](https://github.com/lwip-tcpip/lwip/blob/3d896ba0a37ff3ce73270ca5e230707fe47f60e3/src/core/netif.c#L286-L458)；判定理由：固定快照中的公开网络接口添加入口，定义位置唯一。
- 仅 BM25：命中=True，MRR=0.25，provenance 完整=True。
- 完整无模型：命中=True，MRR=1.0，provenance 完整=True。

### locate-tcp-input · 定位 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：TCP 收包主入口 tcp_input 在哪里实现？
- 预期锚点：tcp_input (src/core/tcp_in.c:L117)
- 源码：[permalink](https://github.com/lwip-tcpip/lwip/blob/3d896ba0a37ff3ce73270ca5e230707fe47f60e3/src/core/tcp_in.c#L117-L592)；判定理由：固定快照中 TCP 输入处理主函数的定义锚点唯一。
- 仅 BM25：命中=True，MRR=1.0，provenance 完整=True。
- 完整无模型：命中=True，MRR=1.0，provenance 完整=True。

### callers-netif-add · 调用链 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：哪些函数直接调用 netif_add？
- 预期锚点：netif_init (src/core/netif.c:L187)；netif_add_noaddr (src/core/netif.c:L249)；ppp_new (src/netif/ppp/ppp.c:L649)
- 源码：[permalink](https://github.com/lwip-tcpip/lwip/blob/3d896ba0a37ff3ce73270ca5e230707fe47f60e3/src/core/netif.c#L286-L458)；判定理由：三个调用者均由 compile database 下的 certain AST 调用边确认。
- 仅 BM25：命中=True，MRR=0.167，provenance 完整=True。
- 完整无模型：命中=True，MRR=0.5，provenance 完整=True。

### callers-tcp-input · 调用链 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：IPv4 和 IPv6 输入路径中谁直接调用 tcp_input？
- 预期锚点：ip4_input (src/core/ipv4/ip4.c:L459)；ip6_input (src/core/ipv6/ip6.c:L508)
- 源码：[permalink](https://github.com/lwip-tcpip/lwip/blob/3d896ba0a37ff3ce73270ca5e230707fe47f60e3/src/core/tcp_in.c#L117-L592)；判定理由：两条 IP 输入路径到 TCP 主入口的调用均为 certain 编译器事实。
- 仅 BM25：命中=False，MRR=0.0，provenance 完整=False。
- 完整无模型：命中=True，MRR=0.5，provenance 完整=True。

### semantic-pbuf-release · 语义检索 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：packet buffer 的引用计数归零后如何释放，并怎样去掉链表头部？
- 预期锚点：pbuf_free (src/core/pbuf.c:L728)；pbuf_free_header (src/core/pbuf.c:L675)；pbuf_dechain (src/core/pbuf.c:L916)
- 源码：[permalink](https://github.com/lwip-tcpip/lwip/blob/3d896ba0a37ff3ce73270ca5e230707fe47f60e3/src/core/pbuf.c#L675-L804)；判定理由：查询不直接给出函数名，检查缓冲区释放、去头和解链语义能否回到源码。
- 仅 BM25：命中=True，MRR=1.0，provenance 完整=True。
- 完整无模型：命中=True，MRR=1.0，provenance 完整=True。

### semantic-netif-state · 语义检索 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：网络接口怎样切换为 up、down 并设置默认接口？
- 预期锚点：netif_set_default (src/core/netif.c:L850)；netif_set_up (src/core/netif.c:L872)；netif_set_down (src/core/netif.c:L950)
- 源码：[permalink](https://github.com/lwip-tcpip/lwip/blob/3d896ba0a37ff3ce73270ca5e230707fe47f60e3/src/core/netif.c#L850-L981)；判定理由：自然语言状态切换问题对应三个公开接口，不依赖图生成题模板。
- 仅 BM25：命中=True，MRR=0.333，provenance 完整=True。
- 完整无模型：命中=True，MRR=1.0，provenance 完整=True。

### locate-pbuf-free · 定位 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：pbuf 引用计数释放入口 pbuf_free 在哪里定义？
- 预期锚点：pbuf_free (src/core/pbuf.c:L728)
- 源码：[permalink](https://github.com/lwip-tcpip/lwip/blob/3d896ba0a37ff3ce73270ca5e230707fe47f60e3/src/core/pbuf.c#L728-L804)；判定理由：固定 revision 中 pbuf 释放入口的定义唯一。
- 仅 BM25：命中=True，MRR=1.0，provenance 完整=True。
- 完整无模型：命中=True，MRR=1.0，provenance 完整=True。

### locate-netif-up · 定位 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：网络接口设置为 up 的函数在哪里实现？
- 预期锚点：netif_set_up (src/core/netif.c:L872)
- 源码：[permalink](https://github.com/lwip-tcpip/lwip/blob/3d896ba0a37ff3ce73270ca5e230707fe47f60e3/src/core/netif.c#L872-L949)；判定理由：自然语言状态操作对应公开 API 的唯一实现。
- 仅 BM25：命中=False，MRR=0.0，provenance 完整=False。
- 完整无模型：命中=True，MRR=0.143，provenance 完整=True。

### locate-tcp-write · 定位 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：TCP 发送数据的 tcp_write 入口在哪里定义？
- 预期锚点：tcp_write (src/core/tcp_out.c:L392)
- 源码：[permalink](https://github.com/lwip-tcpip/lwip/blob/3d896ba0a37ff3ce73270ca5e230707fe47f60e3/src/core/tcp_out.c#L392-L531)；判定理由：固定构建配置下 TCP 写入接口的定义锚点唯一。
- 仅 BM25：命中=True，MRR=0.167，provenance 完整=True。
- 完整无模型：命中=True，MRR=1.0，provenance 完整=True。

### semantic-tcp-send-queue · 语义检索 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：TCP 怎样把应用数据放入发送队列并处理缓冲区限制？
- 预期锚点：tcp_write (src/core/tcp_out.c:L392)
- 源码：[permalink](https://github.com/lwip-tcpip/lwip/blob/3d896ba0a37ff3ce73270ca5e230707fe47f60e3/src/core/tcp_out.c#L392-L531)；判定理由：不直接依赖符号名，检查发送队列语义能否定位到实现。
- 仅 BM25：命中=True，MRR=0.25，provenance 完整=True。
- 完整无模型：命中=True，MRR=1.0，provenance 完整=True。

### impact-netif-add · 影响分析 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；目标：netif_add (src/core/netif.c:L286)；深度：2
- certain gold：netif_init (src/core/netif.c:L187)；netif_add_noaddr (src/core/netif.c:L249)；ppp_new (src/netif/ppp/ppp.c:L649)；lwip_init (src/core/init.c:L341)；pppos_create (src/netif/ppp/pppos.c:L174)
- 源码：[permalink](https://github.com/lwip-tcpip/lwip/blob/3d896ba0a37ff3ce73270ca5e230707fe47f60e3/src/core/netif.c#L286-L458)；判定理由：金标为当前固定快照中 netif_add 的 2 跳 certain 反向可达集。
- 结果：P=1.0 / R=1.0 / F1=1.0；candidate=0（独立展示=True）。

### impact-tcp-input · 影响分析 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；目标：tcp_input (src/core/tcp_in.c:L117)；深度：2
- certain gold：ip4_input (src/core/ipv4/ip4.c:L459)；ip6_input (src/core/ipv6/ip6.c:L508)；ppp_input (src/netif/ppp/ppp.c:L779)；ethernet_input (src/netif/ethernet.c:L80)；rfc7668_input (src/netif/lowpan6_ble.c:L346)；ip_input (src/core/ip.c:L153)；lowpan6_input (src/netif/lowpan6.c:L646)
- 源码：[permalink](https://github.com/lwip-tcpip/lwip/blob/3d896ba0a37ff3ce73270ca5e230707fe47f60e3/src/core/tcp_in.c#L117-L592)；判定理由：金标为 TCP 输入主函数在 2 跳内的完整 certain 调用者集合。
- 结果：P=1.0 / R=1.0 / F1=1.0；candidate=0（独立展示=True）。

### refuse-postgresql-index · 无证据拒答 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：PostgreSQL 慢查询应该怎样设计联合索引？
- 语料范围参考：[permalink](https://github.com/lwip-tcpip/lwip/blob/3d896ba0a37ff3ce73270ca5e230707fe47f60e3/README#L1-L20)；判定理由：数据库优化不属于 lwIP 固定源码或已审核经验范围，必须拒答。
- 结果：refused=True，A/B 证据=0。

### refuse-kubernetes-ingress · 无证据拒答 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：Kubernetes Ingress 证书轮换失败怎样排查？
- 语料范围参考：[permalink](https://github.com/lwip-tcpip/lwip/blob/3d896ba0a37ff3ce73270ca5e230707fe47f60e3/README#L1-L20)；判定理由：集群运维问题不属于 lwIP 代码知识范围，不能用相似网络词拼凑回答。
- 结果：refused=True，A/B 证据=0。

### refuse-nginx-cache · 无证据拒答 · 通过

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：Nginx 反向代理缓存击穿应该如何排查？
- 语料范围参考：[permalink](https://github.com/lwip-tcpip/lwip/blob/3d896ba0a37ff3ce73270ca5e230707fe47f60e3/README#L1-L20)；判定理由：Web 代理运维不属于 lwIP 代码或审核经验边界。
- 结果：refused=True，A/B 证据=0。

### exp-lwip-symptom · 已审核经验复用 · 失败

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：netif_add 初始化异常的已审核记录描述了什么现象？
- 预期 B 卡：`lwIP netif_add 初始化路径审查`；主锚点：netif_add (src/core/netif.c:L286)
- 结果：命中=False，MRR=0.0，锚点完整=False。

### exp-lwip-hypothesis · 已审核经验复用 · 失败

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：netif_add 排障时应优先核对哪些初始化假设？
- 预期 B 卡：`lwIP netif_add 初始化路径审查`；主锚点：netif_add (src/core/netif.c:L286)
- 结果：命中=False，MRR=0.0，锚点完整=False。

### exp-lwip-fix · 已审核经验复用 · 失败

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：已审核经验建议怎样复核 netif_add 的初始化路径？
- 预期 B 卡：`lwIP netif_add 初始化路径审查`；主锚点：netif_add (src/core/netif.c:L286)
- 结果：命中=False，MRR=0.0，锚点完整=False。

### exp-lwip-verify · 已审核经验复用 · 失败

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：netif_add 初始化经验应如何验证回归结果？
- 预期 B 卡：`lwIP netif_add 初始化路径审查`；主锚点：netif_add (src/core/netif.c:L286)
- 结果：命中=False，MRR=0.0，锚点完整=False。

### exp-lwip-anchor · 已审核经验复用 · 失败

- 审核：`approved`；Guoshuaiqi / 2026-09-04；问题：netif_add 初始化经验当前绑定哪个函数与源码范围？
- 预期 B 卡：`lwIP netif_add 初始化路径审查`；主锚点：netif_add (src/core/netif.c:L286)
- 结果：命中=False，MRR=0.0，锚点完整=False。

## 失败案例

> 真实能力失败 0；因没有当前 approved B 卡而被审核门禁阻塞 5。两类结果不会合并包装。

- **blocked_by_review · exp-lwip-symptom**：未命中当前 approved B 卡或主锚点 provenance 不完整
- **blocked_by_review · exp-lwip-hypothesis**：未命中当前 approved B 卡或主锚点 provenance 不完整
- **blocked_by_review · exp-lwip-fix**：未命中当前 approved B 卡或主锚点 provenance 不完整
- **blocked_by_review · exp-lwip-verify**：未命中当前 approved B 卡或主锚点 provenance 不完整
- **blocked_by_review · exp-lwip-anchor**：未命中当前 approved B 卡或主锚点 provenance 不完整

## 口径与边界

- 定位、调用链和语义检索只检查 top-10 是否命中人工指定源码锚点；完整方案的命中还必须是含仓库、revision、USR、文件和行号的 A 级证据。
- 影响分析只对 certain 反向可达集计分；candidate 只作独立线索展示，绝不并入 F1。
- 拒答题要求 `refused=true` 且 `A/B=0`；全程显式传入 `llm_client=None`，没有模型调用。
- 本集只有 20 道人工维护任务，用于求职演示与失败复盘；它不具备统计显著性，不替代自动生成回归集的消融报告。
