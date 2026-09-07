#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/../../.." && pwd)"
source_root="${CODEATLAS_CJSON_SOURCE_ROOT:-$project_root/corpus/cJSON}"
expected_revision="fb16e5cf358798aabb049655975cde8427101056"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/codeatlas-cjson.XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT

actual_revision="$(git -C "$source_root" rev-parse HEAD)"
if [[ "$actual_revision" != "$expected_revision" ]]; then
  printf 'cJSON revision mismatch: expected %s, got %s\n' "$expected_revision" "$actual_revision" >&2
  exit 2
fi

cc -std=c99 -Wall -Wextra -Werror \
  -I "$source_root" \
  "$source_root/cJSON.c" \
  "$project_root/examples/reproductions/cjson-nesting/reproduce.c" \
  -lm -o "$build_dir/reproduce"

output="$("$build_dir/reproduce")"
printf '%s\n' "$output"
diff -u "$project_root/examples/reproductions/cjson-nesting/result.txt" \
  <(printf '%s\n' "$output")
