# CodeAtlas 求职演示说明

## 演示主线

不要把项目讲成“做了几个检索工具”，而是按这一条因果链说明：

```text
编译器事实
  → 分层代码知识
  → 带版本的证据检索
  → Agent 只读诊断
  → 会话经验人工审核
  → 代码变化后自动失效
  → 固定任务和失败案例证明
```

这对应一个真实的 AI 应用问题：模型可以参与规划和表达，但事实、权限、审核和发布都由本地程序控制。

## 三分钟操作

先准备固定 cJSON 数据并启动网页：

```bash
bash demo.sh workflow
codeatlas serve --db data/kb.db --data-dir data
```

1. **总览（20 秒）**：指出固定 commit、函数数、certain/candidate、三级 Wiki 和当前模型状态。强调正式解析来自 compile database，严重编译诊断为 0。
2. **证据检索（30 秒）**：查询 `cJSON_Delete` 或 `parse_value`，展示 A 级引用的 repository、revision、USR、源码范围和定义哈希；再输入 Kubernetes TLS 问题展示拒答。
3. **文件结构（20 秒）**：`codeatlas map file cJSON.c`，或工作台「文件结构」。指出 include 与跨文件 certain 调用分开列，candidate 不进确定关系。
4. **Agent（45 秒）**：先问概念题，展示 Wiki-first 的 discover/understand/verify 时间线；再问影响题，展示结构化直接路由。
5. **知识卡（45 秒）**：设置沉淀目标、确认候选；保存后仍是 `pending`。从 A 级证据选 USR、关系类型，补一条 QA 和审核意见后才能批准。
6. **失效与替代（20 秒）**：解释 parse 后 definition hash 变化会让卡片 `stale`；新卡只有在批准后才把旧卡标为 `superseded`。
7. **评测（20 秒）**：展示 cJSON/lwIP 各 15/20、人工开发检索各 10/10、相对 BM25 的 +10/+20pt、十道经验题的审核阻塞，以及 6 场景门禁。

网页不会执行评测或改源码，只读取本地生成且与当前 repository/revision 匹配的 JSON sidecar。真实模型报告不存在时显示“尚未运行”。

## 面试中要主动说明的边界

- C++ 翻译单元可以入库，方法按函数检索；虚调用只认静态绑定，不是运行时 override，也没有 C++ 语料评测数字；
- 这是按企业研发约束设计的本地原型，不是已部署的多租户平台；
- `type_use / field_access / global_ref` 是编译器引用事实，不是完整数据流；
- 模型是可选主路径，不能批准知识卡、改代码或绕过引用校验；
- 117 道旧检索回归题与 40 道正式人工任务分开报告；
- 当前真实检索失败为 0；十道经验题仍因未建立用户批准的 B 卡而阻塞，统一总门禁和作品集状态都保持未通过。

完整面试话术见 [INTERVIEW_STAR.md](INTERVIEW_STAR.md)。
