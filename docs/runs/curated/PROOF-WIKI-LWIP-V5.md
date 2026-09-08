# CodeAtlas 模型对照

冻结答案已经通过合格评分者完成双评和必要仲裁；结果为 AI 复核，不等于人工盲审，也不自动代表效果提升。

评审来源：`ai_reviewed`；AI 复核不等于人工盲审，同模型的多个 agent 可能共享错误。

协议：`paired-answer-eval-v5`；范围：`full`；每题 3 次。
重复试次用于测波动，不扩充独立样本数；安全统计所有试次。

| 方案 | 题数 | 试次 | 待审 | 正确率 % | 完整度 % | 引用标签合法率 % |
|---|---:|---:|---:|---:|---:|---:|
| 共享代码检索，无 Wiki | 24 | 72 | 0 | 13.9 | 32.6 | 52.8 |
| 共享代码检索 + Wiki 自由阅读 | 24 | 72 | 0 | 19.4 | 42.0 | 65.3 |
| 共享代码检索 + Wiki 渐进阅读 | 24 | 72 | 0 | 18.1 | 47.9 | 77.8 |

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
  "citation_safety_rate": 65.27777777777777,
  "native_planning_rate": 48.333333333333336,
  "native_planning_by_variant": {
    "source_only": 48.333333333333336,
    "wiki_flat": 63.333333333333336,
    "wiki_progressive": 78.33333333333333
  },
  "source_anchor_gain_pt": 0.4166666666666572,
  "source_anchor_completeness": {
    "source_only": 92.91666666666667,
    "wiki_flat": 93.33333333333333,
    "wiki_progressive": 97.08333333333333
  },
  "c_experience_accuracy": null,
  "non_experience_accuracy": {
    "source_only": 13.88888888888889,
    "wiki_flat": 19.444444444444443,
    "wiki_progressive": 18.055555555555557
  },
  "paired_code_comparisons": {
    "source_only": {
      "accuracy": {
        "task_count": 20,
        "mechanism_count": 16,
        "group_count": 16,
        "group_unit": "mechanism",
        "delta_pt": 7.291666666666666,
        "ci95_pt": [
          1.0416666666666665,
          14.583333333333332
        ],
        "unit": "paired_mechanism_mean",
        "bootstrap_seed": 17,
        "mechanisms": [
          "lwip-dhcp-start-discover",
          "lwip-dns-enqueue-capacity",
          "lwip-dns-resolution-state-machine",
          "lwip-ethernet-demux",
          "lwip-ip4-reassembly-limits",
          "lwip-ip4-route-selection",
          "lwip-mem-heap-state",
          "lwip-netbuf-reallocation",
          "lwip-pbuf-header-adjust",
          "lwip-tcp-bind-conflicts",
          "lwip-tcp-pcb-allocation-reclaim",
          "lwip-timeout-dispatch",
          "lwip-udp-bind-conflicts",
          "lwip-udp-connection-state",
          "lwip-udp-pcb-lifecycle",
          "lwip-udp-send-pipeline"
        ]
      },
      "answer_completeness": {
        "task_count": 20,
        "mechanism_count": 16,
        "group_count": 16,
        "group_unit": "mechanism",
        "delta_pt": 10.677083333333334,
        "ci95_pt": [
          -1.5624999999999996,
          21.354166666666668
        ],
        "unit": "paired_mechanism_mean",
        "bootstrap_seed": 17,
        "mechanisms": [
          "lwip-dhcp-start-discover",
          "lwip-dns-enqueue-capacity",
          "lwip-dns-resolution-state-machine",
          "lwip-ethernet-demux",
          "lwip-ip4-reassembly-limits",
          "lwip-ip4-route-selection",
          "lwip-mem-heap-state",
          "lwip-netbuf-reallocation",
          "lwip-pbuf-header-adjust",
          "lwip-tcp-bind-conflicts",
          "lwip-tcp-pcb-allocation-reclaim",
          "lwip-timeout-dispatch",
          "lwip-udp-bind-conflicts",
          "lwip-udp-connection-state",
          "lwip-udp-pcb-lifecycle",
          "lwip-udp-send-pipeline"
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
        "mechanism_count": 16,
        "group_count": 16,
        "group_unit": "mechanism",
        "delta_pt": -1.0416666666666672,
        "ci95_pt": [
          -12.5,
          12.499999999999998
        ],
        "unit": "paired_mechanism_mean",
        "bootstrap_seed": 17,
        "mechanisms": [
          "lwip-dhcp-start-discover",
          "lwip-dns-enqueue-capacity",
          "lwip-dns-resolution-state-machine",
          "lwip-ethernet-demux",
          "lwip-ip4-reassembly-limits",
          "lwip-ip4-route-selection",
          "lwip-mem-heap-state",
          "lwip-netbuf-reallocation",
          "lwip-pbuf-header-adjust",
          "lwip-tcp-bind-conflicts",
          "lwip-tcp-pcb-allocation-reclaim",
          "lwip-timeout-dispatch",
          "lwip-udp-bind-conflicts",
          "lwip-udp-connection-state",
          "lwip-udp-pcb-lifecycle",
          "lwip-udp-send-pipeline"
        ]
      },
      "answer_completeness": {
        "task_count": 20,
        "mechanism_count": 16,
        "group_count": 16,
        "group_unit": "mechanism",
        "delta_pt": 9.878472222222221,
        "ci95_pt": [
          -4.8090277777777795,
          26.37152777777778
        ],
        "unit": "paired_mechanism_mean",
        "bootstrap_seed": 17,
        "mechanisms": [
          "lwip-dhcp-start-discover",
          "lwip-dns-enqueue-capacity",
          "lwip-dns-resolution-state-machine",
          "lwip-ethernet-demux",
          "lwip-ip4-reassembly-limits",
          "lwip-ip4-route-selection",
          "lwip-mem-heap-state",
          "lwip-netbuf-reallocation",
          "lwip-pbuf-header-adjust",
          "lwip-tcp-bind-conflicts",
          "lwip-tcp-pcb-allocation-reclaim",
          "lwip-timeout-dispatch",
          "lwip-udp-bind-conflicts",
          "lwip-udp-connection-state",
          "lwip-udp-pcb-lifecycle",
          "lwip-udp-send-pipeline"
        ]
      },
      "code_input_token_reduction_pct": null
    },
    "source_only": {
      "treatment": "wiki_progressive",
      "accuracy": {
        "task_count": 20,
        "mechanism_count": 16,
        "group_count": 16,
        "group_unit": "mechanism",
        "delta_pt": 6.249999999999999,
        "ci95_pt": [
          -4.166666666666668,
          17.708333333333332
        ],
        "unit": "paired_mechanism_mean",
        "bootstrap_seed": 17,
        "mechanisms": [
          "lwip-dhcp-start-discover",
          "lwip-dns-enqueue-capacity",
          "lwip-dns-resolution-state-machine",
          "lwip-ethernet-demux",
          "lwip-ip4-reassembly-limits",
          "lwip-ip4-route-selection",
          "lwip-mem-heap-state",
          "lwip-netbuf-reallocation",
          "lwip-pbuf-header-adjust",
          "lwip-tcp-bind-conflicts",
          "lwip-tcp-pcb-allocation-reclaim",
          "lwip-timeout-dispatch",
          "lwip-udp-bind-conflicts",
          "lwip-udp-connection-state",
          "lwip-udp-pcb-lifecycle",
          "lwip-udp-send-pipeline"
        ]
      },
      "answer_completeness": {
        "task_count": 20,
        "mechanism_count": 16,
        "group_count": 16,
        "group_unit": "mechanism",
        "delta_pt": 20.555555555555554,
        "ci95_pt": [
          6.024305555555555,
          36.545138888888886
        ],
        "unit": "paired_mechanism_mean",
        "bootstrap_seed": 17,
        "mechanisms": [
          "lwip-dhcp-start-discover",
          "lwip-dns-enqueue-capacity",
          "lwip-dns-resolution-state-machine",
          "lwip-ethernet-demux",
          "lwip-ip4-reassembly-limits",
          "lwip-ip4-route-selection",
          "lwip-mem-heap-state",
          "lwip-netbuf-reallocation",
          "lwip-pbuf-header-adjust",
          "lwip-tcp-bind-conflicts",
          "lwip-tcp-pcb-allocation-reclaim",
          "lwip-timeout-dispatch",
          "lwip-udp-bind-conflicts",
          "lwip-udp-connection-state",
          "lwip-udp-pcb-lifecycle",
          "lwip-udp-send-pipeline"
        ]
      },
      "code_input_token_reduction_pct": null
    }
  }
}
```

未完成语义 gold 冻结、明确来源的复核或留出验证时，不得用于效果声明；AI 评分结果必须注明 AI 复核来源。
