# CodeAtlas 模型对照

冻结答案已经通过合格评分者完成双评和必要仲裁；结果为 AI 复核，不等于人工盲审，也不自动代表效果提升。

评审来源：`ai_reviewed`；AI 复核不等于人工盲审，同模型的多个 agent 可能共享错误。

协议：`paired-answer-eval-v5`；范围：`full`；每题 3 次。
重复试次用于测波动，不扩充独立样本数；安全统计所有试次。

| 方案 | 题数 | 试次 | 待审 | 正确率 % | 完整度 % | 引用标签合法率 % |
|---|---:|---:|---:|---:|---:|---:|
| 共享代码检索，无 Wiki | 24 | 72 | 0 | 25.0 | 46.1 | 72.2 |
| 共享代码检索 + Wiki 自由阅读 | 24 | 72 | 0 | 29.2 | 59.8 | 91.7 |
| 共享代码检索 + Wiki 渐进阅读 | 24 | 72 | 0 | 27.8 | 52.8 | 80.6 |

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
    "citation_safety": true,
    "native_planning": false,
    "paired_trials_complete": true,
    "non_experience_regression": true,
    "codeatlas_gain": true,
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
    "source_only": 25.0,
    "wiki_flat": 29.166666666666668,
    "wiki_progressive": 27.77777777777778
  },
  "paired_code_comparisons": {
    "source_only": {
      "accuracy": {
        "task_count": 20,
        "mechanism_count": 19,
        "group_count": 19,
        "group_unit": "mechanism",
        "delta_pt": 4.385964912280702,
        "ci95_pt": [
          -10.526315789473683,
          18.42105263157895
        ],
        "unit": "paired_mechanism_mean",
        "bootstrap_seed": 17,
        "mechanisms": [
          "cjson-allocator-hook-state",
          "cjson-array-insert-boundaries",
          "cjson-array-size-child-chain",
          "cjson-array-tail-invariant",
          "cjson-convenience-add-cleanup",
          "cjson-deep-duplicate-failure",
          "cjson-detach-list-rewire",
          "cjson-find-pointer-ownership",
          "cjson-generate-merge-patch-null-result",
          "cjson-json-pointer-walk",
          "cjson-minify-inplace",
          "cjson-number-dual-representation",
          "cjson-reference-alias-insertion",
          "cjson-replace-object-item",
          "cjson-rfc7396-merge",
          "cjson-set-valuestring-guards",
          "cjson-string-array-cleanup",
          "cjson-string-reference-ownership",
          "cjson-utils-sort-mutation"
        ]
      },
      "answer_completeness": {
        "task_count": 20,
        "mechanism_count": 19,
        "group_count": 19,
        "group_unit": "mechanism",
        "delta_pt": 14.32748538011696,
        "ci95_pt": [
          5.116959064327485,
          22.63157894736842
        ],
        "unit": "paired_mechanism_mean",
        "bootstrap_seed": 17,
        "mechanisms": [
          "cjson-allocator-hook-state",
          "cjson-array-insert-boundaries",
          "cjson-array-size-child-chain",
          "cjson-array-tail-invariant",
          "cjson-convenience-add-cleanup",
          "cjson-deep-duplicate-failure",
          "cjson-detach-list-rewire",
          "cjson-find-pointer-ownership",
          "cjson-generate-merge-patch-null-result",
          "cjson-json-pointer-walk",
          "cjson-minify-inplace",
          "cjson-number-dual-representation",
          "cjson-reference-alias-insertion",
          "cjson-replace-object-item",
          "cjson-rfc7396-merge",
          "cjson-set-valuestring-guards",
          "cjson-string-array-cleanup",
          "cjson-string-reference-ownership",
          "cjson-utils-sort-mutation"
        ]
      },
      "code_input_token_reduction_pct": null,
      "gain_observed": true
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
      "accuracy": {
        "task_count": 20,
        "mechanism_count": 19,
        "group_count": 19,
        "group_unit": "mechanism",
        "delta_pt": -1.7543859649122808,
        "ci95_pt": [
          -14.035087719298247,
          14.035087719298245
        ],
        "unit": "paired_mechanism_mean",
        "bootstrap_seed": 17,
        "mechanisms": [
          "cjson-allocator-hook-state",
          "cjson-array-insert-boundaries",
          "cjson-array-size-child-chain",
          "cjson-array-tail-invariant",
          "cjson-convenience-add-cleanup",
          "cjson-deep-duplicate-failure",
          "cjson-detach-list-rewire",
          "cjson-find-pointer-ownership",
          "cjson-generate-merge-patch-null-result",
          "cjson-json-pointer-walk",
          "cjson-minify-inplace",
          "cjson-number-dual-representation",
          "cjson-reference-alias-insertion",
          "cjson-replace-object-item",
          "cjson-rfc7396-merge",
          "cjson-set-valuestring-guards",
          "cjson-string-array-cleanup",
          "cjson-string-reference-ownership",
          "cjson-utils-sort-mutation"
        ]
      },
      "answer_completeness": {
        "task_count": 20,
        "mechanism_count": 19,
        "group_count": 19,
        "group_unit": "mechanism",
        "delta_pt": -9.356725146198832,
        "ci95_pt": [
          -20.497076023391813,
          3.2163742690058474
        ],
        "unit": "paired_mechanism_mean",
        "bootstrap_seed": 17,
        "mechanisms": [
          "cjson-allocator-hook-state",
          "cjson-array-insert-boundaries",
          "cjson-array-size-child-chain",
          "cjson-array-tail-invariant",
          "cjson-convenience-add-cleanup",
          "cjson-deep-duplicate-failure",
          "cjson-detach-list-rewire",
          "cjson-find-pointer-ownership",
          "cjson-generate-merge-patch-null-result",
          "cjson-json-pointer-walk",
          "cjson-minify-inplace",
          "cjson-number-dual-representation",
          "cjson-reference-alias-insertion",
          "cjson-replace-object-item",
          "cjson-rfc7396-merge",
          "cjson-set-valuestring-guards",
          "cjson-string-array-cleanup",
          "cjson-string-reference-ownership",
          "cjson-utils-sort-mutation"
        ]
      },
      "code_input_token_reduction_pct": null
    },
    "source_only": {
      "treatment": "wiki_progressive",
      "accuracy": {
        "task_count": 20,
        "mechanism_count": 19,
        "group_count": 19,
        "group_unit": "mechanism",
        "delta_pt": 2.631578947368421,
        "ci95_pt": [
          -11.403508771929825,
          17.543859649122805
        ],
        "unit": "paired_mechanism_mean",
        "bootstrap_seed": 17,
        "mechanisms": [
          "cjson-allocator-hook-state",
          "cjson-array-insert-boundaries",
          "cjson-array-size-child-chain",
          "cjson-array-tail-invariant",
          "cjson-convenience-add-cleanup",
          "cjson-deep-duplicate-failure",
          "cjson-detach-list-rewire",
          "cjson-find-pointer-ownership",
          "cjson-generate-merge-patch-null-result",
          "cjson-json-pointer-walk",
          "cjson-minify-inplace",
          "cjson-number-dual-representation",
          "cjson-reference-alias-insertion",
          "cjson-replace-object-item",
          "cjson-rfc7396-merge",
          "cjson-set-valuestring-guards",
          "cjson-string-array-cleanup",
          "cjson-string-reference-ownership",
          "cjson-utils-sort-mutation"
        ]
      },
      "answer_completeness": {
        "task_count": 20,
        "mechanism_count": 19,
        "group_count": 19,
        "group_unit": "mechanism",
        "delta_pt": 4.970760233918128,
        "ci95_pt": [
          -9.064327485380117,
          18.71345029239766
        ],
        "unit": "paired_mechanism_mean",
        "bootstrap_seed": 17,
        "mechanisms": [
          "cjson-allocator-hook-state",
          "cjson-array-insert-boundaries",
          "cjson-array-size-child-chain",
          "cjson-array-tail-invariant",
          "cjson-convenience-add-cleanup",
          "cjson-deep-duplicate-failure",
          "cjson-detach-list-rewire",
          "cjson-find-pointer-ownership",
          "cjson-generate-merge-patch-null-result",
          "cjson-json-pointer-walk",
          "cjson-minify-inplace",
          "cjson-number-dual-representation",
          "cjson-reference-alias-insertion",
          "cjson-replace-object-item",
          "cjson-rfc7396-merge",
          "cjson-set-valuestring-guards",
          "cjson-string-array-cleanup",
          "cjson-string-reference-ownership",
          "cjson-utils-sort-mutation"
        ]
      },
      "code_input_token_reduction_pct": null
    }
  }
}
```

未完成语义 gold 冻结、明确来源的复核或留出验证时，不得用于效果声明；AI 评分结果必须注明 AI 复核来源。
