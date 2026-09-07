# Maintenance pre-registration worksheet

Status: `ai_assisted_draft`. No human reviewer or upstream result is implied.

The shipped `eval/maintenance.yaml` binds 24 independent CC0 synthetic executable
fixtures: cJSON and lwIP each have 4 effective, 4 unrelated-to-claim and 4 failed
build specifications. The corpus names and fixed revisions identify the future
upstream experiment targets. The fixture programs are **not extracted upstream
code** and their passing tests do not establish CodeAtlas maintenance performance.

Before collecting an upstream trial, create a NEW manifest/run name and fill:

- Case ID and corpus revision; never retune gold after seeing an adapter decision.
- Exact public claim and unchanged main source anchor.
- Independent case directory containing source, patch, compile configuration and
  external oracle, each bound by SHA-256 in `artifacts`.
- `material_kind: upstream_mutation` only after those are actual upstream inputs.
- Expected revalidation from a source/compile/run assertion, not CodeAtlas's own
  dependency graph, staleness output, or observed decision.
- `external_oracle`: predeclared assertion IDs, descriptions, expected values,
  expected outcome and `expected_revalidation` (null for a failure scenario).
- Human preregistration review, timestamp and final manifest hash, if available.
  The evaluator validates the provenance declaration; it cannot prove blind review.

## Runner contract

`maintenance_eval.evaluate(path)` validates and returns `not_run`. It never runs
commands, accesses a model, applies patches or changes upstream checkouts.
`mode="execute"` requires an explicit injected executor `(case, inputs) -> dict`.
The executor must create isolated work copies. Compiler argv is data, never an
implicit shell command. The evaluator rechecks original artifacts before/after
trials and at completion; executors are trusted Python code, not sandboxed code.
Oracle data and the classification are not passed to an adapter as decision inputs.

Return `compile_exit`, `run_exit`, `assertions: [{id, actual}]`, optional
`compile_ms`/`run_ms`, and `adapters` with `current`, `main_anchor_hash`, and
`invalidate_all`. Each adapter records `decision: keep|revalidate|unavailable`,
nullable `citation_refreshed`, `wiki_updated`, `elapsed_ms`, and `{value, unit}`
cost. For the simple hash baseline the runner may instead return measured
`main_anchor_before`/`main_anchor_after` (SHA-256; after=null means missing).
The invalidate-all baseline is mechanically `revalidate` after a successful probe;
its refresh outcomes, time and cost remain unmeasured unless explicitly supplied.

Compiler failures are counted separately. Failed compilation, execution, oracle
assertions, unknown decisions and not-run trials cannot improve semantic rates.
Missed invalidation uses only effective cases; false invalidation uses only
unrelated-to-claim cases. Citation refresh/Wiki update are observed event rates,
not a claim that every update was necessary or correct.

## Cost interpretation

Report build/update time and cost separately from per-query time and cost. Keep
unmeasured fields null. `amortization` needs same-unit costs, an explicit
`quality_comparable=true`, and positive per-query savings. Do not mix milliseconds,
tokens or USD, and do not substitute cost savings for answer quality evidence.
