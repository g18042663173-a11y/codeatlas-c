#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/../../.." && pwd)"
source_root="${CODEATLAS_LWIP_SOURCE_ROOT:-$project_root/corpus/lwip}"
expected_revision="3d896ba0a37ff3ce73270ca5e230707fe47f60e3"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/codeatlas-lwip.XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT

if [[ ! -r "$source_root/src/include/lwip/ip4_addr.h" ]]; then
  printf 'lwIP source is not locally readable: %s\n' "$source_root" >&2
  printf 'Set CODEATLAS_LWIP_SOURCE_ROOT to the pinned checkout if the workspace copy is cloud-evicted.\n' >&2
  exit 2
fi
actual_revision="$(git -C "$source_root" rev-parse HEAD)"
if [[ "$actual_revision" != "$expected_revision" ]]; then
  printf 'lwIP revision mismatch: expected %s, got %s\n' "$expected_revision" "$actual_revision" >&2
  exit 2
fi

cc -std=c99 -Wall -Wextra -ffunction-sections -fdata-sections \
  -I "$project_root/examples/reproductions/lwip-netif/include" \
  -I "$source_root/test/unit/arch" \
  -I "$source_root/contrib/ports/unix/port/include" \
  -I "$source_root/src/include" \
  -c "$source_root/src/core/netif.c" -o "$build_dir/netif.o"
cc -std=c99 -Wall -Wextra -ffunction-sections -fdata-sections \
  -I "$project_root/examples/reproductions/lwip-netif/include" \
  -I "$source_root/test/unit/arch" \
  -I "$source_root/contrib/ports/unix/port/include" \
  -I "$source_root/src/include" \
  -c "$source_root/src/core/ipv4/ip4_addr.c" -o "$build_dir/ip4_addr.o"
cc -std=c99 -Wall -Wextra -ffunction-sections -fdata-sections \
  -I "$project_root/examples/reproductions/lwip-netif/include" \
  -I "$source_root/test/unit/arch" \
  -I "$source_root/contrib/ports/unix/port/include" \
  -I "$source_root/src/include" \
  -c "$project_root/examples/reproductions/lwip-netif/reproduce.c" -o "$build_dir/reproduce.o"
cc -Wl,-dead_strip "$build_dir/netif.o" "$build_dir/ip4_addr.o" \
  "$build_dir/reproduce.o" -o "$build_dir/reproduce"

output="$("$build_dir/reproduce")"
printf '%s\n' "$output"
diff -u "$project_root/examples/reproductions/lwip-netif/result.txt" \
  <(printf '%s\n' "$output")
