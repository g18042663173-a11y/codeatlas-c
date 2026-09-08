# CodeAtlas 模型对照

> 2026-09-08：以下为保留的历史评分，不是当前有效效果结果。引用正文完整性待核查，汇总状态为 `pending_evidence_audit`。原答案和 gold 未改，尚未重评；见 [Wiki 审计](../../WIKI-EVIDENCE-AUDIT.md)。

冻结答案已经通过合格评分者完成双评和必要仲裁；仍有 1 个语义判断未决，因此包含它的主对照保持未测。

评审来源：`unresolved`；AI 复核不等于人工盲审，同模型的多个 agent 可能共享错误。

协议：`paired-answer-eval-v5`；范围：`full`；每题 3 次。
重复试次用于测波动，不扩充独立样本数；安全统计所有试次。

| 方案 | 题数 | 试次 | 待审 | 正确率 % | 完整度 % | 引用标签合法率 % |
|---|---:|---:|---:|---:|---:|---:|
| 共享代码检索，无 Wiki | 24 | 72 | 0 | 23.6 | 46.8 | 72.2 |
| 共享代码检索 + Wiki 自由阅读 | 24 | 72 | 1 | 待审/未测 | 待审/未测 | 91.7 |
| 共享代码检索 + Wiki 渐进阅读 | 24 | 72 | 0 | 29.2 | 52.2 | 80.6 |

## 门禁与成对差异

锚点命中率仅衡量召回；带真引用的错误结论仍由答案评审判错。
成本未测；上下文节省只使用同一代码题群的完整服务 usage，缺失不按 0 计算。

```json
{
  "passed": false,
  "effect_observed": false,
  "human_release_eligible": false,
  "execution_complete": true,
  "gates": {
    "answer_review_complete": false,
    "citation_safety": true,
    "native_planning": false,
    "paired_trials_complete": true,
    "non_experience_regression": false,
    "codeatlas_gain": false,
    "frozen_semantic_holdout": true,
    "review_provenance": false,
    "review_packet_complete": true,
    "judge_calibration": true,
    "full_three_repeat_run": true,
    "protocol_identity_bound": true,
    "raw_trials_bound": true,
    "review_integrity_bound": true,
    "snapshot_pinned": true
  },
  "citation_safety_rate": 81.48148148148148,
  "native_planning_rate": 70.0,
  "native_planning_by_variant": {
    "source_only": 70.0,
    "wiki_flat": 91.66666666666667,
    "wiki_progressive": 78.33333333333333
  },
  "source_anchor_gain_pt": 0.0,
  "source_anchor_completeness": {
    "source_only": 100.0,
    "wiki_flat": 100.0,
    "wiki_progressive": 100.0
  },
  "c_experience_accuracy": null,
  "non_experience_accuracy": {
    "source_only": null,
    "wiki_flat": null,
    "wiki_progressive": null
  },
  "paired_code_comparisons": {
    "source_only": {
      "accuracy": {
        "delta_pt": null,
        "ci95_pt": null
      },
      "answer_completeness": {
        "delta_pt": null,
        "ci95_pt": null
      },
      "code_input_token_reduction_pct": null,
      "gain_observed": false
    }
  },
  "code_input_tokens": {
    "source_only": null,
    "wiki_flat": null,
    "wiki_progressive": null
  },
  "context_token_reduction_pct": null,
  "inference_boundary": "Paired task bootstrap is descriptive; 3 repeats are not 3 independent tasks.",
  "primary_contrast": {
    "treatment": "wiki_flat",
    "baselines": [
      "source_only"
    ]
  },
  "secondary_reading_contrasts": {
    "wiki_flat": {
      "treatment": "wiki_progressive",
      "accuracy": null,
      "answer_completeness": null,
      "code_input_token_reduction_pct": null
    },
    "source_only": {
      "treatment": "wiki_progressive",
      "accuracy": null,
      "answer_completeness": null,
      "code_input_token_reduction_pct": null
    }
  }
}
```

未完成语义 gold 冻结、明确来源的复核或留出验证时，不得用于效果声明；AI 评分结果必须注明 AI 复核来源。
