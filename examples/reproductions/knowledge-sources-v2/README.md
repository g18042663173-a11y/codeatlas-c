# Twelve task-free public reproduction sources

These are new local C API fixtures: six cJSON and six lwIP cases, excluding the
older nesting-limit and default-netif examples. No model was called, and no
follow-up question, answer key, summary or knowledge card is authored here.
The fixture assertions verify the executed C setup; they are not a later QA gold.

From the project root:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python examples/reproductions/knowledge-sources-v2/verify.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python examples/reproductions/knowledge-sources-v2/verify.py --rerun
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python examples/reproductions/knowledge-sources-v2/run.py cjson-unicode
```

`run.py` only creates temporary compiler outputs and prints actual capture JSON.
It validates the pinned revision and all discovered non-system dependencies
against the Git object contents. It never rewrites the recorded outputs.
`BUILD` in recorded commands denotes that isolated temporary directory; every
project source path is relative. The host compiler/version and command options
are recorded. No syscall, protocol, allocator or OS abstraction is mocked.
lwIP uses the existing `test/unit/lwipopts.h`, including `NO_SYS=0`, unchanged.
Only the source units needed for each local API probe are linked; dead stripping
does not emulate an entire operational stack. Stack-backed pbuf fixtures are not
passed to `pbuf_free` and make no claim about real allocator/network lifetimes.

Each directory contains exact C inputs, commands, execution records (including
stdout/stderr/exit), source definitions with line ranges and hashes, and a
machine-captured session-style trace explicitly marked as not a human transcript.
`material.txt` is the complete selected experimental material, including retained
initial failures. Each repository's measured lengths are ranked into two short,
two medium and two long cases; these are relative strata, not absolute long
conversation evidence. No filler is added.

Two initial failures remain readable: the Unicode probe initially asserted that
invalid hex must be rejected, but this pinned cJSON maps it to a zero byte; the
revised probe records that real behavior and keeps the old fixture/output. The
IPv4 probe initially lacked `def.c` at link time; linking that same-version unit
resolved `lwip_htonl` without changing configuration.

`source-manifest.json` is a schema-2 source-only catalog. Its case/session/common
attachment descriptors can seed a later reuse manifest. It deliberately cannot
start answer generation: follow-up tasks, reviewed facts/source spans, presentation
layout and reviewed gold remain absent. Source reproducibility does not establish
holdout independence or any model benefit.

The original fixture programs and captured observations are CC0-1.0. Upstream
source excerpts retain their original licenses (cJSON MIT, lwIP BSD-style);
see the pinned checkout's `corpus/cJSON/LICENSE`, `corpus/lwip/COPYING` and source
file copyright headers. No upstream source or old report/card is changed.
