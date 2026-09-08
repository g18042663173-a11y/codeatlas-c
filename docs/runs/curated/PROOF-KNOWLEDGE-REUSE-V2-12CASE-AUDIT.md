# CodeAtlas 12-case knowledge-card review audit

Status: `unresolved`; claim: `insufficient_evidence`.

This audit proves collection and review execution state, not a positive effect. Correctness ranges keep unresolved trials in the denominator; no missing score is imputed.

| Measure | Count |
|---|---:|
| answer_trials | 324 |
| independent_scores_expected | 648 |
| independent_scores_valid | 635 |
| independent_scores_repairable_invalid | 7 |
| independent_scores_transport_or_unknown | 6 |
| disagreements | 82 |
| adjudications_valid | 80 |
| adjudications_repairable_invalid | 0 |
| adjudications_transport_or_unknown | 2 |
| unresolved_trials | 14 |
| repairable_score_positions | 7 |
| single_role_recoverable_positions | 3 |
| dual_independent_gap_trials | 2 |
| semantic_or_unknown_unresolved_trials | 9 |

Failure types:

- `independent_scores`: URLError=6, ValueError=7
- `adjudications`: TimeoutError=1, URLError=1

Known limitations:

- The historical v1 projection mapped a cited source to answer-key evidence by same-file identity without requiring line-range overlap. Its citation-relation scores are excluded from effect proof; v2 fixes future attempts without rewriting this frozen run.

| Variant | Trials | Resolved | Correctness range | Input tokens |
|---|---:|---:|---:|---:|
| raw_session | 108 | 101 | 0.407–0.472 | 464772 + 2 missing |
| generic_summary | 108 | 105 | 0.481–0.509 | 491808 + 2 missing |
| structured_card | 108 | 104 | 0.481–0.519 | 484326 + 2 missing |

Complete usage triplets: 106; structured-card vs raw-session input reduction: -4.21%.

The full prompts, answers, reviewer prose and request ledger remain local and ignored.
