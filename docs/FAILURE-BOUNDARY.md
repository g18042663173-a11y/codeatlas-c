# 失败边界（追问用）

现场被追问时用这一页，不要把「能画图」说成「什么都分析了」。

## 证据

- 向量单独命中不算 A 级证据。没有符号或 BM25 命中时，`engine.ask` 会丢掉向量结果，避免把无关问题编成代码结论。
- Wiki 是 C 级背景。没有 A 级代码事实或 B 级已审知识卡就拒答。
- 引用合法不等于回答正确。模型自由叙述即使带了真实行号，仍标待复核。

## 图

- `#include` 不是函数调用。`codeatlas map` 把 include 和跨文件 `calls` 分开列。
- 文件/模块图不是控制流，也不是数据流。`type_use` / `field_access` / `global_ref` 只是引用事实。
- 函数指针、取地址、未解析调用是 candidate，不进影响结论，也不进结构大纲的确定调用。
- 影响分析只沿 certain 调用边反向走 hop；受影响模块名与 Wiki、`map` 共用 `graph/modules.py`。
- C++ 虚调用的 certain 边是编译器静态绑定的声明目标，不是运行时 override。基类指针调 `size()` 记到基类方法，即使对象实际是子类。
- 模板实例化、ADL、大部分运算符重载与异常路径没有做成完整语义图。有 compile_commands 的 C++ TU 可以入库，不代表基带 C++ 工程已经评测。

## Agent 与版本

- 模型计划不合法、重复工具或超出允许的下一步，会降级到本地规则，并留下 `fallback_reason`。
- 最多 6 次只读工具；概念题 Wiki-first，不能一上来乱翻源码。
- checkpoint / 快照钉的是某一版源码知识库，不是 LangGraph 对话 Memory。运行中途不会换版本。

## 数字

- 能说：cJSON / lwIP Recall@10 相对 BM25 +10 / +20pt，拒答 100%，维护场景 16 个语义决策全对。
- 不能说：知识卡省 token、Wiki 正确率稳定赢、基于 LangGraph、和 seCall 一套内核。
- 旧 40 题 30/40 是历史 gold 冻结，不是当前检索失败。
