# Frozen model-produced materials

`knowledge_reuse-v4.json` is intentionally absent until all four V4 Flash
summary/card products are generated from the frozen public sessions and every
section passes two isolated evidence reviews (plus arbitration when needed).

The workflow is:

```bash
codeatlas eval knowledge-produce
codeatlas eval knowledge-material-review \
  eval/frozen/knowledge_reuse-v4.pending.json \
  path/to/independent-material-reviews.json
codeatlas eval campaign prepare data/proof/campaign-v1
```

Neither a pending product nor an unresolved review is accepted by the proof
campaign. The generated artifact contains no API key and is bound to the source
manifest, source bytes, model sessions, prompt, production usage, and reviews.
