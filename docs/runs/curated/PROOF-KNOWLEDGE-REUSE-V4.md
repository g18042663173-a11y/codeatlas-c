# CodeAtlas 模型对照

冻结答案已经通过合格评分者完成双评和必要仲裁；结果为 AI 复核，不等于人工盲审，也不自动代表效果提升。

评审来源：`ai_reviewed`；AI 复核不等于人工盲审，同模型的多个 agent 可能共享错误。

协议：`paired-answer-eval-v5`；范围：`short_synthetic_pilot`；每题 3 次。
重复试次用于测波动，不扩充独立样本数；安全统计所有试次。

| 方案 | 题数 | 试次 | 待审 | 正确率 % | 完整度 % | 引用标签合法率 % |
|---|---:|---:|---:|---:|---:|---:|
| 原顺序会话 | 6 | 18 | 0 | 0.0 | 65.1 | 88.9 |
| 通用摘要 | 6 | 18 | 0 | 0.0 | 68.5 | 100.0 |
| 结构化知识卡 | 6 | 18 | 0 | 0.0 | 65.9 | 83.3 |

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
    "answer_review_complete": true,
    "citation_safety": false,
    "native_planning": true,
    "paired_trials_complete": true,
    "non_experience_regression": true,
    "codeatlas_gain": false,
    "frozen_semantic_holdout": true,
    "review_provenance": true,
    "review_packet_complete": true,
    "judge_calibration": true,
    "full_three_repeat_run": true,
    "protocol_identity_bound": true,
    "raw_trials_bound": true,
    "review_integrity_bound": true,
    "snapshot_pinned": true
  },
  "citation_safety_rate": 90.74074074074075,
  "native_planning_rate": 0.0,
  "native_planning_by_variant": {
    "raw_session": 0.0,
    "generic_summary": 0.0,
    "structured_card": 0.0
  },
  "source_anchor_gain_pt": 0.0,
  "source_anchor_completeness": {
    "raw_session": 0.0,
    "generic_summary": 0.0,
    "structured_card": 0.0
  },
  "c_experience_accuracy": null,
  "non_experience_accuracy": {
    "raw_session": 0.0,
    "generic_summary": 0.0,
    "structured_card": 0.0
  },
  "paired_code_comparisons": {
    "raw_session": {
      "accuracy": {
        "task_count": 6,
        "mechanism_count": 2,
        "group_count": 2,
        "group_unit": "case",
        "delta_pt": 0,
        "ci95_pt": [
          0,
          0
        ],
        "unit": "paired_case_mean",
        "bootstrap_seed": 17,
        "mechanisms": [
          "cjson-nesting",
          "lwip-netif"
        ]
      },
      "answer_completeness": {
        "task_count": 6,
        "mechanism_count": 2,
        "group_count": 2,
        "group_unit": "case",
        "delta_pt": 0.8465608465608456,
        "ci95_pt": [
          -0.5291005291005307,
          2.222222222222222
        ],
        "unit": "paired_case_mean",
        "bootstrap_seed": 17,
        "mechanisms": [
          "cjson-nesting",
          "lwip-netif"
        ]
      },
      "code_input_token_reduction_pct": -5.702927358149612,
      "gain_observed": false
    },
    "generic_summary": {
      "accuracy": {
        "task_count": 6,
        "mechanism_count": 2,
        "group_count": 2,
        "group_unit": "case",
        "delta_pt": 0,
        "ci95_pt": [
          0,
          0
        ],
        "unit": "paired_case_mean",
        "bootstrap_seed": 17,
        "mechanisms": [
          "cjson-nesting",
          "lwip-netif"
        ]
      },
      "answer_completeness": {
        "task_count": 6,
        "mechanism_count": 2,
        "group_count": 2,
        "group_unit": "case",
        "delta_pt": -2.5661375661375683,
        "ci95_pt": [
          -2.9100529100529116,
          -2.2222222222222254
        ],
        "unit": "paired_case_mean",
        "bootstrap_seed": 17,
        "mechanisms": [
          "cjson-nesting",
          "lwip-netif"
        ]
      },
      "code_input_token_reduction_pct": null,
      "gain_observed": false
    }
  },
  "code_input_tokens": {
    "raw_session": 41505,
    "generic_summary": null,
    "structured_card": 43872
  },
  "context_token_reduction_pct": null,
  "inference_boundary": "Paired task bootstrap is descriptive; 3 repeats are not 3 independent tasks.",
  "primary_contrast": {
    "treatment": "structured_card",
    "baselines": [
      "raw_session",
      "generic_summary"
    ]
  }
}
```

未完成语义 gold 冻结、明确来源的复核或留出验证时，不得用于效果声明；AI 评分结果必须注明 AI 复核来源。
