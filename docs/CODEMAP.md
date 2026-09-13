# CodeAtlas 源码对照

被分析的 C 仓用 `codeatlas map` 看文件关系。这一页看的是 **本仓库 Python 包** 怎么拆。两套图不要讲成一个。

## 提问从哪进

```text
cli.py / server/app.py
        │
        ├─ retrieve/engine.py + rerank.py     问答、拒答
        ├─ agent.py + contracts.py            最多 6 次只读工具
        ├─ graph/traverse.py                  影响分析（certain 调用）
        └─ graph/structure.py                 文件/模块/include 大纲
                 │
                 ▼
        parser/ast_walker.py + compile_db.py + languages.py  抽 C/C++ 事实
        wiki/generator.py                     文件 → 模块 → 仓库页
        snapshots.py                          钉住一版知识
        experience/ + conversation.py         知识卡与公开会话
        eval/                                 评测，不当内核讲
        compat/                               只用于 graph explain
```

| 你要讲的 | 先打开 |
|---|---|
| 编译器事实、certain / candidate | `src/codeatlas/parser/ast_walker.py` |
| 谁调用谁、影响面 | `src/codeatlas/graph/traverse.py` |
| 文件 include、模块名 | `src/codeatlas/graph/modules.py`、`structure.py` |
| 检索和拒答 | `src/codeatlas/retrieve/engine.py` |
| Agent 阶段和工具上限 | `src/codeatlas/agent.py` |
| 三级 Wiki | `src/codeatlas/wiki/generator.py` |
| 快照切换 | `src/codeatlas/snapshots.py` |
| 本机工作台 | `src/codeatlas/server/app.py`、`server/static/index.html` |
| 命令入口 | `src/codeatlas/cli.py`（文件大，按子命令翻） |

`eval/` 里是回归、人工题和价值证明，用来对数字，不解释运行时。`compat/` 只给面试词表画图，默认运行不经过它。

## 和 C 仓结构图的差别

- 被分析的 C/C++ 仓：`node` / `edge` 来自 libclang，`codeatlas map` 只读 certain 边。C++ 方法记成函数，虚调用按静态绑定。
- 本仓：上面这张是 Python import 关系，手写对照，不是静态分析产物。
