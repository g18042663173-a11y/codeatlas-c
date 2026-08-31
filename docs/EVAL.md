# 评测索引与统一口径

最后核验：2026-08-31，WSL2 Ubuntu、Python 3.12、Clang 18、本地 CPU、TF-IDF+SVD 向量降级通道。

| 语料 | 题目 | BM25 Recall@10 | 完整方案 Recall@10 | MRR@10 变化 | 自动生成报告 |
|---|---:|---:|---:|---:|---|
| lwIP（约 13.5 万行） | 58 | 64.6% | 73.5% | 0.472 → 0.457 | [EVAL-LWIP.md](EVAL-LWIP.md) |
| cJSON（约 3.5 千行） | 59 | 56.8% | 67.0% | 0.454 → 0.461 | [EVAL-CJSON.md](EVAL-CJSON.md) |

## 可以公开引用的结论

- lwIP 完整方案相对仅 BM25 的 Recall@10 提升 12.1 个百分点；MRR 略降 0.011，不能声称所有指标都提升。
- cJSON 完整方案相对仅 BM25 的 Recall@10 提升 11.9 个百分点，MRR 提升 0.010。
- 图扩展收益主要来自结构性查询；非循环题型并未证明图扩展改善语义理解。
- 摘要头降低单节点上下文成本，让固定预算可容纳更多节点，但上下文总 token 不保证在所有语料上下降。
- 评测只覆盖检索，不覆盖最终自然语言答案质量；lwIP 金标全部来自 AST 图事实，存在循环性偏向，必须结合分题型表解释。

## 复现

```bash
pip install -e ".[dev]"
pytest -q
bash demo.sh cjson  # 更新 EVAL-CJSON.md
bash demo.sh        # 更新 EVAL-LWIP.md
```

两次演示使用不同数据库和报告文件，互不覆盖。公开 README、简历和面试材料应以本索引及两份生成报告为准。
