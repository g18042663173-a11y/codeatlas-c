# cJSON nesting-limit reproduction

This is a public, deterministic reproduction, not a production incident.

- Upstream: `https://github.com/DaveGamble/cJSON`
- Revision: `fb16e5cf358798aabb049655975cde8427101056`
- Command: `./examples/reproductions/cjson-nesting/run.sh`
- Expected output: [`result.txt`](result.txt)
- Environment used for the checked result: macOS 27.0, arm64, Apple clang 21.0.0

The harness checks the fixed depths 999, 1000, and 1001 through
`cJSON_ParseWithOpts`. The inputs are intentionally not derived from
`CJSON_NESTING_LIMIT`: a future macro change must change the observed result,
not silently move both the boundary and the test cases. It records
success/failure and `parse_end`; it does not claim that every deeply nested
failure in another application has the same cause.

If the workspace checkout is not locally readable, set
`CODEATLAS_CJSON_SOURCE_ROOT` to another checkout at the pinned revision.

## Artifact hashes (SHA-256)

- `reproduce.c`: `cda8b6b0fef4cf4721d1086778637731b1337a56ae5fbe398afea849f30ad784`
- `run.sh`: `529274dbbd63556db765061836196cc91af288bcff2d449fc03c3bf9843cf481`
- `result.txt`: `ed76b1069221053876dd4995aaee6e228f4983ad8f97717d51bf1ff02be0b133`
